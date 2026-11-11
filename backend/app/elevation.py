"""How high the ground is, for the climb on each of the bike map's segments.

The heights come from the public terrain tiles on AWS (Mapzen's, "skadi": NASA's SRTM survey, void-filled;
free, no key): one file per degree of latitude and longitude, a grid of heights about 30 m apart. The
tiles an area needs are fetched once into data/elevation/ and read from there after.

A survey this coarse has a meter or so of ripple from one grid point to the next, which along a flat
street would add up to a climb that isn't there. So each tile is smoothed first (the mean over
SMOOTH_CELLS grid points each way, about 150 m), and a height between grid points is read off its four
neighbors. Long hills keep their height; a short, sharp bluff comes out a little gentler than it is.
"""

from __future__ import annotations

import gzip
import logging
import math
from array import array
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

from app import geo_kernel
from app.roadgraph import _POLYLINE_FACTOR, RoadGraph

logger = logging.getLogger(__name__)

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{band}/{name}.hgt.gz"
SMOOTH_CELLS = 5


class Heights(Protocol):
    def heights(self, lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
        """Meters above sea level at each point."""


class ElevationError(RuntimeError):
    """A terrain tile couldn't be fetched or read."""


def tile_name(lat: float, lng: float) -> str:
    """The tile a point is in, named by its southwest corner: "N43W088"."""
    south, west = math.floor(lat), math.floor(lng)
    return f"{'N' if south >= 0 else 'S'}{abs(south):02d}{'E' if west >= 0 else 'W'}{abs(west):03d}"


class Elevation:
    """Heights from terrain tiles kept in folder, fetched as they're first needed."""

    def __init__(self, folder: Path, http: httpx.Client | None = None) -> None:
        self._folder = folder
        self._http = http
        self._tiles: dict[str, np.ndarray] = {}

    def heights(self, lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
        out = np.zeros(lat.size)
        south, west = np.floor(lat).astype(np.int64), np.floor(lng).astype(np.int64)
        for corner in {(int(s), int(w)) for s, w in zip(south.tolist(), west.tolist())}:
            here = (south == corner[0]) & (west == corner[1])
            grid = self._tile(tile_name(*corner))
            cells = grid.shape[0] - 1  # a tile's last row and column are the next tile's first
            row = (corner[0] + 1 - lat[here]) * cells  # row 0 is the north edge
            col = (lng[here] - corner[1]) * cells
            r0 = np.clip(np.floor(row).astype(np.int64), 0, cells - 1)
            c0 = np.clip(np.floor(col).astype(np.int64), 0, cells - 1)
            fr, fc = row - r0, col - c0
            out[here] = (
                grid[r0, c0] * (1 - fr) * (1 - fc)
                + grid[r0, c0 + 1] * (1 - fr) * fc
                + grid[r0 + 1, c0] * fr * (1 - fc)
                + grid[r0 + 1, c0 + 1] * fr * fc
            )
        return out

    def _tile(self, name: str) -> np.ndarray:
        grid = self._tiles.get(name)
        if grid is None:
            grid = self._tiles[name] = _smoothed(self._read(name))
        return grid

    def _read(self, name: str) -> np.ndarray:
        path = self._folder / f"{name}.hgt.gz"
        if not path.exists():
            if self._http is None:
                raise ElevationError(f"no terrain tile {path.name} in {self._folder}")
            url = TILE_URL.format(band=name[:3], name=name)
            try:
                response = self._http.get(url, timeout=120.0, follow_redirects=True)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ElevationError(f"couldn't fetch the terrain tile {name}: {type(exc).__name__}") from None
            self._folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
            logger.info("Terrain tile %s fetched (%.1f MB)", name, len(response.content) / 1e6)
        try:
            raw = gzip.decompress(path.read_bytes())
        except (OSError, EOFError) as exc:
            raise ElevationError(f"couldn't read the terrain tile {path.name}: {exc}") from None
        side = math.isqrt(len(raw) // 2)
        if side < 2 or side * side * 2 != len(raw):
            raise ElevationError(f"the terrain tile {path.name} isn't a square grid of heights")
        grid = np.frombuffer(raw, dtype=">i2").reshape(side, side).astype(np.float32)
        grid[grid < -1000] = 0.0  # a gap in the survey (-32768): none on land in these tiles, some over water
        return grid


def _smoothed(grid: np.ndarray) -> np.ndarray:
    """Each height as the mean of the SMOOTH_CELLS by SMOOTH_CELLS grid points around it (fewer at the edges)."""
    half = SMOOTH_CELLS // 2
    for axis in (0, 1):
        padded = np.concatenate((np.repeat(grid.take([0], axis), half, axis), grid, np.repeat(grid.take([-1], axis), half, axis)), axis)
        sums = np.cumsum(padded, axis=axis, dtype=np.float64)
        sums = np.concatenate((np.zeros_like(sums.take([0], axis)), sums), axis)
        size = grid.shape[axis]
        grid = ((sums.take(range(SMOOTH_CELLS, SMOOTH_CELLS + size), axis) - sums.take(range(size), axis)) / SMOOTH_CELLS).astype(np.float32)
    return grid


def add_climb(graph: RoadGraph, ground: Heights) -> None:
    """Give each segment its climb: the meters gained along its line, in its direction of travel."""
    if not graph.edge_count:
        return
    blob, ptr = graph.geometry.blob, graph.geometry.ptr
    lats, lngs, counts = [], [], np.empty(graph.edge_count, dtype=np.int64)
    for edge in range(graph.edge_count):
        lat, lng = geo_kernel.decoded(blob, ptr[edge], ptr[edge + 1], _POLYLINE_FACTOR)
        lats.append(lat)
        lngs.append(lng)
        counts[edge] = lat.size
    height = ground.heights(np.concatenate(lats), np.concatenate(lngs))
    ends = np.cumsum(counts)
    starts = ends - counts
    rise = np.maximum(np.diff(height), 0.0)
    rise[ends[:-1] - 1] = 0.0  # from one segment's last point to the next one's first: not a climb
    total = np.concatenate(([0.0], np.cumsum(rise)))
    climb = total[ends - 1] - total[starts]
    graph.climb_m = array("f", np.round(climb, 1).astype(np.float32).tobytes())
    graph.stats["climb_m"] = round(float(climb.sum()))  # every segment's, added up: how hilly the map is
