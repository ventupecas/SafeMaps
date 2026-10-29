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
