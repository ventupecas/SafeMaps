"""Personalized cost scoring: the algorithmic core of the engine.

Pure functions only, with no I/O and no Google types. Given candidate routes
and a profile, produce a ranked list. Feed it cached data to tune weights for
free.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models import CandidateRoute, RouteFeatures, ScoreBreakdown, ScoredRoute, ScoringProfile


def score_route(features: RouteFeatures, profile: ScoringProfile) -> ScoreBreakdown:
    """Personalized cost of one route, in perceived minutes. Lower is better."""
    terms = {
        "time": profile.time_weight * features.duration_min,
        "distance": profile.distance_weight * features.distance_km,
        "highway": profile.highway_weight * features.highway_min,
        "traffic": profile.traffic_weight * features.traffic_delay_min,
        "maneuvers": profile.maneuver_weight * features.maneuver_count,
        "busy_crossings": profile.busy_crossing_weight * features.busy_crossing_turns,
        "hotspot_crossings": profile.hotspot_crossing_weight * features.hotspot_crossing_turns,
        "scenic": -profile.scenic_weight * features.winding_min,
    }
    return ScoreBreakdown(**terms, total=sum(terms.values()))


def rank_candidates(candidates: Sequence[CandidateRoute], profile: ScoringProfile) -> list[ScoredRoute]:
    """Score every candidate and sort them best first.

    Candidates much slower than the fastest are kept, so the UI can still show
    them, but marked ineligible and sorted after every eligible route. The
    fastest route is always eligible, so for non-empty input the first element
    is the winner.
    """
    if not candidates:
        return []

    ceiling = min(c.features.duration_min for c in candidates) * profile.max_detour_ratio
    scored = [
        ScoredRoute(
            **candidate.model_dump(),
            breakdown=score_route(candidate.features, profile),
            eligible=candidate.features.duration_min <= ceiling,
        )
        for candidate in candidates
    ]
    # Stable sort: on a tie the earlier candidate wins, and Google's own routes come first.
    scored.sort(key=lambda route: (not route.eligible, route.breakdown.total))
    return scored
