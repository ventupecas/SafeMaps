"""TomTom's traffic map for the OpenStreetMap page (ROUTES_MAP=osm), fetched by this server.

The page's "Show traffic" layer asks GET /traffic-tiles/{z}/{x}/{y}.png, and
the server fetches that tile from TomTom with TOMTOM_API_KEY, so the key never
reaches a browser. Each tile is kept for TILE_REFRESH and shared by everyone
looking at that spot, and the tiles fetched are counted against
ROUTES_TOMTOM_DAILY_TILES (TomTom's free plan has 200,000 a month).

The tiles color every road by its speed compared with free flow: green,
orange, red.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import timedelta
from threading import Lock

import httpx

from app.usage import DailyUsage

TOMTOM_FLOW_TILE_URL = "https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png"
TILE_REFRESH = timedelta(minutes=3)
MAX_TILES_KEPT = 2_000  # about 20 MB
MAX_ZOOM = 22


class TileError(RuntimeError):
    """A tile that can't be served; status is the HTTP status to answer with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class TrafficTiles:
    def __init__(
        self,
        key: str,
        http: httpx.Client,
        usage: DailyUsage,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = key
        self._http = http
        self.usage = usage  # tiles fetched today, against ROUTES_TOMTOM_DAILY_TILES
        self._clock = clock
        self._kept: OrderedDict[tuple[int, int, int], tuple[float, bytes]] = OrderedDict()
        self._lock = Lock()

    def tile(self, z: int, x: int, y: int) -> bytes:
        """The PNG for one map tile, from what's kept if it's recent enough."""
        if not (0 <= z <= MAX_ZOOM and 0 <= x < 2**z and 0 <= y < 2**z):
            raise TileError("no such tile", 404)
        key = (z, x, y)
        with self._lock:
            kept = self._kept.get(key)
            if kept and self._clock() - kept[0] <= TILE_REFRESH.total_seconds():
                self._kept.move_to_end(key)
                return kept[1]
        if not self.usage.take():
            raise TileError(f"today's {self.usage.limit} traffic tiles are used up (ROUTES_TOMTOM_DAILY_TILES)", 429)
        try:
            response = self._http.get(TOMTOM_FLOW_TILE_URL.format(z=z, x=x, y=y), params={"key": self._key}, timeout=10.0)
        except httpx.HTTPError as exc:
            raise TileError(f"couldn't reach TomTom: {type(exc).__name__}", 502) from None  # the URL holds the key
        if response.status_code in (401, 403):
            raise TileError("TomTom refused the key, or its free allowance is used up; check TOMTOM_API_KEY", 502)
        if response.is_error:
            raise TileError(f"TomTom answered {response.status_code}", 502)
        with self._lock:
            self._kept[key] = (self._clock(), response.content)
            self._kept.move_to_end(key)
            while len(self._kept) > MAX_TILES_KEPT:
                self._kept.popitem(last=False)
        return response.content
