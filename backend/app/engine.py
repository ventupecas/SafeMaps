"""The pipeline: coordinates in, personalized ranked routes out.

This is the entry point for the HTTP API and the CLI. Fetching candidates
doesn't depend on the profile, so one set of candidates can be ranked under
any number of profiles without extra API calls.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.candidates import CandidateOptions, RawCandidate, RouteFetcher, gather_candidates
from app.config import Settings
from app.driving_side import DrivingSide, driving_side_for_country
from app.features import TurnContext, extract_features
from app.hotspots import CrashHotspots
from app.models import CandidateRoute, LatLng, RoutePlan, ScoringProfile
from app.scoring import rank_candidates


class NoRouteFoundError(LookupError):
    """Google returned no drivable route between the two points."""


def load_hotspots(settings: Settings) -> CrashHotspots | None:
    """Load the crash dataset named by ROUTES_CRASH_DATA, if any. Do this once at startup."""
    return CrashHotspots.from_csv(settings.crash_data_path) if settings.crash_data_path else None


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
    """Fetch candidate routes and compute their features."""
    raw = gather_candidates(fetcher, origin, destination, options)
    if not raw:
        raise NoRouteFoundError(
            f"No drivable route from {origin.lat},{origin.lng} to {destination.lat},{destination.lng}"
        )
    return [_to_candidate(candidate, context) for candidate in raw]


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
    """Fetch candidates and pick the best one for this profile."""
    return plan_from_candidates(build_candidates(fetcher, origin, destination, options, context), profile)


def _to_candidate(raw: RawCandidate, context: TurnContext) -> CandidateRoute:
    route = raw.route
    return CandidateRoute(
        id=raw.id,
        source=raw.source,
        description=route.get("description", ""),
        encoded_polyline=route["polyline"]["encodedPolyline"],
        # Only the plain request's default counts. The avoid-highways request has its own default.
        is_google_default=raw.source == "google" and "DEFAULT_ROUTE" in route.get("routeLabels", []),
        features=extract_features(route, context),
    )
