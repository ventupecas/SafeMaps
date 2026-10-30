"""Pure geometry helpers: polyline codec, great-circle math and path curvature.

Dependency-free and side-effect-free, so feature extraction and candidate
generation can share them and tests can check them in isolation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from itertools import pairwise

EARTH_RADIUS_M = 6_371_008.8

Point = tuple[float, float]  # (lat, lng) in degrees


def decode_polyline(encoded: str, precision: int = 5) -> list[Point]:
    """Decode a Google encoded polyline into (lat, lng) pairs.

    See https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    """
    factor = 10**precision
    points: list[Point] = []
    index = lat = lng = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= len(encoded):
                    raise ValueError("Truncated polyline string")
                chunk = ord(encoded[index]) - 63
                index += 1
                result |= (chunk & 0x1F) << shift
                shift += 5
                if chunk < 0x20:
                    break
            # The lowest bit carries the sign; the value is stored inverted when negative.
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        points.append((lat / factor, lng / factor))
    return points


def encode_polyline(points: Iterable[Point], precision: int = 5) -> str:
    """Encode (lat, lng) pairs as a Google encoded polyline."""
    factor = 10**precision
    chunks: list[str] = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat, ilng = round(lat * factor), round(lng * factor)
        for delta in (ilat - prev_lat, ilng - prev_lng):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                chunks.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            chunks.append(chr(value + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(chunks)


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
    return sum(haversine_m(p, q) for p, q in pairwise(points))


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
    dense = [points[0]]
    for a, b in pairwise(points):
        segment = haversine_m(a, b)
        steps = max(1, math.ceil(segment / spacing_m))
        bearing = initial_bearing_deg(a, b)
        dense.extend(destination_point(a, bearing, segment * k / steps) for k in range(1, steps))
        dense.append(b)
    return dense


def total_heading_change_deg(points: Sequence[Point], min_segment_m: float = 30.0) -> float:
    """Sum of absolute heading changes along a path, in degrees.

    Points closer than min_segment_m to the last kept point are skipped first:
    encoded polylines are quantized to about 1 m, which makes the bearing of a
    very short segment mostly noise.
    """
    if not points:
        return 0.0
    kept = [points[0]]
    for point in points[1:]:
        if haversine_m(kept[-1], point) >= min_segment_m:
            kept.append(point)

    headings = [initial_bearing_deg(p, q) for p, q in pairwise(kept)]
    total = 0.0
    for h1, h2 in pairwise(headings):
        diff = abs(h2 - h1) % 360
        total += min(diff, 360 - diff)
    return total
