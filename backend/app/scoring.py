"""Personalized cost scoring: the algorithmic core of the engine.

Pure functions only, with no I/O and no Google types. Given candidate routes
and a profile, produce a ranked list. Feed it cached data to tune weights for
free.

Two ways to rank (ScoringProfile.ranking):

* weighted: every factor is converted to perceived minutes and summed; the
  lowest total wins.
* priority: safety first, then fuel, then time. The route with the fewest
  risk points (risky turns, lane closures, school zones during school hours,
  railroad crossings, weather hazards, driving into a low sun) wins; among equally safe routes, the one
  using the least fuel (within fuel_tolerance, since the fuel model is
  rough); among those, the fastest. Each route gets a short reason for its
  place.

Either way, routes slower than max_detour_ratio times the fastest (or more
than max_extra_min slower, when the driver sets that), and routes through a
closed road, are shown but can't win. (If every route goes through
a closure, the closure data is more likely wrong than the road: none is
ruled out for it.)
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.models import SIGNALED_CROSSING_SHARE, CandidateRoute, RouteFeatures, ScoreBreakdown, ScoredRoute, ScoringProfile
from app.road_events import WORKERS_PRESENT_FACTOR
from app.sun import DARK_TURN_FACTOR

_EPSILON = 1e-9


def score_route(features: RouteFeatures, profile: ScoringProfile) -> ScoreBreakdown:
    """Personalized cost of one route, in perceived minutes. Lower is better."""
    night = DARK_TURN_FACTOR if features.after_dark else 1.0  # turns across traffic, judged by headlights
    terms = {
        "time": profile.time_weight * features.duration_min,
        "distance": profile.distance_weight * features.distance_km,
        "fuel": profile.fuel_weight * features.fuel_l,
        "highway": profile.highway_weight * features.highway_min,
        "traffic": profile.traffic_weight * features.traffic_delay_min,
        "maneuvers": profile.maneuver_weight * features.maneuver_count,
        "busy_crossings": night
        * profile.busy_crossing_weight
        * (features.busy_crossing_turns - (1 - SIGNALED_CROSSING_SHARE) * features.signaled_busy_crossing_turns),
        "hotspot_crossings": night * profile.hotspot_crossing_weight * features.hotspot_crossing_turns,
        "lane_closures": profile.lane_closure_weight
        * (features.lane_closures + (WORKERS_PRESENT_FACTOR - 1) * features.lane_closures_with_workers),
        "school_zones": profile.school_zone_weight * features.school_zones,
        "rail_crossings": profile.rail_crossing_weight * features.rail_crossings,
        "weather_hazards": profile.weather_hazard_weight * features.weather_hazards,
        "sun_glare": profile.sun_glare_weight * features.sun_glare_min,
        "scenic": -profile.scenic_weight * features.winding_min,
    }
    return ScoreBreakdown(**terms, total=sum(terms.values()))


def risk(route: ScoredRoute) -> float:
    """Safety cost: risky turns (busy and crash-prone junctions), lane closures, school zones, railroad
    crossings, weather hazards and sun glare."""
    return _risk_terms(route.breakdown)


# The ScoreBreakdown terms that are risks: ranking, the safety score and its breakdown all count these.
_RISK_TERMS = (
    "busy_crossings",
    "hotspot_crossings",
    "lane_closures",
    "school_zones",
    "rail_crossings",
    "weather_hazards",
    "sun_glare",
)


def _risk_terms(b: ScoreBreakdown) -> float:
    return sum(getattr(b, term) for term in _RISK_TERMS)


# The safety score weighs risks the same way for every profile (the default's
# weights), so "92" means the same on the fastest profile as on the calm one. A
# route with no risks scores 100; SAFETY_SCALE risk points halve it; a closed
# road on the way counts as CLOSURE_RISK more.
SAFETY_REFERENCE = ScoringProfile()
SAFETY_SCALE = 10.0
CLOSURE_RISK = 30.0


def safety_score(features: RouteFeatures) -> int:
    """0-100, higher is safer: one number for a route's risks, comparable across routes and trips."""
    points = sum(_safety_points(features).values())
    return round(100 / (1 + points / SAFETY_SCALE))


def safety_risks(features: RouteFeatures) -> dict[str, float]:
    """The risk points behind a route's safety score, by kind: only the kinds the route has."""
    return {kind: round(points, 2) for kind, points in _safety_points(features).items() if points > _EPSILON}


def _safety_points(features: RouteFeatures) -> dict[str, float]:
    b = score_route(features, SAFETY_REFERENCE)
    return {**{term: getattr(b, term) for term in _RISK_TERMS}, "closures": CLOSURE_RISK * features.closures}


# How reasons name the risks, for the kinds any candidate has.
_RISK_NAMES = (
    ("lane_closures", "lane closures"),
    ("school_zones", "school zones"),
    ("rail_crossings", "railroad crossings"),
    ("weather_hazards", "weather hazards"),
    ("sun_glare_min", "sun glare"),
)


def _risk_words(scored: Sequence[ScoredRoute]) -> str:
    names = ["risky turns"] + [name for field, name in _RISK_NAMES if any(getattr(r.features, field) for r in scored)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def rank_candidates(candidates: Sequence[CandidateRoute], profile: ScoringProfile) -> list[ScoredRoute]:
    """Score every candidate and sort them best first.

    Candidates much slower than the fastest open one, or through a closed
    road, are kept, so the UI can still show them, but marked ineligible and
    sorted after every eligible route. The fastest open route is always
    eligible, so for non-empty input the first element is the winner.
    """
    if not candidates:
        return []

    honor_closures = not all(c.features.closures for c in candidates)
    open_routes = [c for c in candidates if not (honor_closures and c.features.closures)]
    ceiling = _time_limit(min(c.features.duration_min for c in open_routes), profile)
    scored = [
        ScoredRoute(
            **candidate.model_dump(),
            breakdown=score_route(candidate.features, profile),
            eligible=candidate in open_routes and candidate.features.duration_min <= ceiling,
            safety_score=safety_score(candidate.features),
            safety_risks=safety_risks(candidate.features),
        )
        for candidate in candidates
    ]
    if profile.ranking == "priority":
        return _rank_by_priority(scored, profile)
    # Stable sort: on a tie the earlier candidate wins, and Google's own routes come first.
    scored.sort(key=lambda route: (not route.eligible, route.breakdown.total))
    return scored


def _time_limit(fastest_min: float, profile: ScoringProfile) -> float:
    """The longest trip time that can still win: max_detour_ratio times the fastest, or max_extra_min more if less."""
    limit = fastest_min * profile.max_detour_ratio
    return limit if profile.max_extra_min is None else min(limit, fastest_min + profile.max_extra_min)


def _limit_words(fastest_min: float, profile: ScoringProfile) -> tuple[str, str]:
    """How reasons name the time limit that applies: (over it, within it)."""
    if profile.max_extra_min is not None and fastest_min + profile.max_extra_min < fastest_min * profile.max_detour_ratio:
        extra = f"{profile.max_extra_min:g} min"
        return f"over {extra} longer than the fastest route", f"within {extra} of the fastest time"
    ratio = f"{profile.max_detour_ratio:g}x"
    return f"over {ratio} the fastest route's time", f"within {ratio} the fastest time"


def _rank_by_priority(scored: list[ScoredRoute], profile: ScoringProfile) -> list[ScoredRoute]:
    eligible = [route for route in scored if route.eligible]
    ranked: list[ScoredRoute] = []
    remaining = list(eligible)
    while remaining:
        best = _priority_pick(remaining, profile)
        ranked.append(best)
        remaining = [route for route in remaining if route.id != best.id]
    ranked += sorted((route for route in scored if not route.eligible), key=lambda route: route.features.duration_min)

    winner = ranked[0]
    risky = _risk_words(scored)
    limit = _limit_words(min(route.features.duration_min for route in eligible), profile)
    return [
        route.model_copy(update={"reason": _priority_reason(route, winner, eligible, profile, risky, scored, limit)})
        for route in ranked
    ]


def _priority_pick(pool: list[ScoredRoute], profile: ScoringProfile) -> ScoredRoute:
    safest = _within(pool, risk, 0.0)
    thriftiest = _within(safest, _fuel, profile.fuel_tolerance)
    return min(thriftiest, key=lambda route: route.features.duration_min)  # first on a tie: input order


def _priority_reason(
    route: ScoredRoute,
    winner: ScoredRoute,
    eligible: list[ScoredRoute],
    profile: ScoringProfile,
    risky: str,
    all_routes: Sequence[ScoredRoute] = (),
    limit: tuple[str, str] = ("", ""),
) -> str:
    over, within = limit
    if not route.eligible:
        if route.features.closures:
            return "goes through a closed road"
        return f"too slow: {over}"
    if route.id == winner.id:
        safest = _within(eligible, risk, 0.0)
        slower_but_safer = any(
            not other.eligible and not other.features.closures and risk(other) < risk(route) - _EPSILON
            for other in all_routes
        )
        if slower_but_safer:
            return f"safest {within}; safer routes take longer"
        if len(safest) == 1:
            return f"safest: fewest {risky}"
        if len(_within(safest, _fuel, profile.fuel_tolerance)) == 1:
            return "as safe as any, and uses the least fuel"
        return "as safe and fuel-efficient as any, and the fastest"
    if risk(route) > risk(winner) + _EPSILON:
        return f"more {risky} than the winner"
    if _fuel(route) > _fuel(winner) * (1 + profile.fuel_tolerance) + _EPSILON:
        return "as safe, but uses more fuel"
    return "as safe and efficient, but slower"


def _fuel(route: ScoredRoute) -> float:
    return route.features.fuel_l


def _within(pool: list[ScoredRoute], measure: Callable[[ScoredRoute], float], tolerance: float) -> list[ScoredRoute]:
    """The routes whose measure is within tolerance (a share) of the lowest."""
    lowest = min(measure(route) for route in pool)
    return [route for route in pool if measure(route) <= lowest * (1 + tolerance) + _EPSILON]
