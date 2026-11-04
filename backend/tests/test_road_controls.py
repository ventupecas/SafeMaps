"""Which way each stop sign and light faces, and whether a route meets it."""

from app.geo import initial_bearing_deg
from app.road_controls import Control, RoadControls, TaggedNode, Way, controls_from

A, B, C, D = (43.0, -88.0), (43.0, -87.999), (43.0, -87.997), (43.0, -87.995)
EAST = initial_bearing_deg(A, B)
WEST = (EAST + 180) % 360


def close(angle, target):
    return abs((angle - target + 180) % 360 - 180) < 1


def only_control(node, way=None, uses=None):
    way = way or Way((1, 2, 3, 4), (A, B, C, D))
    controls = controls_from({2: node}, [way], uses if uses is not None else {1: 2, 4: 2})
    return controls[0] if controls else None


def test_a_sign_with_no_direction_faces_the_nearer_junction():
    # B is nearer the junction at A than the one at D: it's for traffic heading west, to A.
    assert close(only_control(TaggedNode("stop", B)).bearings[0], WEST)


def test_the_mappers_direction_wins():
    assert close(only_control(TaggedNode("stop", B, "forward")).bearings[0], EAST)
    assert close(only_control(TaggedNode("stop", B, "backward")).bearings[0], WEST)
    assert only_control(TaggedNode("stop", B, "both")).bearings == ()


def test_a_one_way_street_has_one_direction():
    assert close(only_control(TaggedNode("signal", B), Way((1, 2, 3, 4), (A, B, C, D), True)).bearings[0], EAST)
    assert close(only_control(TaggedNode("signal", B), Way((1, 2, 3, 4), (A, B, C, D), False)).bearings[0], WEST)


def test_on_a_junction_a_light_is_for_everyone_and_a_stop_sign_is_left_out_unless_all_way():
    on_junction = {1: 2, 2: 2, 4: 2}
    assert only_control(TaggedNode("signal", B), uses=on_junction).bearings == ()
    assert only_control(TaggedNode("stop", B), uses=on_junction) is None
    assert only_control(TaggedNode("stop", B, all_way=True), uses=on_junction).bearings == ()


def test_a_sign_on_the_cross_street_is_not_on_the_route():
    # 3 m north of the road, as a cross street's sign can be, facing south: close enough, but not this way.
    beside = (A[0] + 0.00003, B[1])
    controls = RoadControls([Control("stop", *beside, (180.0,))])
    assert controls.along([A, C]) == []
    assert controls.along([(43.001, B[1]), (42.999, B[1])])[0]["kind"] == "stop"


def test_lights_at_one_junction_count_once_and_a_saved_file_reads_back(tmp_path):
    controls = RoadControls([Control("signal", 43.0, -87.999), Control("signal", 43.0, -87.9988), Control("signal", 43.0, -87.997)])
    assert len(controls.along([A, D])) == 2
    path = tmp_path / "controls.json.gz"
    controls.save(path)
    assert RoadControls.load(path).counts() == {"stop": 0, "signal": 3}
