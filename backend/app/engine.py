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
from datetime import datetime, time
from typing import Any

from app.cache import CacheMiss
from app.candidates import CandidateOptions, RawCandidate, RouteFetcher, gather_candidates
from app.config import Settings
from app.driving_side import DrivingSide, driving_side_for_country
from app.features import TurnContext, directions, extract_features
from app.geo import Point, decode_polyline
from app.hotspots import CrashHotspots
from app.models import CandidateRoute, LatLng, RoutePlan, ScoredRoute, ScoringProfile
from app.pricing import design_match, price_route
from app.road_events import RoadEvent, jams_on_routes
from app.roadgraph import RoadGraph
from app.router import OutsideGraphError, RoutedPath, Router
from app.routes_client import RoutesApiError
from app.scoring import rank_candidates

# A route is left out of the plan when more than this share of it runs along a
# better-ranked one: on the map it would look like the same route. The winner
# and the standard route (the baseline the winner is compared with) always stay.
MAX_SHARED_WITH_BETTER = 0.7


class NoRouteFoundError(LookupError):
    """Google returned no drivable route between the two points."""


@dataclass
class CandidatePool:
    candidates: list[CandidateRoute]
    notes: list[str] = field(default_factory=list)  # anything the viewer should know, e.g. a skipped route
    events: list[RoadEvent] = field(default_factory=list)  # slowdowns Google reported along its suggestions


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
    graph: RoadGraph | None = None,
    *,
    now: datetime | None = None,
) -> TurnContext:
    """Turn-judging context for one trip, leaving now (local time; the clock's by default).

    The driving side comes from the trip's country (the frontend gets it from
    the Places selection), else ROUTES_DEFAULT_COUNTRY, else keep-right. With
    the road graph, every route's turns, Google's included, are judged by the
    roads meeting at each junction, and its school zones count during school hours.
    """
    code = country_code or settings.default_country
    return TurnContext(
        driving_side=driving_side_for_country(code) if code else DrivingSide.RIGHT,
        hotspots=hotspots,
        min_hotspot_crashes=settings.crash_min_count,
        junctions=graph,
        school_hours=school_in_session(now or datetime.now(), settings.school_hours),
    )


def school_in_session(now: datetime, windows: Sequence[tuple[time, time]]) -> bool:
    """Whether school zones count at now: in one of the windows on a weekday, outside July and August.

    A simplification: school holidays and each district's own calendar aren't known.
    """
    if now.weekday() >= 5 or now.month in (7, 8):
        return False
    return any(start <= now.time() <= end for start, end in windows)


def build_candidates(
    fetcher: RouteFetcher,
    origin: LatLng,
    destination: LatLng,
    options: CandidateOptions = CandidateOptions(),
    context: TurnContext = TurnContext(),
) -> list[CandidateRoute]:
    """Fetch Google's candidate routes and compute their features."""
    return [
        _candidate(c.id, c.source, c.route, context, live_traffic=options.traffic_aware)
        for c in _fetch_google(fetcher, origin, destination, options)
    ]


def _fetch_google(
    fetcher: RouteFetcher, origin: LatLng, destination: LatLng, options: CandidateOptions
) -> list[RawCandidate]:
    raw = gather_candidates(fetcher, origin, destination, options)
    if not raw:
        raise NoRouteFoundError(
            f"No drivable route from {origin.lat},{origin.lng} to {destination.lat},{destination.lng}"
        )
    return raw


def build_router_candidates(
    fetcher: RouteFetcher,
    router: Router,
    origin: LatLng,
    destination: LatLng,
    profiles: Sequence[ScoringProfile],
    *,
    alternatives: int = 3,
    price: bool = True,
    heading: int | None = None,
    context: TurnContext | None = None,
    traffic_on_polyline: bool = False,
    baseline: ScoringProfile | None = None,
) -> CandidatePool:
    """Google's suggestions plus our router's designs, all with live traffic when price is True.

    With one profile the router proposes up to `alternatives` routes for it:
    its best design, then alternatives that take other corridors than that
    design and Google's suggestions. With several, it proposes each profile's
    best route, and the pool is shared. Without pricing, our routes keep the
    graph's no-traffic estimates, Google's suggestions are fetched without
    traffic, and they're optional: if Google can't be asked (no API key,
    nothing recorded), our routes still come back, with a note.

    heading is the direction a moving car is driving (degrees clockwise from
    north): every route then starts along its road instead of turning around.

    context is the trip's turn context, the router's own by default. Its road
    events are steered around and counted on every route. With
    traffic_on_polyline (and price), Google's suggestions also report their
    slow and jammed stretches: our designs avoid them, and pool.events lists them.

    With a baseline profile, Google isn't asked at all (fetcher may be None,
    price must be False): the standard route to compare with is our router's
    best route for the baseline, e.g. the fastest. That's how the
    OpenStreetMap map runs, since Google's terms don't allow its results there.
    """
    context = context or router.context
    pool = CandidatePool([])
    if baseline is None:
        _add_google_suggestions(pool, fetcher, context, origin, destination, price, heading, traffic_on_polyline and price)
    else:
        _add_baseline(pool, router, context, origin, destination, baseline, heading)
    on_offer = [decode_polyline(candidate.encoded_polyline) for candidate in pool.candidates]
    per_profile = alternatives if len(profiles) == 1 else 1
    try:
        paths = _unique_paths(
            router,
            origin,
            destination,
            profiles,
            per_profile,
            avoid=on_offer,
            heading=heading,
            events=[*context.events, *pool.events],
            school_hours=context.school_hours,
        )
    except OutsideGraphError as exc:
        pool.notes.append(f"Own router skipped: {exc}.")
        paths = []
    _add_own_routes(pool, fetcher, router, context, origin, destination, paths, price, heading)

    if not pool.candidates:
        raise NoRouteFoundError(" ".join(pool.notes) or "No route found.")
    return pool


def _add_baseline(
    pool: CandidatePool,
    router: Router,
    context: TurnContext,
    origin: LatLng,
    destination: LatLng,
    profile: ScoringProfile,
    heading: int | None,
) -> None:
    """Our router's best route for profile, as the standard route: the baseline without Google."""
    try:
        found = router.routes(
            origin.as_tuple(),
            destination.as_tuple(),
            profile,
            count=1,
            heading=heading,
            events=context.events,
            school_hours=context.school_hours,
        )
    except OutsideGraphError as exc:
        pool.notes.append(f"Standard route skipped: {exc}.")
        return
    if found:
        route = router.to_route_json(found[0])
        pool.candidates.append(_candidate("standard", "baseline", route, context, live_traffic=False, standard=True))


def _add_google_suggestions(
    pool: CandidatePool,
    fetcher: RouteFetcher,
    context: TurnContext,
    origin: LatLng,
    destination: LatLng,
    price: bool,
    heading: int | None,
    traffic_on_polyline: bool,
) -> None:
    options = CandidateOptions(
        include_avoid_highways=False,
        via_offset_fractions=(),
        traffic_aware=price,
        origin_heading=heading,
        traffic_on_polyline=traffic_on_polyline,
    )
    try:
        raw = _fetch_google(fetcher, origin, destination, options)
    except (RoutesApiError, CacheMiss, NoRouteFoundError) as exc:
        if price:
            raise  # pricing our routes needs Google too, so there would be nothing to show
        pool.notes.append(f"Google's suggestions skipped: {exc}")
        return
    pool.candidates += [_candidate(c.id, c.source, c.route, context, live_traffic=price) for c in raw]
    if traffic_on_polyline:
        pool.events += jams_on_routes(c.route for c in raw)


def _add_own_routes(
    pool: CandidatePool,
    fetcher: RouteFetcher,
    router: Router,
    context: TurnContext,
    origin: LatLng,
    destination: LatLng,
    paths: Sequence[RoutedPath],
    price: bool,
    heading: int | None,
) -> None:
    seen = {candidate.encoded_polyline for candidate in pool.candidates}
    for number, path in enumerate(paths, start=1):
        candidate_id = f"own-{number}"
        match = None
        if price:
            try:
                priced = price_route(fetcher, router, origin, destination, path, origin_heading=heading)
            except (RoutesApiError, CacheMiss) as exc:
                pool.notes.append(f"{candidate_id} not priced: {exc}")
                continue
            if priced is None:
                pool.notes.append(f"{candidate_id} not priced: Google found no route through its points.")
                continue
            route, match = priced.route, priced.design_match
            if priced.attempts > 1:
                pool.notes.append(
                    f"{candidate_id}: Google strayed from the design at first; the skipped stretch was pinned "
                    f"and it was priced again ({priced.design_match:.0%} match)."
                )
        else:
            route = router.to_route_json(path)

        polyline = route.get("polyline", {}).get("encodedPolyline")
        if not polyline or polyline in seen:
            pool.notes.append(f"{candidate_id} came back identical to another candidate.")
            continue
        seen.add(polyline)
        pool.candidates.append(
            _candidate(candidate_id, "own", route, context, live_traffic=price, design_match=match)
        )


def plan_from_candidates(candidates: Sequence[CandidateRoute], profile: ScoringProfile) -> RoutePlan:
    """Rank already-fetched candidates under one profile, leaving out near-copies of better routes."""
    ranked = rank_candidates(candidates, profile)
    points = {route.id: decode_polyline(route.encoded_polyline) for route in ranked}
    kept: list[ScoredRoute] = []
    hidden: dict[str, str] = {}
    for route in ranked:
        followed = None if not kept or route.is_standard else _mostly_follows(route, kept, points)
        if followed is None:
            kept.append(route)
        else:
            hidden[route.id] = followed
    return RoutePlan(profile=profile, winner=ranked[0], candidates=kept, hidden=hidden)


def _mostly_follows(route: ScoredRoute, better: Sequence[ScoredRoute], points: dict[str, list[Point]]) -> str | None:
    """Id of the first better route that route mostly runs along, or mostly contains (plus a loop), if any."""
    mine = points[route.id]
    for other in better:
        theirs = points[other.id]
        if max(design_match(theirs, mine), design_match(mine, theirs)) > MAX_SHARED_WITH_BETTER:
            return other.id
    return None


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
    *,
    avoid: Sequence[Sequence[Point]],
    heading: int | None,
    events: Sequence[RoadEvent],
    school_hours: bool,
) -> list[RoutedPath]:
    paths: dict[tuple[int, ...], RoutedPath] = {}
    for profile in profiles:
        found = router.routes(
            origin.as_tuple(),
            destination.as_tuple(),
            profile,
            count=per_profile,
            avoid=avoid,
            heading=heading,
            events=events,
            school_hours=school_hours,
        )
        for path in found:
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
    standard: bool = False,
) -> CandidateRoute:
    # Only the plain request's default counts. Other requests label their own default.
    google_default = source == "google" and "DEFAULT_ROUTE" in route.get("routeLabels", [])
    return CandidateRoute(
        id=candidate_id,
        source=source,
        description=route.get("description", ""),
        encoded_polyline=route["polyline"]["encodedPolyline"],
        is_google_default=google_default,
        is_standard=standard or google_default,
        live_traffic=live_traffic,
        design_match=design_match,
        features=extract_features(route, context),
        maneuvers=directions(route, context),
    )
