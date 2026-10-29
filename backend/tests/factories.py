"""Builders for synthetic Routes API responses.

They produce the JSON shape the Routes API returns for our field mask, with
geometry and timings chosen so each route has an obvious character. These are
test fixtures, not real Google data.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.geo import Point, decode_polyline, destination_point, encode_polyline, path_length_m

START: Point = (37.7749, -122.4194)
END: Point = (37.3382, -121.8863)


def straight(start: Point, bearing: float, length_m: float, segment_m: float = 100.0) -> list[Point]:
    count = max(1, round(length_m / segment_m))
    points = [start]
    for _ in range(count):
        points.append(destination_point(points[-1], bearing, length_m / count))
    return points


def zigzag(start: Point, bearing: float, length_m: float, segment_m: float = 100.0) -> list[Point]:
    """Swings 60 degrees either side of bearing every segment: about 1200 degrees of turning per km."""
    points = [start]
    for i in range(round(length_m / segment_m)):
        swing = 60.0 if i % 2 == 0 else -60.0
        points.append(destination_point(points[-1], bearing + swing, segment_m))
    return points


def step(points: Sequence[Point], speed_kmh: float, maneuver: str = "STRAIGHT") -> dict[str, Any]:
    length_m = path_length_m(points)
    return {
        "distanceMeters": round(length_m),
        "staticDuration": f"{round(length_m / (speed_kmh / 3.6))}s",
        "polyline": {"encodedPolyline": encode_polyline(points)},
        "navigationInstruction": {"maneuver": maneuver},
    }


def route(
    steps: Sequence[dict[str, Any]],
    *,
    traffic_delay_s: int = 0,
    labels: Sequence[str] = ("DEFAULT_ROUTE",),
    description: str = "",
) -> dict[str, Any]:
    static_s = sum(int(s["staticDuration"][:-1]) for s in steps)
    points = [point for s in steps for point in decode_polyline(s["polyline"]["encodedPolyline"])]
    return {
        "distanceMeters": sum(s["distanceMeters"] for s in steps),
        "duration": f"{static_s + traffic_delay_s}s",
        "staticDuration": f"{static_s}s",
        "description": description,
        "routeLabels": list(labels),
        "polyline": {"encodedPolyline": encode_polyline(points)},
        "legs": [{"steps": list(steps)}],
    }


def highway_route(**kwargs: Any) -> dict[str, Any]:
    """22 km, 15 min: 1 km local, 20 km at 100 km/h, 1 km local. Two decision maneuvers."""
    a = straight(START, 135, 1_000)
    b = straight(a[-1], 135, 20_000, segment_m=500)
    c = straight(b[-1], 135, 1_000)
    kwargs.setdefault("description", "US-101 S")
    return route([step(a, 40, "DEPART"), step(b, 100, "RAMP_RIGHT"), step(c, 40, "TURN_RIGHT")], **kwargs)


def local_route(**kwargs: Any) -> dict[str, Any]:
    """18 km at 55 km/h (19.6 min) on straight arterials, five turns: two of them lefts."""
    bearings = (90, 180, 90, 180, 90, 180)
    maneuvers = ("DEPART", "TURN_RIGHT", "TURN_LEFT", "TURN_RIGHT", "TURN_LEFT", "TURN_RIGHT")
    steps, cursor = [], START
    for bearing, maneuver in zip(bearings, maneuvers, strict=True):
        points = straight(cursor, bearing, 3_000)
        steps.append(step(points, 55, maneuver))
        cursor = points[-1]
    kwargs.setdefault("labels", ("DEFAULT_ROUTE_ALTERNATE",))
    kwargs.setdefault("description", "El Camino Real")
    return route(steps, **kwargs)


JUNCTION: Point = straight(START, 0, 1_000)[-1]


def junction_route(maneuver: str, *, speed_in: float = 55, speed_out: float = 55, **kwargs: Any) -> dict[str, Any]:
    """Two 1 km roads meeting at JUNCTION; the second starts with maneuver."""
    a = straight(START, 0, 1_000)
    b = straight(a[-1], 270 if "LEFT" in maneuver else 90, 1_000)
    return route([step(a, speed_in, "DEPART"), step(b, speed_out, maneuver)], **kwargs)


def winding_route(**kwargs: Any) -> dict[str, Any]:
    """16 km, 21.5 min: 15 km of winding road at 45 km/h between two short straights."""
    a = straight(START, 200, 500)
    b = zigzag(a[-1], 160, 15_000)
    c = straight(b[-1], 160, 500)
    kwargs.setdefault("labels", ("DEFAULT_ROUTE_ALTERNATE",))
    kwargs.setdefault("description", "Skyline Blvd")
    return route([step(a, 40, "DEPART"), step(b, 45, "TURN_LEFT"), step(c, 40, "TURN_RIGHT")], **kwargs)
