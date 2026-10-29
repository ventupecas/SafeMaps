"""Price the engine's own routes with Google's live traffic.

For each route the router designs, ask the Routes API for a traffic-aware
route that passes through points along it. Google's answer is what gets
scored and drawn: its live travel time, its turn-by-turn steps and its
polyline. Nothing from Google is written back into the road graph, and the
response is only cached briefly (see RoutesClient).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.candidates import RouteFetcher
from app.geo import Point, decode_polyline, densify, haversine_m, point_along
from app.models import LatLng
from app.roadgraph import RoadGraph
from app.router import RoutedPath, Router
from app.routes_client import RouteRequest

MAX_VIA_POINTS = 25  # the Routes API's limit on intermediate waypoints
FILLER_SPACING_M = 3_000.0  # also pin long stretches without turns
MIN_VIA_SEGMENT_M = 40.0  # on shorter segments Google may snap to the cross street
MATCH_TOLERANCE_M = 30.0
_M_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class ViaPoint:
    point: LatLng
    heading: int  # direction of travel, degrees clockwise from north


@dataclass(frozen=True)
class PricedRoute:
    route: dict[str, Any]  # Google's traffic-aware route through the via points
    design_match: float  # share of it that follows the designed path
    via_count: int


def via_points(graph: RoadGraph, path: RoutedPath, limit: int = MAX_VIA_POINTS) -> list[ViaPoint]:
    """Points that pin Google to the designed path: one just after each turn, plus fillers on long straights."""
    picks: list[int] = []
    turn_pending = False
    since_last_m = 0.0
    for index, (edge, maneuver) in enumerate(zip(path.edges, path.maneuvers, strict=True)):
        if index > 0 and maneuver != "STRAIGHT":
            turn_pending = True
        since_last_m += graph.length_m[edge]
        if (turn_pending or since_last_m >= FILLER_SPACING_M) and graph.length_m[edge] >= MIN_VIA_SEGMENT_M:
            picks.append(edge)
            turn_pending = False
            since_last_m = 0.0
    if len(picks) > limit:
        # Spread the allowance evenly along the route.
        picks = [picks[round(k * (len(picks) - 1) / (limit - 1))] for k in range(limit)] if limit > 1 else picks[:limit]
    return [_midpoint(graph, edge) for edge in picks]


def price_route(
    fetcher: RouteFetcher,
    router: Router,
    origin: LatLng,
    destination: LatLng,
    path: RoutedPath,
) -> PricedRoute | None:
    """Ask Google for a live-traffic route along path; None if Google found no route."""
    vias = via_points(router.graph, path)
    request = RouteRequest(
        origin=origin,
        destination=destination,
        via=tuple(via.point for via in vias),
        via_headings=tuple(via.heading for via in vias),
        traffic_aware=True,
    )
    routes = fetcher.compute_routes(request)
    if not routes:
        return None
    route = routes[0]
    actual = decode_polyline(route.get("polyline", {}).get("encodedPolyline", ""))
    return PricedRoute(route=route, design_match=design_match(router.path_points(path), actual), via_count=len(vias))


def design_match(designed: Sequence[Point], actual: Sequence[Point], tolerance_m: float = MATCH_TOLERANCE_M) -> float:
    """Share of the actual route lying within tolerance_m of the designed one."""
    if len(designed) < 2 or len(actual) < 2:
        return 0.0
    cell_deg = tolerance_m / _M_PER_DEG_LAT

    def cell(point: Point) -> tuple[int, int]:
        return math.floor(point[0] / cell_deg), math.floor(point[1] / cell_deg)

    index: dict[tuple[int, int], list[Point]] = defaultdict(list)
    for point in densify(designed, tolerance_m / 2):
        index[cell(point)].append(point)
    # Longitude cells are narrower than tolerance_m away from the equator.
    reach_lng = math.ceil(1 / max(math.cos(math.radians(designed[0][0])), 0.01))

    samples = densify(actual, 25.0)
    hits = 0
    for point in samples:
        ci, cj = cell(point)
        near = (
            other
            for i in range(ci - 1, ci + 2)
            for j in range(cj - reach_lng, cj + reach_lng + 1)
            for other in index.get((i, j), ())
        )
        if any(haversine_m(point, other) <= tolerance_m for other in near):
            hits += 1
    return hits / len(samples)


def _midpoint(graph: RoadGraph, edge: int) -> ViaPoint:
    (lat, lng), bearing = point_along(graph.edge_points(edge), graph.length_m[edge] / 2)
    return ViaPoint(point=LatLng(lat=lat, lng=lng), heading=round(bearing) % 360)
