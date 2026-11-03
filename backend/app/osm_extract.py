"""OpenStreetMap data from an extract file (.osm.pbf) instead of the Overpass API.

Where Overpass can't be reached, an extract of the area does the same job: OpenStreetMap US's Slice
service (https://slice.openstreetmap.us) cuts one from the latest planet data, for any area drawn on
its map. This module picks out of it what each Overpass query in app.overpass asks for, and writes
it in Overpass's JSON form, so the graph is built from it exactly as from downloaded tiles:

    roads.json.gz  build_query: drivable ways, their nodes, signals and railroad crossings on them,
                   turn restrictions on them, and schools (as their centers)
    bike.json.gz   build_bike_query: trails, cycleways and paths (not sidewalks), their nodes, and
                   their own signals and railroad crossings
    walk.json.gz   build_walk_query: footways and sidewalks, paths, steps and pedestrian streets,
                   their nodes, and their own crossings

The file is read as it streams by, with osmium keeping the nodes' locations for the ways that
follow them (and assembling schools' outlines). Needs pyosmium (pip install osmium).

Map data (c) OpenStreetMap contributors, under the Open Database License (ODbL).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any, TextIO

from app.overpass import DRIVABLE_HIGHWAYS

logger = logging.getLogger(__name__)

BIKE_PATHS = frozenset({"cycleway", "path", "footway", "pedestrian", "track", "bridleway"})
WALK_PATHS = frozenset({"footway", "path", "pedestrian", "steps", "cycleway", "track", "bridleway"})
KINDS = ("roads", "bike", "walk")


def kinds_of_way(tags: dict[str, str]) -> list[str]:
    """Which of the queries a way with these tags is in."""
    highway = tags.get("highway")
    kinds = []
    if highway in DRIVABLE_HIGHWAYS:
        kinds.append("roads")
    if highway in BIKE_PATHS and tags.get("footway") != "sidewalk":
        kinds.append("bike")
    if highway in WALK_PATHS:
        kinds.append("walk")
    return kinds


def tagged_node_kinds(tags: dict[str, str]) -> list[str]:
    """For which queries a node's tags are wanted (on a way that query keeps)."""
    kinds = []
    if tags.get("highway") == "traffic_signals" or tags.get("railway") == "level_crossing":
        kinds.append("roads")
    if tags.get("highway") in ("traffic_signals", "crossing") or tags.get("railway") in ("level_crossing", "crossing"):
        kinds += ["bike", "walk"]
    return kinds


def extract(pbf: Path, out_dir: Path) -> dict[str, Path]:
    """Write roads.json.gz, bike.json.gz and walk.json.gz from pbf into out_dir; their paths by kind.
    Kept while newer than pbf: a rebuild doesn't read the extract again."""
    paths = {kind: out_dir / f"{kind}.json.gz" for kind in KINDS}
    if all(path.exists() and path.stat().st_mtime >= pbf.stat().st_mtime for path in paths.values()):
        logger.info("%s: already converted", pbf.name)
        return paths
    import osmium  # only for extracts: pip install osmium

    out_dir.mkdir(parents=True, exist_ok=True)
    tagged: dict[int, dict[str, str]] = {}  # signals and crossings, for the ways that pass them
    road_ways: set[int] = set()
    written: dict[str, set[int]] = {kind: set() for kind in KINDS}  # nodes already out, per file
    counts = {kind: {"ways": 0, "nodes": 0, "tagged nodes": 0, "restrictions": 0, "schools": 0} for kind in KINDS}
    with _Writers(paths) as out:
        # One pass: nodes come first in the file (osmium keeps their locations), then ways, relations,
        # and the areas osmium assembles from them (for schools' outlines).
        for obj in osmium.FileProcessor(str(pbf)).with_areas():
            if obj.is_node():
                if not len(obj.tags):
                    continue
                tags = {tag.k: tag.v for tag in obj.tags}
                if tags.get("amenity") == "school" and obj.location.valid():
                    out.write("roads", {"type": "node", "id": obj.id, "lat": obj.location.lat, "lon": obj.location.lon, "tags": tags})
                    counts["roads"]["schools"] += 1
                elif tagged_node_kinds(tags):
                    tagged[obj.id] = tags
            elif obj.is_way():
                tags = {tag.k: tag.v for tag in obj.tags}
                kinds = kinds_of_way(tags)
                if not kinds:
                    continue
                refs = [node.ref for node in obj.nodes]
                nodes = [
                    (node.ref, node.lat, node.lon, tagged.get(node.ref))
                    for node in obj.nodes
                    if node.location.valid()
                ]
                for kind in kinds:
                    out.write(kind, {"type": "way", "id": obj.id, "nodes": refs, "tags": tags})
                    counts[kind]["ways"] += 1
                    for ref, lat, lon, node_tags in nodes:
                        if ref in written[kind]:
                            continue  # a junction: written with the first way through it
                        written[kind].add(ref)
                        element: dict[str, Any] = {"type": "node", "id": ref, "lat": lat, "lon": lon}
                        if node_tags and kind in tagged_node_kinds(node_tags):
                            element["tags"] = node_tags
                            counts[kind]["tagged nodes"] += 1
                        out.write(kind, element)
                        counts[kind]["nodes"] += 1
                if "roads" in kinds:
                    road_ways.add(obj.id)
            elif obj.is_relation():
                tags = {tag.k: tag.v for tag in obj.tags}
                if tags.get("type") != "restriction":
                    continue
                members = [{"type": {"n": "node", "w": "way", "r": "relation"}[m.type], "ref": m.ref, "role": m.role} for m in obj.members]
                if any(m["type"] == "way" and m["ref"] in road_ways for m in members):
                    out.write("roads", {"type": "relation", "id": obj.id, "members": members, "tags": tags})
                    counts["roads"]["restrictions"] += 1
            elif obj.is_area() and obj.tags.get("amenity") == "school":
                # Overpass's "out center": the middle of the outline's bounding box.
                points = [(node.lat, node.lon) for ring in obj.outer_rings() for node in ring if node.location.valid()]
                if points:
                    lats, lngs = [p[0] for p in points], [p[1] for p in points]
                    center = {"lat": (min(lats) + max(lats)) / 2, "lon": (min(lngs) + max(lngs)) / 2}
                    kind = "way" if obj.from_way() else "relation"
                    out.write("roads", {"type": kind, "id": obj.orig_id(), "center": center, "tags": {tag.k: tag.v for tag in obj.tags}})
                    counts["roads"]["schools"] += 1
    for kind in KINDS:
        logger.info("%s: %s", kind, ", ".join(f"{value:,} {name}" for name, value in counts[kind].items() if value))
    return paths


class _Writers:
    """One Overpass JSON file per kind, one element a line (so app.overpass.iter_elements can read it
    a line at a time), put in place only when complete."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = paths
        self._files: dict[str, TextIO] = {}
        self._first: dict[str, bool] = {}

    def __enter__(self) -> _Writers:
        for kind, path in self._paths.items():
            self._files[kind] = gzip.open(path.with_suffix(".tmp"), "wt", encoding="utf-8")
            self._files[kind].write('{"elements": [\n')
            self._first[kind] = True
        return self

    def write(self, kind: str, element: dict[str, Any]) -> None:
        handle = self._files[kind]
        if not self._first[kind]:
            handle.write(",\n")
        self._first[kind] = False
        handle.write(json.dumps(element, separators=(",", ":")))

    def __exit__(self, kind, error, trace) -> None:
        for name, handle in self._files.items():
            if error is None:
                handle.write("\n]}\n")
            handle.close()
            temporary = self._paths[name].with_suffix(".tmp")
            if error is None:
                os.replace(temporary, self._paths[name])
            else:
                temporary.unlink(missing_ok=True)
