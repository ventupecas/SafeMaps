"""Crash hotspot lookup: how many recorded crashes happened near a point.

Load any crash dataset with one row per crash, or one row per location with a
count. Many cities publish one, for example NYC's "Motor Vehicle Collisions -
Crashes", Chicago's "Traffic Crashes" and the UK's STATS19. Filter it to the
area you'll demo in and save it as CSV with these columns:

    lat,lng[,crashes]

Headers named latitude/longitude (any case) also work. Rows without usable
coordinates are skipped, since many datasets have them.
"""

from __future__ import annotations

import csv
import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

from app.geo import Point, haversine_m

logger = logging.getLogger(__name__)

# Crashes are merged onto an ~11 m grid on load (they cluster at junctions, so
# this shrinks big files a lot), then bucketed into ~110 m cells for lookup.
_SNAP_DECIMALS = 4
_CELL_DEG = 0.001
_M_PER_DEG_LAT = 111_320.0

_LAT_COLUMNS = ("lat", "latitude")
_LNG_COLUMNS = ("lng", "lon", "long", "longitude")


class CrashHotspots:
    def __init__(self, crashes: Iterable[tuple[Point, float]]) -> None:
        """Index (point, crash count) pairs. Pairs at the same spot are summed."""
        merged: dict[Point, float] = defaultdict(float)
        for (lat, lng), count in crashes:
            merged[(round(lat, _SNAP_DECIMALS), round(lng, _SNAP_DECIMALS))] += count

        self._cells: dict[tuple[int, int], list[tuple[Point, float]]] = defaultdict(list)
        for point, count in merged.items():
            self._cells[_cell(point)].append((point, count))
        self.locations = len(merged)
        self.total_crashes = sum(merged.values())

    @classmethod
    def from_csv(cls, path: Path) -> CrashHotspots:
        path = Path(path)
        skipped = 0

        def rows(reader: csv.DictReader, lat_col: str, lng_col: str, count_col: str | None) -> Iterator[tuple[Point, float]]:
            nonlocal skipped
            for row in reader:
                try:
                    lat, lng = float(row[lat_col]), float(row[lng_col])
                    count = float(row[count_col]) if count_col and row[count_col] else 1.0
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                # (0, 0) is a common placeholder for "location unknown".
                if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
                    skipped += 1
                    continue
                yield (lat, lng), count

        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            columns = {name.strip().lower(): name for name in reader.fieldnames or []}
            lat_col = next((columns[name] for name in _LAT_COLUMNS if name in columns), None)
            lng_col = next((columns[name] for name in _LNG_COLUMNS if name in columns), None)
            if lat_col is None or lng_col is None:
                raise ValueError(f"{path.name} needs latitude and longitude columns; found {reader.fieldnames}")
            hotspots = cls(rows(reader, lat_col, lng_col, columns.get("crashes")))

        if skipped:
            logger.info("Skipped %d rows without usable coordinates in %s", skipped, path.name)
        return hotspots

    def spots(self) -> Iterator[tuple[Point, float]]:
        """Every place with crashes, and how many."""
        for spots in self._cells.values():
            yield from spots

    def crashes_near(self, point: Point, radius_m: float) -> float:
        """Total crashes recorded within radius_m of point."""
        cell_lat, cell_lng = _cell(point)
        # Cells narrow toward the poles, so search as many as the radius spans.
        reach_lat = math.ceil(radius_m / (_M_PER_DEG_LAT * _CELL_DEG))
        lng_cell_m = _M_PER_DEG_LAT * _CELL_DEG * max(math.cos(math.radians(point[0])), 0.01)
        reach_lng = math.ceil(radius_m / lng_cell_m)

        total = 0.0
        for i in range(cell_lat - reach_lat, cell_lat + reach_lat + 1):
            for j in range(cell_lng - reach_lng, cell_lng + reach_lng + 1):
                for other, count in self._cells.get((i, j), ()):
                    if haversine_m(point, other) <= radius_m:
                        total += count
        return total


def _cell(point: Point) -> tuple[int, int]:
    return math.floor(point[0] / _CELL_DEG), math.floor(point[1] / _CELL_DEG)
