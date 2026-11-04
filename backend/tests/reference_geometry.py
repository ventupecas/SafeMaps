"""The geometry as it was before it was compiled: plain Python, slow, and easy to read. What app.geo_kernel is held to.

Copied from the code the compiled functions replaced (commit 7fedb6d), with dictionaries of lists for its indexes.
ReferenceMap answers from a map's columns the way RoadGraph used to. Nothing here calls the compiled code.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from itertools import pairwise

from app.features import HOTSPOT_RADIUS_M
from app.hotspots import CrashHotspots
from app.road_events import (
    EVENT_MATCH_DEG,
    EVENT_MATCH_M,
    EVENT_ON_ROUTE_M,
    ON_ROUTE_M,
    OFFICIAL_SOURCES,
    PARALLEL_DEG,
    RAMP_KINDS,
    REPEAT_SHARE,
    RoadEvent,
)
from app.roadgraph import (
    ALONG_MATCH_M,
    DIVIDED_ROAD_M,
    DIVIDED_ROAD_MIN_DEG,
    JUNCTION_MATCH_M,
    NO_SNAP_CLASSES,
    RAIL_MATCH_M,
    SAME_ROAD_M,
    SCHOOL_ZONE_M,
    SIGNAL_MATCH_M,
    VEHICLE_MATCH_DEG,
    VEHICLE_MATCH_M,
    RoadGraph,
    classify_junction,
)
from app.sun import glare

EARTH_RADIUS_M = 6_371_008.8
_M_PER_DEG_LAT = 111_320.0
_SIGNAL_CELL_DEG = 0.0005
_FINE_CELL_DEG = 0.0005
_FINE_LOOKUP_M = 100.0
_SNAP_CELL_DEG = 0.005
_EDGE_CELL_DEG = 0.002
_EDGE_SAMPLE_M = 100.0
_VEHICLE_SEARCH_M = (300.0, 1_000.0, 2_500.0)
_VEHICLE_SURE_M = 15.0

Point = tuple[float, float]


# ---- Polylines and great circles


def decode_polyline(encoded: str, precision: int = 5) -> list[Point]:
    factor = 10**precision
    points: list[Point] = []
    index = lat = lng = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= len(encoded):
                    raise ValueError("Truncated polyline string")
                chunk = ord(encoded[index]) - 63
                index += 1
                result |= (chunk & 0x1F) << shift
                shift += 5
                if chunk < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        points.append((lat / factor, lng / factor))
    return points


def encode_polyline(points: Iterable[Point], precision: int = 5) -> str:
    factor = 10**precision
    chunks: list[str] = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat, ilng = round(lat * factor), round(lng * factor)
        for delta in (ilat - prev_lat, ilng - prev_lng):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                chunks.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            chunks.append(chr(value + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(chunks)


def haversine_m(a: Point, b: Point) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing_deg(a: Point, b: Point) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlng = lng2 - lng1
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angle_between_deg(a: float, b: float) -> float:
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def destination_point(origin: Point, bearing_deg: float, distance_m: float) -> Point:
    lat1, lng1 = map(math.radians, origin)
    bearing = math.radians(bearing_deg)
    angular = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(bearing))
    lng2 = lng1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lng2) + 540) % 360 - 180


def distance_to_path_m(point: Point, path: Sequence[Point]) -> tuple[float, float]:
    if len(path) < 2:
        raise ValueError("A path needs at least two points")
    m_per_deg = math.radians(1) * EARTH_RADIUS_M
    m_per_deg_lng = m_per_deg * math.cos(math.radians(point[0]))

    def xy(p: Point) -> tuple[float, float]:
        return (p[1] - point[1]) * m_per_deg_lng, (p[0] - point[0]) * m_per_deg

    best_m, best_bearing = math.inf, 0.0
    for a, b in pairwise(path):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
        distance = math.hypot(ax + t * dx, ay + t * dy)
        if distance < best_m and length_sq > 0:
            best_m, best_bearing = distance, initial_bearing_deg(a, b)
    return best_m, best_bearing


def path_length_m(points: Sequence[Point]) -> float:
    return sum(haversine_m(p, q) for p, q in pairwise(points))


def densify(points: Sequence[Point], spacing_m: float) -> list[Point]:
    if not points:
        return []
    dense = [points[0]]
    for a, b in pairwise(points):
        segment = haversine_m(a, b)
        steps = max(1, math.ceil(segment / spacing_m))
        bearing = initial_bearing_deg(a, b)
        dense.extend(destination_point(a, bearing, segment * k / steps) for k in range(1, steps))
        dense.append(b)
    return dense


def total_heading_change_deg(points: Sequence[Point], min_segment_m: float = 30.0) -> float:
    if not points:
        return 0.0
    kept = [points[0]]
    for point in points[1:]:
        if haversine_m(kept[-1], point) >= min_segment_m:
            kept.append(point)
    headings = [initial_bearing_deg(p, q) for p, q in pairwise(kept)]
    total = 0.0
    for h1, h2 in pairwise(headings):
        diff = abs(h2 - h1) % 360
        total += min(diff, 360 - diff)
    return total


def share_into_sun(points: Sequence[Point], sun: tuple[float, float] | None) -> float:
    total = into = 0.0
    for a, b in zip(points, points[1:]):
        length = haversine_m(a, b)
        total += length
        if length > 1 and glare(initial_bearing_deg(a, b), sun):
            into += length
    return into / total if total else 0.0


# ---- Which events a route drives through


def carries(travel: float, line: float | None, direction: float | None, along: bool) -> bool:
    if along and line is not None:
        return angle_between_deg(travel, line) <= EVENT_MATCH_DEG
    if line is not None and min(angle_between_deg(travel, line), 180 - angle_between_deg(travel, line)) > PARALLEL_DEG:
        return False
    return direction is None or angle_between_deg(travel, direction) <= EVENT_MATCH_DEG


class RouteIndex:
    def __init__(self, points: Sequence[Point]) -> None:
        dense = densify(points, EVENT_MATCH_M / 3)
        self.cell_deg = EVENT_MATCH_M / 111_320.0
        self.reach_lng = math.ceil(1 / max(math.cos(math.radians(dense[0][0])), 0.01))
        self.cells: dict[tuple[int, int], list[tuple[Point, float]]] = defaultdict(list)
        for i, point in enumerate(dense):
            a, b = (point, dense[i + 1]) if i + 1 < len(dense) else (dense[i - 1], point)
            self.cells[self._cell(point)].append((point, initial_bearing_deg(a, b)))

    def bearings_near(self, point: Point, within_m: float) -> list[float]:
        ci, cj = self._cell(point)
        return [
            bearing
            for i in range(ci - 1, ci + 2)
            for j in range(cj - self.reach_lng, cj + self.reach_lng + 1)
            for other, bearing in self.cells.get((i, j), ())
            if haversine_m(point, other) <= within_m
        ]

    def runs_along(self, event: RoadEvent) -> bool:
        if len(event.path) == 1:
            near = self.bearings_near(event.path[0], EVENT_MATCH_M)
            return any(carries(bearing, None, event.direction, event.along) for bearing in near)
        if event.road in RAMP_KINDS and not (
            self.bearings_near(event.path[0], ON_ROUTE_M) and self.bearings_near(event.path[-1], ON_ROUTE_M)
        ):
            return False
        spacing = EVENT_MATCH_M / 2
        samples = densify(event.path, spacing)
        longest = run = misses = 0
        for i, point in enumerate(samples):
            a, b = (point, samples[i + 1]) if i + 1 < len(samples) else (samples[i - 1], point)
            line = initial_bearing_deg(a, b)
            near = self.bearings_near(point, ON_ROUTE_M)
            if any(carries(bearing, line, event.direction, event.along) for bearing in near):
                run += 1 + misses
                misses = 0
            elif run and not misses:
                misses = 1
            else:
                run = misses = 0
            longest = max(longest, run)
        return longest * spacing >= min(EVENT_ON_ROUTE_M, path_length_m(event.path) / 2)

    def share_along(self, event: RoadEvent) -> float:
        samples = densify(event.path, EVENT_MATCH_M / 2)
        matched = 0
        for i, point in enumerate(samples):
            a, b = (point, samples[i + 1]) if i + 1 < len(samples) else (samples[i - 1], point)
            line = initial_bearing_deg(a, b)
            if any(carries(bearing, line, event.direction, event.along) for bearing in self.bearings_near(point, ON_ROUTE_M)):
                matched += 1
        return matched / len(samples)

    def _cell(self, point: Point) -> tuple[int, int]:
        return math.floor(point[0] / self.cell_deg), math.floor(point[1] / self.cell_deg)


def near_events(points: Sequence[Point], events: Iterable[RoadEvent]) -> list[RoadEvent]:
    margin = EVENT_MATCH_M / 111_320.0 * 2
    south, north = min(p[0] for p in points) - margin, max(p[0] for p in points) + margin
    west, east = min(p[1] for p in points) - margin * 1.5, max(p[1] for p in points) + margin * 1.5
    return [
        event
        for event in events
        if min(p[0] for p in event.path) <= north
        and max(p[0] for p in event.path) >= south
        and min(p[1] for p in event.path) <= east
        and max(p[1] for p in event.path) >= west
    ]


def count_on_route(points: Sequence[Point], events: Iterable[RoadEvent]) -> tuple[int, int, int]:
    if len(points) < 2:
        return 0, 0, 0
    relevant = near_events(points, [event for event in events if event.kind in ("closed", "lanes")])
    if not relevant:
        return 0, 0, 0
    route = RouteIndex(points)
    closures = lane_closures = with_workers = 0
    for event in relevant:
        if route.runs_along(event):
            if event.kind == "closed":
                closures += 1
            else:
                lane_closures += 1
                with_workers += event.workers
    return closures, lane_closures, with_workers


def weather_on_route(points: Sequence[Point], events: Iterable[RoadEvent]) -> int:
    if len(points) < 2:
        return 0
    relevant = near_events(points, [event for event in events if event.kind == "hazard"])
    if not relevant:
        return 0
    route = RouteIndex(points)
    return sum(route.runs_along(event) for event in relevant)


def delay_on_route(points: Sequence[Point], events: Iterable[RoadEvent]) -> float:
    if len(points) < 2:
        return 0.0
    traffic = near_events(points, [event for event in events if event.delay_min > 0 and len(event.path) > 1])
    if not traffic:
        return 0.0
    route = RouteIndex(points)
    return sum(event.delay_min * route.share_along(event) for event in traffic)


def without_repeats(events: Sequence[RoadEvent]) -> list[RoadEvent]:
    official = [
        event for event in events if event.source in OFFICIAL_SOURCES and event.kind in ("closed", "lanes") and len(event.path) > 1
    ]
    if not official:
        return list(events)
    indexes: dict[int, RouteIndex] = {}

    def index(event: RoadEvent) -> RouteIndex:
        if id(event) not in indexes:
            indexes[id(event)] = RouteIndex(event.path)
        return indexes[id(event)]

    def repeated(event: RoadEvent) -> bool:
        lines = [index(other) for other in near_events(event.path, official) if other.kind in ("closed", event.kind)]
        if not lines:
            return False
        samples = densify(event.path, EVENT_MATCH_M / 2)
        covered = 0
        for i, point in enumerate(samples):
            a, b = (point, samples[i + 1]) if i + 1 < len(samples) else (samples[i - 1], point)
            line = initial_bearing_deg(a, b)
            covered += any(
                carries(bearing, line, event.direction, event.along)
                for other in lines
                for bearing in other.bearings_near(point, ON_ROUTE_M)
            )
        return covered >= REPEAT_SHARE * len(samples)

    return [
        event
        for event in events
        if not (event.source == "tomtom" and event.kind in ("closed", "lanes") and len(event.path) > 1 and repeated(event))
    ]


# ---- How well a route follows a design


class NearIndex:
    def __init__(self, points: Sequence[Point], tolerance_m: float) -> None:
        self.tolerance_m = tolerance_m
        self.cell_deg = tolerance_m / _M_PER_DEG_LAT
        self.cells: dict[tuple[int, int], list[Point]] = defaultdict(list)
        for point in points:
            self.cells[self._cell(point)].append(point)
        latitude = points[0][0] if points else 0.0
        self.reach_lng = math.ceil(1 / max(math.cos(math.radians(latitude)), 0.01))

    def near(self, point: Point) -> bool:
        ci, cj = self._cell(point)
        return any(
            haversine_m(point, other) <= self.tolerance_m
            for i in range(ci - 1, ci + 2)
            for j in range(cj - self.reach_lng, cj + self.reach_lng + 1)
            for other in self.cells.get((i, j), ())
        )

    def _cell(self, point: Point) -> tuple[int, int]:
        return math.floor(point[0] / self.cell_deg), math.floor(point[1] / self.cell_deg)


def design_match(designed: Sequence[Point], actual: Sequence[Point], tolerance_m: float) -> float:
    if len(designed) < 2 or len(actual) < 2:
        return 0.0
    index = NearIndex(densify(designed, tolerance_m / 2), tolerance_m)
    samples = densify(actual, 25.0)
    return sum(1 for point in samples if index.near(point)) / len(samples)


# ---- A map's lookups, from dictionaries of lists


def _cell(point: Point, size_deg: float) -> tuple[int, int]:
    return math.floor(point[0] / size_deg), math.floor(point[1] / size_deg)


def _flat_distance_m(point: Point, a: Point, b: Point) -> float:
    m_per_deg_lng = _M_PER_DEG_LAT * math.cos(math.radians(point[0]))
    ax, ay = (a[1] - point[1]) * m_per_deg_lng, (a[0] - point[0]) * _M_PER_DEG_LAT
    dx, dy = (b[1] - point[1]) * m_per_deg_lng - ax, (b[0] - point[0]) * _M_PER_DEG_LAT - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    return math.hypot(ax + t * dx, ay + t * dy)


class ReferenceMap:
    """The lookups RoadGraph used to do, on a graph's plain columns."""

    def __init__(self, graph: RoadGraph) -> None:
        self.lat = list(graph.lat)
        self.lng = list(graph.lng)
        self.edge_from = list(graph.edge_from)
        self.edge_to = list(graph.edge_to)
        self.length_m = list(graph.length_m)
        self.signals = list(graph.signals)
        self.road_class = list(graph.road_class)
        self.name = list(graph.name)
        self.oneway = list(graph.oneway)
        self.bearing_start = list(graph.bearing_start)
        self.bearing_end = list(graph.bearing_end)
        self.geometry = list(graph.geometry)
        self.classes = graph.classes
        self.names = graph.names
        self.rail_crossing_points = graph.rail_crossing_points
        self.school_points = graph.school_points
        self.out_edges: list[list[int]] = [[] for _ in self.lat]
        self.in_edges: list[list[int]] = [[] for _ in self.lat]
        for edge, (start, end) in enumerate(zip(self.edge_from, self.edge_to, strict=True)):
            self.out_edges[start].append(edge)
            self.in_edges[end].append(edge)
        self._cell_indexes: dict[tuple[bool, float], dict[tuple[int, int], list[int]]] = {}
        self._signal_cells: dict[tuple[int, int], list[int]] | None = None
        self._edge_cells: dict[tuple[int, int], list[int]] | None = None
        self._point_cells: dict[str, dict[tuple[int, int], list[Point]]] = {}

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

    def node_kind(self, node: int) -> str:
        edges = self.out_edges[node] + self.in_edges[node]
        kind = classify_junction(self.class_name(edge) for edge in edges)
        if kind != "interchange" and edges and all(self.oneway[edge] for edge in edges) and not self._on_divided_road(node):
            kind = "one-way"
        return kind

    def _on_divided_road(self, node: int) -> bool:
        here = self._named_directions(node)
        if not here:
            return False
        point = self.point(node)
        for other in self.nodes_near(point, DIVIDED_ROAD_M, snappable_only=False):
            if other == node or haversine_m(point, self.point(other)) > DIVIDED_ROAD_M:
                continue
            for name, bearing in self._named_directions(other):
                if any(name == mine and angle_between_deg(bearing, my_bearing) >= DIVIDED_ROAD_MIN_DEG for mine, my_bearing in here):
                    return True
        return False

    def _named_directions(self, node: int) -> list[tuple[int, float]]:
        leaving = [(self.name[edge], self.bearing_start[edge]) for edge in self.out_edges[node] if self.name[edge]]
        reaching = [(self.name[edge], self.bearing_end[edge]) for edge in self.in_edges[node] if self.name[edge]]
        return leaving + reaching

    def junction_kind(self, point: Point) -> str | None:
        node = self.nearest(point, JUNCTION_MATCH_M, snappable_only=False)
        return None if node is None else self.node_kind(node)

    def edges_in_box(self, south: float, west: float, north: float, east: float) -> list[int]:
        cells = self._edge_index()
        (i0, j0), (i1, j1) = _cell((south, west), _EDGE_CELL_DEG), _cell((north, east), _EDGE_CELL_DEG)
        found: set[int] = set()
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                found.update(cells.get((i, j), ()))
        return sorted(found)

    def signal_at(self, point: Point) -> bool:
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
        cells = self._signal_index()
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

    def road_names_at(self, point: Point, heading: float) -> set[str]:
        """Every name road_name_at could answer: which of several equally near roads comes first was never promised."""
        return {self.names[self.name[edge]] for edge in self.roads_at(point, heading) if self.name[edge]}

    def rail_crossings_at(self, stretches: Iterable[Sequence[Point]]) -> list[Point]:
        return self._points_along("rail", self.rail_crossing_points, stretches, RAIL_MATCH_M)

    def schools_at(self, stretches: Iterable[Sequence[Point]]) -> list[Point]:
        return self._points_along("school", self.school_points, stretches, SCHOOL_ZONE_M)

    def _points_along(self, kind: str, points: Sequence[Point], stretches: Iterable[Sequence[Point]], within_m: float) -> list[Point]:
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

    def segments_under(self, point: Point, heading: float, road: str = "any") -> list[int]:
        offsets: dict[int, float] = {}
        checked: set[int] = set()
        for radius in _VEHICLE_SEARCH_M:
            for node in self.nodes_near(point, radius, snappable_only=False):
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
                break
        if not offsets:
            return []
        nearest = min(offsets.values())
        return sorted((edge for edge, offset in offsets.items() if offset <= nearest + SAME_ROAD_M), key=offsets.get)

    def _is_road(self, edge: int, road: str) -> bool:
        if road == "any":
            return True
        return self.class_name(edge).endswith("_link") == (road in RAMP_KINDS)

    def edges_along(self, points: Sequence[Point], within_m: float = ALONG_MATCH_M) -> set[int]:
        on_route: set[int] = set()
        for point in densify(points, within_m):
            for node in self.nodes_near(point, within_m, snappable_only=False):
                if node not in on_route and haversine_m(point, self.point(node)) <= within_m:
                    on_route.add(node)
        return {edge for node in on_route for edge in self.out_edges[node] if self.edge_to[edge] in on_route}

    def roads_at(self, point: Point, direction: float | None, road: str = "any") -> set[int]:
        cells = self._edge_index()
        ci, cj = _cell(point, _EDGE_CELL_DEG)
        offsets: dict[int, float] = {}
        for edge in {e for i in (ci - 1, ci, ci + 1) for j in (cj - 1, cj, cj + 1) for e in cells.get((i, j), ())}:
            if not self._is_road(edge, road):
                continue
            chord = (self.point(self.edge_from[edge]), self.point(self.edge_to[edge]))
            if _flat_distance_m(point, *chord) > EVENT_MATCH_M + min(self.length_m[edge] / 2, _EDGE_SAMPLE_M):
                continue
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
                filed = {_cell((lat1 + (lat2 - lat1) * k / steps, lng1 + (lng2 - lng1) * k / steps), _EDGE_CELL_DEG) for k in range(steps + 1)}
                for cell in filed:
                    cells[cell].append(edge)
            self._edge_cells = dict(cells)
        return self._edge_cells

    def nearest(self, point: Point, max_distance_m: float, *, snappable_only: bool) -> int | None:
        best, best_m = None, max_distance_m
        for node in self.nodes_near(point, max_distance_m, snappable_only=snappable_only):
            distance = haversine_m(point, self.point(node))
            if distance <= best_m:
                best, best_m = node, distance
        return best

    def nodes_near(self, point: Point, distance_m: float, *, snappable_only: bool) -> Iterator[int]:
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

    def crashes_at(self, node: int, hotspots: CrashHotspots) -> float:
        return hotspots.crashes_near(self.point(node), HOTSPOT_RADIUS_M)
