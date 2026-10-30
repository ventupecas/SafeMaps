"""A small synthetic OpenStreetMap extract for router tests, in Overpass JSON form.

A grid of streets about 200 m apart, placed in downtown Milwaukee:

    row 2          + --- + --- +      North St (residential)
                   |     |     |
    row 1    + === + === + === +      Main St (primary arterial, cols 0-3)
                   |     |     |
    row 0          + -~- + --- +      South St (residential, with a bend node)

           col 0  col 1 col 2 col 3

Columns 1-3 are residential avenues ("1 Ave".."3 Ave") running rows 0-2.
Main St's west end, (1, 0), is a dead end. These are test fixtures, not real
OSM data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.geo import Point

BASE: Point = (43.0389, -87.9065)
LAT_STEP = 0.0018  # ~200 m
LNG_STEP = 0.00246  # ~200 m at this latitude


def junction(row: int, col: int) -> int:
    return 1000 + 10 * row + col


def point(row: int, col: int) -> Point:
    return (BASE[0] + row * LAT_STEP, BASE[1] + col * LNG_STEP)


@dataclass
class Osm:
    elements: list[dict[str, Any]] = field(default_factory=list)
    _next_way: int = 1
    _next_relation: int = 1

    def node(self, node_id: int, at: Point, **tags: str) -> int:
        element: dict[str, Any] = {"type": "node", "id": node_id, "lat": at[0], "lon": at[1]}
        if tags:
            element["tags"] = tags
        self.elements.append(element)
        return node_id

    def way(self, nodes: list[int], **tags: str) -> int:
        way_id = self._next_way
        self._next_way += 1
        self.elements.append({"type": "way", "id": way_id, "nodes": nodes, "tags": tags})
        return way_id

    def restriction(self, kind: str, from_way: int, via_node: int, to_way: int, **tags: str) -> None:
        self.elements.append(
            {
                "type": "relation",
                "id": self._next_relation,
                "members": [
                    {"type": "way", "ref": from_way, "role": "from"},
                    {"type": "node", "ref": via_node, "role": "via"},
                    {"type": "way", "ref": to_way, "role": "to"},
                ],
                "tags": {"type": "restriction", "restriction": kind, **tags},
            }
        )
        self._next_relation += 1


@dataclass
class GridCity:
    osm: Osm
    main: int
    south: int
    north: int
    avenues: dict[int, int]


def grid_city(*, main: dict[str, str] | None = None, north: dict[str, str] | None = None) -> GridCity:
    """Build the grid; main and north override those streets' tags."""
    osm = Osm()
    for row in range(3):
        for col in range(4):
            if row == 1 or col > 0:
                osm.node(junction(row, col), point(row, col))
    # A shape node on South St between cols 1 and 2: geometry, not a junction.
    bend = osm.node(2000, ((point(0, 1)[0] + 0.0001), (point(0, 1)[1] + point(0, 2)[1]) / 2))

    main_id = osm.way([junction(1, c) for c in range(4)], **{"highway": "primary", "name": "Main St", **(main or {})})
    south_id = osm.way(
        [junction(0, 1), bend, junction(0, 2), junction(0, 3)], highway="residential", name="South St"
    )
    north_id = osm.way(
        [junction(2, c) for c in range(1, 4)], **{"highway": "residential", "name": "North St", **(north or {})}
    )
    avenues = {
        c: osm.way([junction(r, c) for r in range(3)], highway="residential", name=f"{c} Ave") for c in range(1, 4)
    }
    return GridCity(osm, main_id, south_id, north_id, avenues)


MEDIAN_HALF_LNG = 0.000123  # ~10 m: the carriageways run 20 m apart


def divided_road() -> Osm:
    """Glen Ave, a divided road running north for 400 m, as two one-way carriageways.

        row 2   sb2 <-- nb2       a turnaround at each end,
                 |       ^        and a median opening at row 1
        row 1   sb1 <-- nb1
                 v       |
        row 0   sb0 --> nb0

    Northbound is on the east side, southbound on the west (traffic keeps right).
    """
    osm = Osm()
    for row in range(3):
        lat, lng = point(row, 1)
        osm.node(3100 + row, (lat, lng + MEDIAN_HALF_LNG))  # northbound carriageway
        osm.node(3200 + row, (lat, lng - MEDIAN_HALF_LNG))  # southbound carriageway
    glen = {"highway": "secondary", "name": "Glen Ave", "oneway": "yes"}
    osm.way([3100, 3101, 3102], **glen)
    osm.way([3202, 3201, 3200], **glen)
    for north, south in ((3101, 3201), (3102, 3202)):
        osm.way([north, south], highway="secondary_link", oneway="yes")  # median opening, north-end turnaround
    osm.way([3200, 3100], highway="secondary_link", oneway="yes")  # south-end turnaround
    return osm


def freeway_with_exits() -> Osm:
    """I-94 running east for 2 km, with an exit to Hawley Rd at 600 m and one to 68th St at 1400 m.

        Frontage Rd  +-------------+---------------+-------------+
                     |             | Hawley Rd     | 68th St     |
            on-ramp  |             +               +             | exit
                     |              \ exit          \ exit       |
        I-94         +===============+===============+===========+>   (eastbound only)
                     0             600 m          1400 m       2000 m

    The frontage road and the ramps at each end let traffic loop back, so the
    whole map survives build_graph (it keeps what every car can drive out of).
    """
    osm = Osm()
    west = point(0, 0)
    per_m = 1 / 81_400  # degrees of longitude per meter here
    freeway = [osm.node(4000 + k, (west[0], west[1] + m * per_m)) for k, m in enumerate((0, 600, 1400, 2000))]
    osm.way(freeway, highway="motorway", name="East-West Freeway", oneway="yes")
    frontage = [osm.node(4300, (west[0] + 0.0035, west[1]))]
    for k, (at, street) in enumerate(((1, "North Hawley Road"), (2, "South 68th Street"))):
        lat, lng = osm_point(osm, freeway[at])
        ramp_end = osm.node(4100 + k, (lat + 0.0015, lng + 0.002))
        frontage.append(osm.node(4200 + k, (lat + 0.0035, lng + 0.002)))
        osm.way([freeway[at], ramp_end], highway="motorway_link", oneway="yes")
        osm.way([ramp_end, frontage[-1]], highway="secondary", name=street)
    frontage.append(osm.node(4301, (west[0] + 0.0035, west[1] + 2000 * per_m)))
    osm.way(frontage, highway="tertiary", name="Frontage Road")
    osm.way([freeway[-1], frontage[-1]], highway="motorway_link", oneway="yes")  # exit at the east end
    osm.way([frontage[0], freeway[0]], highway="motorway_link", oneway="yes")  # on-ramp at the west end
    return osm


def osm_point(osm: Osm, node_id: int) -> Point:
    node = next(e for e in osm.elements if e["type"] == "node" and e["id"] == node_id)
    return node["lat"], node["lon"]
