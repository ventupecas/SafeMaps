"""The HTTP API, end to end with a fake Google and the synthetic grid graph."""

import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache import CacheMode
from app.config import Settings
from app.main import create_app
from app.overpass import BBox
from app.roadgraph import build_graph
from tests.factories import highway_route, winding_route
from tests.osm_factory import grid_city, point

ORIGIN = {"lat": point(1, 0)[0], "lng": point(1, 0)[1]}
DESTINATION = {"lat": point(2, 2)[0], "lng": point(2, 2)[1]}


class FakeGoogle:
    """MockTransport handler: Google's own route for plain requests, a fixed route through any via points."""

    def __init__(self, *, no_route: bool = False) -> None:
        self.no_route = no_route
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "routes.googleapis.com":
            return httpx.Response(404)  # e.g. the WZDx feed: unreachable in these tests unless a test serves it
        body = json.loads(request.content)
        self.requests.append(body)
        if self.no_route:
            return httpx.Response(200, json={})
        route = winding_route() if "intermediates" in body else highway_route()
        return httpx.Response(200, json={"routes": [route]})


def make_settings(tmp_path, **overrides) -> Settings:
    graph_path = tmp_path / "grid.json.gz"
    if not graph_path.exists():
        build_graph(grid_city().osm.elements, area="grid").save(graph_path)
    values = dict(
        google_maps_api_key="test-key",
        cache_mode=CacheMode.RECORD,
        cache_dir=tmp_path / "cache",
        cache_max_age=timedelta(days=30),
        traffic_aware=False,
        default_country=None,
        crash_data_path=None,
        crash_min_count=3.0,
        graph_path=graph_path,
        traffic_fresh_for=timedelta(minutes=5),
        daily_limit=100,
    )
    return Settings(**{**values, **overrides})


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def api(tmp_path, google):
    app = create_app(make_settings(tmp_path), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:  # the context manager runs startup (graph loading)
        yield client


def test_health_reports_what_is_loaded_without_leaking_the_key(api):
    response = api.get("/health")
    assert response.status_code == 200
    health = response.json()
    assert health["road_graph"] == {"area": "grid", "junctions": 10}
    assert health["google_api_key_set"] is True
    assert "test-key" not in response.text


def test_profiles(api):
    body = api.get("/profiles").json()
    assert body["default"] == "safe-economy"
    assert {"fastest", "safe-economy", "calm", "eco"} <= {p["name"] for p in body["profiles"]}


def test_route_with_our_router(api, google):
    response = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION})
    assert response.status_code == 200
    trip = response.json()
    plan = trip["plan"]
    assert plan["profile"]["name"] == "safe-economy"
    assert plan["winner"]["encoded_polyline"]
    assert {c["source"] for c in plan["candidates"]} >= {"google", "own"}
    assert trip["own_router_used"] and "OpenStreetMap" in trip["attribution"][0]
    assert trip["driving_side"] == "right"
    assert trip["google"]["live_requests"] == len(google.requests) >= 2
    assert all(body["routingPreference"] == "TRAFFIC_AWARE" for body in google.requests)


def test_route_google_only_without_traffic(api, google):
    trip = api.post(
        "/route", json={"origin": ORIGIN, "destination": DESTINATION, "use_router": False, "live_traffic": False}
    ).json()
    assert {c["source"] for c in trip["plan"]["candidates"]} <= {"google", "avoid-highways", "via-offset"}
    assert trip["attribution"] == []
    assert all(body["routingPreference"] == "TRAFFIC_UNAWARE" for body in google.requests)


def test_rerouting_a_moving_car_sends_its_heading(api, google):
    response = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "heading": 89.6})
    assert response.status_code == 200
    assert google.requests and all(body["origin"]["location"]["heading"] == 90 for body in google.requests)


def test_custom_weights_and_country(api):
    trip = api.post(
        "/route",
        json={
            "origin": ORIGIN,
            "destination": DESTINATION,
            "profile": "calm",
            "weights": {"busy_crossing_weight": 6},
            "country_code": "GB",
        },
    ).json()
    assert trip["plan"]["profile"]["busy_crossing_weight"] == 6
    assert trip["driving_side"] == "left"


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "nope"},
        {"weights": {"no_such_weight": 1}},
        {"country_code": "USA"},
        {"origin": {"lat": 123, "lng": 0}},
        {"heading": 400},
        {"road_events": "waze"},
        {"surprise": True},
    ],
)
def test_bad_requests_are_422(api, change):
    response = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, **change})
    assert response.status_code == 422


def test_no_route_is_404(tmp_path):
    google = FakeGoogle(no_route=True)
    app = create_app(make_settings(tmp_path), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as api:
        far = {"lat": 44.5, "lng": -88.0}  # outside the graph too
        assert api.post("/route", json={"origin": far, "destination": DESTINATION}).status_code == 404


def test_unrecorded_trip_in_replay_is_503(tmp_path, google):
    app = create_app(make_settings(tmp_path, cache_mode=CacheMode.REPLAY), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as api:
        response = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION})
    assert response.status_code == 503
    assert google.requests == []


def test_daily_limit_is_429(tmp_path, google):
    app = create_app(make_settings(tmp_path, daily_limit=1), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as api:
        first = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION})
        assert first.status_code == 200  # Google's route fit in the limit; our routes couldn't be priced
        assert any("not priced" in note for note in first.json()["notes"])
        other = {"lat": point(0, 3)[0], "lng": point(0, 3)[1]}
        assert api.post("/route", json={"origin": ORIGIN, "destination": other}).status_code == 429


def test_cors_allows_local_pages_only(api):
    def preflight(origin: str) -> httpx.Response:
        return api.options(
            "/route",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
        )

    assert preflight("http://localhost:5500").headers.get("access-control-allow-origin") == "http://localhost:5500"
    assert "access-control-allow-origin" not in preflight("https://example.com").headers


def test_the_web_map_is_served_alongside_the_api(api):
    page = api.get("/")
    assert page.status_code == 200 and "<title>Safe Route Planner</title>" in page.text
    assert api.get("/app.js").status_code == 200
    assert api.get("/health").json()["status"] == "ok"  # API routes still win over files


def test_road_events_fall_back_to_google_traffic_without_a_511_key(api, google):
    trip = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION}).json()
    assert (trip["road_events_source"], trip["road_events_skipped"]) == ("google", ["wzdx", "wi511"])
    plain = [body for body in google.requests if "intermediates" not in body]
    assert [body.get("extraComputations") for body in plain] == [["TRAFFIC_ON_POLYLINE"]]
    assert all("extraComputations" not in body for body in google.requests if "intermediates" in body)


def test_road_events_can_be_switched_off(api, google):
    trip = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "road_events": "off"}).json()
    assert (trip["road_events_source"], trip["road_events"]) == (None, [])
    assert all("extraComputations" not in body for body in google.requests)


def test_wisconsin_511_closures_reach_the_plan(tmp_path, google):
    closed_main_st = {
        "ID": 7,
        "RoadwayName": "Main St",
        "DirectionOfTravel": "Both Directions",
        "Description": "Closed for a water main break",
        "IsFullClosure": True,
        "LanesAffected": "All lanes closed",
        "Latitude": point(1, 1)[0],
        "Longitude": point(1, 1)[1],
        "LatitudeSecondary": point(1, 2)[0],
        "LongitudeSecondary": point(1, 2)[1],
        "StartDate": 0,
        "PlannedEndDate": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "511wi.gov":
            return httpx.Response(200, json=[closed_main_st])
        return google(request)

    settings = make_settings(tmp_path, wi511_api_key="wi-key")
    app = create_app(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with TestClient(app) as client:
        trip = client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "live_traffic": False}).json()
    assert trip["road_events_source"] == "wi511"
    (closed,) = trip["road_events"]  # snapped to Main St both ways, and drawn once
    assert closed["kind"] == "closed" and closed["description"].startswith("Main St:")
    assert all(not route["features"]["closures"] for route in trip["plan"]["candidates"] if route["source"] == "own")


def test_health_lists_the_road_event_sources(api):
    road_events = api.get("/health").json()["road_events"]
    assert road_events.pop("matched_ahead") is False  # the test map covers no named area: nothing to match ahead
    assert road_events == {
        "order": ["wzdx+wi511+tomtom", "google"],
        "wzdx": "https://511wi.gov/api/wzdx",
        "wi511_key_set": False,
        "tomtom_key_set": False,
        "tomtom_requests_today": 0,
        "tomtom_tiles_today": 0,
        "apify_enabled": False,
        "apify_tiles_today": 0,
    }


def test_an_access_code_guards_route_requests_when_set(tmp_path, google):
    app = create_app(make_settings(tmp_path, access_code="expo-2026"), http=httpx.Client(transport=httpx.MockTransport(google)))
    trip = {"origin": ORIGIN, "destination": DESTINATION, "live_traffic": False}
    with TestClient(app) as client:
        assert client.post("/route", json=trip).status_code == 401
        assert client.post("/route", json=trip, headers={"X-Access-Code": "wrong"}).status_code == 401
        assert client.post("/route", json=trip, headers={"X-Access-Code": "expo-2026"}).status_code == 200
        assert client.get("/").status_code == 200  # the page itself loads; the code comes in its link
    assert len(google.requests) == 1  # nothing reached Google before the right code


def test_routes_come_with_turn_by_turn_directions(api):
    trip = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "live_traffic": False}).json()
    own = next(route for route in trip["plan"]["candidates"] if route["source"] == "own")
    assert own["maneuvers"] and all(step["instruction"] and step["location"] for step in own["maneuvers"])


def test_the_openstreetmap_map_never_asks_google(tmp_path, google):
    app = create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.get("/app-config").json() == {"map": "osm", "traffic_tiles": False}  # no TomTom key
        trip = client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "live_traffic": True}).json()
    assert google.requests == []  # no Google: not even with live traffic asked for
    standard = [route for route in trip["plan"]["candidates"] if route["is_standard"]]
    assert [route["source"] for route in standard] == ["baseline"]  # our fastest route stands in for Google's
    assert all(not route["live_traffic"] for route in trip["plan"]["candidates"])
    assert trip["road_events_source"] != "google" and "google" not in trip["road_events_skipped"]


def test_googles_default_is_the_standard_route_on_the_google_map(api):
    trip = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION}).json()
    assert [r["id"] for r in trip["plan"]["candidates"] if r["is_standard"]] == ["google-1"]
    assert api.get("/app-config").json() == {"map": "google", "traffic_tiles": False}


class FakeTomTom:
    """MockTransport handler for api.tomtom.com (its incidents and a traffic tile); anything else goes to fallback."""

    def __init__(self, incidents, fallback) -> None:
        self.incidents, self.fallback, self.requests = incidents, fallback, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.tomtom.com":
            return self.fallback(request)
        self.requests.append(request)
        if request.url.path.endswith("incidentDetails"):
            return httpx.Response(200, json={"incidents": self.incidents})
        return httpx.Response(200, content=b"\x89PNG traffic", headers={"Content-Type": "image/png"})


def main_st_closed_both_ways() -> list[dict]:
    from tests.test_road_events import tomtom_incident

    east = [point(1, 1), point(1, 2)]
    return [tomtom_incident(8, east, **{"from": "1 Ave", "to": "2 Ave"}), tomtom_incident(8, east[::-1])]


def test_tomtom_traffic_on_the_openstreetmap_map(tmp_path, google):
    tomtom = FakeTomTom(main_st_closed_both_ways(), google)
    settings = make_settings(tmp_path, map="osm", tomtom_api_key="tt-key")
    with TestClient(create_app(settings, http=httpx.Client(transport=httpx.MockTransport(tomtom)))) as client:
        assert client.get("/app-config").json() == {"map": "osm", "traffic_tiles": True}
        trip = client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION}).json()
        first = client.get("/traffic-tiles/12/1047/1504.png")
        again = client.get("/traffic-tiles/12/1047/1504.png")
        assert client.get("/traffic-tiles/3/9/0.png").status_code == 404  # no such tile at zoom 3
        health = client.get("/health").json()["road_events"]

    assert trip["road_events_source"] == "tomtom"  # the WZDx feed is unreachable here, and there's no 511 key
    (closed,) = trip["road_events"]  # Main St, both ways: one closure, drawn once
    assert closed["source"] == "tomtom" and "both directions" in closed["description"]
    assert all(not route["features"]["closures"] for route in trip["plan"]["candidates"])
    assert (first.status_code, first.headers["content-type"], first.content) == (200, "image/png", b"\x89PNG traffic")
    assert again.content == first.content and "tt-key" not in first.text
    incidents, tile = tomtom.requests  # one each: the second tile came from what the server kept
    assert incidents.url.params["key"] == tile.url.params["key"] == "tt-key"
    assert tile.url.path == "/traffic/map/4/tile/flow/relative0/12/1047/1504.png"
    assert (health["tomtom_requests_today"], health["tomtom_tiles_today"]) == (1, 1)


def test_road_events_are_matched_to_the_map_before_the_first_trip(tmp_path, google):
    tomtom = FakeTomTom(main_st_closed_both_ways(), google)
    settings = make_settings(tmp_path, map="osm", tomtom_api_key="tt-key")
    with TestClient(create_app(settings, http=httpx.Client(transport=httpx.MockTransport(tomtom)))) as client:
        engine = client.app.state.engine
        assert engine._snapped == {}  # the test map covers no named area, so startup had nothing to do
        graph = engine.graph
        graph.bbox = BBox(min(graph.lat), min(graph.lng), max(graph.lat), max(graph.lng))
        engine.warm_up()
    assert len(engine._snapped) == 1  # Main St's closure, both ways, matched once for every trip to come


def test_tomtom_stays_off_the_google_map(tmp_path, google):
    tomtom = FakeTomTom(main_st_closed_both_ways(), google)
    settings = make_settings(tmp_path, tomtom_api_key="tt-key")
    with TestClient(create_app(settings, http=httpx.Client(transport=httpx.MockTransport(tomtom)))) as client:
        trip = client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "road_events": "tomtom"}).json()
        assert client.get("/traffic-tiles/12/1047/1504.png").status_code == 404
    assert (trip["road_events_source"], trip["road_events"], tomtom.requests) == (None, [], [])


def test_an_access_code_guards_the_traffic_map_too(tmp_path, google):
    tomtom = FakeTomTom([], google)
    settings = make_settings(tmp_path, map="osm", tomtom_api_key="tt-key", access_code="expo-2026")
    with TestClient(create_app(settings, http=httpx.Client(transport=httpx.MockTransport(tomtom)))) as client:
        assert client.get("/traffic-tiles/12/1047/1504.png").status_code == 401
        assert client.get("/traffic-tiles/12/1047/1504.png?code=expo-2026").status_code == 200
    assert len(tomtom.requests) == 1
