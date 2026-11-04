"""Price the engine's own routes with Google's live traffic.

For each route the router designs, ask the Routes API for a traffic-aware
route that passes through points along it. Google's answer is what gets
scored and drawn: its live travel time, its turn-by-turn steps and its
polyline. Nothing from Google is written back into the road graph, and the
response is only cached briefly (see RoutesClient).

Google takes the quickest path between pins, so it can stray from the design,
often through the very left turns the design avoided. When it does, the
stretch it skipped gets a pin and Google is asked once more.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

import app.geo_kernel as kernel
from app.cache import CacheMiss
from app.candidates import RouteFetcher
from app.features import HIGHWAY_SPEED_KMH
from app.geo import Point, columns, decode_polyline, point_along
from app.models import LatLng
from app.roadgraph import RoadGraph
from app.router import RoutedPath, Router
from app.routes_client import RouteRequest, RoutesApiError

MAX_VIA_POINTS = 25  # the Routes API's limit on intermediate waypoints
# Stretches without turns need pins too. On streets a gap of more than a few
# blocks invites a shortcut; freeways have few alternatives.
STREET_PIN_SPACING_M = 800.0
FREEWAY_PIN_SPACING_M = 3_000.0
MIN_VIA_SEGMENT_M = 40.0  # on shorter segments Google may snap to the cross street
# Never pin these: Google may not route through alleys and parking-lot
# connectors, and a pin there makes it loop around the block to pass it.
UNPINNABLE_CLASSES = frozenset({"service"})
MATCH_TOLERANCE_M = 30.0
# Below this match, pin what Google skipped and ask once more (one extra request).
RETRY_BELOW_MATCH = 0.99
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
    attempts: int = 1  # 2 when Google strayed and was asked again


def via_points(graph: RoadGraph, path: RoutedPath, limit: int = MAX_VIA_POINTS) -> list[ViaPoint]:
    """Points that pin Google to the designed path: one just after each turn, plus spacers on straight stretches."""
    return [_midpoint(graph, path.edges[index]) for index in _pin_indexes(graph, path, limit)]


def price_route(
    fetcher: RouteFetcher,
    router: Router,
    origin: LatLng,
    destination: LatLng,
    path: RoutedPath,
    *,
    origin_heading: int | None = None,
) -> PricedRoute | None:
    """Ask Google for a live-traffic route along path; None if Google found no route.

    origin_heading is the direction a moving car is driving (see RouteRequest).
    """
    graph = router.graph
    designed = router.path_points(path)
    pins = _pin_indexes(graph, path)
    ask = partial(_ask_google, fetcher, graph, origin, destination, path, designed=designed, origin_heading=origin_heading)
    priced = ask(pins)
    if priced is None or priced.design_match >= RETRY_BELOW_MATCH:
        return priced

    actual = decode_polyline(priced.route.get("polyline", {}).get("encodedPolyline", ""))
    repinned = _pin_indexes(graph, path, extra=_skipped_stretches(graph, path, actual))
    if repinned == pins:
        return priced  # nothing new to pin, so asking again would give the same answer
    try:
        retry = ask(repinned)
    except (RoutesApiError, CacheMiss):
        return priced  # keep the first answer rather than lose the route
    if retry is None or retry.design_match <= priced.design_match:
        return priced
    return PricedRoute(retry.route, retry.design_match, retry.via_count, attempts=2)


def design_match(designed: Sequence[Point], actual: Sequence[Point], tolerance_m: float = MATCH_TOLERANCE_M) -> float:
    """Share of the actual route lying within tolerance_m of the designed one."""
    if len(designed) < 2 or len(actual) < 2:
        return 0.0
    index = _NearIndex(designed, tolerance_m)
    lat, lng = kernel.densified(*columns(actual), 25.0)
    return int(kernel.share_near(index.grid, lat, lng, tolerance_m, index.reach_lng)) / lat.size


def _ask_google(
    fetcher: RouteFetcher,
    graph: RoadGraph,
    origin: LatLng,
    destination: LatLng,
    path: RoutedPath,
    pins: Sequence[int],
    *,
    designed: Sequence[Point],
    origin_heading: int | None,
) -> PricedRoute | None:
    vias = [_midpoint(graph, path.edges[index]) for index in pins]
    request = RouteRequest(
        origin=origin,
        destination=destination,
        via=tuple(via.point for via in vias),
        via_headings=tuple(via.heading for via in vias),
        origin_heading=origin_heading,
        traffic_aware=True,
    )
    routes = fetcher.compute_routes(request)
    if not routes:
        return None
    route = routes[0]
    actual = decode_polyline(route.get("polyline", {}).get("encodedPolyline", ""))
    return PricedRoute(route=route, design_match=design_match(designed, actual), via_count=len(vias))


def _pin_indexes(graph: RoadGraph, path: RoutedPath, limit: int = MAX_VIA_POINTS, extra: Iterable[int] = ()) -> list[int]:
    """Path indexes to pin, by priority: extra pins, then just after each turn, then spacers."""
    after_turns: list[int] = []
    spacers: list[int] = []
    turn_pending = False
    since_last_m = 0.0
    for index, (edge, maneuver) in enumerate(zip(path.edges, path.maneuvers, strict=True)):
        if index > 0 and maneuver != "STRAIGHT":
            turn_pending = True
        since_last_m += graph.length_m[edge]
        if not _pinnable(graph, edge):
            continue
        freeway = graph.speed_kmh[edge] >= HIGHWAY_SPEED_KMH
        if turn_pending:
            after_turns.append(index)
        elif since_last_m >= (FREEWAY_PIN_SPACING_M if freeway else STREET_PIN_SPACING_M):
            spacers.append(index)
        else:
            continue
        turn_pending = False
        since_last_m = 0.0

    picks: list[int] = []
    for group in (sorted(set(extra)), after_turns, spacers):
        candidates = [index for index in group if index not in picks]
        picks += _spread(candidates, limit - len(picks))
    return sorted(picks)


def _skipped_stretches(graph: RoadGraph, path: RoutedPath, actual: Sequence[Point]) -> list[int]:
    """One path index in the middle of each stretch of the design that the actual route doesn't follow."""
    if len(actual) < 2:
        return []
    near = _NearIndex(actual, MATCH_TOLERANCE_M)
    runs: list[list[int]] = []
    previous_off = False
    for index, edge in enumerate(path.edges):
        off = not near.near(_midpoint(graph, edge).point.as_tuple())
        if off and not previous_off:
            runs.append([])
        if off:
            runs[-1].append(index)
        previous_off = off

    pins = []
    for run in runs:
        usable = [index for index in run if _pinnable(graph, path.edges[index])]
        if usable:
            pins.append(usable[len(usable) // 2])
    return pins


def _pinnable(graph: RoadGraph, edge: int) -> bool:
    return graph.length_m[edge] >= MIN_VIA_SEGMENT_M and graph.class_name(edge) not in UNPINNABLE_CLASSES


def _spread(items: list[int], count: int) -> list[int]:
    """count items spaced evenly through items, keeping the first and last."""
    if len(items) <= count:
        return list(items)
    if count <= 1:
        return items[: max(count, 0)]
    return [items[round(k * (len(items) - 1) / (count - 1))] for k in range(count)]


def _midpoint(graph: RoadGraph, edge: int) -> ViaPoint:
    (lat, lng), bearing = point_along(graph.edge_points(edge), graph.length_m[edge] / 2)
    return ViaPoint(point=LatLng(lat=lat, lng=lng), heading=round(bearing) % 360)


class _NearIndex:
    """Answers "is any indexed point within tolerance_m of this one?" with a grid lookup."""

    def __init__(self, path: Sequence[Point], tolerance_m: float) -> None:
        """The path's points, close enough together that nothing beside it falls between them."""
        self.tolerance_m = tolerance_m
        lat, lng = kernel.densified(*columns(path), tolerance_m / 2)
        self.grid = kernel.make_grid(lat, lng, tolerance_m / _M_PER_DEG_LAT)
        # Longitude cells are narrower than tolerance_m away from the equator.
        self.reach_lng = kernel.reach_lng_at(float(lat[0]) if lat.size else 0.0)

    def near(self, point: Point) -> bool:
        return bool(kernel.any_within(self.grid, point[0], point[1], self.tolerance_m, self.reach_lng))
