import pytest

from app.driving_side import DrivingSide
from app.features import TurnContext, extract_features, parse_duration_s
from app.hotspots import CrashHotspots
from tests.factories import JUNCTION, highway_route, junction_route, local_route, winding_route

KEEP_RIGHT = TurnContext(DrivingSide.RIGHT)
KEEP_LEFT = TurnContext(DrivingSide.LEFT)


def crossings(route, context=KEEP_RIGHT):
    return extract_features(route, context).crossing_turns


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("1234s", 1234.0), ("12.5s", 12.5), ("0s", 0.0), (None, 0.0), ("", 0.0)],
)
def test_parse_duration(text, seconds):
    assert parse_duration_s(text) == seconds


def test_parse_duration_rejects_other_formats():
    with pytest.raises(ValueError):
        parse_duration_s("12 minutes")


def test_highway_route_features():
    f = extract_features(highway_route())
    assert f.distance_km == pytest.approx(22, abs=0.1)
    assert f.duration_min == pytest.approx(15, abs=0.1)
    assert f.highway_km == pytest.approx(20, abs=0.1)
    assert f.highway_min == pytest.approx(12, abs=0.1)
    assert f.maneuver_count == 2  # RAMP_RIGHT and TURN_RIGHT; DEPART doesn't count
    assert f.winding_min == 0
    assert f.traffic_delay_min == 0


def test_local_route_has_no_highway_or_winding_time():
    f = extract_features(local_route())
    assert f.highway_min == 0
    assert f.winding_min == 0
    assert f.maneuver_count == 5


def test_winding_route_counts_only_the_winding_step():
    f = extract_features(winding_route())
    assert f.winding_min == pytest.approx(20, abs=0.1)
    assert f.highway_min == 0


def test_traffic_delay_is_duration_minus_static_duration():
    f = extract_features(highway_route(traffic_delay_s=360))
    assert f.duration_min == pytest.approx(21, abs=0.1)
    assert f.static_duration_min == pytest.approx(15, abs=0.1)
    assert f.traffic_delay_min == pytest.approx(6, abs=0.01)


def test_far_side_turn_depends_on_driving_side():
    left, right = junction_route("TURN_LEFT"), junction_route("TURN_RIGHT")
    assert len(crossings(left, KEEP_RIGHT)) == 1
    assert crossings(right, KEEP_RIGHT) == ()
    assert crossings(left, KEEP_LEFT) == ()
    assert len(crossings(right, KEEP_LEFT)) == 1


@pytest.mark.parametrize("maneuver", ["UTURN_LEFT", "UTURN_RIGHT"])
def test_u_turns_cross_traffic_on_either_side(maneuver):
    assert len(crossings(junction_route(maneuver), KEEP_RIGHT)) == 1
    assert len(crossings(junction_route(maneuver), KEEP_LEFT)) == 1


@pytest.mark.parametrize("maneuver", ["ROUNDABOUT_LEFT", "FORK_LEFT", "MERGE", "STRAIGHT", "TURN_SLIGHT_RIGHT"])
def test_roundabouts_forks_merges_and_near_side_turns_do_not_cross(maneuver):
    assert crossings(junction_route(maneuver)) == ()


def test_far_side_ramp_crosses_from_a_street_but_not_off_a_highway():
    assert len(crossings(junction_route("RAMP_LEFT", speed_in=40))) == 1
    assert crossings(junction_route("RAMP_LEFT", speed_in=100)) == ()


def test_junction_is_busy_when_either_road_is_arterial():
    (quiet,) = crossings(junction_route("TURN_LEFT", speed_in=30, speed_out=30))
    (onto_arterial,) = crossings(junction_route("TURN_LEFT", speed_in=30, speed_out=60))
    (off_arterial,) = crossings(junction_route("TURN_LEFT", speed_in=60, speed_out=30))
    assert not quiet.busy
    assert onto_arterial.busy and off_arterial.busy
    assert onto_arterial.road_speed_kmh == pytest.approx(60, abs=0.5)


def test_crash_hotspot_at_the_junction():
    context = TurnContext(DrivingSide.RIGHT, CrashHotspots([(JUNCTION, 4)]), min_hotspot_crashes=3)
    (turn,) = crossings(junction_route("TURN_LEFT", speed_in=30, speed_out=30), context)
    assert turn.hotspot and not turn.busy
    assert turn.nearby_crashes == 4
    assert turn.location.as_tuple() == pytest.approx(JUNCTION, abs=1e-4)

    sparse = TurnContext(DrivingSide.RIGHT, CrashHotspots([(JUNCTION, 2)]), min_hotspot_crashes=3)
    assert not crossings(junction_route("TURN_LEFT"), sparse)[0].hotspot


def test_no_crash_data_means_no_hotspots():
    (turn,) = crossings(junction_route("TURN_LEFT"))
    assert turn.nearby_crashes == 0 and not turn.hotspot


def test_crossing_turn_counts():
    f = extract_features(local_route())  # two lefts between 55 km/h roads
    assert (len(f.crossing_turns), f.busy_crossing_turns, f.hotspot_crossing_turns) == (2, 2, 0)
    assert extract_features(highway_route()).crossing_turns == ()


def test_route_without_steps_still_scores():
    f = extract_features({"distanceMeters": 5000, "duration": "600s", "polyline": {"encodedPolyline": "x"}})
    assert f.distance_km == 5
    assert f.duration_min == 10
    assert f.traffic_delay_min == 0  # missing staticDuration falls back to duration
    assert (f.highway_min, f.winding_min, f.maneuver_count) == (0, 0, 0)


class FixedJunctions:
    """A JunctionLookup that reports the same kind of junction everywhere."""

    def __init__(self, kind):
        self.kind = kind

    def junction_kind(self, point):
        return self.kind


def test_road_graph_overrides_the_speed_proxy():
    fast_left = junction_route("TURN_LEFT", speed_in=100, speed_out=100)
    slow_left = junction_route("TURN_LEFT", speed_in=30, speed_out=30)

    # Freeway-to-freeway splits cross no oncoming traffic.
    assert crossings(fast_left, TurnContext(junctions=FixedJunctions("interchange"))) == ()
    # "Busy" follows the roads at the junction, not the speeds.
    (turn,) = crossings(slow_left, TurnContext(junctions=FixedJunctions("busy")))
    assert turn.busy
    (turn,) = crossings(fast_left, TurnContext(junctions=FixedJunctions("quiet")))
    assert not turn.busy
    # No junction close enough, freeway speed on both sides: a split inside an interchange.
    assert crossings(fast_left, TurnContext(junctions=FixedJunctions(None))) == ()
    # No junction close enough otherwise: fall back to the speed proxy.
    (turn,) = crossings(junction_route("TURN_LEFT", speed_in=100, speed_out=40), TurnContext(junctions=FixedJunctions(None)))
    assert turn.busy
    # Without a road graph the freeway rule doesn't apply: an at-grade turn can't be ruled out.
    (turn,) = crossings(fast_left)
    assert turn.busy


def test_all_one_way_junction_is_not_a_crossing():
    assert crossings(junction_route("TURN_LEFT"), TurnContext(junctions=FixedJunctions("one-way"))) == ()
