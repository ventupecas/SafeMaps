"""Pure geometry helpers: polyline codec, great-circle math and path curvature.

Side-effect-free, so feature extraction and candidate generation can share
them and tests can check them in isolation. Whole paths go through the compiled
app.geo_kernel; a single sum is as quick here.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from itertools import pairwise

import numpy as np

import app.geo_kernel as kernel

EARTH_RADIUS_M = 6_371_008.8

Point = tuple[float, float]  # (lat, lng) in degrees


def decode_polyline(encoded: str, precision: int = 5) -> list[Point]:
    """Decode a Google encoded polyline into (lat, lng) pairs.

    See https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    """
    if not encoded:
        return []
    data = np.frombuffer(encoded.encode("latin-1"), dtype=np.uint8)
    lat, lng = kernel.decoded(data, 0, data.size, float(10**precision))
    return list(zip(lat.tolist(), lng.tolist()))


def encode_polyline(points: Iterable[Point], precision: int = 5) -> str:
    """Encode (lat, lng) pairs as a Google encoded polyline."""
    lat, lng = columns(points if isinstance(points, (list, tuple)) else list(points))
    return kernel.encoded(lat, lng, float(10**precision)).tobytes().decode("ascii")


def columns(points: Sequence[Point]) -> tuple[np.ndarray, np.ndarray]:
    """A path's latitudes and longitudes, for the compiled functions."""
    table = np.array(points, dtype=np.float64).reshape(-1, 2)
    return table[:, 0].copy(), table[:, 1].copy()


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance between two points, in meters."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing_deg(a: Point, b: Point) -> float:
    """Compass bearing from a toward b, in degrees clockwise from north (0-360)."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlng = lng2 - lng1
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def initial_bearings_deg(lat1: np.ndarray, lng1: np.ndarray, lat2: np.ndarray, lng2: np.ndarray) -> np.ndarray:
    """initial_bearing_deg for whole columns of points at once."""
    lat1, lat2, dlng = np.radians(lat1), np.radians(lat2), np.radians(lng2) - np.radians(lng1)
    x = np.sin(dlng) * np.cos(lat2)
    y = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlng)
    return (np.degrees(np.arctan2(x, y)) + 360) % 360


def angle_between_deg(a: float, b: float) -> float:
    """Smallest difference between two compass bearings, 0-180 degrees."""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def distance_to_path_m(point: Point, path: Sequence[Point]) -> tuple[float, float]:
    """Distance from point to the nearest spot on path, and the path's bearing there.

    Uses a flat projection around point: accurate to well under a meter at the
    few hundred meters this is used for.
    """
    if len(path) < 2:
        raise ValueError("A path needs at least two points")
    m_per_deg = math.radians(1) * EARTH_RADIUS_M
    m_per_deg_lng = m_per_deg * math.cos(math.radians(point[0]))

    def xy(p: Point) -> tuple[float, float]:
        return (p[1] - point[1]) * m_per_deg_lng, (p[0] - point[0]) * m_per_deg

    best_m, best_bearing = math.inf, 0.0
    for a, b in pairwise(path):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
        distance = math.hypot(ax + t * dx, ay + t * dy)
        if distance < best_m and length_sq > 0:
            best_m, best_bearing = distance, initial_bearing_deg(a, b)
    return best_m, best_bearing


def destination_point(origin: Point, bearing_deg: float, distance_m: float) -> Point:
    """The point reached by travelling distance_m from origin along bearing_deg."""
    lat1, lng1 = map(math.radians, origin)
    bearing = math.radians(bearing_deg)
    angular = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lng2 = lng1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lng2) + 540) % 360 - 180


def midpoint(a: Point, b: Point) -> Point:
    """Halfway point along the great circle from a to b."""
    return destination_point(a, initial_bearing_deg(a, b), haversine_m(a, b) / 2)


def path_length_m(points: Sequence[Point]) -> float:
    """Total length of a polyline, in meters."""
    return float(kernel.path_length(*columns(points)))


def point_along(points: Sequence[Point], distance_m: float) -> tuple[Point, float]:
    """The point distance_m along a path, and the path's bearing there."""
    if len(points) < 2:
        raise ValueError("A path needs at least two points")
    remaining = max(0.0, distance_m)
    for a, b in pairwise(points):
        segment = haversine_m(a, b)
        if segment > 0 and remaining <= segment:
            bearing = initial_bearing_deg(a, b)
            return destination_point(a, bearing, remaining), bearing
        remaining -= segment
    return points[-1], initial_bearing_deg(points[-2], points[-1])


def densify(points: Sequence[Point], spacing_m: float) -> list[Point]:
    """The path's points, plus extra ones so no gap is longer than spacing_m."""
    if not points:
        return []
    lat, lng = kernel.densified(*columns(points), spacing_m)
    return list(zip(lat.tolist(), lng.tolist()))


def total_heading_change_deg(points: Sequence[Point], min_segment_m: float = 30.0) -> float:
    """Sum of absolute heading changes along a path, in degrees.

    Points closer than min_segment_m to the last kept point are skipped first:
    encoded polylines are quantized to about 1 m, which makes the bearing of a
    very short segment mostly noise.
    """
    if not points:
        return 0.0
    return float(kernel.heading_change(*columns(points), min_segment_m))


def simplify(points: Sequence[Point], tolerance_m: float) -> list[Point]:
    """The path with fewer points, none of the dropped ones more than tolerance_m off it (Douglas-Peucker).

    Uses a flat projection around the path's first point: fine across a city.
    """
    if len(points) < 3:
        return list(points)
    m_per_deg = math.radians(1) * EARTH_RADIUS_M
    m_per_deg_lng = m_per_deg * math.cos(math.radians(points[0][0]))
    xy = [(lng * m_per_deg_lng, lat * m_per_deg) for lat, lng in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    spans = [(0, len(points) - 1)]  # a stack rather than recursion: bus routes run to thousands of points
    while spans:
        first, last = spans.pop()
        (ax, ay), (bx, by) = xy[first], xy[last]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        worst, worst_m = -1, tolerance_m
        for i in range(first + 1, last):
            px, py = xy[i]
            t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
            distance = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if distance > worst_m:
                worst, worst_m = i, distance
        if worst >= 0:
            keep[worst] = True
            spans += [(first, worst), (worst, last)]
    return [point for point, kept in zip(points, keep, strict=True) if kept]
