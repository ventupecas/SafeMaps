"""Stop signs and traffic lights along a route, for the page's third-person driving view.

They come from an OpenStreetMap extract, into a file of their own beside the road map, so nothing here
touches the search or the safety score:

    python -m app.road_controls data/osm/pbf/milwaukee-metro.osm.pbf data/graphs/milwaukee-metro-controls.json.gz

Only what's mapped can show: OpenStreetMap has most traffic lights, but far from every stop sign.

A sign sits on the road it faces, a little before the junction, so a route on the cross street passes
close by it without having to stop. Each one keeps the directions of travel it applies to: the mapper's
when tagged, or else toward the nearer junction along its road. A stop sign on the junction itself
with no direction given could be for any of the roads there, and is left out rather than telling
drivers on the through road to stop. A light on a junction is for everyone arriving at it.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import ceil
from pathlib import Path
from typing import Any

from app.geo import Point, angle_between_deg, distance_to_path_m, haversine_m, initial_bearing_deg
from app.overpass import DRIVABLE_HIGHWAYS

CELL_DEG = 0.001  # about 100 m
SAMPLE_M = 50.0  # a route's long segments are looked along at this spacing, under a cell
MATCH_M = 4.0  # a road's own sign is on its line, give or take the polyline's rounding (about 1 m)
FACING_DEG = 50.0  # how far off a sign's direction of travel a route may head and still face it
MERGE_M = 25.0  # the same kind this close along a route is one junction's

KINDS = {"stop": "stop", "traffic_signals": "signal"}
TOWARD = {"forward": True, "backward": False}


@dataclass(frozen=True)
class Control:
    kind: str  # stop or signal
    lat: float
    lng: float
    bearings: tuple[float, ...] = ()  # the directions of travel it applies to; none: all of them


@dataclass(frozen=True)
class TaggedNode:
    kind: str
    at: Point
    direction: str | None = None  # forward, backward or both, along the way
    all_way: bool = False


@dataclass(frozen=True)
class Way:
    refs: tuple[int, ...]
    points: tuple[Point, ...]
    oneway: bool | None = None  # True along the way, False against it, None both ways


class RoadControls:
    def __init__(self, controls: Iterable[Control]) -> None:
        self.controls = list(controls)
        self.cells: dict[tuple[int, int], list[Control]] = {}
        for control in self.controls:
            self.cells.setdefault(_cell(control.lat, control.lng), []).append(control)

    @classmethod
    def load(cls, path: Path) -> RoadControls:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            saved = json.load(handle)
        return cls(Control(kind, lat, lng, tuple(bearings)) for kind, rows in saved.items() for lat, lng, bearings in rows)

    def save(self, path: Path) -> None:
        saved: dict[str, list[list[Any]]] = {"stop": [], "signal": []}
        for control in self.controls:
            saved[control.kind].append([round(control.lat, 6), round(control.lng, 6), [round(b, 1) for b in control.bearings]])
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(saved, handle, separators=(",", ":"))

    def counts(self) -> dict[str, int]:
        counted = Counter(control.kind for control in self.controls)
        return {"stop": counted["stop"], "signal": counted["signal"]}

    def along(self, points: Sequence[Point]) -> list[dict[str, Any]]:
        """The stop signs and lights a route meets, in order: kind, lat, lng and along_m, the distance to it."""
        found: dict[Control, float] = {}
        walked = 0.0
        for a, b in pairwise(points):
            length = haversine_m(a, b)
            if length == 0:
                continue
            heading = initial_bearing_deg(a, b)
            for control in self._near(a, b, length):
                if control in found or not _faces(control, heading):
                    continue
                spot = (control.lat, control.lng)
                if distance_to_path_m(spot, (a, b))[0] <= MATCH_M:
                    found[control] = walked + min(length, haversine_m(a, spot))
            walked += length
        met: list[dict[str, Any]] = []
        for control, along_m in sorted(found.items(), key=lambda item: item[1]):
            if any(earlier["kind"] == control.kind and along_m - earlier["along_m"] < MERGE_M for earlier in met[-3:]):
                continue
            met.append({"kind": control.kind, "lat": control.lat, "lng": control.lng, "along_m": round(along_m, 1)})
        return met

    def _near(self, a: Point, b: Point, length: float) -> list[Control]:
        cells: set[tuple[int, int]] = set()
        steps = ceil(length / SAMPLE_M)
        for step in range(steps + 1):
            share = step / steps
            row, col = _cell(a[0] + (b[0] - a[0]) * share, a[1] + (b[1] - a[1]) * share)
            cells.update((row + r, col + c) for r in (-1, 0, 1) for c in (-1, 0, 1))
        return [control for cell in cells for control in self.cells.get(cell, ())]


def _cell(lat: float, lng: float) -> tuple[int, int]:
    return (int(lat // CELL_DEG), int(lng // CELL_DEG))


def _faces(control: Control, heading: float) -> bool:
    return not control.bearings or any(angle_between_deg(heading, bearing) <= FACING_DEG for bearing in control.bearings)


def controls_from(nodes: Mapping[int, TaggedNode], ways: Iterable[Way], uses: Mapping[int, int]) -> list[Control]:
    """The stop signs and lights on the given ways. uses: how many drivable ways each node is on."""
    bearings: dict[int, list[float]] = {}
    for way in ways:
        for index, ref in enumerate(way.refs):
            if ref in nodes:
                bearing = _direction_of_travel(nodes[ref], way, index, uses)
                bearings.setdefault(ref, [])
                if bearing is not None:
                    bearings[ref].append(bearing)
    controls = []
    for ref, facing in bearings.items():
        node = nodes[ref]
        if node.kind == "stop" and not facing and node.direction != "both" and not node.all_way:
            continue  # on a junction, for a road we can't tell
        controls.append(Control(node.kind, node.at[0], node.at[1], tuple(facing)))
    return controls


def _direction_of_travel(node: TaggedNode, way: Way, index: int, uses: Mapping[int, int]) -> float | None:
    if node.direction == "both":
        return None
    along = TOWARD.get(node.direction or "")
    if along is None:
        along = way.oneway
    if along is None:
        if uses.get(way.refs[index], 0) >= 2:
            return None  # on the junction itself
        ahead = _to_junction(way, index, 1, uses)
        behind = _to_junction(way, index, -1, uses)
        if ahead is None and behind is None:
            return None
        along = behind is None or (ahead is not None and ahead <= behind)
    return _heading(way.points, index, along)


def _to_junction(way: Way, index: int, step: int, uses: Mapping[int, int]) -> float | None:
    distance = 0.0
    at = index
    while 0 <= at + step < len(way.refs):
        distance += haversine_m(way.points[at], way.points[at + step])
        at += step
        if uses.get(way.refs[at], 0) >= 2:
            return distance
    return None


def _heading(points: Sequence[Point], index: int, along: bool) -> float:
    first, second = (index, index + 1) if index + 1 < len(points) else (index - 1, index)
    return initial_bearing_deg(points[first], points[second]) if along else initial_bearing_deg(points[second], points[first])


def read_extract(pbf: Path) -> list[Control]:
    """Stop signs and lights from an .osm.pbf extract, which holds its nodes before its ways."""
    import osmium  # only for extracts: pip install osmium

    nodes: dict[int, TaggedNode] = {}
    uses: Counter[int] = Counter()
    ways: list[Way] = []
    for obj in osmium.FileProcessor(str(pbf)).with_locations():
        if obj.is_node():
            kind = KINDS.get(obj.tags.get("highway", ""))
            if kind and obj.location.valid():
                direction = obj.tags.get("traffic_signals:direction") or obj.tags.get("direction") if kind == "signal" else obj.tags.get("direction")
                nodes[obj.id] = TaggedNode(kind, (obj.location.lat, obj.location.lon), direction, obj.tags.get("stop") == "all")
        elif obj.is_way():
            if obj.tags.get("highway") not in DRIVABLE_HIGHWAYS or obj.tags.get("area") == "yes":
                continue
            refs = tuple(node.ref for node in obj.nodes)
            uses.update(set(refs))
            if any(ref in nodes for ref in refs) and all(node.location.valid() for node in obj.nodes):
                oneway = {"yes": True, "1": True, "true": True, "-1": False}.get(obj.tags.get("oneway", ""))
                if oneway is None and (obj.tags.get("junction") == "roundabout" or obj.tags.get("highway") in ("motorway", "motorway_link")):
                    oneway = True
                ways.append(Way(refs, tuple((node.location.lat, node.location.lon) for node in obj.nodes), oneway))
    return controls_from(nodes, ways, uses)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pick the stop signs and traffic lights out of an OpenStreetMap extract.")
    parser.add_argument("pbf", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    controls = RoadControls(read_extract(args.pbf))
    controls.save(args.out)
    print(f"{args.out}: {controls.counts()}")


if __name__ == "__main__":
    main()
