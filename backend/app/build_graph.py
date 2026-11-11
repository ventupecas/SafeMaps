"""Build the engine's road graph from OpenStreetMap.

Run from the backend/ directory:

    python -m app.build_graph milwaukee-metro
    python -m app.build_graph milwaukee-metro --tiles 4 --refresh
    python -m app.build_graph milwaukee-metro --bike      # the bike map, for bike routes
    python -m app.build_graph milwaukee-metro --walk      # the walking map, for walking routes
    python -m app.build_graph milwaukee-metro --pbf data/osm/pbf/milwaukee-metro.osm.pbf   # from an extract

Raw Overpass tiles are kept under data/osm/<area>/, so a rebuild after a code
change reuses them. The graph is written to data/graphs/<area>.json.gz, which
is where ROUTES_GRAPH points by default.

With --bike, the bike map: the same road tiles plus trails, cycleways and paths
(kept under data/osm/<area>-bike/), with bike rules and each segment's traffic
stress, written to data/graphs/<area>-bike.json.gz (ROUTES_BIKE_GRAPH).

With --pbf, the same data from an OpenStreetMap extract instead of Overpass (where Overpass can't be
reached; see app.osm_extract), converted once into data/osm/<area>-pbf/.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import httpx

from app.config import BACKEND_DIR
from app.elevation import Elevation, ElevationError, add_climb
from app.overpass import AREAS, DEFAULT_ENDPOINT, OverpassError, build_bike_query, build_walk_query, download_area, iter_elements
from app.osm_extract import extract
from app.roadgraph import build_graph


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.build_graph", description=__doc__.splitlines()[0])
    parser.add_argument("area", choices=AREAS, help="area to build")
    parser.add_argument("--tiles", type=int, default=3, help="split the area into N x N download tiles (default 3)")
    parser.add_argument("--refresh", action="store_true", help="download again even if tiles are on disk")
    parser.add_argument("--download-only", action="store_true", help="fetch the tiles but don't build")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Overpass API URL")
    parser.add_argument("--bike", action="store_true", help="build the bike map (roads plus trails and paths)")
    parser.add_argument("--walk", action="store_true", help="build the walking map (roads plus footways, sidewalks and steps)")
    parser.add_argument("--roads-only", action="store_true", help="with --bike or --walk: skip the paths download (streets only)")
    parser.add_argument("--pbf", type=Path, help="read an OpenStreetMap extract (.osm.pbf) instead of downloading from Overpass")
    parser.add_argument("--no-hills", action="store_true", help="with --bike: leave out each segment's climb (no terrain tiles are fetched)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    raw_dir = BACKEND_DIR / "data" / "osm" / args.area
    started = time.monotonic()
    if args.pbf:
        if not args.pbf.exists():
            print(f"error: no such file: {args.pbf}", file=sys.stderr)
            return 1
        converted = extract(args.pbf, BACKEND_DIR / "data" / "osm" / f"{args.area}-pbf")
        paths = [converted["roads"]]
        if not args.roads_only:
            paths += [converted["walk"]] if args.walk else [converted["bike"]] if args.bike else []
        return _build(args, paths, started)
    try:
        paths = download_area(
            args.area, AREAS[args.area], raw_dir, tiles=args.tiles, endpoint=args.endpoint, refresh=args.refresh
        )
        if args.walk and not args.roads_only:
            paths += download_area(
                f"{args.area} footways and sidewalks",
                AREAS[args.area],
                BACKEND_DIR / "data" / "osm" / f"{args.area}-walk",
                tiles=args.tiles,
                endpoint=args.endpoint,
                refresh=args.refresh,
                query=build_walk_query,
            )
        if args.bike and not args.roads_only:
            paths += download_area(
                f"{args.area} trails and paths",
                AREAS[args.area],
                BACKEND_DIR / "data" / "osm" / f"{args.area}-bike",
                tiles=args.tiles,
                endpoint=args.endpoint,
                refresh=args.refresh,
                query=build_bike_query,
            )
    except OverpassError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.download_only:
        return 0
    return _build(args, paths, started)


def _build(args: argparse.Namespace, paths: list[Path], started: float) -> int:
    mode = "bike" if args.bike else "walk" if args.walk else "car"
    graph = build_graph(iter_elements(paths), area=args.area, bbox=AREAS[args.area], mode=mode)
    if mode == "bike" and not args.no_hills:
        # The climb along each segment, from public terrain tiles kept in data/elevation/ (app.elevation).
        try:
            with httpx.Client() as http:
                add_climb(graph, Elevation(BACKEND_DIR / "data" / "elevation", http))
        except ElevationError as exc:
            print(f"warning: built without hills ({exc}): routes show no climb, and the flat profile rides as everyday", file=sys.stderr)
    out = BACKEND_DIR / "data" / "graphs" / f"{args.area}{'' if mode == 'car' else '-' + mode}.json.gz"
    graph.save(out)

    print(f"\nWrote {out} ({out.stat().st_size / 1e6:.1f} MB) in {time.monotonic() - started:.0f} s")
    for key, value in sorted(graph.stats.items()):
        print(f"  {key:<26} {value:>9,}")
    print("Map data © OpenStreetMap contributors (ODbL). Credit it wherever these routes are shown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
