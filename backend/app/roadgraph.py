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
import json
import logging
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.features import is_winding
from app.geo import Point, decode_polyline, encode_polyline, haversine_m, initial_bearing_deg, path_length_m
from app.overpass import ATTRIBUTION, DRIVABLE_HIGHWAYS, BBox

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

    time_s: list[float] = field(init=False, repr=False)
    out_edges: list[list[int]] = field(init=False, repr=False)
    max_speed_kmh: float = field(init=False)
    _snap_cells: dict[tuple[int, int], list[int]] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.time_s = [
            length / (speed / 3.6) + SIGNAL_DELAY_S * signals
            for length, speed, signals in zip(self.length_m, self.speed_kmh, self.signals, strict=True)
        ]
        self.out_edges = [[] for _ in self.lat]
        for edge, node in enumerate(self.edge_from):
            self.out_edges[node].append(edge)
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
        cells = self._snap_index()
        cell_lat, cell_lng = _snap_cell(point)
        reach_lat = math.ceil(max_distance_m / (_M_PER_DEG_LAT * _SNAP_CELL_DEG))
        lng_cell_m = _M_PER_DEG_LAT * _SNAP_CELL_DEG * max(math.cos(math.radians(point[0])), 0.01)
        reach_lng = math.ceil(max_distance_m / lng_cell_m)

        best, best_m = None, max_distance_m
        for i in range(cell_lat - reach_lat, cell_lat + reach_lat + 1):
            for j in range(cell_lng - reach_lng, cell_lng + reach_lng + 1):
                for node in cells.get((i, j), ()):
                    distance = haversine_m(point, self.point(node))
                    if distance <= best_m:
                        best, best_m = node, distance
        return best

    def _snap_index(self) -> dict[tuple[int, int], list[int]]:
        if self._snap_cells is None:
            snappable = set()
            for edge in range(self.edge_count):
                if self.class_name(edge) not in NO_SNAP_CLASSES:
                    snappable.add(self.edge_from[edge])
                    snappable.add(self.edge_to[edge])
            cells: dict[tuple[int, int], list[int]] = defaultdict(list)
            for node in snappable:
                cells[_snap_cell(self.point(node))].append(node)
            self._snap_cells = dict(cells)
        return self._snap_cells

    def save(self, path: Path) -> None:
        data = {
            "format": GRAPH_FORMAT,
            "area": self.area,
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
            },
            "banned": [[edge, other] for edge, others in sorted(self.banned.items()) for other in sorted(others)],
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
        )


def build_graph(elements: Iterable[Mapping[str, Any]], *, area: str = "", bbox: BBox | None = None) -> RoadGraph:
    """Build a RoadGraph from Overpass JSON elements (nodes, ways and restriction relations)."""
    coords: dict[int, Point] = {}
    signal_nodes: set[int] = set()
    ways: dict[int, Mapping[str, Any]] = {}
    relations: dict[int, Mapping[str, Any]] = {}
    for element in elements:
        kind = element.get("type")
        if kind == "node":
            if "lat" in element:
                coords[element["id"]] = (element["lat"], element["lon"])
            if element.get("tags", {}).get("highway") == "traffic_signals":
                signal_nodes.add(element["id"])
        elif kind == "way":
            ways[element["id"]] = element
        elif kind == "relation":
            relations[element["id"]] = element

    stats: Counter[str] = Counter()
    roads = []
    for way in ways.values():
        road = _Road.from_way(way, coords)
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

    builder = _EdgeBuilder(coords, signal_nodes)
    for road in roads:
        start = 0
        for i in range(1, len(road.refs)):
            if uses[road.refs[i]] >= 2:
                builder.add_segment(road, road.refs[start : i + 1])
                start = i

    banned = _apply_restrictions(builder, relations.values(), stats)
    graph = builder.finish(area, bbox, banned, stats)
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


class _EdgeBuilder:
    def __init__(self, coords: Mapping[int, Point], signal_nodes: set[int]) -> None:
        self.coords = coords
        self.signal_nodes = signal_nodes
        self.node_ids: dict[int, int] = {}  # OSM node id -> junction index
        self.lat: list[float] = []
        self.lng: list[float] = []
        self.names: dict[str, int] = {"": 0}
        self.classes = list(DRIVABLE_HIGHWAYS)
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
        e["class"].append(self.classes.index(road.highway))
        e["name"].append(self.names.setdefault(road.name, len(self.names)))
        e["way"].append(road.way_id)
        e["oneway"].append(not (road.forward and road.backward))
        e["winding"].append(is_winding(points))
        e["bearing_start"].append(_departure_bearing(points))
        e["bearing_end"].append(_departure_bearing(points[::-1], reverse=True))
        e["geometry"].append(encode_polyline(points))

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
        )
        stats["junctions"] = graph.node_count
        stats["road_segments"] = graph.edge_count
        stats["traffic_signals"] = sum(graph.signals)
        graph.stats = dict(stats)
        return graph


def _apply_restrictions(
    builder: _EdgeBuilder, relations: Iterable[Mapping[str, Any]], stats: Counter[str]
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
        kind = _restriction_kind(relation.get("tags", {}))
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


def _restriction_kind(tags: Mapping[str, str]) -> str | None:
    exempt = {value.strip() for value in tags.get("except", "").split(";")}
    if exempt & {"motorcar", "motor_vehicle"}:
        return None
    for key in ("restriction:motorcar", "restriction:motor_vehicle", "restriction"):
        if key in tags:
            return tags[key].strip()
    if "restriction:conditional" in tags:
        # Time-limited restrictions are treated as always on: that can skip a
        # legal turn, but never routes an illegal one.
        return tags["restriction:conditional"].split("@")[0].strip()
    return None


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


def _snap_cell(point: Point) -> tuple[int, int]:
    return math.floor(point[0] / _SNAP_CELL_DEG), math.floor(point[1] / _SNAP_CELL_DEG)
