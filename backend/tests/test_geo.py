import pytest

from app.geo import (
    angle_between_deg,
    decode_polyline,
    destination_point,
    distance_to_path_m,
    encode_polyline,
    haversine_m,
    initial_bearing_deg,
    midpoint,
    total_heading_change_deg,
)

# The worked example from Google's polyline algorithm documentation.
GOOGLE_EXAMPLE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
GOOGLE_POINTS = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_decode_matches_google_example():
    decoded = decode_polyline(GOOGLE_EXAMPLE)
    assert len(decoded) == len(GOOGLE_POINTS)
    for point, expected in zip(decoded, GOOGLE_POINTS):
        assert point == pytest.approx(expected)


def test_encode_matches_google_example():
    assert encode_polyline(GOOGLE_POINTS) == GOOGLE_EXAMPLE


def test_decode_empty_and_truncated():
    assert decode_polyline("") == []
    with pytest.raises(ValueError):
        decode_polyline(GOOGLE_EXAMPLE[:-1])


def test_haversine_san_francisco_to_los_angeles():
    assert haversine_m((37.7749, -122.4194), (34.0522, -118.2437)) == pytest.approx(559_100, rel=0.01)


def test_destination_point_round_trip():
    start = (37.0, -122.0)
    end = destination_point(start, 90, 1_000)
    assert haversine_m(start, end) == pytest.approx(1_000, rel=1e-6)
    assert initial_bearing_deg(start, end) == pytest.approx(90, abs=0.01)


def test_midpoint_is_equidistant():
    a, b = (37.0, -122.0), (38.0, -121.0)
    mid = midpoint(a, b)
    assert haversine_m(a, mid) == pytest.approx(haversine_m(mid, b), rel=1e-6)


def test_heading_change_straight_and_right_angle():
    north = [destination_point((37.0, -122.0), 0, d) for d in (0, 100, 200)]
    assert total_heading_change_deg(north) == pytest.approx(0, abs=0.5)

    corner = north + [destination_point(north[-1], 90, 100)]
    assert total_heading_change_deg(corner) == pytest.approx(90, abs=0.5)


def test_heading_change_ignores_tiny_segments():
    a = (37.0, -122.0)
    jitter = destination_point(a, 90, 2)  # 2 m sideways: noise, not a turn
    b = destination_point(a, 0, 100)
    assert total_heading_change_deg([a, jitter, b]) == pytest.approx(0, abs=0.5)


def test_angle_between_bearings_wraps_around_north():
    assert angle_between_deg(350, 10) == pytest.approx(20)
    assert angle_between_deg(90, 270) == pytest.approx(180)
    assert angle_between_deg(45, 45) == 0


def test_distance_to_path_and_its_bearing_there():
    west, east = (43.0, -88.0), destination_point((43.0, -88.0), 90, 1_000)
    beside = destination_point(midpoint(west, east), 0, 100)  # 100 m north of the middle
    distance, bearing = distance_to_path_m(beside, [west, east])
    assert distance == pytest.approx(100, abs=0.5)
    assert bearing == pytest.approx(90, abs=0.5)
    past_the_end = destination_point(east, 90, 50)
    assert distance_to_path_m(past_the_end, [west, east])[0] == pytest.approx(50, abs=0.5)
