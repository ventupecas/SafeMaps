"""Build the engine's road graph from OpenStreetMap.

Run from the backend/ directory:

    python -m app.build_graph milwaukee-metro
    python -m app.build_graph milwaukee-metro --tiles 4 --refresh

Raw Overpass tiles are kept under data/osm/<area>/, so a rebuild after a code
change reuses them. The graph is written to data/graphs/<area>.json.gz, which
is where ROUTES_GRAPH points by default.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence

from app.config import BACKEND_DIR
from app.overpass import AREAS, DEFAULT_ENDPOINT, OverpassError, download_area, iter_elements
from app.roadgraph import build_graph


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.build_graph", description=__doc__.splitlines()[0])
    parser.add_argument("area", choices=AREAS, help="area to build")
    parser.add_argument("--tiles", type=int, default=3, help="split the area into N x N download tiles (default 3)")
    parser.add_argument("--refresh", action="store_true", help="download again even if tiles are on disk")
    parser.add_argument("--download-only", action="store_true", help="fetch the tiles but don't build")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Overpass API URL")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    raw_dir = BACKEND_DIR / "data" / "osm" / args.area
    started = time.monotonic()
    try:
        paths = download_area(
            args.area, AREAS[args.area], raw_dir, tiles=args.tiles, endpoint=args.endpoint, refresh=args.refresh
        )
    except OverpassError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.download_only:
        return 0

    graph = build_graph(iter_elements(paths), area=args.area, bbox=AREAS[args.area])
    out = BACKEND_DIR / "data" / "graphs" / f"{args.area}.json.gz"
    graph.save(out)

    print(f"\nWrote {out} ({out.stat().st_size / 1e6:.1f} MB) in {time.monotonic() - started:.0f} s")
    for key, value in sorted(graph.stats.items()):
        print(f"  {key:<26} {value:>9,}")
    print("Map data © OpenStreetMap contributors (ODbL). Credit it wherever these routes are shown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
