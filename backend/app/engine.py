"""The pipeline: coordinates in, personalized ranked routes out.

This is the entry point for the HTTP API and the CLI. Two ways to fill the
candidate pool:

* build_candidates: Google's routes only, widened with avoid-highways and
  via-offset requests.
* build_router_candidates: Google's own suggestions plus routes our router
  designs on the OSM road graph, each priced by Google with live traffic.

Either pool can then be ranked under any number of profiles.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.cache import CacheMiss
from app.candidates import CandidateOptions, RouteFetcher, gather_candidates
from app.config import Settings
from app.driving_side import DrivingSide, driving_side_for_country
from app.features import TurnContext, extract_features
from app.hotspots import CrashHotspots
from app.models import CandidateRoute, LatLng, RoutePlan, ScoringProfile
from app.pricing import price_route
from app.roadgraph import RoadGraph
from app.router import OutsideGraphError, RoutedPath, Router
from app.routes_client import RoutesApiError
from app.scoring import rank_candidates


class NoRouteFoundError(LookupError):
    """Google returned no drivable route between the two points."""


@dataclass
class CandidatePool:
    candidates: list[CandidateRoute]
    notes: list[str] = field(default_factory=list)  # anything the viewer should know, e.g. a skipped route


def load_hotspots(settings: Settings) -> CrashHotspots | None:
    """Load the crash dataset named by ROUTES_CRASH_DATA, if any. Do this once at startup."""
    return CrashHotspots.from_csv(settings.crash_data_path) if settings.crash_data_path else None


def load_graph(settings: Settings) -> RoadGraph | None:
    """Load the road graph named by ROUTES_GRAPH, if it has been built. Do this once at startup."""
    return RoadGraph.load(settings.graph_path) if settings.graph_path.exists() else None


def make_turn_context(
    settings: Settings,
    hotspots: CrashHotspots | None,
    country_code: str | None = None,
) -> TurnContext:
    """Turn-judging context for one trip.

    The driving side comes from the trip's country (the frontend gets it from
    the Places selection), else ROUTES_DEFAULT_COUNTRY, else keep-right.
    """
    code = country_code or settings.default_country
    return TurnContext(
        driving_side=driving_side_for_country(code) if code else DrivingSide.RIGHT,
        hotspots=hotspots,
        min_hotspot_crashes=settings.crash_min_count,
    )


def build_candidates(
    fetcher: RouteFetcher,
    origin: LatLng,
    destination: LatLng,
    options: CandidateOptions = CandidateOptions(),
    context: TurnContext = TurnContext(),
) -> list[CandidateRoute]:
    """Fetch Google's candidate routes and compute their features."""
    raw = gather_candidates(fetcher, origin, destination, options)
    if not raw:
        raise NoRouteFoundError(
            f"No drivable route from {origin.lat},{origin.lng} to {destination.lat},{destination.lng}"
        )
    return [
        _candidate(c.id, c.source, c.route, context, live_traffic=options.traffic_aware) for c in raw
    ]


def build_router_candidates(
    fetcher: RouteFetcher,
    router: Router,
    origin: LatLng,
    destination: LatLng,
    profiles: Sequence[ScoringProfile],
    *,
    alternatives: int = 3,
    price: bool = True,
) -> CandidatePool:
    """Google's suggestions plus our router's designs, all with live traffic when price is True.

    With one profile the router proposes up to `alternatives` routes for it.
    With several, it proposes each profile's best route, and the pool is
    shared. Without pricing, our routes keep the graph's no-traffic estimates,
    Google's suggestions are fetched without traffic, and they're optional:
    if Google can't be asked (no API key, nothing recorded), our routes still
    come back, with a note.
    """
    pool = CandidatePool([])
    _add_google_suggestions(pool, fetcher, router.context, origin, destination, price)
    try:
        paths = _unique_paths(router, origin, destination, profiles, alternatives if len(profiles) == 1 else 1)
    except OutsideGraphError as exc:
        pool.notes.append(f"Own router skipped: {exc}.")
        paths = []
    _add_own_routes(pool, fetcher, router, origin, destination, paths, price)

    if not pool.candidates:
        raise NoRouteFoundError(" ".join(pool.notes) or "No route found.")
    return pool


def _add_google_suggestions(
    pool: CandidatePool,
    fetcher: RouteFetcher,
    context: TurnContext,
    origin: LatLng,
    destination: LatLng,
    price: bool,
) -> None:
    options = CandidateOptions(include_avoid_highways=False, via_offset_fractions=(), traffic_aware=price)
    try:
        pool.candidates += build_candidates(fetcher, origin, destination, options, context)
    except (RoutesApiError, CacheMiss, NoRouteFoundError) as exc:
        if price:
            raise  # pricing our routes needs Google too, so there would be nothing to show
        pool.notes.append(f"Google's suggestions skipped: {exc}")


def _add_own_routes(
    pool: CandidatePool,
    fetcher: RouteFetcher,
    router: Router,
    origin: LatLng,
    destination: LatLng,
    paths: Sequence[RoutedPath],
    price: bool,
) -> None:
    context = router.context

    seen = {candidate.encoded_polyline for candidate in pool.candidates}
    for number, path in enumerate(paths, start=1):
        candidate_id = f"own-{number}"
        design_match = None
        if price:
            try:
                priced = price_route(fetcher, router, origin, destination, path)
            except (RoutesApiError, CacheMiss) as exc:
                pool.notes.append(f"{candidate_id} not priced: {exc}")
                continue
            if priced is None:
                pool.notes.append(f"{candidate_id} not priced: Google found no route through its points.")
                continue
            route, design_match = priced.route, priced.design_match
        else:
            route = router.to_route_json(path)

        polyline = route.get("polyline", {}).get("encodedPolyline")
        if not polyline or polyline in seen:
            pool.notes.append(f"{candidate_id} came back identical to another candidate.")
            continue
        seen.add(polyline)
        pool.candidates.append(
            _candidate(candidate_id, "own", route, context, live_traffic=price, design_match=design_match)
        )


def plan_from_candidates(candidates: Sequence[CandidateRoute], profile: ScoringProfile) -> RoutePlan:
    """Rank already-fetched candidates under one profile."""
    ranked = rank_candidates(candidates, profile)
    return RoutePlan(profile=profile, winner=ranked[0], candidates=ranked)


def plan_route(
    fetcher: RouteFetcher,
    origin: LatLng,
    destination: LatLng,
    profile: ScoringProfile,
    options: CandidateOptions = CandidateOptions(),
    context: TurnContext = TurnContext(),
) -> RoutePlan:
    """Fetch Google's candidates and pick the best one for this profile."""
    return plan_from_candidates(build_candidates(fetcher, origin, destination, options, context), profile)


def _unique_paths(
    router: Router,
    origin: LatLng,
    destination: LatLng,
    profiles: Sequence[ScoringProfile],
    per_profile: int,
) -> list[RoutedPath]:
    paths: dict[tuple[int, ...], RoutedPath] = {}
    for profile in profiles:
        for path in router.routes(origin.as_tuple(), destination.as_tuple(), profile, count=per_profile):
            paths.setdefault(path.edges, path)
    return list(paths.values())


def _candidate(
    candidate_id: str,
    source: str,
    route: dict[str, Any],
    context: TurnContext,
    *,
    live_traffic: bool,
    design_match: float | None = None,
) -> CandidateRoute:
    return CandidateRoute(
        id=candidate_id,
        source=source,
        description=route.get("description", ""),
        encoded_polyline=route["polyline"]["encodedPolyline"],
        # Only the plain request's default counts. Other requests label their own default.
        is_google_default=source == "google" and "DEFAULT_ROUTE" in route.get("routeLabels", []),
        live_traffic=live_traffic,
        design_match=design_match,
        features=extract_features(route, context),
    )
