"""A drivable road graph built from OpenStreetMap, for the engine's own router.

Junctions are nodes. The road between two junctions is a directed edge with
its length, expected no-traffic speed, road class, name, shape and whether
the road is one-way. Turn restrictions ("no left turn", "only straight on")
become banned edge-to-edge transitions.

Build it once with `python -m app.build_graph`. The result is saved as
gzipped JSON and loaded at startup.
"""

from __future__ import annotations

import gzip
import heapq
import json
import logging
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.features import is_winding
from app.geo import (
    Point,
    angle_between_deg,
    decode_polyline,
    densify,
    distance_to_path_m,
    encode_polyline,
    haversine_m,
    initial_bearing_deg,
    path_length_m,
)
from app.overpass import ATTRIBUTION, DRIVABLE_HIGHWAYS, BBox
from app.road_events import EVENT_MATCH_DEG, EVENT_MATCH_M, RAMP_KINDS, RoadEvent, carries, road_words

logger = logging.getLogger(__name__)

GRAPH_FORMAT = 1
MPH_TO_KMH = 1.609344

# Posted speed when OSM has no maxspeed tag, in mph (typical US values).
DEFAULT_SPEED_MPH = {
    "motorway": 65,
    "motorway_link": 40,
    "trunk": 55,
    "trunk_link": 35,
    "primary": 40,
    "primary_link": 30,
    "secondary": 35,
    "secondary_link": 25,
    "tertiary": 30,
    "tertiary_link": 25,
    "unclassified": 25,
    "residential": 25,
    "living_street": 15,
    "service": 15,
}
# Share of the posted speed a car averages with no traffic. Lower on streets
# with frequent junctions; time at traffic signals is added separately.
SPEED_FACTOR = {"motorway": 0.95, "trunk": 0.9, "motorway_link": 0.75, "trunk_link": 0.75}
DEFAULT_SPEED_FACTOR = 0.8
SIGNAL_DELAY_S = 12.0

# Classes a trip can't start or end on: you can't park on a freeway or a ramp.
NO_SNAP_CLASSES = frozenset({"motorway", "motorway_link", "trunk_link"})
# A junction is busy when one of these roads meets there: arterial or bigger.
BUSY_CLASSES = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary"})
# Where only these meet, the junction is an interchange: no oncoming traffic to cross.
GRADE_SEPARATED_CLASSES = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})
# A one-way road with a same-named carriageway running the other way within
# this distance is half of a divided road, not a one-way street: its oncoming
# traffic is just across the median.
DIVIDED_ROAD_M = 50.0
DIVIDED_ROAD_MIN_DEG = 150.0
# How far a turn reported by Google may sit from the OSM junction it happens at.
JUNCTION_MATCH_M = 30.0
# How close a route must pass a traffic signal to count it.
SIGNAL_MATCH_M = 20.0
# How close a route given as points must pass both junctions of a segment to follow it.
ALONG_MATCH_M = 20.0
# A moving car is on a segment that passes within VEHICLE_MATCH_M of its
# position (GPS error) and runs within VEHICLE_MATCH_DEG of its heading there.
VEHICLE_MATCH_M = 40.0
VEHICLE_MATCH_DEG = 60.0
# Segments end at junctions, which can be far apart on a freeway, so the
# search for a car's segment widens until one passes this close.
_VEHICLE_SEARCH_M = (300.0, 1_000.0, 2_500.0)
_VEHICLE_SURE_M = 15.0
# A road event's stretch is matched to the road it lies on: each end snaps to
# the segment there, running the event's way, and the stretch is the shortest
# drive between them, if that's at most STRETCH_DETOUR times the straight
# distance plus STRETCH_SLACK_M (roads curve; a much longer drive is another
# road). A single point matches the nearest road within EVENT_MATCH_M.
STRETCH_DETOUR = 1.5
STRETCH_SLACK_M = 500.0
MAX_WAYPOINTS = 12  # lines with this few points are waypoints, matched one to the next
WAYPOINT_SPOT_M = 30.0  # waypoints closer together than this mark one spot
SAME_ROAD_M = 5.0  # roads this much farther from a point than the nearest one are other roads
# Hazards from OSM: a street within SCHOOL_ZONE_M of a school (its center) is in
# its school zone; freeways never are. A route passes a railroad crossing when
# it goes within RAIL_MATCH_M of one.
SCHOOL_ZONE_M = 200.0
NO_SCHOOL_ZONE_CLASSES = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})
RAIL_MATCH_M = 20.0
# A ramp closure's ramp leaves or joins the freeway within RAMP_NAMED_SEARCH_M of
# its position when the ramp's street is named and matched, else RAMP_SEARCH_M.
RAMP_SEARCH_M = 300.0
RAMP_NAMED_SEARCH_M = 1_500.0
FREEWAY_CLASSES = frozenset({"motorway", "trunk"})  # the roads ramps leave and join
_EDGE_CELL_DEG = 0.002  # ~220 m; segments are filed in every cell along their chord
_EDGE_SAMPLE_M = 100.0
_SIGNAL_CELL_DEG = 0.0005  # ~55 m: small cells keep the per-point lookup cheap
_FINE_CELL_DEG = 0.0005  # for node lookups within a few dozen meters
_FINE_LOOKUP_M = 100.0

_NO_ACCESS = frozenset({"no", "private", "agricultural", "forestry", "emergency", "delivery"})
_EXCLUDED_SERVICE = frozenset({"parking_aisle", "parking", "driveway", "drive-through", "emergency_access"})
_MAXSPEED = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$")
_SNAP_CELL_DEG = 0.005
_M_PER_DEG_LAT = 111_320.0


@dataclass
class RoadGraph:
    area: str
    bbox: BBox | None
    lat: list[float]
    lng: list[float]
    edge_from: list[int]
    edge_to: list[int]
    length_m: list[float]
    speed_kmh: list[float]
    signals: list[int]
    road_class: list[int]  # index into classes
    name: list[int]  # index into names; 0 is unnamed
    way_id: list[int]
    oneway: list[bool]
    winding: list[bool]
    bearing_start: list[float]  # heading when leaving the start junction
    bearing_end: list[float]  # heading when arriving at the end junction
    geometry: list[str]  # encoded polyline, in the direction of travel
    classes: list[str]
    names: list[str]
    banned: dict[int, frozenset[int]]  # edge -> edges it may not turn onto
    stats: dict[str, int] = field(default_factory=dict)
    # Hazards, when the map was built with them (empty in older map files):
    rail_crossings: list[int] = field(default_factory=list)  # per edge: railroad crossings at grade
    school_zone: list[bool] = field(default_factory=list)  # per edge: within SCHOOL_ZONE_M of a school
    rail_crossing_points: list[Point] = field(default_factory=list)
    school_points: list[Point] = field(default_factory=list)
    # "car", or "bike" for the bike map (build_graph(mode="bike")): its segments carry stress, its
    # speeds are riding speeds, and it has trails and paths cars can't use.
    mode: str = "car"
    stress: list[int] = field(default_factory=list)  # per segment, bike map only: Level of Traffic Stress, 1-4

    time_s: list[float] = field(init=False, repr=False)
    out_edges: list[list[int]] = field(init=False, repr=False)
    in_edges: list[list[int]] = field(init=False, repr=False)
    max_speed_kmh: float = field(init=False)
    _cell_indexes: dict[tuple[bool, float], dict[tuple[int, int], list[int]]] = field(
        init=False, default_factory=dict, repr=False
    )
    _node_kinds: dict[int, str] = field(init=False, default_factory=dict, repr=False)
    _signal_cells: dict[tuple[int, int], list[int]] | None = field(init=False, default=None, repr=False)
    _edge_cells: dict[tuple[int, int], list[int]] | None = field(init=False, default=None, repr=False)
    _point_cells: dict[str, dict[tuple[int, int], list[Point]]] = field(init=False, default_factory=dict, repr=False)
    # Meters on each named road of the routes our router built, by their encoded polyline: naming a
    # route (engine.name_routes) reads them here instead of looking its road up every 100 m.
    known_road_lengths: dict[str, dict[str, float]] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.rail_crossings = self.rail_crossings or [0] * len(self.edge_from)
        self.school_zone = self.school_zone or [False] * len(self.edge_from)
        self.stress = self.stress or [0] * len(self.edge_from)
        self.time_s = [
            length / (speed / 3.6) + SIGNAL_DELAY_S * signals
            for length, speed, signals in zip(self.length_m, self.speed_kmh, self.signals, strict=True)
        ]
        self.out_edges = [[] for _ in self.lat]
        self.in_edges = [[] for _ in self.lat]
        for edge, (start, end) in enumerate(zip(self.edge_from, self.edge_to, strict=True)):
            self.out_edges[start].append(edge)
            self.in_edges[end].append(edge)
        self.max_speed_kmh = max(self.speed_kmh, default=100.0)

    @property
    def node_count(self) -> int:
        return len(self.lat)

    @property
    def edge_count(self) -> int:
        return len(self.edge_from)

    def point(self, node: int) -> Point:
        return (self.lat[node], self.lng[node])

    def edge_points(self, edge: int) -> list[Point]:
        return decode_polyline(self.geometry[edge])

    def class_name(self, edge: int) -> str:
        return self.classes[self.road_class[edge]]

    def nearest_node(self, point: Point, max_distance_m: float = 1_000.0) -> int | None:
        """Closest junction a trip can start or end at, or None if none is within max_distance_m."""
        return self._nearest(point, max_distance_m, snappable_only=True)

    def node_kind(self, node: int) -> str:
        """What meets at node: 'interchange' or 'one-way' (no oncoming traffic to cross), else 'busy' or 'quiet'."""
        kind = self._node_kinds.get(node)
        if kind is None:
            edges = self.out_edges[node] + self.in_edges[node]
            kind = classify_junction(self.class_name(edge) for edge in edges)
            if (
                kind != "interchange"
                and edges
                and all(self.oneway[edge] for edge in edges)
                and not self._on_divided_road(node)
            ):
                kind = "one-way"  # e.g. two one-way streets crossing downtown
            self._node_kinds[node] = kind
        return kind

    def _on_divided_road(self, node: int) -> bool:
        """True when a road through node is one carriageway of a divided road: its name runs the other way close by."""
        here = self._named_directions(node)
        if not here:
            return False
        point = self.point(node)
        for other in self._nodes_near(point, DIVIDED_ROAD_M, snappable_only=False):
            if other == node or haversine_m(point, self.point(other)) > DIVIDED_ROAD_M:
                continue
            for name, bearing in self._named_directions(other):
                if any(
                    name == mine and angle_between_deg(bearing, my_bearing) >= DIVIDED_ROAD_MIN_DEG
                    for mine, my_bearing in here
                ):
                    return True
        return False

    def _named_directions(self, node: int) -> list[tuple[int, float]]:
        """(name, direction of travel) of each named road segment leaving or reaching node."""
        leaving = [(self.name[edge], self.bearing_start[edge]) for edge in self.out_edges[node] if self.name[edge]]
        reaching = [(self.name[edge], self.bearing_end[edge]) for edge in self.in_edges[node] if self.name[edge]]
        return leaving + reaching

    def junction_kind(self, point: Point) -> str | None:
        """Kind of the junction at point (see node_kind), or None if no junction is that close."""
        node = self._nearest(point, JUNCTION_MATCH_M, snappable_only=False)
        return None if node is None else self.node_kind(node)

    def edges_in_box(self, south: float, west: float, north: float, east: float) -> list[int]:
        """Segments with a part inside the box (as the segment index files them: about 100 m apart)."""
        cells = self._edge_index()
        (i0, j0), (i1, j1) = _cell((south, west), _EDGE_CELL_DEG), _cell((north, east), _EDGE_CELL_DEG)
        found: set[int] = set()
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                found.update(cells.get((i, j), ()))
        return sorted(found)

    def signal_at(self, point: Point) -> bool:
        """Whether a traffic signal controls the junction at point (one within JUNCTION_MATCH_M)."""
        ci, cj = _cell(point, _SIGNAL_CELL_DEG)
        reach_lng = math.ceil(1 / max(math.cos(math.radians(point[0])), 0.01))
        cells = self._signal_index()
        return any(
            haversine_m(point, self.point(node)) <= JUNCTION_MATCH_M
            for i in range(ci - 1, ci + 2)
            for j in range(cj - reach_lng, cj + reach_lng + 1)
            for node in cells.get((i, j), ())
        )

    def signals_along(self, stretches: Iterable[Sequence[Point]]) -> int:
        """Distinct traffic signals within SIGNAL_MATCH_M of the given stretches of road.

        A signal sits where the road carrying it ends (its junction), which is
        where OSM usually tags it. Pass only surface-street stretches: a freeway
        bridging a signalized street would otherwise pick its signal up.
        """
        cells = self._signal_index()
        reach_lng = 1
        found: set[int] = set()
        for stretch in stretches:
            if not stretch:
                continue
            reach_lng = math.ceil(1 / max(math.cos(math.radians(stretch[0][0])), 0.01))
            for point in densify(stretch, SIGNAL_MATCH_M / 2):
                ci, cj = _cell(point, _SIGNAL_CELL_DEG)
                for i in range(ci - 1, ci + 2):
                    for j in range(cj - reach_lng, cj + reach_lng + 1):
                        for node in cells.get((i, j), ()):
                            if node not in found and haversine_m(point, self.point(node)) <= SIGNAL_MATCH_M:
                                found.add(node)
        return len(found)

    def road_name_at(self, point: Point, heading: float) -> str:
        """The name of the road at point running along heading ("" if unnamed or none is near), for directions."""
        roads = self._roads_at(point, heading)
        named = [self.names[self.name[edge]] for edge in roads if self.name[edge]]
        return named[0] if named else ""

    def rail_crossings_along(self, stretches: Iterable[Sequence[Point]]) -> int:
        """Railroad crossings at grade the given stretches of road pass (within RAIL_MATCH_M)."""
        return len(self.rail_crossings_at(stretches))

    def rail_crossings_at(self, stretches: Iterable[Sequence[Point]]) -> list[Point]:
        """Where the given stretches of road cross railroad tracks at grade, for the map."""
        return self._points_along("rail", self.rail_crossing_points, stretches, RAIL_MATCH_M)

    def schools_along(self, stretches: Iterable[Sequence[Point]]) -> int:
        """Schools within SCHOOL_ZONE_M of the given stretches of road: the school zones they pass."""
        return len(self.schools_at(stretches))

    def schools_at(self, stretches: Iterable[Sequence[Point]]) -> list[Point]:
        """The schools whose zones the given stretches of road pass, for the map."""
        return self._points_along("school", self.school_points, stretches, SCHOOL_ZONE_M)

    def _points_along(
        self, kind: str, points: Sequence[Point], stretches: Iterable[Sequence[Point]], within_m: float
    ) -> list[Point]:
        if not points:
            return []
        cell_deg = within_m / _M_PER_DEG_LAT
        cells = self._point_cells.get(kind)
        if cells is None:
            grouped: dict[tuple[int, int], list[Point]] = defaultdict(list)
            for point in points:
                grouped[_cell(point, cell_deg)].append(point)
            cells = self._point_cells[kind] = dict(grouped)
        found: set[Point] = set()
        for stretch in stretches:
            if not stretch:
                continue
            reach_lng = math.ceil(1 / max(math.cos(math.radians(stretch[0][0])), 0.01))
            for sample in densify(stretch, within_m / 2):
                ci, cj = _cell(sample, cell_deg)
                for i in range(ci - 1, ci + 2):
                    for j in range(cj - reach_lng, cj + reach_lng + 1):
                        for point in cells.get((i, j), ()):
                            if point not in found and haversine_m(sample, point) <= within_m:
                                found.add(point)
        return sorted(found)

    def _signal_index(self) -> dict[tuple[int, int], list[int]]:
        if self._signal_cells is None:
            cells: dict[tuple[int, int], list[int]] = defaultdict(list)
            for node in {self.edge_to[edge] for edge in range(self.edge_count) if self.signals[edge]}:
                cells[_cell(self.point(node), _SIGNAL_CELL_DEG)].append(node)
            self._signal_cells = dict(cells)
        return self._signal_cells

    def segment_under(self, point: Point, heading: float) -> int | None:
        """The segment a car at point, driving along heading, is on; None if no segment fits.

        Unlike nearest_node, freeways and ramps count: a moving car may be on one.
        """
        candidates = self._segments_under(point, heading)
        return candidates[0] if candidates else None

    def _segments_under(self, point: Point, heading: float, road: str = "any") -> list[int]:
        """Segments within VEHICLE_MATCH_M of point running along heading, nearest first: those about as near as the nearest.

        At a junction several touch the point (the one arriving, the ones
        leaving, a ramp merging), so there's often more than one. road limits
        them to ramps ("ramp") or to everything else ("mainline").
        """
        offsets: dict[int, float] = {}
        checked: set[int] = set()
        for radius in _VEHICLE_SEARCH_M:
            for node in self._nodes_near(point, radius, snappable_only=False):
                # The car drives toward the end of its segment, so skip junctions behind it.
                if node in checked or (
                    haversine_m(point, self.point(node)) > VEHICLE_MATCH_M
                    and angle_between_deg(initial_bearing_deg(point, self.point(node)), heading) > 90
                ):
                    continue
                checked.add(node)
                for edge in self.in_edges[node]:
                    if not self._is_road(edge, road):
                        continue
                    offset, bearing = distance_to_path_m(point, self.edge_points(edge))
                    if offset <= VEHICLE_MATCH_M and angle_between_deg(bearing, heading) <= VEHICLE_MATCH_DEG:
                        offsets[edge] = offset
            if offsets and min(offsets.values()) <= _VEHICLE_SURE_M:
                break  # a wider search could only find roads farther from the car
        if not offsets:
            return []
        nearest = min(offsets.values())
        return sorted((edge for edge, offset in offsets.items() if offset <= nearest + SAME_ROAD_M), key=offsets.get)

    def _is_road(self, edge: int, road: str) -> bool:
        """Whether edge is the kind of road an event says it's on (see RoadEvent.road)."""
        if road == "any":
            return True
        return self.class_name(edge).endswith("_link") == (road in RAMP_KINDS)

    def _ramp_near(self, event: RoadEvent) -> list[int] | None:
        """The ramp a ramp event names, as its segments in travel order; None if no ramp fits.

        Sources place ramp closures on the freeway near the ramp, so this looks
        for the junction where a ramp leaves (off-ramp) or joins (on-ramp) the
        freeway, rather than the nearest line:
        - a freeway-to-freeway ramp ("... TO I-94 WB"): the nearest within
          RAMP_NAMED_SEARCH_M that leads onto that freeway that way, from where
          it splits from ramps going elsewhere;
        - one reaching the street the event names, within RAMP_NAMED_SEARCH_M;
        - else the nearest within RAMP_SEARCH_M.
        """
        point = event.path[len(event.path) // 2]
        wanted = frozenset(event.ramp_road.split())
        heading_ok = lambda bearing: event.direction is None or angle_between_deg(bearing, event.direction) <= EVENT_MATCH_DEG  # noqa: E731
        named: list[tuple[float, int]] = []
        nearest: list[tuple[float, int]] = []
        toward: list[tuple[float, list[int]]] = []
        wide = wanted or event.ramp_toward is not None
        for node in self._nodes_near(point, RAMP_NAMED_SEARCH_M if wide else RAMP_SEARCH_M, snappable_only=False):
            distance = haversine_m(point, self.point(node))
            ramps: list[tuple[int, bool]] = []  # (ramp segment, leaves the freeway)
            if event.road in ("off-ramp", "ramp") and any(
                heading_ok(self.bearing_end[e]) for e in self.in_edges[node] if self.class_name(e) in FREEWAY_CLASSES
            ):
                ramps += [(e, True) for e in self.out_edges[node] if self._is_link(e)]
            if event.road in ("on-ramp", "ramp") and any(
                heading_ok(self.bearing_start[e]) for e in self.out_edges[node] if self.class_name(e) in FREEWAY_CLASSES
            ):
                ramps += [(e, False) for e in self.in_edges[node] if self._is_link(e)]
            for ramp, leaving in ramps:
                if event.ramp_toward is not None:
                    branch = self._ramp_branch(ramp, event.ramp_toward) if leaving else None
                    if branch:
                        toward.append((distance, branch))
                elif wanted and wanted <= self._ramp_street_words(ramp, leaving):
                    named.append((distance, ramp))
                elif distance <= RAMP_SEARCH_M:
                    nearest.append((distance, ramp))
        if event.ramp_toward is not None:
            return min(toward, default=(0.0, None))[1]
        best = min(named or nearest, default=None)
        return self._ramp_chain(best[1], leaving=self._leaves_freeway(best[1])) if best else None

    def _ramp_branch(self, ramp: int, toward: float) -> list[int] | None:
        """The ramp segments from ramp onto a freeway heading toward, after the last split; None if it leads elsewhere."""
        came_from: dict[int, int | None] = {ramp: None}
        queue = [ramp]
        while queue:
            edge = queue.pop(0)
            for nxt in self.out_edges[self.edge_to[edge]]:
                if self.class_name(nxt) in FREEWAY_CLASSES:
                    if angle_between_deg(self.bearing_start[nxt], toward) > EVENT_MATCH_DEG:
                        continue
                    path = [edge]
                    while came_from[path[-1]] is not None:
                        path.append(came_from[path[-1]])  # type: ignore[arg-type]
                    path.reverse()
                    # Segments before a split also carry traffic bound elsewhere: leave them open.
                    for i in range(len(path) - 1, 0, -1):
                        if sum(1 for e in self.out_edges[self.edge_to[path[i - 1]]] if self._is_link(e)) > 1:
                            return path[i:]
                    return path
                if self._is_link(nxt) and nxt not in came_from and len(came_from) < 40:
                    came_from[nxt] = edge
                    queue.append(nxt)
        return None

    def _ramp_street_words(self, ramp: int, leaving: bool) -> frozenset[str]:
        """Words naming the streets at the far end of a ramp: followed forward from an exit, back from an entrance."""
        words: set[str] = set()
        seen: set[int] = set()
        frontier = [ramp]
        for _ in range(8):  # ramps run a few segments at most
            following: list[int] = []
            for edge in frontier:
                if edge in seen:
                    continue
                seen.add(edge)
                node = self.edge_to[edge] if leaving else self.edge_from[edge]
                for other in self.out_edges[node] + self.in_edges[node]:
                    if not self._is_link(other) and self.class_name(other) not in FREEWAY_CLASSES:
                        words |= road_words(self.names[self.name[other]])
                following += self.out_edges[node] if leaving else self.in_edges[node]
            frontier = [edge for edge in following if self._is_link(edge)]
        return frozenset(words)

    def _is_link(self, edge: int) -> bool:
        return self.class_name(edge).endswith("_link")

    def _leaves_freeway(self, ramp: int) -> bool:
        """Whether a ramp segment starts at the freeway (an exit) rather than ending at it (an entrance)."""
        return any(self.class_name(edge) in FREEWAY_CLASSES for edge in self.in_edges[self.edge_from[ramp]])

    def _ramp_chain(self, ramp: int, *, leaving: bool) -> list[int]:
        """The ramp's segments in travel order, followed from the freeway until it reaches a street or splits."""
        chain = [ramp]
        for _ in range(8):
            end = self.edge_to[chain[-1]] if leaving else self.edge_from[chain[0]]
            onward = [e for e in (self.out_edges[end] if leaving else self.in_edges[end]) if self._is_link(e)]
            if len(onward) != 1 or onward[0] in chain:
                break
            if leaving:
                chain.append(onward[0])
            else:
                chain.insert(0, onward[0])
        return chain

    def edges_along(self, points: Sequence[Point], within_m: float = ALONG_MATCH_M) -> set[int]:
        """Segments a route given as points follows: both of their junctions lie on it."""
        on_route: set[int] = set()
        for point in densify(points, within_m):
            for node in self._nodes_near(point, within_m, snappable_only=False):
                if node not in on_route and haversine_m(point, self.point(node)) <= within_m:
                    on_route.add(node)
        return {edge for node in on_route for edge in self.out_edges[node] if self.edge_to[edge] in on_route}

    def event_edges(self, event: RoadEvent) -> set[int]:
        """The segments a road event (a closure, a jam) lies on, in the directions of traffic it affects."""
        if event.road in RAMP_KINDS and not event.along:
            return set(self._ramp_near(event) or ())
        if len(event.path) == 1:
            return self._roads_at(event.path[0], event.direction, event.road)
        return {edge for stretch in self._stretches(event) for edge in stretch}

    def snap_events(self, events: Iterable[RoadEvent]) -> list[RoadEvent]:
        """Road events redrawn along the roads they lie on, one per direction of traffic.

        Sources often give only the two ends of a stretch; scoring and drawing
        need the road between them. Points, and lines no road matches, stay as
        they are, except ramp events: where no ramp matches, they're dropped,
        since their line lies on the freeway, which stays open.
        """
        snapped = []
        for event in events:
            if event.road in RAMP_KINDS and not event.along:
                ramp = self._ramp_near(event)
                if not ramp:
                    logger.debug("No ramp found for road event %r; left out", event.description[:80])
                else:
                    # The whole ramp, in travel order: its first segment alone runs right beside the freeway.
                    points: list[Point] = []
                    for edge in ramp:
                        segment = self.edge_points(edge)
                        points.extend(segment if not points else segment[1:])
                    snapped.append(replace(event, path=tuple(points), direction=None, along=True))
                continue
            stretches = self._stretches(event) if len(event.path) > 1 else []
            if not stretches:
                snapped.append(event)
            for stretch in stretches:
                points: list[Point] = []
                for edge in stretch:
                    segment = self.edge_points(edge)
                    points.extend(segment if not points else segment[1:])
                snapped.append(replace(event, path=tuple(points), direction=None, along=True))
        return snapped

    def _stretches(self, event: RoadEvent) -> list[list[int]]:
        """The drive along the road from the event's first point to its last, and back if both ways are affected."""
        path = list(event.path)
        forward = initial_bearing_deg(path[0], path[-1])
        if event.along or (event.direction is not None and angle_between_deg(forward, event.direction) <= 90):
            ways = [path]
        elif event.direction is not None:
            ways = [path[::-1]]
        else:
            ways = [path, path[::-1]]
        return [stretch for way in ways if (stretch := self._stretch_through(way, event.road))]

    def _stretch_through(self, path: Sequence[Point], road: str) -> list[int] | None:
        """A stretch through the path's points in order: piece by piece when they're a few waypoints.

        A long segment given by a handful of waypoints (a 511 travel time
        "via" another highway) can't be matched from its two ends alone. A
        detailed line can: matching it point by point would only be slower.
        """
        if len(path) <= 2 or len(path) > MAX_WAYPOINTS:
            return self._stretch(path, road)
        # Points closer together than WAYPOINT_SPOT_M are one spot: a short line drawn
        # with many points (TomTom's) is matched from its two ends.
        waypoints = [path[0]]
        for point in path[1:-1]:
            if haversine_m(point, waypoints[-1]) >= WAYPOINT_SPOT_M:
                waypoints.append(point)
        if len(waypoints) > 1 and haversine_m(path[-1], waypoints[-1]) < WAYPOINT_SPOT_M:
            waypoints.pop()  # the line's end stands for that spot
        waypoints.append(path[-1])
        if len(waypoints) <= 2:
            return self._stretch(path, road)
        edges: list[int] = []
        for a, b in zip(waypoints, waypoints[1:]):
            if haversine_m(a, b) < SAME_ROAD_M:
                continue  # turned back to the same spot
            piece = self._stretch([a, b], road)
            if piece is None:
                return None
            edges += [edge for edge in piece if not edges or edge != edges[-1]]
        return edges or None

    def _stretch(self, path: Sequence[Point], road: str = "any") -> list[int] | None:
        """The shortest drive from a segment at the path's first point to one at its last, along their direction."""
        starts = self._segments_under(path[0], initial_bearing_deg(path[0], path[1]), road)
        ends = self._segments_under(path[-1], initial_bearing_deg(path[-2], path[-1]), road)
        both = [edge for edge in starts if edge in ends]
        if both:
            return [both[0]]
        # A segment that only touches the stretch's first point at its end (or
        # its last point at its start) is the road before (or after) it, not part of it.
        targets: dict[int, int | None] = {}
        for edge in ends:
            touches = haversine_m(path[-1], self.point(self.edge_from[edge])) <= SAME_ROAD_M
            targets.setdefault(self.edge_from[edge], None if touches else edge)
        limit = haversine_m(path[0], path[-1]) * STRETCH_DETOUR + STRETCH_SLACK_M
        best: dict[int, float] = {}
        first_edge: dict[int, int | None] = {}  # where the drive starts: a node, and the segment that reaches it
        for edge in starts:
            node = self.edge_to[edge]
            touches = haversine_m(path[0], self.point(node)) <= SAME_ROAD_M
            if node not in best:
                best[node], first_edge[node] = 0.0, None if touches else edge
        came_by: dict[int, int] = {}
        heap = [(0.0, node) for node in best]
        while heap:
            distance, node = heapq.heappop(heap)
            if node in targets:
                middle: list[int] = []
                end = targets[node]
                while node in came_by:
                    middle.append(came_by[node])
                    node = self.edge_from[came_by[node]]
                stretch = [edge for edge in (first_edge[node], *middle[::-1], end) if edge is not None]
                return stretch or None
            if distance > best[node]:
                continue
            for edge in self.out_edges[node]:
                reached = distance + self.length_m[edge]
                nxt = self.edge_to[edge]
                if reached <= limit and reached < best.get(nxt, math.inf):
                    best[nxt] = reached
                    came_by[nxt] = edge
                    heapq.heappush(heap, (reached, nxt))
        return None  # only a much longer drive connects the ends: not one road

    def _roads_at(self, point: Point, direction: float | None, road: str = "any") -> set[int]:
        """Segments of the road nearest point (both ways of it unless direction says), within EVENT_MATCH_M."""
        cells = self._edge_index()
        ci, cj = _cell(point, _EDGE_CELL_DEG)
        offsets: dict[int, float] = {}
        for edge in {e for i in (ci - 1, ci, ci + 1) for j in (cj - 1, cj, cj + 1) for e in cells.get((i, j), ())}:
            if not self._is_road(edge, road):
                continue
            chord = (self.point(self.edge_from[edge]), self.point(self.edge_to[edge]))
            if _flat_distance_m(point, *chord) > EVENT_MATCH_M + min(self.length_m[edge] / 2, _EDGE_SAMPLE_M):
                continue  # far even allowing for a curve: skip decoding its shape
            offset, bearing = distance_to_path_m(point, self.edge_points(edge))
            if offset <= EVENT_MATCH_M and carries(bearing, None, direction, False):
                offsets[edge] = offset
        nearest = min(offsets.values(), default=0.0)
        return {edge for edge, offset in offsets.items() if offset <= nearest + SAME_ROAD_M}

    def _edge_index(self) -> dict[tuple[int, int], list[int]]:
        if self._edge_cells is None:
            cells: dict[tuple[int, int], list[int]] = defaultdict(list)
            for edge in range(self.edge_count):
                (lat1, lng1), (lat2, lng2) = self.point(self.edge_from[edge]), self.point(self.edge_to[edge])
                steps = max(1, math.ceil(self.length_m[edge] / _EDGE_SAMPLE_M))
                filed = {_cell((lat1 + (lat2 - lat1) * k / steps, lng1 + (lng2 - lng1) * k / steps), _EDGE_CELL_DEG)
                         for k in range(steps + 1)}  # fmt: skip
                for cell in filed:
                    cells[cell].append(edge)
            self._edge_cells = dict(cells)
        return self._edge_cells

    def _nearest(self, point: Point, max_distance_m: float, *, snappable_only: bool) -> int | None:
        best, best_m = None, max_distance_m
        for node in self._nodes_near(point, max_distance_m, snappable_only=snappable_only):
            distance = haversine_m(point, self.point(node))
            if distance <= best_m:
                best, best_m = node, distance
        return best

    def _nodes_near(self, point: Point, distance_m: float, *, snappable_only: bool) -> Iterator[int]:
        """Junctions in the grid cells within distance_m of point: a superset of those within distance_m."""
        cell_deg = _FINE_CELL_DEG if distance_m <= _FINE_LOOKUP_M else _SNAP_CELL_DEG
        cells = self._cell_index(snappable_only, cell_deg)
        cell_lat, cell_lng = _cell(point, cell_deg)
        reach_lat = math.ceil(distance_m / (_M_PER_DEG_LAT * cell_deg))
        lng_cell_m = _M_PER_DEG_LAT * cell_deg * max(math.cos(math.radians(point[0])), 0.01)
        reach_lng = math.ceil(distance_m / lng_cell_m)
        for i in range(cell_lat - reach_lat, cell_lat + reach_lat + 1):
            for j in range(cell_lng - reach_lng, cell_lng + reach_lng + 1):
                yield from cells.get((i, j), ())

    def _cell_index(self, snappable_only: bool, cell_deg: float) -> dict[tuple[int, int], list[int]]:
        index = self._cell_indexes.get((snappable_only, cell_deg))
        if index is None:
            if snappable_only:
                nodes: Iterable[int] = {
                    node
                    for edge in range(self.edge_count)
                    if self.class_name(edge) not in NO_SNAP_CLASSES
                    for node in (self.edge_from[edge], self.edge_to[edge])
                }
            else:
                nodes = range(self.node_count)
            cells: dict[tuple[int, int], list[int]] = defaultdict(list)
            for node in nodes:
                cells[_cell(self.point(node), cell_deg)].append(node)
            index = self._cell_indexes[snappable_only, cell_deg] = dict(cells)
        return index

    def save(self, path: Path) -> None:
        data = {
            "format": GRAPH_FORMAT,
            "area": self.area,
            "mode": self.mode,
            "bbox": [self.bbox.south, self.bbox.west, self.bbox.north, self.bbox.east] if self.bbox else None,
            "attribution": ATTRIBUTION,
            "classes": self.classes,
            "names": self.names,
            "stats": self.stats,
            "nodes": {"lat": [round(x, 7) for x in self.lat], "lng": [round(x, 7) for x in self.lng]},
            "edges": {
                "from": self.edge_from,
                "to": self.edge_to,
                "length_m": [round(x, 1) for x in self.length_m],
                "speed_kmh": [round(x, 1) for x in self.speed_kmh],
                "signals": self.signals,
                "class": self.road_class,
                "name": self.name,
                "way": self.way_id,
                "oneway": [int(x) for x in self.oneway],
                "winding": [int(x) for x in self.winding],
                "bearing_start": [round(x, 1) for x in self.bearing_start],
                "bearing_end": [round(x, 1) for x in self.bearing_end],
                "geometry": self.geometry,
                "rail_crossings": self.rail_crossings,
                "school": [int(x) for x in self.school_zone],
                **({"stress": self.stress} if self.mode == "bike" else {}),
            },
            "banned": [[edge, other] for edge, others in sorted(self.banned.items()) for other in sorted(others)],
            "rail_crossing_points": [[round(lat, 7), round(lng, 7)] for lat, lng in self.rail_crossing_points],
            "school_points": [[round(lat, 7), round(lng, 7)] for lat, lng in self.school_points],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        os.close(fd)
        try:
            with gzip.open(tmp_name, "wt", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> RoadGraph:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("format") != GRAPH_FORMAT:
            raise ValueError(f"{path.name} is graph format {data.get('format')}; rebuild it with app.build_graph")
        edges = data["edges"]
        banned: dict[int, set[int]] = defaultdict(set)
        for edge, other in data["banned"]:
            banned[edge].add(other)
        return cls(
            area=data["area"],
            bbox=BBox(*data["bbox"]) if data["bbox"] else None,
            lat=data["nodes"]["lat"],
            lng=data["nodes"]["lng"],
            edge_from=edges["from"],
            edge_to=edges["to"],
            length_m=edges["length_m"],
            speed_kmh=edges["speed_kmh"],
            signals=edges["signals"],
            road_class=edges["class"],
            name=edges["name"],
            way_id=edges["way"],
            oneway=[bool(x) for x in edges["oneway"]],
            winding=[bool(x) for x in edges["winding"]],
            bearing_start=edges["bearing_start"],
            bearing_end=edges["bearing_end"],
            geometry=edges["geometry"],
            classes=data["classes"],
            names=data["names"],
            banned={edge: frozenset(others) for edge, others in banned.items()},
            stats=data.get("stats", {}),
            # Maps built before hazards were added have none: rebuild to get them.
            rail_crossings=edges.get("rail_crossings", []),
            school_zone=[bool(x) for x in edges.get("school", [])],
            rail_crossing_points=[(lat, lng) for lat, lng in data.get("rail_crossing_points", [])],
            school_points=[(lat, lng) for lat, lng in data.get("school_points", [])],
            mode=data.get("mode", "car"),
            stress=edges.get("stress", []),
        )


def classify_junction(classes: Iterable[str]) -> str:
    """'interchange' where only freeways and their ramps meet, 'busy' where an arterial or bigger meets, else 'quiet'."""
    classes = set(classes)
    # Two trunk roads alone may cross at grade, so a freeway or a ramp must be there too.
    if classes and classes <= GRADE_SEPARATED_CLASSES and classes & {"motorway", "motorway_link", "trunk_link"}:
        return "interchange"
    return "busy" if classes & BUSY_CLASSES else "quiet"


def build_graph(
    elements: Iterable[Mapping[str, Any]], *, area: str = "", bbox: BBox | None = None, mode: str = "car"
) -> RoadGraph:
    """Build a RoadGraph from Overpass JSON elements (nodes, ways and restriction relations)."""
    coords: dict[int, Point] = {}
    signal_nodes: set[int] = set()
    crossing_nodes: set[int] = set()
    schools: list[Point] = []
    ways: dict[int, Mapping[str, Any]] = {}
    relations: dict[int, Mapping[str, Any]] = {}
    for element in elements:
        kind = element.get("type")
        tags = element.get("tags", {})
        if tags.get("amenity") == "school":
            center = element.get("center") or element
            if "lat" in center:
                schools.append((center["lat"], center["lon"]))
            continue
        if kind == "node":
            if "lat" in element:
                coords[element["id"]] = (element["lat"], element["lon"])
            if tags.get("highway") == "traffic_signals":
                signal_nodes.add(element["id"])
            if tags.get("railway") == "level_crossing":
                crossing_nodes.add(element["id"])
            if mode == "bike":
                # A trail's own signal to cross a street, and its own crossing of the tracks.
                if tags.get("highway") == "crossing" and tags.get("crossing") == "traffic_signals":
                    signal_nodes.add(element["id"])
                if tags.get("railway") == "crossing":
                    crossing_nodes.add(element["id"])
        elif kind == "way":
            ways[element["id"]] = element
        elif kind == "relation":
            relations[element["id"]] = element

    stats: Counter[str] = Counter()
    roads = []
    for way in ways.values():
        road = _Road.from_way(way, coords) if mode == "car" else _Road.for_bikes(way, coords)
        if road is None:
            stats["ways_skipped"] += 1
        else:
            roads.append(road)
    stats["ways_used"] = len(roads)

    # A junction is any node shared by two roads, or where a road ends.
    uses: Counter[int] = Counter()
    for road in roads:
        uses.update(road.refs)
        uses[road.refs[0]] += 1
        uses[road.refs[-1]] += 1

    builder = _EdgeBuilder(coords, signal_nodes, crossing_nodes, mode)
    for road in roads:
        start = 0
        for i in range(1, len(road.refs)):
            if uses[road.refs[i]] >= 2:
                builder.add_segment(road, road.refs[start : i + 1])
                start = i

    banned = _apply_restrictions(builder, relations.values(), stats, mode)
    graph = builder.finish(area, bbox, banned, stats)
    _mark_school_zones(graph, schools)
    graph.rail_crossing_points = sorted(coords[node] for node in crossing_nodes if node in coords)
    graph.stats.update(
        rail_crossings=len(graph.rail_crossing_points),
        schools=len(schools),
        school_zone_segments=sum(graph.school_zone),
    )
    if mode == "bike":
        graph.stats.update({f"stress_{level}_km": round(sum(l for l, s in zip(graph.length_m, graph.stress) if s == level) / 2000) for level in (1, 2, 3, 4)})
    logger.info("Built %s: %d junctions, %d road segments", area or "graph", graph.node_count, graph.edge_count)
    return graph


@dataclass
class _Road:
    way_id: int
    refs: list[int]
    highway: str
    speed_kmh: float
    name: str
    forward: bool
    backward: bool
    stress: int = 0  # bike map only: Level of Traffic Stress, 1-4

    @classmethod
    def for_bikes(cls, way: Mapping[str, Any], coords: Mapping[int, Point]) -> _Road | None:
        """The way as a bike map has it: who may ride it, which ways, how fast, and how stressful."""
        tags = way.get("tags", {})
        if not _bikeable(tags):
            return None
        direction = _bike_direction(tags)
        if direction is None:
            return None
        refs: list[int] = []
        for ref in way.get("nodes", []):
            if ref in coords and (not refs or refs[-1] != ref):
                refs.append(ref)
        if len(refs) < 2:
            return None
        return cls(
            way_id=way["id"],
            refs=refs,
            highway=tags["highway"],
            speed_kmh=BIKE_UNPAVED_KMH if tags.get("surface") in UNPAVED_SURFACES else BIKE_SPEED_KMH,
            name=tags.get("name") or tags.get("ref") or "",
            forward=direction[0],
            backward=direction[1],
            stress=traffic_stress(tags),
        )

    @classmethod
    def from_way(cls, way: Mapping[str, Any], coords: Mapping[int, Point]) -> _Road | None:
        tags = way.get("tags", {})
        if not _drivable(tags):
            return None
        direction = _direction(tags)
        if direction is None:
            return None
        refs: list[int] = []
        for ref in way.get("nodes", []):
            if ref in coords and (not refs or refs[-1] != ref):
                refs.append(ref)
        if len(refs) < 2:
            return None
        highway = tags["highway"]
        return cls(
            way_id=way["id"],
            refs=refs,
            highway=highway,
            speed_kmh=_speed_kmh(tags) * SPEED_FACTOR.get(highway, DEFAULT_SPEED_FACTOR),
            name=tags.get("name") or tags.get("ref") or "",
            forward=direction[0],
            backward=direction[1],
        )


def _mark_school_zones(graph: RoadGraph, schools: Sequence[Point]) -> None:
    """Flag the street segments within SCHOOL_ZONE_M of a school."""
    graph.school_points = sorted(set(schools))
    if not schools:
        return
    cell_deg = SCHOOL_ZONE_M / _M_PER_DEG_LAT
    cells: dict[tuple[int, int], list[Point]] = defaultdict(list)
    for school in graph.school_points:
        cells[_cell(school, cell_deg)].append(school)
    reach_lng = math.ceil(1 / max(math.cos(math.radians(graph.school_points[0][0])), 0.01))
    for edge in range(graph.edge_count):
        if graph.class_name(edge) in NO_SCHOOL_ZONE_CLASSES:
            continue
        for sample in densify(graph.edge_points(edge), SCHOOL_ZONE_M / 2):
            ci, cj = _cell(sample, cell_deg)
            if any(
                haversine_m(sample, school) <= SCHOOL_ZONE_M
                for i in range(ci - 1, ci + 2)
                for j in range(cj - reach_lng, cj + reach_lng + 1)
                for school in cells.get((i, j), ())
            ):
                graph.school_zone[edge] = True
                break


class _EdgeBuilder:
    def __init__(
        self, coords: Mapping[int, Point], signal_nodes: set[int], crossing_nodes: set[int] = frozenset(), mode: str = "car"
    ) -> None:
        self.mode = mode
        self.coords = coords
        self.signal_nodes = signal_nodes
        self.crossing_nodes = crossing_nodes
        self.node_ids: dict[int, int] = {}  # OSM node id -> junction index
        self.lat: list[float] = []
        self.lng: list[float] = []
        self.names: dict[str, int] = {"": 0}
        self.classes = list(DRIVABLE_HIGHWAYS) + (list(BIKE_ONLY_HIGHWAYS) if mode == "bike" else [])
        self.edges: dict[str, list[Any]] = defaultdict(list)

    def junction(self, osm_id: int) -> int:
        index = self.node_ids.get(osm_id)
        if index is None:
            index = self.node_ids[osm_id] = len(self.lat)
            lat, lng = self.coords[osm_id]
            self.lat.append(lat)
            self.lng.append(lng)
        return index

    def add_segment(self, road: _Road, refs: list[int]) -> None:
        if refs[0] == refs[-1]:
            return  # a loop back to the same junction is never on a shortest path
        points = [self.coords[ref] for ref in refs]
        if road.forward:
            self._add_edge(road, refs, points)
        if road.backward:
            self._add_edge(road, refs[::-1], points[::-1])

    def _add_edge(self, road: _Road, refs: list[int], points: list[Point]) -> None:
        e = self.edges
        e["from"].append(self.junction(refs[0]))
        e["to"].append(self.junction(refs[-1]))
        e["length_m"].append(path_length_m(points))
        e["speed_kmh"].append(road.speed_kmh)
        # Signals count on arrival: the start junction's belongs to the road before.
        e["signals"].append(sum(1 for ref in refs[1:] if ref in self.signal_nodes))
        e["rail_crossings"].append(sum(1 for ref in refs[1:] if ref in self.crossing_nodes))
        e["class"].append(self.classes.index(road.highway))
        e["name"].append(self.names.setdefault(road.name, len(self.names)))
        e["way"].append(road.way_id)
        e["oneway"].append(not (road.forward and road.backward))
        e["winding"].append(is_winding(points))
        e["bearing_start"].append(_departure_bearing(points))
        e["bearing_end"].append(_departure_bearing(points[::-1], reverse=True))
        e["geometry"].append(encode_polyline(points))
        e["stress"].append(road.stress)

    def finish(self, area: str, bbox: BBox | None, banned: dict[int, set[int]], stats: Counter[str]) -> RoadGraph:
        e = self.edges
        keep_nodes = _largest_strongly_connected(len(self.lat), e["from"], e["to"])
        node_map = {old: new for new, old in enumerate(sorted(keep_nodes))}
        keep_edges = [i for i in range(len(e["from"])) if e["from"][i] in keep_nodes and e["to"][i] in keep_nodes]
        edge_map = {old: new for new, old in enumerate(keep_edges)}
        stats["junctions_dropped"] = len(self.lat) - len(keep_nodes)

        def column(name: str) -> list[Any]:
            return [e[name][i] for i in keep_edges]

        remapped_banned: dict[int, frozenset[int]] = {}
        for edge, others in banned.items():
            if edge in edge_map:
                kept = frozenset(edge_map[o] for o in others if o in edge_map)
                if kept:
                    remapped_banned[edge_map[edge]] = kept

        graph = RoadGraph(
            area=area,
            bbox=bbox,
            lat=[self.lat[n] for n in sorted(keep_nodes)],
            lng=[self.lng[n] for n in sorted(keep_nodes)],
            edge_from=[node_map[n] for n in column("from")],
            edge_to=[node_map[n] for n in column("to")],
            length_m=column("length_m"),
            speed_kmh=column("speed_kmh"),
            signals=column("signals"),
            road_class=column("class"),
            name=column("name"),
            way_id=column("way"),
            oneway=column("oneway"),
            winding=column("winding"),
            bearing_start=column("bearing_start"),
            bearing_end=column("bearing_end"),
            geometry=column("geometry"),
            classes=self.classes,
            names=sorted(self.names, key=self.names.__getitem__),
            banned=remapped_banned,
            rail_crossings=column("rail_crossings"),
            mode=self.mode,
            stress=column("stress") if self.mode == "bike" else [],
        )
        stats["junctions"] = graph.node_count
        stats["road_segments"] = graph.edge_count
        stats["traffic_signals"] = sum(graph.signals)
        graph.stats = dict(stats)
        return graph


def _apply_restrictions(
    builder: _EdgeBuilder, relations: Iterable[Mapping[str, Any]], stats: Counter[str], mode: str = "car"
) -> dict[int, set[int]]:
    """Turn restriction relations into banned (incoming edge, outgoing edge) pairs."""
    e = builder.edges
    edges_in: dict[int, list[int]] = defaultdict(list)
    edges_out: dict[int, list[int]] = defaultdict(list)
    for edge, (start, end) in enumerate(zip(e["from"], e["to"], strict=True)):
        edges_out[start].append(edge)
        edges_in[end].append(edge)

    banned: dict[int, set[int]] = defaultdict(set)
    for relation in relations:
        kind = _restriction_kind(relation.get("tags", {}), mode)
        if kind is None or not kind.startswith(("no_", "only_")):
            stats["restrictions_ignored"] += 1
            continue
        members = relation.get("members", [])
        from_ways = {m["ref"] for m in members if m.get("role") == "from" and m.get("type") == "way"}
        to_ways = {m["ref"] for m in members if m.get("role") == "to" and m.get("type") == "way"}
        vias = [m for m in members if m.get("role") == "via"]
        if len(vias) != 1 or vias[0].get("type") != "node":
            # Restrictions through a way (mostly U-turns across a median) need
            # multi-segment state the router doesn't model.
            stats["restrictions_unsupported"] += 1
            continue
        via = builder.node_ids.get(vias[0]["ref"])
        ins = [edge for edge in edges_in.get(via, ()) if e["way"][edge] in from_ways]
        outs = [edge for edge in edges_out.get(via, ()) if e["way"][edge] in to_ways]
        if via is None or not ins or not outs:
            stats["restrictions_unmatched"] += 1
            continue
        if kind.startswith("no_"):
            for edge in ins:
                banned[edge].update(outs)
        else:
            allowed = set(outs)
            for edge in ins:
                banned[edge].update(other for other in edges_out[via] if other not in allowed)
        stats["restrictions_applied"] += 1
    return banned


def _restriction_kind(tags: Mapping[str, str], mode: str = "car") -> str | None:
    exempt = {value.strip() for value in tags.get("except", "").split(";")}
    if exempt & ({"bicycle"} if mode == "bike" else {"motorcar", "motor_vehicle"}):
        return None
    keys = ("restriction:bicycle", "restriction") if mode == "bike" else ("restriction:motorcar", "restriction:motor_vehicle", "restriction")
    for key in keys:
        if key in tags:
            return tags[key].strip()
    if "restriction:conditional" in tags:
        # Time-limited restrictions are treated as always on: that can skip a
        # legal turn, but never routes an illegal one.
        return tags["restriction:conditional"].split("@")[0].strip()
    return None


# ---- The bike map: who may ride a way, which ways, how fast, and how stressful it is.

# Ways only bikes (and people on foot) use. Footways and pedestrian streets only where bikes are
# allowed on them; never sidewalks (whether adults may ride on them varies by city: not assumed).
BIKE_ONLY_HIGHWAYS = ("cycleway", "path", "footway", "pedestrian", "track", "bridleway")
NO_BIKE_HIGHWAYS = frozenset({"motorway", "motorway_link"})  # freeways: no bikes in Wisconsin
_BIKES_WELCOME = frozenset({"yes", "designated", "permissive"})
BIKE_SPEED_KMH = 16.0  # an everyday rider's average, stops aside
BIKE_UNPAVED_KMH = 12.0
UNPAVED_SURFACES = frozenset({"unpaved", "gravel", "fine_gravel", "compacted", "dirt", "earth", "ground", "grass", "sand", "mud"})
OFF_STREET_HIGHWAYS = frozenset(BIKE_ONLY_HIGHWAYS)
QUIET_HIGHWAYS = frozenset({"residential", "living_street", "service", "unclassified"})
# Lanes on a road (both directions together) when OSM doesn't say: the worse case, so a busy
# street with unknown lanes isn't taken for a quiet one.
DEFAULT_LANES = {"trunk": 4, "primary": 4, "secondary": 4, "trunk_link": 2, "primary_link": 2, "secondary_link": 2}


def _bikeable(tags: Mapping[str, str]) -> bool:
    highway = tags.get("highway")
    if highway not in DRIVABLE_HIGHWAYS and highway not in BIKE_ONLY_HIGHWAYS or highway in NO_BIKE_HIGHWAYS:
        return False
    if tags.get("area") == "yes" or tags.get("footway") == "sidewalk":
        return False
    if highway == "service" and tags.get("service") in _EXCLUDED_SERVICE:
        return False
    bicycle = tags.get("bicycle")
    if highway in ("footway", "pedestrian", "bridleway") and bicycle not in _BIKES_WELCOME:
        return False
    if highway == "track" and bicycle not in _BIKES_WELCOME and tags.get("surface") in UNPAVED_SURFACES | {None}:
        return False
    if bicycle in ("no", "dismount", "use_sidepath"):
        return False
    if bicycle in _BIKES_WELCOME:
        return True
    # Otherwise the most specific general access tag decides.
    for key in ("vehicle", "access"):
        if key in tags:
            return tags[key] not in _NO_ACCESS
    return True


def _bike_direction(tags: Mapping[str, str]) -> tuple[bool, bool] | None:
    """Like a car's (see _direction), except where bikes may ride against a one-way street."""
    if tags.get("oneway:bicycle") == "no":
        return (True, True)
    if any(tags.get(key, "").startswith("opposite") for key in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both")):
        return (True, True)
    if tags.get("highway") in OFF_STREET_HIGHWAYS and tags.get("oneway") not in ("yes", "-1"):
        return (True, True)
    return _direction(tags)


def traffic_stress(tags: Mapping[str, str]) -> int:
    """Level of Traffic Stress (Mekuria, Furth and Nixon, 2012): 1 for anyone (trails, protected lanes,
    very quiet streets) to 4 for only the most confident riders (fast, multi-lane traffic).

    From the way's speed limit (or its class's usual one), its lanes (or the worse case for its class),
    and its bike facility: a separated track, a painted lane, or none (sharrows count as none).
    """
    highway = tags.get("highway", "")
    if highway in OFF_STREET_HIGHWAYS:
        return 1
    facilities = {tags.get(key, "") for key in ("cycleway", "cycleway:both", "cycleway:left", "cycleway:right")}
    if facilities & {"track", "opposite_track"}:
        return 1
    if highway == "living_street":
        return 1
    mph = _speed_kmh(tags) / MPH_TO_KMH
    try:
        lanes = int(tags.get("lanes", "").split(";")[0])
    except ValueError:
        lanes = DEFAULT_LANES.get(highway, 2)
    if facilities & {"lane", "buffered_lane", "opposite_lane"}:
        if mph <= 30 and lanes <= 2:
            return 2
        return 3 if mph <= 35 else 4
    if mph <= 25:
        if lanes <= 2:
            return 1 if highway in QUIET_HIGHWAYS else 2
        return 2 if lanes <= 3 else 3
    if mph <= 30:
        return 2 if lanes <= 2 else 3 if lanes <= 3 else 4
    if mph <= 35:
        return 3 if lanes <= 2 else 4
    return 4


def _drivable(tags: Mapping[str, str]) -> bool:
    if tags.get("highway") not in DRIVABLE_HIGHWAYS or tags.get("area") == "yes":
        return False
    if tags.get("highway") == "service" and tags.get("service") in _EXCLUDED_SERVICE:
        return False
    # The most specific access tag wins.
    for key in ("motorcar", "motor_vehicle", "vehicle", "access"):
        if key in tags:
            return tags[key] not in _NO_ACCESS
    return True


def _direction(tags: Mapping[str, str]) -> tuple[bool, bool] | None:
    """(forward allowed, backward allowed) along the way's node order; None if unusable."""
    oneway = tags.get("oneway", "").lower()
    if oneway in ("yes", "true", "1"):
        return (True, False)
    if oneway in ("-1", "reverse"):
        return (False, True)
    if oneway in ("reversible", "alternating"):
        return None  # direction changes by time of day
    if oneway == "no":
        return (True, True)
    if tags.get("highway") in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return (True, False)
    return (True, True)


def _speed_kmh(tags: Mapping[str, str]) -> float:
    match = _MAXSPEED.match(tags.get("maxspeed", "").split(";")[0])
    if match:
        value = float(match.group(1))
        return value * MPH_TO_KMH if match.group(2) == "mph" else value
    return DEFAULT_SPEED_MPH.get(tags.get("highway", ""), 25) * MPH_TO_KMH


def _departure_bearing(points: list[Point], *, reverse: bool = False, min_m: float = 15.0) -> float:
    """Heading over the first min_m of points, flipped when measuring an arrival from the far end."""
    origin = points[0]
    target = points[-1]
    for point in points[1:]:
        if haversine_m(origin, point) >= min_m:
            target = point
            break
    bearing = initial_bearing_deg(origin, target)
    return (bearing + 180.0) % 360.0 if reverse else bearing


def _largest_strongly_connected(count: int, sources: list[int], targets: list[int]) -> set[int]:
    """Nodes of the largest strongly connected component (iterative Kosaraju).

    Dropping everything else removes islands such as gated estates or parking
    lots that a trip could enter but never leave.
    """
    forward: list[list[int]] = [[] for _ in range(count)]
    backward: list[list[int]] = [[] for _ in range(count)]
    for a, b in zip(sources, targets, strict=True):
        forward[a].append(b)
        backward[b].append(a)

    order: list[int] = []
    seen = [False] * count
    for root in range(count):
        if seen[root]:
            continue
        seen[root] = True
        stack = [(root, iter(forward[root]))]
        while stack:
            node, children = stack[-1]
            for child in children:
                if not seen[child]:
                    seen[child] = True
                    stack.append((child, iter(forward[child])))
                    break
            else:
                stack.pop()
                order.append(node)

    component = [-1] * count
    sizes: Counter[int] = Counter()
    label = 0
    for root in reversed(order):
        if component[root] != -1:
            continue
        component[root] = label
        stack = [root]
        while stack:
            node = stack.pop()
            sizes[label] += 1
            for parent in backward[node]:
                if component[parent] == -1:
                    component[parent] = label
                    stack.append(parent)
        label += 1

    if not sizes:
        return set()
    largest = sizes.most_common(1)[0][0]
    return {node for node in range(count) if component[node] == largest}


def _flat_distance_m(point: Point, a: Point, b: Point) -> float:
    """Distance from point to the segment a-b on a flat projection around point: no bearing, little
    trigonometry. For ruling out far segments quickly; distance_to_path_m measures the near ones."""
    m_per_deg_lng = _M_PER_DEG_LAT * math.cos(math.radians(point[0]))
    ax, ay = (a[1] - point[1]) * m_per_deg_lng, (a[0] - point[0]) * _M_PER_DEG_LAT
    dx, dy = (b[1] - point[1]) * m_per_deg_lng - ax, (b[0] - point[0]) * _M_PER_DEG_LAT - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    return math.hypot(ax + t * dx, ay + t * dy)


def _cell(point: Point, size_deg: float) -> tuple[int, int]:
    return math.floor(point[0] / size_deg), math.floor(point[1] / size_deg)
