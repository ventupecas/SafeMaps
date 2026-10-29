"""Download OpenStreetMap road data for an area from the Overpass API.

The area is split into tiles so each query stays well inside Overpass's time
and size limits. Raw responses are saved per tile, so rebuilding the graph
after a code change doesn't download anything again.

Map data (c) OpenStreetMap contributors, under the Open Database License
(ODbL). Show that credit wherever routes built from it are displayed.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
USER_AGENT = "personalized-routing-engine/0.1 (expo prototype)"
ATTRIBUTION = "© OpenStreetMap contributors"

# Road types a car can use. Tracks, paths and footways are left out.
DRIVABLE_HIGHWAYS = (
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "service",
)


@dataclass(frozen=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    def tiles(self, rows: int, cols: int) -> list[BBox]:
        lat_step = (self.north - self.south) / rows
        lng_step = (self.east - self.west) / cols
        return [
            BBox(
                self.south + r * lat_step,
                self.west + c * lng_step,
                self.south + (r + 1) * lat_step,
                self.west + (c + 1) * lng_step,
            )
            for r in range(rows)
            for c in range(cols)
        ]

    def contains(self, lat: float, lng: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lng <= self.east


AREAS: dict[str, BBox] = {
    # Milwaukee, Waukesha, Ozaukee and Washington counties (the Milwaukee
    # metropolitan area) with a small margin. Lake Michigan bounds it to the east.
    "milwaukee-metro": BBox(south=42.83, west=-88.55, north=43.55, east=-87.75),
}


MAX_SPLIT_DEPTH = 2  # a failing tile splits into 4 pieces, and those into 4 again


class OverpassError(RuntimeError):
    """Overpass refused or failed a query."""

    def __init__(self, message: str, *, splittable: bool = False) -> None:
        super().__init__(message)
        # True when a smaller query might succeed: the server was overloaded or ran out of time.
        self.splittable = splittable


def build_query(bbox: BBox, timeout_s: int = 600) -> str:
    """Drivable ways, their nodes, traffic signals and turn restrictions inside bbox."""
    box = f"{bbox.south},{bbox.west},{bbox.north},{bbox.east}"
    highways = "|".join(DRIVABLE_HIGHWAYS)
    return (
        f"[out:json][timeout:{timeout_s}];\n"
        f'way["highway"~"^({highways})$"]({box})->.roads;\n'
        ".roads out body qt;\n"
        "node(w.roads)->.road_nodes;\n"
        ".road_nodes out skel qt;\n"
        'node.road_nodes["highway"="traffic_signals"];\n'
        "out body qt;\n"
        'rel(bw.roads)["type"="restriction"];\n'
        "out body qt;\n"
    )


def download_area(
    area: str,
    bbox: BBox,
    raw_dir: Path,
    *,
    tiles: int = 3,
    endpoint: str = DEFAULT_ENDPOINT,
    refresh: bool = False,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Path]:
    """Fetch every tile of bbox that isn't already on disk; return all tile paths.

    A tile that keeps failing because Overpass is overloaded or runs out of
    time is split into four smaller queries, up to MAX_SPLIT_DEPTH times.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    client = http or httpx.Client(timeout=httpx.Timeout(900.0, connect=30.0), headers={"User-Agent": USER_AGENT})
    downloader = _TileDownloader(client, endpoint, raw_dir, refresh, sleep)
    paths: list[Path] = []
    try:
        for index, tile in enumerate(bbox.tiles(tiles, tiles)):
            label = f"{area} tile {index + 1} of {tiles * tiles}"
            paths += downloader.fetch(tile, f"tile-{tiles}x{tiles}-{index:02d}", label)
    finally:
        if http is None:
            client.close()
    return paths


def iter_elements(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            yield from json.load(handle).get("elements", [])


@dataclass
class _TileDownloader:
    client: httpx.Client
    endpoint: str
    raw_dir: Path
    refresh: bool
    sleep: Callable[[float], None]

    def fetch(self, tile: BBox, name: str, label: str, depth: int = 0) -> list[Path]:
        path = self.raw_dir / f"{name}.json.gz"
        pieces = sorted(self.raw_dir.glob(f"{name}[abcd]*.json.gz"))
        if not self.refresh:
            if path.exists():
                logger.info("%s: already downloaded", label)
                return [path]
            if pieces:
                # An earlier run had to split this tile: finish that rather than retry it whole.
                return self._fetch_pieces(tile, name, label, depth)

        logger.info("%s: downloading", label)
        try:
            data = _fetch(self.client, self.endpoint, build_query(tile), self.sleep)
        except OverpassError as exc:
            if not exc.splittable or depth >= MAX_SPLIT_DEPTH:
                raise
            logger.warning("%s failed (%s); splitting it into 4 smaller queries", label, exc)
            return self._fetch_pieces(tile, name, label, depth)

        _write_gzip_json(path, data)
        for stale in pieces:
            stale.unlink()  # the whole tile replaces pieces from an earlier split
        logger.info("%s: %d elements", label, len(data.get("elements", [])))
        return [path]

    def _fetch_pieces(self, tile: BBox, name: str, label: str, depth: int) -> list[Path]:
        paths: list[Path] = []
        for letter, piece in zip("abcd", tile.tiles(2, 2), strict=True):
            piece_label = f"{label}{letter}" if depth else f"{label}, piece {letter}"
            paths += self.fetch(piece, name + letter, piece_label, depth + 1)
        return paths


def _fetch(client: httpx.Client, endpoint: str, query: str, sleep: Callable[[float], None], attempts: int = 4) -> dict:
    error = ""
    splittable = True
    for attempt in range(1, attempts + 1):
        try:
            response = client.post(endpoint, data={"data": query})
        except httpx.TransportError as exc:
            error = f"network error: {exc}"
            # Can't connect at all (e.g. a blocked network): smaller queries won't help.
            splittable = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        else:
            if response.status_code == 200:
                data = response.json()
                # Overpass reports timeouts and memory exhaustion in a 200
                # response with partial data. Partial road data would silently
                # drop streets, so treat it as a failure.
                remark = data.get("remark", "")
                if "error" in remark.lower():
                    raise OverpassError(f"Overpass stopped early ({remark.strip()})", splittable=True)
                return data
            error = f"HTTP {response.status_code}"
            splittable = True
            if response.status_code not in (429, 502, 503, 504):
                raise OverpassError(f"Overpass returned {error}: {response.text[:300]}")
        if attempt < attempts:
            wait = 30 * attempt
            logger.warning("Overpass %s; retrying in %d s", error, wait)
            sleep(wait)
    raise OverpassError(f"Overpass failed after {attempts} attempts: {error}", splittable=splittable)


def _write_gzip_json(path: Path, data: Any) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        with gzip.open(tmp_name, "wt", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
