"""Personalized cost scoring: the algorithmic core of the engine.

Pure functions only, with no I/O and no Google types. Given candidate routes
and a profile, produce a ranked list. Feed it cached data to tune weights for
free.

Two ways to rank (ScoringProfile.ranking):

* weighted: every factor is converted to perceived minutes and summed; the
  lowest total wins.
* priority: safety first, then fuel, then time. The route with the fewest
  risky turns and lane closures wins; among equally safe routes, the one
  using the least fuel (within fuel_tolerance, since the fuel model is
  rough); among those, the fastest. Each route gets a short reason for its
  place.

Either way, routes slower than max_detour_ratio times the fastest, and routes
through a closed road, are shown but can't win. (If every route goes through
a closure, the closure data is more likely wrong than the road: none is
ruled out for it.)
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.models import CandidateRoute, RouteFeatures, ScoreBreakdown, ScoredRoute, ScoringProfile

_EPSILON = 1e-9


def score_route(features: RouteFeatures, profile: ScoringProfile) -> ScoreBreakdown:
    """Personalized cost of one route, in perceived minutes. Lower is better."""
    terms = {
        "time": profile.time_weight * features.duration_min,
        "distance": profile.distance_weight * features.distance_km,
        "fuel": profile.fuel_weight * features.fuel_l,
        "highway": profile.highway_weight * features.highway_min,
        "traffic": profile.traffic_weight * features.traffic_delay_min,
        "maneuvers": profile.maneuver_weight * features.maneuver_count,
        "busy_crossings": profile.busy_crossing_weight * features.busy_crossing_turns,
        "hotspot_crossings": profile.hotspot_crossing_weight * features.hotspot_crossing_turns,
        "lane_closures": profile.lane_closure_weight * features.lane_closures,
        "scenic": -profile.scenic_weight * features.winding_min,
    }
    return ScoreBreakdown(**terms, total=sum(terms.values()))


def risk(route: ScoredRoute) -> float:
    """Safety cost: the profile's penalties for risky turns (busy and crash-prone junctions) and lane closures."""
    return route.breakdown.busy_crossings + route.breakdown.hotspot_crossings + route.breakdown.lane_closures


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
    ceiling = min(c.features.duration_min for c in open_routes) * profile.max_detour_ratio
    scored = [
        ScoredRoute(
            **candidate.model_dump(),
            breakdown=score_route(candidate.features, profile),
            eligible=candidate in open_routes and candidate.features.duration_min <= ceiling,
        )
        for candidate in candidates
    ]
    if profile.ranking == "priority":
        return _rank_by_priority(scored, profile)
    # Stable sort: on a tie the earlier candidate wins, and Google's own routes come first.
    scored.sort(key=lambda route: (not route.eligible, route.breakdown.total))
    return scored


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
    risky = "risky turns and lane closures" if any(route.features.lane_closures for route in scored) else "risky turns"
    return [
        route.model_copy(update={"reason": _priority_reason(route, winner, eligible, profile, risky)})
        for route in ranked
    ]


def _priority_pick(pool: list[ScoredRoute], profile: ScoringProfile) -> ScoredRoute:
    safest = _within(pool, risk, 0.0)
    thriftiest = _within(safest, _fuel, profile.fuel_tolerance)
    return min(thriftiest, key=lambda route: route.features.duration_min)  # first on a tie: input order


def _priority_reason(
    route: ScoredRoute, winner: ScoredRoute, eligible: list[ScoredRoute], profile: ScoringProfile, risky: str
) -> str:
    if not route.eligible:
        if route.features.closures:
            return "goes through a closed road"
        return f"too slow: over {profile.max_detour_ratio:g}x the fastest route's time"
    if route.id == winner.id:
        safest = _within(eligible, risk, 0.0)
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
