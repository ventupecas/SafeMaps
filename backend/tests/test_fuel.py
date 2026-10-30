import pytest

from app.features import extract_features
from app.fuel import CONGESTION_L_PER_HOUR, MIN_L_PER_KM, TURN_L, cruise_l_per_100km, cruise_liters
from app.models import CandidateRoute, CrossingTurn, RouteFeatures
from app.profiles import PRESETS
from app.scoring import rank_candidates
from tests.factories import highway_route


def test_consumption_is_u_shaped_in_speed():
    assert cruise_l_per_100km(20) > cruise_l_per_100km(60) < cruise_l_per_100km(120)


def test_rough_mpg_ballpark():
    # Steady highway cruising for a midsize sedan: roughly 30-40 mpg (5.9-7.8 L/100 km).
    assert 5.9 < cruise_l_per_100km(90) < 7.8


def test_min_per_km_is_a_true_lower_bound():
    assert all(MIN_L_PER_KM <= cruise_l_per_100km(v) / 100 + 1e-12 for v in range(1, 200))


def test_route_fuel_counts_cruising_turns_and_traffic():
    calm = extract_features(highway_route())
    jammed = extract_features(highway_route(traffic_delay_s=1800))
    # 1 km at 40, 20 km at 100, 1 km at 40, and two turns.
    expected = cruise_liters(1000, 40) * 2 + cruise_liters(20_000, 100) + 2 * TURN_L
    assert calm.fuel_l == pytest.approx(expected, rel=0.02)
    assert jammed.fuel_l - calm.fuel_l == pytest.approx(0.5 * CONGESTION_L_PER_HOUR)


def test_route_without_steps_uses_its_average_speed():
    f = extract_features({"distanceMeters": 10_000, "duration": "600s", "polyline": {"encodedPolyline": "x"}})
    assert f.fuel_l == pytest.approx(cruise_liters(10_000, 60))


def busy_left() -> CrossingTurn:
    return CrossingTurn(location=None, maneuver="TURN_LEFT", road_speed_kmh=60, nearby_crashes=0, busy=True, hotspot=False)


def route(route_id: str, *, minutes: float, fuel: float, busy_lefts: int) -> CandidateRoute:
    features = RouteFeatures(
        distance_km=20,
        duration_min=minutes,
        static_duration_min=minutes,
        highway_min=0,
        highway_km=0,
        traffic_delay_min=0,
        maneuver_count=busy_lefts,
        winding_min=0,
        fuel_l=fuel,
        crossing_turns=tuple(busy_left() for _ in range(busy_lefts)),
    )
    return CandidateRoute(id=route_id, source="google", encoded_polyline=route_id, features=features)


def test_safe_economy_trades_minutes_for_safety_and_fuel():
    quick = route("quick", minutes=20, fuel=2.0, busy_lefts=2)
    careful = route("careful", minutes=23, fuel=1.8, busy_lefts=0)  # 3 min slower, safer, 0.2 L less
    assert rank_candidates([quick, careful], PRESETS["fastest"])[0].id == "quick"
    assert rank_candidates([quick, careful], PRESETS["safe-economy"])[0].id == "careful"


def test_detour_cap_still_applies():
    quick = route("quick", minutes=20, fuel=2.0, busy_lefts=2)
    endless = route("endless", minutes=40, fuel=1.0, busy_lefts=0)  # 2x slower: beyond max_detour_ratio
    assert rank_candidates([quick, endless], PRESETS["safe-economy"])[0].id == "quick"


class Signals:
    """A junction lookup that reports a fixed number of signals and records what it was asked about."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.asked: list[list] = []

    def junction_kind(self, point):
        return None

    def signals_along(self, stretches):
        self.asked.append(list(stretches))
        return self.count


def test_signals_add_fuel_and_freeway_stretches_are_left_out():
    from app.features import TurnContext
    from app.fuel import SIGNAL_L

    plain = extract_features(highway_route())
    lookup = Signals(4)
    with_signals = extract_features(highway_route(), TurnContext(junctions=lookup))
    assert (plain.signal_count, with_signals.signal_count) == (0, 4)
    assert with_signals.fuel_l - plain.fuel_l == pytest.approx(4 * SIGNAL_L)
    (stretches,) = lookup.asked
    assert len(stretches) == 2  # the two 1 km streets, not the 20 km freeway step
