"""Sun glare: where the sun is, the minutes a route drives toward it, and the router steering around it."""

from datetime import datetime, timedelta, timezone

import pytest

from app.features import TurnContext, extract_features
from app.geo import destination_point, encode_polyline, initial_bearing_deg
from app.models import ScoringProfile
from app.roadgraph import build_graph
from app.router import Router
from app.sun import glare, sun_position
from tests.osm_factory import grid_city, point
from tests.test_roadgraph import edge_between

MILWAUKEE = (43.04, -87.91)
CDT, CST = timezone(timedelta(hours=-5)), timezone(timedelta(hours=-6))


@pytest.mark.parametrize(
    ("moment", "azimuth", "elevation"),
    [
        (datetime(2026, 3, 20, 18, 12, tzinfo=timezone.utc), 180, 47),  # equinox, solar noon: due south, 90 - 43 high
        (datetime(2026, 6, 21, 20, 30, tzinfo=CDT), 303, 0),  # midsummer sunset, far to the northwest
        (datetime(2026, 12, 21, 8, 0, tzinfo=CST), 129, 5),  # a winter morning, low in the southeast
    ],
)
def test_the_sun_is_where_it_should_be_over_milwaukee(moment, azimuth, elevation):
    found_azimuth, found_elevation = sun_position(moment, *MILWAUKEE)
    assert found_azimuth == pytest.approx(azimuth, abs=5) and found_elevation == pytest.approx(elevation, abs=2)


def test_glare_needs_a_low_sun_ahead():
    assert glare(265, (270.0, 10.0))  # heading west into the sunset
    assert not glare(90, (270.0, 10.0))  # the sun behind
    assert not glare(270, (270.0, 40.0))  # high: the visor blocks it
    assert not glare(270, (270.0, -5.0)) and not glare(270, None)  # down, or not known


def westward_route(minutes: float) -> dict:
    line = [MILWAUKEE, destination_point(MILWAUKEE, 270, 5_000)]
    polyline = {"encodedPolyline": encode_polyline(line)}
    step = {"distanceMeters": 5_000, "staticDuration": f"{minutes * 60:.0f}s", "polyline": polyline,
            "navigationInstruction": {"maneuver": "DEPART"}}  # fmt: skip
    seconds = f"{minutes * 60:.0f}s"
    return {"duration": seconds, "staticDuration": seconds, "distanceMeters": 5_000, "polyline": polyline, "legs": [{"steps": [step]}]}


def test_a_route_driven_into_the_setting_sun_counts_its_minutes():
    route = westward_route(8)
    assert extract_features(route, TurnContext(sun=(270.0, 8.0))).sun_glare_min == pytest.approx(8)
    assert extract_features(route, TurnContext(sun=(90.0, 8.0))).sun_glare_min == 0  # sunrise behind
    assert extract_features(route, TurnContext()).sun_glare_min == 0


def test_the_router_makes_roads_into_a_low_sun_cost_more():
    graph = build_graph(grid_city().osm.elements)
    router = Router(graph)
    eastbound = edge_between(graph, (1, 1), (1, 2))
    westbound = edge_between(graph, (1, 2), (1, 1))
    toward = initial_bearing_deg(point(1, 1), point(1, 2))  # Main St, eastbound
    profile = ScoringProfile()
    plain = router._costs(profile, [], keep=()).edges
    sunrise = router._costs(profile, [], keep=(), sun=(toward, 10.0)).edges
    assert sunrise[eastbound] > plain[eastbound] and sunrise[westbound] == plain[westbound]


def test_it_is_dark_after_dusk():
    from app.sun import dark

    milwaukee = (43.0389, -87.9065)
    assert not dark(sun_position(datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc), *milwaukee))  # noon
    assert dark(sun_position(datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc), *milwaukee))  # 9 PM
    assert not dark(None)
