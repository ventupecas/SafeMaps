import pytest
from pydantic import ValidationError

from app.driving_side import DrivingSide
from app.features import TurnContext, extract_features
from app.models import CandidateRoute, CrossingTurn, RouteFeatures, ScoringProfile
from app.profiles import PRESETS, get_profile
from app.scoring import SAFETY_SCALE, rank_candidates, safety_risks, safety_score, score_route
from tests.factories import highway_route, junction_route, local_route, winding_route


def features(**overrides) -> RouteFeatures:
    values = dict(
        distance_km=10.0,
        duration_min=20.0,
        static_duration_min=20.0,
        highway_min=0.0,
        highway_km=0.0,
        traffic_delay_min=0.0,
        maneuver_count=0,
        winding_min=0.0,
    )
    return RouteFeatures(**{**values, **overrides})


def candidate(route_id: str, raw: dict | None = None, context=TurnContext(), **feature_overrides) -> CandidateRoute:
    return CandidateRoute(
        id=route_id,
        source="google",
        encoded_polyline=route_id,
        features=extract_features(raw, context) if raw else features(**feature_overrides),
    )


def crossing(*, busy: bool = False, hotspot: bool = False, signal: bool = False) -> CrossingTurn:
    return CrossingTurn(
        location=None,
        maneuver="TURN_LEFT",
        road_speed_kmh=60 if busy else 30,
        nearby_crashes=5 if hotspot else 0,
        busy=busy,
        hotspot=hotspot,
        signal=signal,
    )


def test_score_terms_are_perceived_minutes():
    f = features(distance_km=10, duration_min=20, highway_min=8, traffic_delay_min=4, maneuver_count=6, winding_min=5)
    profile = ScoringProfile(
        time_weight=1, distance_weight=0.5, highway_weight=1, traffic_weight=2, maneuver_weight=0.25, scenic_weight=0.4
    )
    b = score_route(f, profile)
    assert (b.time, b.distance, b.highway, b.traffic, b.maneuvers, b.scenic) == (20, 5, 8, 8, 1.5, -2)
    assert b.total == pytest.approx(40.5)


def test_crossing_turns_cost_extra_only_when_busy_or_crash_prone():
    f = features(crossing_turns=(crossing(busy=True), crossing(busy=True, hotspot=True), crossing()))
    b = score_route(f, ScoringProfile(busy_crossing_weight=0.5, hotspot_crossing_weight=2))
    assert (b.busy_crossings, b.hotspot_crossings) == (1.0, 2.0)
    assert b.total == pytest.approx(20 + 3)


@pytest.mark.parametrize(
    ("side", "loser", "winner"),
    [(DrivingSide.RIGHT, "TURN_LEFT", "TURN_RIGHT"), (DrivingSide.LEFT, "TURN_RIGHT", "TURN_LEFT")],
)
def test_prefers_near_side_turns_for_the_local_driving_side(side, loser, winner):
    # Identical timings, so only the turn direction separates them. The loser
    # goes first so a plain tie-break can't make the test pass.
    context = TurnContext(side)
    candidates = [candidate(loser, junction_route(loser), context), candidate(winner, junction_route(winner), context)]
    assert rank_candidates(candidates, ScoringProfile())[0].id == winner


@pytest.fixture
def archetypes() -> list[CandidateRoute]:
    return [
        candidate("highway", highway_route()),
        candidate("local", local_route()),
        candidate("winding", winding_route()),
    ]


def winner(candidates, profile_name: str) -> str:
    return rank_candidates(candidates, PRESETS[profile_name])[0].id


def test_presets_pick_different_routes(archetypes):
    assert winner(archetypes, "fastest") == "highway"
    assert winner(archetypes, "highway-averse") != "highway"
    assert winner(archetypes, "scenic") == "winding"


def test_calm_profile_avoids_a_congested_highway():
    jammed = [candidate("highway", highway_route(traffic_delay_s=360)), candidate("winding", winding_route())]
    assert winner(jammed, "fastest") == "highway"  # still 0.5 min quicker
    assert winner(jammed, "calm") == "winding"  # the 6-minute jam is weighted 2.5x


def test_detour_ceiling_blocks_a_cheap_but_slow_route():
    candidates = [candidate("fast", duration_min=10, highway_min=10), candidate("slow", duration_min=20)]
    hates_highways = dict(highway_weight=5)

    strict = rank_candidates(candidates, ScoringProfile(**hates_highways, max_detour_ratio=1.5))
    assert [r.id for r in strict] == ["fast", "slow"]
    assert strict[1].eligible is False
    assert strict[1].breakdown.total < strict[0].breakdown.total  # cheaper, but too slow

    lenient = rank_candidates(candidates, ScoringProfile(**hates_highways, max_detour_ratio=2.5))
    assert lenient[0].id == "slow"


def test_ties_keep_input_order():
    ranked = rank_candidates([candidate("first"), candidate("second")], ScoringProfile())
    assert [r.id for r in ranked] == ["first", "second"]


def test_empty_input_ranks_to_empty_list():
    assert rank_candidates([], ScoringProfile()) == []


def test_scenic_refund_cannot_exceed_time_cost():
    with pytest.raises(ValidationError, match="scenic_weight"):
        ScoringProfile(time_weight=0.5, scenic_weight=0.6)


def test_get_profile_applies_validated_overrides():
    tuned = get_profile("scenic", {"scenic_weight": 0.9})
    assert tuned.scenic_weight == 0.9
    assert tuned.highway_weight == PRESETS["scenic"].highway_weight
    assert tuned.name == "scenic (custom)"

    with pytest.raises(ValueError):
        get_profile("scenic", {"no_such_weight": 1})
    with pytest.raises(ValueError, match="Unknown profile"):
        get_profile("nope")


def test_the_safety_score_comes_from_the_risks_it_lists():
    risky = features(rail_crossings=2, school_zones=1, closures=1)
    risks = safety_risks(risky)
    assert set(risks) == {"rail_crossings", "school_zones", "closures"}  # only the kinds the route has
    assert safety_score(risky) == round(100 / (1 + sum(risks.values()) / SAFETY_SCALE))
    assert safety_risks(features()) == {} and safety_score(features()) == 100


def test_a_busy_turn_at_a_traffic_signal_counts_half():
    f = features(crossing_turns=(crossing(busy=True), crossing(busy=True, signal=True)))
    b = score_route(f, ScoringProfile(busy_crossing_weight=2, hotspot_crossing_weight=4))
    assert b.busy_crossings == pytest.approx(2 + 1)
    crash_prone = features(crossing_turns=(crossing(busy=True, hotspot=True, signal=True),))
    assert score_route(crash_prone, ScoringProfile(busy_crossing_weight=2, hotspot_crossing_weight=4)).hotspot_crossings == 4
