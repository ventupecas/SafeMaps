"""Bus routes for the OpenStreetMap map's Transit layer, from Milwaukee County Transit System's schedule.

Google's map has a transit layer of its own. OpenStreetMap has no free one with
buses, so the server reads the schedule MCTS publishes (GTFS: free, no key) and
hands the page each route's line. A route is drawn by its most-run shape in each
direction, simplified to within SIMPLIFY_M: about 100 lines for the county.

The feed is about 9 MB, so it's fetched at most once a REFRESH and the lines kept
in the cache folder: a restart reads them back instead of fetching again. When
the feed can't be fetched, the last lines saved are served, however old.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from threading import Lock

import httpx

from app.geo import encode_polyline, simplify

logger = logging.getLogger(__name__)

MCTS_GTFS_URL = "https://kamino.mcts.org/gtfs/google_transit.zip"
ATTRIBUTION = "Bus routes: Milwaukee County Transit System"
REFRESH = timedelta(days=7)
RETRY = timedelta(minutes=10)  # after a failed fetch with nothing saved
SIMPLIFY_M = 6.0
DEFAULT_COLOR = "#1a73e8"


@dataclass(frozen=True)
class BusRoute:
    name: str  # what's on the bus: "30", "BlueLine"
    long_name: str  # "Sherman - Wisconsin"
    color: str  # "#863399"
    paths: tuple[str, ...]  # encoded polylines, one per direction


def read_gtfs(data: bytes) -> list[BusRoute]:
    """Each route of a GTFS feed (a zip), by its most-run shape in each direction, in route number order."""
    with zipfile.ZipFile(io.BytesIO(data)) as feed:
        routes = {row["route_id"]: row for row in _rows(feed, "routes.txt")}
        runs: Counter[tuple[str, str, str]] = Counter(
            (row["route_id"], row.get("direction_id") or "", row["shape_id"])
            for row in _rows(feed, "trips.txt")
            if row.get("shape_id")
        )
        main: dict[tuple[str, str], str] = {}
        for (route_id, direction, shape_id), _count in runs.most_common():
            main.setdefault((route_id, direction), shape_id)  # most_common: the first seen is the most run
        wanted = set(main.values())
        shapes: dict[str, list[tuple[int, float, float]]] = {}
        for row in _rows(feed, "shapes.txt"):
            if row["shape_id"] in wanted:
                shapes.setdefault(row["shape_id"], []).append(
                    (int(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))
                )
    lines: dict[str, list[str]] = {}
    for (route_id, _direction), shape_id in sorted(main.items()):
        points = [(lat, lng) for _, lat, lng in sorted(shapes.get(shape_id, []))]
        if len(points) >= 2 and route_id in routes:
            lines.setdefault(route_id, []).append(encode_polyline(simplify(points, SIMPLIFY_M)))
    found = []
    for route_id, paths in lines.items():
        row = routes[route_id]
        color = (row.get("route_color") or "").strip().lstrip("#")
        found.append(
            BusRoute(
                name=(row.get("route_short_name") or "").strip() or route_id,
                long_name=" ".join((row.get("route_long_name") or "").split()),
                color=f"#{color}" if re.fullmatch(r"[0-9A-Fa-f]{6}", color) and color.upper() != "FFFFFF" else DEFAULT_COLOR,
                paths=tuple(paths),
            )
        )
    return sorted(found, key=lambda route: _number_order(route.name))


def _rows(feed: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with feed.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def _number_order(name: str) -> tuple[int, str]:
    """'4' before '30' before '40U' before 'BlueLine'."""
    digits = re.match(r"\d+", name)
    return (int(digits.group()) if digits else 10**6, name)


class BusRoutes:
    """The county's bus routes, fetched at most once a REFRESH and kept on disk. Failures serve what's saved."""

    def __init__(
        self, http: httpx.Client, url: str, saved: Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._http = http
        self._url = url
        self._saved = saved
        self._clock = clock
        self._routes: list[BusRoute] | None = None
        self._checked_at = float("-inf")
        self._lock = Lock()  # one fetch at a time: the feed is large

    def routes(self) -> list[BusRoute]:
        with self._lock:
            now = self._clock()
            if self._routes is not None and now - self._checked_at < REFRESH.total_seconds():
                return self._routes
            if self._routes is None and self._saved.exists() and now - self._saved.stat().st_mtime < REFRESH.total_seconds():
                self._routes, self._checked_at = self._read_saved(), self._saved.stat().st_mtime
                if self._routes:
                    return self._routes
            try:
                response = self._http.get(self._url, timeout=60.0, follow_redirects=True)
                response.raise_for_status()
                routes = read_gtfs(response.content)
            except (httpx.HTTPError, zipfile.BadZipFile, KeyError, ValueError) as exc:
                logger.warning("Bus routes unavailable: %s", type(exc).__name__)
                self._routes = self._routes or self._read_saved()
                # Nothing at all: try again soon. Old lines: keep them a while before trying again.
                self._checked_at = now - REFRESH.total_seconds() + RETRY.total_seconds()
                return self._routes
            self._routes, self._checked_at = routes, now
            self._save(routes)
            logger.info("Bus routes: %d from %s", len(routes), self._url)
            return routes

    def _read_saved(self) -> list[BusRoute]:
        try:
            rows = json.loads(self._saved.read_text(encoding="utf-8"))
            return [BusRoute(**{**row, "paths": tuple(row["paths"])}) for row in rows]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def _save(self, routes: list[BusRoute]) -> None:
        try:
            self._saved.parent.mkdir(parents=True, exist_ok=True)
            self._saved.write_text(json.dumps([asdict(route) for route in routes]), encoding="utf-8")
        except OSError as exc:
            logger.warning("Bus routes not saved: %s", exc)
