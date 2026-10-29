"""Candidate generation: widen the pool of routes the scorer can choose from.

Google returns at most a few routes, and often only one. To give the
personalized scorer real choices, we ask Google in different ways:

1. The plain request, with alternatives enabled.
2. The same request with highways avoided (still the cheapest SKU).
3. If the pool is still small, a request through a "via" waypoint pushed
   sideways from the straight origin-destination line. This is the coordinate
   manipulation: we move a point, Google routes through it, and we get a
   genuinely different corridor.

Every request goes through the RoutesClient, so all of them are cached.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.geo import destination_point, haversine_m, initial_bearing_deg, midpoint
from app.models import LatLng
from app.routes_client import RouteRequest


class RouteFetcher(Protocol):
    def compute_routes(self, request: RouteRequest) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CandidateOptions:
    include_avoid_highways: bool = True
    # Sideways offsets, as a fraction of the straight-line trip length.
    # Positive is right of the direction of travel, negative is left.
    via_offset_fractions: tuple[float, ...] = (0.25, -0.25)
    max_via_offset_m: float = 15_000.0
    # Via requests stop once the pool holds this many distinct routes.
    min_candidates: int = 3
    traffic_aware: bool = False


@dataclass(frozen=True)
class RawCandidate:
    id: str
    source: str
    route: Mapping[str, Any]


def via_points(
    origin: LatLng,
    destination: LatLng,
    fractions: Sequence[float],
    max_offset_m: float,
) -> list[LatLng]:
    """Points offset at right angles from the midpoint of the origin-destination line."""
    a, b = origin.as_tuple(), destination.as_tuple()
    span_m = haversine_m(a, b)
    mid = midpoint(a, b)
    heading = initial_bearing_deg(mid, b)

    points = []
    for fraction in fractions:
        offset_m = min(abs(fraction) * span_m, max_offset_m)
        if offset_m == 0:
            continue
        side = 90.0 if fraction > 0 else -90.0
        lat, lng = destination_point(mid, heading + side, offset_m)
        points.append(LatLng(lat=lat, lng=lng))
    return points


def gather_candidates(
    fetcher: RouteFetcher,
    origin: LatLng,
    destination: LatLng,
    options: CandidateOptions = CandidateOptions(),
) -> list[RawCandidate]:
    """Collect distinct raw routes from Google, using as few requests as the options allow."""
    # Keyed by encoded polyline: different requests often return the same path.
    pool: dict[str, RawCandidate] = {}

    def add(source: str, routes: list[dict[str, Any]]) -> None:
        for route in routes:
            polyline = route.get("polyline", {}).get("encodedPolyline")
            if not polyline or polyline in pool:
                continue
            number = sum(1 for candidate in pool.values() if candidate.source == source) + 1
            pool[polyline] = RawCandidate(id=f"{source}-{number}", source=source, route=route)

    base = RouteRequest(origin=origin, destination=destination, traffic_aware=options.traffic_aware)
    add("google", fetcher.compute_routes(base))
    if not pool:
        return []  # No drivable route at all; detours won't find one either.

    if options.include_avoid_highways:
        add("avoid-highways", fetcher.compute_routes(replace(base, avoid_highways=True)))

    for point in via_points(origin, destination, options.via_offset_fractions, options.max_via_offset_m):
        if len(pool) >= options.min_candidates:
            break
        add("via-offset", fetcher.compute_routes(replace(base, via=(point,))))

    return list(pool.values())
