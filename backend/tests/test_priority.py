"""Priority ranking: safety first, then fuel, then time."""

import pytest

from app.models import CandidateRoute, CrossingTurn, RouteFeatures, ScoringProfile
from app.profiles import PRESETS
from app.router import PRIORITY_FUEL_BOOST, PRIORITY_SAFETY_BOOST, design_profile
from app.scoring import rank_candidates

PRIORITY = ScoringProfile(ranking="priority")


def turn(*, hotspot: bool = False) -> CrossingTurn:
    return CrossingTurn(location=None, maneuver="TURN_LEFT", road_speed_kmh=60, nearby_crashes=5 if hotspot else 0, busy=True, hotspot=hotspot)


def route(route_id: str, *, minutes: float, fuel: float, busy_lefts: int = 0, crash_lefts: int = 0) -> CandidateRoute:
    turns = tuple(turn() for _ in range(busy_lefts)) + tuple(turn(hotspot=True) for _ in range(crash_lefts))
    features = RouteFeatures(
        distance_km=20,
        duration_min=minutes,
        static_duration_min=minutes,
        highway_min=0,
        highway_km=0,
        traffic_delay_min=0,
        maneuver_count=len(turns),
        winding_min=0,
        fuel_l=fuel,
        crossing_turns=turns,
    )
    return CandidateRoute(id=route_id, source="google", encoded_polyline=route_id, features=features)


def ranked_ids(candidates, profile=PRIORITY) -> list[str]:
    return [r.id for r in rank_candidates(candidates, profile)]


def test_safety_beats_fuel_and_time():
    risky = route("risky", minutes=25, fuel=2.0, busy_lefts=1)
    safe = route("safe", minutes=35, fuel=2.6)
    assert ranked_ids([risky, safe]) == ["safe", "risky"]


def test_crash_prone_turns_count_as_less_safe_than_busy_ones():
    crash = route("crash", minutes=25, fuel=2.0, crash_lefts=1)
    busy = route("busy", minutes=30, fuel=2.2, busy_lefts=1)
    assert ranked_ids([crash, busy])[0] == "busy"


def test_fuel_beats_time_among_equally_safe_routes():
    quick = route("quick", minutes=25, fuel=2.4)
    thrifty = route("thrifty", minutes=32, fuel=2.0)
    assert ranked_ids([quick, thrifty]) == ["thrifty", "quick"]


def test_fuel_within_tolerance_is_a_tie_so_time_decides():
    slow = route("slow", minutes=30, fuel=2.00)
    quick = route("quick", minutes=25, fuel=2.05)  # 2.5% more: within the 3% tolerance
    assert ranked_ids([slow, quick]) == ["quick", "slow"]


def test_detour_cap_still_applies():
    quick = route("quick", minutes=20, fuel=2.0, busy_lefts=1)
    endless = route("endless", minutes=40, fuel=1.5)  # safest and thriftiest, but 2x slower
    ranked = rank_candidates([quick, endless], PRIORITY)
    assert [r.id for r in ranked] == ["quick", "endless"]
    assert not ranked[1].eligible and "too slow" in ranked[1].reason


def test_reasons_explain_the_order():
    ranked = rank_candidates(
        [
            route("risky", minutes=24, fuel=2.0, busy_lefts=1),
            route("safe-slow", minutes=30, fuel=2.1),
            route("safe-thirsty", minutes=26, fuel=2.6),
            route("safe-best", minutes=27, fuel=2.1),
        ],
        PRIORITY,
    )
    reasons = {r.id: r.reason for r in ranked}
    assert ranked[0].id == "safe-best"
    assert reasons["safe-best"] == "as safe and fuel-efficient as any, and the fastest"
    assert reasons["safe-slow"] == "as safe and efficient, but slower"
    assert reasons["safe-thirsty"] == "as safe, but uses more fuel"
    assert reasons["risky"] == "more risky turns than the winner"


def test_default_profile_ranks_by_priority_and_router_designs_lean_harder():
    default = PRESETS["safe-economy"]
    assert default.ranking == "priority"
    design = design_profile(default)
    assert design.busy_crossing_weight == pytest.approx(default.busy_crossing_weight * PRIORITY_SAFETY_BOOST)
    assert design.fuel_weight == pytest.approx(default.fuel_weight * PRIORITY_FUEL_BOOST)
    assert design_profile(PRESETS["calm"]) is PRESETS["calm"]  # weighted profiles design as they score


def with_events(candidate: CandidateRoute, *, closures: int = 0, lane_closures: int = 0) -> CandidateRoute:
    features = candidate.features.model_copy(update={"closures": closures, "lane_closures": lane_closures})
    return candidate.model_copy(update={"features": features})


def test_a_lane_closure_counts_against_safety():
    merge = with_events(route("merge", minutes=25, fuel=2.0), lane_closures=1)
    clear = route("clear", minutes=30, fuel=2.3)
    ranked = rank_candidates([merge, clear], PRIORITY)
    assert [r.id for r in ranked] == ["clear", "merge"]
    assert ranked[0].reason == "safest: fewest risky turns and lane closures"


def test_a_route_through_a_closed_road_cannot_win():
    closed = with_events(route("closed", minutes=20, fuel=1.5), closures=1)
    open_ = route("open", minutes=40, fuel=3.0)  # more than 1.6x the closed route's time: measured from open roads
    ranked = rank_candidates([closed, open_], PRIORITY)
    assert [(r.id, r.eligible) for r in ranked] == [("open", True), ("closed", False)]
    assert ranked[1].reason == "goes through a closed road"


def test_when_every_route_is_closed_the_closure_data_is_doubted():
    a = with_events(route("a", minutes=20, fuel=1.5), closures=1)
    b = with_events(route("b", minutes=25, fuel=2.0), closures=1)
    assert all(r.eligible for r in rank_candidates([a, b], PRIORITY))


def test_a_winner_that_wins_only_because_safer_routes_are_too_slow_says_so():
    quick = route("quick", minutes=17, fuel=1.5, busy_lefts=3)
    careful = route("careful", minutes=28, fuel=1.9)  # safer, but over 1.6x the time
    ranked = rank_candidates([quick, careful], PRIORITY)
    assert ranked[0].id == "quick"
    assert ranked[0].reason == "safest within 1.6x the fastest time; safer routes take longer"
