"""Bus routes for the OpenStreetMap map's Transit layer, from Milwaukee County Transit System's schedule.

Google's map has a transit layer of its own. OpenStreetMap has no free one with
buses, so the server reads the schedule MCTS publishes (GTFS: free, no key) and
hands the page each route's line. A route is drawn by its most-run shape in each
direction, simplified to within SIMPLIFY_M: about 100 lines for the county.

The feed is about 9 MB, so it's fetched at most once a REFRESH and the lines kept
in the cache folder: a restart reads them back instead of fetching again. Old
lines are served while new ones are fetched in the background, and stay when the
feed can't be fetched. Only the very first fetch, with nothing saved, is waited for.
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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, Thread

import httpx

from app.geo import encode_polyline, simplify

logger = logging.getLogger(__name__)

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


def read_agency(data: bytes) -> str | None:
    """Who runs the buses in a GTFS feed (agency.txt), to credit on the map."""
    with zipfile.ZipFile(io.BytesIO(data)) as feed:
        if "agency.txt" not in feed.namelist():
            return None
        names = [row.get("agency_name", "").strip() for row in _rows(feed, "agency.txt")]
    return " and ".join(name for name in names if name) or None


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
        self._agency: str | None = None
        self._checked_at = float("-inf")
        self._refreshing = False
        self._fetched = Event()  # set whenever no fetch is under way
        self._fetched.set()
        self._lock = Lock()

    @property
    def attribution(self) -> str:
        return f"Bus routes: {self._agency or 'the transit agency'}"

    @property
    def agency(self) -> str | None:
        return self._agency

    def routes_now(self) -> list[BusRoute]:
        """The lines already in hand, without fetching: for /health."""
        with self._lock:
            return list(self._routes or [])

    def routes(self) -> list[BusRoute]:
        with self._lock:
            if self._routes is None:
                self._read_saved()
            first_fetch_under_way = self._refreshing and not self._routes
            if not first_fetch_under_way and (self._refreshing or self._clock() - self._checked_at < REFRESH.total_seconds()):
                return self._routes or []
            if not first_fetch_under_way:
                self._refreshing = True
                self._fetched.clear()
            waiting = not self._routes
        if first_fetch_under_way:
            self._fetched.wait(timeout=90)  # the first fetch, started by another request: wait for it too
        elif waiting:
            self._refresh()  # nothing to show yet: this request waits for the feed
        else:
            Thread(target=self._refresh, name="bus-routes-refresh", daemon=True).start()
        return self._routes or []

    def _refresh(self) -> None:
        try:
            response = self._http.get(self._url, timeout=60.0, follow_redirects=True)
            response.raise_for_status()
            routes, agency = read_gtfs(response.content), read_agency(response.content)
        except (httpx.HTTPError, zipfile.BadZipFile, KeyError, ValueError) as exc:
            logger.warning("Bus routes unavailable: %s", type(exc).__name__)
            with self._lock:
                # Try again after RETRY, keeping whatever lines there are meanwhile.
                self._checked_at = self._clock() - REFRESH.total_seconds() + RETRY.total_seconds()
                self._refreshing = False
            self._fetched.set()
            return
        self._save(routes, agency)
        with self._lock:
            self._routes, self._agency, self._checked_at, self._refreshing = routes, agency, self._clock(), False
        self._fetched.set()
        logger.info("Bus routes: %d from %s", len(routes), self._url)

    def _read_saved(self) -> None:
        """The lines saved last time, as fresh as the file: none (and due now) without one."""
        self._routes = []
        try:
            saved = json.loads(self._saved.read_text(encoding="utf-8"))
            self._routes = [BusRoute(**{**row, "paths": tuple(row["paths"])}) for row in saved["routes"]]
            self._agency = saved.get("agency")
            self._checked_at = self._saved.stat().st_mtime
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self._routes = []

    def _save(self, routes: list[BusRoute], agency: str | None) -> None:
        try:
            self._saved.parent.mkdir(parents=True, exist_ok=True)
            data = {"agency": agency, "routes": [asdict(route) for route in routes]}
            self._saved.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            logger.warning("Bus routes not saved: %s", exc)


class AllBusRoutes:
    """Several agencies' bus routes as one: each feed fetched, kept and retried on its own (a BusRoutes each),
    so one agency's feed failing leaves the others' lines on the map."""

    def __init__(self, feeds: Sequence[BusRoutes]) -> None:
        self._feeds = list(feeds)

    @classmethod
    def of(cls, http: httpx.Client, urls: Sequence[str], folder: Path) -> AllBusRoutes:
        """The first feed's lines are kept in bus-routes.json, as before there could be several; the next in bus-routes-2.json."""
        names = ["bus-routes.json", *(f"bus-routes-{number}.json" for number in range(2, len(urls) + 1))]
        return cls([BusRoutes(http, url, folder / name) for url, name in zip(urls, names)])

    @property
    def attribution(self) -> str:
        agencies = [feed.agency for feed in self._feeds if feed.agency]
        return f"Bus routes: {', '.join(agencies) or 'the transit agencies'}"

    def routes_now(self) -> list[BusRoute]:
        return self._merged(feed.routes_now() for feed in self._feeds)

    def routes(self) -> list[BusRoute]:
        return self._merged(feed.routes() for feed in self._feeds)

    @staticmethod
    def _merged(lists: Iterable[list[BusRoute]]) -> list[BusRoute]:
        return sorted((route for routes in lists for route in routes), key=lambda route: _number_order(route.name))
