"""The HTTP API, end to end with a fake Google and the synthetic grid graph."""

import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache import CacheMode
from app.config import Settings
from app.geo import decode_polyline, encode_polyline, haversine_m, initial_bearing_deg
from app.main import create_app
from app.overpass import BBox
from app.road_controls import Control, RoadControls, roads_along
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
        weather_alerts=False,  # tests that want them serve their own
        bus_routes_url=None,
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


def test_a_place_off_the_openstreetmap_map_is_named_and_its_area_is_given(tmp_path, google):
    box = BBox(south=ORIGIN["lat"] - 0.05, west=ORIGIN["lng"] - 0.05, north=ORIGIN["lat"] + 0.05, east=ORIGIN["lng"] + 0.05)
    build_graph(grid_city().osm.elements, area="grid", bbox=box).save(tmp_path / "grid.json.gz")
    app = create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))
    far = {"lat": 44.5, "lng": -88.0}
    with TestClient(app) as api:
        assert api.get("/app-config").json()["area"] == [box.south, box.west, box.north, box.east]
        for trip, place in (
            ({"origin": far, "destination": DESTINATION}, "The start"),
            ({"origin": ORIGIN, "destination": far}, "The destination"),
            ({"origin": ORIGIN, "stops": [DESTINATION, far], "destination": ORIGIN}, "Stop 2"),
        ):
            answer = api.post("/route", json=trip)
            assert answer.status_code == 422, trip
            assert answer.json()["detail"] == f"{place} isn't near any road on this map: pick a spot inside the area it covers."
        assert api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION}).status_code == 200


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
        "tomtom_tiles_today": 0, "tomtom_daily_requests": 80, "tomtom_daily_tiles": 6000,
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


def test_routes_are_known_by_their_roads_and_a_driver_can_cap_the_extra_minutes(api):
    trip = api.post(
        "/route", json={"origin": ORIGIN, "destination": DESTINATION, "live_traffic": False, "max_extra_min": 5}
    ).json()
    assert trip["plan"]["profile"]["max_extra_min"] == 5
    own = [route["via"] for route in trip["plan"]["candidates"] if route["source"] == "own"]
    assert own and all(own) and len(set(own)) == len(own)  # each of our designs reads apart, e.g. "Main St"
    assert trip["plan"]["winner"]["via"] == trip["plan"]["candidates"][0]["via"]
    assert api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "max_extra_min": -1}).status_code == 422


def test_every_route_has_a_safety_score_and_the_trip_its_departure_time(api):
    trip = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "departure_offset_min": 30}).json()
    assert all(0 <= route["safety_score"] <= 100 for route in trip["plan"]["candidates"])
    leaving = datetime.fromisoformat(trip["leaving_at"])
    assert timedelta(minutes=28) < leaving - datetime.now() < timedelta(minutes=31)
    assert isinstance(trip["low_sun"], bool) and trip["weather_alerts"] == []


def test_winter_weather_alerts_make_turning_across_traffic_count_more(tmp_path, google):
    advisory = {
        "event": "Winter Weather Advisory",
        "headline": "Winter Weather Advisory until 6:00 PM CST",
        "ends": "2026-12-01T18:00:00-06:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            assert request.headers["User-Agent"]  # the service refuses requests without one
            return httpx.Response(200, json={"features": [{"properties": advisory}]})
        return google(request)

    app = create_app(make_settings(tmp_path, weather_alerts=True), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with TestClient(app) as client:
        trip = client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "live_traffic": False}).json()
    (alert,) = trip["weather_alerts"]
    assert alert["event"] == "Winter Weather Advisory" and alert["slippery"]
    assert trip["plan"]["profile"]["busy_crossing_weight"] == pytest.approx(3.0 * 1.5)


@pytest.fixture
def osm_api(tmp_path, google):
    app = create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        yield client


def test_a_trip_with_a_stop_is_planned_leg_by_leg(osm_api):
    stop = {"lat": point(1, 3)[0], "lng": point(1, 3)[1]}
    trip = osm_api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "stops": [stop]}).json()
    winner = trip["plan"]["winner"]
    assert winner["id"] == "trip-recommended" and winner["reason"] == "the recommended route on every leg"
    arrivals = [step for step in winner["maneuvers"] if step["maneuver"] == "WAYPOINT"]
    assert [step["instruction"] for step in arrivals] == ["Arrive at stop 1"]
    path = decode_polyline(winner["encoded_polyline"])
    assert min(haversine_m(p, point(1, 3)) for p in path) < 30  # it goes through the stop
    too_many = {"origin": ORIGIN, "destination": DESTINATION, "stops": [stop] * 4}
    assert osm_api.post("/route", json=too_many).status_code == 422


def test_the_best_time_to_leave_is_compared_across_the_next_hour(osm_api):
    body = osm_api.post("/leave-times", json={"origin": ORIGIN, "destination": DESTINATION}).json()
    assert [t["offset_min"] for t in body["times"]] == [0, 15, 30, 60]
    assert body["best_offset_min"] in (0, 15, 30, 60)
    assert all(0 <= t["safety_score"] <= 100 and t["duration_min"] > 0 for t in body["times"])
    # Leaving now is the trip the page just planned: the same routes, judged the same way.
    winner = osm_api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION}).json()["plan"]["winner"]
    now = body["times"][0]
    assert (now["safety_score"], now["duration_min"]) == (winner["safety_score"], round(winner["features"]["duration_min"], 1))


def test_visitors_answers_are_counted_without_places(tmp_path, google):
    app = create_app(make_settings(tmp_path, access_code="expo"), http=httpx.Client(transport=httpx.MockTransport(google)))
    answer = {"choice": "yes", "safety_gain": 30, "extra_min": 4.5, "profile": "safe-economy"}
    with TestClient(app) as client:
        assert client.post("/feedback", json=answer).status_code == 401
        code = {"X-Access-Code": "expo"}
        assert client.post("/feedback", json=answer, headers=code).status_code == 204
        assert client.post("/feedback", json={**answer, "choice": "no", "extra_min": 12}, headers=code).status_code == 204
        assert client.post("/feedback", json={**answer, "choice": "maybe"}, headers=code).status_code == 422
        summary = client.get("/feedback-summary").json()
        assert client.get("/health").json()["feedback"] == {"yes": 1, "no": 1}
    assert (summary["yes"], summary["no"], summary["yes_share"]) == (1, 1, 0.5)
    assert (summary["extra_min_when_yes"], summary["extra_min_when_no"]) == (4.5, 12.0)
    saved = (tmp_path / "cache" / "feedback.jsonl").read_text(encoding="utf-8")
    assert "lat" not in saved and saved.count("\n") == 2


def test_routes_come_with_turn_by_turn_directions(api):
    trip = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "live_traffic": False}).json()
    own = next(route for route in trip["plan"]["candidates"] if route["source"] == "own")
    assert own["maneuvers"] and all(step["instruction"] and step["location"] for step in own["maneuvers"])


def test_the_openstreetmap_map_never_asks_google(tmp_path, google):
    app = create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.get("/app-config").json() == {"map": "osm", "area": None, "traffic_tiles": False, "bus_routes": False, "bike": False, "walk": False, "transit": False, "fuel_price": 3.25}  # no TomTom key
        trip = client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "live_traffic": True}).json()
    assert google.requests == []  # no Google: not even with live traffic asked for
    standard = [route for route in trip["plan"]["candidates"] if route["is_standard"]]
    assert [route["source"] for route in standard] == ["baseline"]  # our fastest route stands in for Google's
    assert all(not route["live_traffic"] for route in trip["plan"]["candidates"])
    assert trip["road_events_source"] != "google" and "google" not in trip["road_events_skipped"]


def test_googles_default_is_the_standard_route_on_the_google_map(api):
    trip = api.post("/route", json={"origin": ORIGIN, "destination": DESTINATION}).json()
    assert [r["id"] for r in trip["plan"]["candidates"] if r["is_standard"]] == ["google-1"]
    assert api.get("/app-config").json() == {"map": "google", "area": None, "traffic_tiles": False, "bus_routes": False, "bike": False, "walk": False, "transit": False, "fuel_price": 3.25}


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
        assert client.get("/app-config").json() == {"map": "osm", "area": None, "traffic_tiles": True, "bus_routes": False, "bike": False, "walk": False, "transit": False, "fuel_price": 3.25}
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


def test_bus_routes_for_the_openstreetmap_maps_transit_layer(tmp_path, google):
    from tests.test_transit import feed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=feed()) if request.url.host == "transit.example" else google(request)

    settings = make_settings(tmp_path, map="osm", bus_routes_url="https://transit.example/gtfs.zip")
    with TestClient(create_app(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))) as client:
        assert client.get("/app-config").json()["bus_routes"] is True
        body = client.get("/bus-routes").json()
    assert [route["name"] for route in body["routes"]] == ["4", "30"]
    assert body["attribution"] == "Bus routes: Milwaukee County Transit System"
    # Google's map has its own transit layer.
    google_map = create_app(make_settings(tmp_path, bus_routes_url="https://transit.example/gtfs.zip"), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with TestClient(google_map) as client:
        assert client.get("/bus-routes").status_code == 404


def test_the_expo_stats_count_todays_trips_without_places(tmp_path, google):
    app = create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.get("/expo-stats").json()["trips"]["trips"] == 0
        client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION})
        client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION, "counted": False})  # a sample trip
        client.post("/feedback", json={"choice": "yes", "safety_gain": 5, "extra_min": 1, "profile": "safe-economy"})
        stats = client.get("/expo-stats").json()
    assert stats["trips"]["trips"] == 1 and stats["trips_ever"] == 1
    assert {"risky_turns_avoided", "rail_crossings_avoided", "school_zones_avoided", "closures_avoided"} <= set(stats["trips"])
    assert stats["answers"]["yes"] == 1
    saved = (tmp_path / "cache" / "trips.jsonl").read_text(encoding="utf-8")
    assert "lat" not in saved and saved.count("\n") == 1


def test_the_control_page_pauses_demo_mode_and_restarts_the_stats(tmp_path, google):
    app = create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.get("/expo-control").json() == {"demo_paused": False, "stats_since": None}
        client.post("/route", json={"origin": ORIGIN, "destination": DESTINATION})  # a rehearsal trip
        through_the_tunnel = {"CF-Connecting-IP": "203.0.113.9"}
        assert client.post("/expo-control", json={"demo_paused": True}, headers=through_the_tunnel).status_code == 403
        assert client.post("/expo-control", json={"demo_paused": True}).status_code == 403  # the test client isn't this computer
    app = create_app(make_settings(tmp_path, map="osm", admin_code="booth"), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.post("/expo-control", json={"reset_stats": True}).status_code == 401
        state = client.post("/expo-control", json={"demo_paused": True, "reset_stats": True}, headers={"X-Admin-Code": "booth"}).json()
        assert state["demo_paused"] and state["stats_since"]
        assert client.get("/expo-stats").json()["trips"]["trips"] == 0  # the rehearsal no longer counts
        assert client.get("/expo-stats").json()["trips_ever"] == 1
    # A restart keeps what was set.
    with TestClient(create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))) as client:
        assert client.get("/expo-control").json()["demo_paused"] is True


def test_guesses_in_the_safer_route_game_are_counted(tmp_path, google):
    app = create_app(make_settings(tmp_path, map="osm"), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.post("/guess", json={"right": True, "picked_score": 80, "other_score": 40}).status_code == 204
        assert client.post("/guess", json={"right": False, "picked_score": 40, "other_score": 80}).status_code == 204
        assert client.post("/guess", json={"right": True, "picked_score": 180, "other_score": 4}).status_code == 422
        guesses = client.get("/expo-stats").json()["guesses"]
    assert guesses == {"total": 2, "right": 1, "right_share": 0.5}


def test_bike_routes_through_the_api(tmp_path, google):
    from tests.test_bike import city_with_a_trail

    bike_map = tmp_path / "grid-bike.json.gz"
    build_graph(city_with_a_trail().osm.elements, area="grid", mode="bike").save(bike_map)
    app = create_app(make_settings(tmp_path, map="osm", bike_graph_path=bike_map), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.get("/app-config").json()["bike"] is True
        trip = client.post("/bike-route", json={"origin": ORIGIN, "destination": DESTINATION, "profile": "family"}).json()
        assert client.post("/bike-route", json={"origin": ORIGIN, "destination": DESTINATION, "profile": "racing"}).status_code == 422
        box = {"south": 43.03, "west": -87.91, "north": 43.05, "east": -87.89}
        stress = client.get("/bike-stress", params=box).json()
        health = client.get("/health").json()
    winner = trip["plan"]["winner"]
    assert 0 <= winner["safety_score"] <= 100 and winner["stress_segments"] and winner["features"]["duration_min"] > 0
    assert any(c["is_standard"] for c in trip["plan"]["candidates"])
    assert stress["lines"]["1"] and stress["lines"]["4"]  # quiet avenues, and Main St
    built = build_graph(city_with_a_trail().osm.elements, area="grid", mode="bike")
    trails = sum(1 for edge in range(built.edge_count) if built.class_name(edge) in ("cycleway", "path"))
    assert trails > 0 and health["bike_map"] == {"segments": built.edge_count, "trails": trails, "cyclist_crash_locations": 0}


def test_no_bike_map_no_bike_routes(api):
    assert api.get("/app-config").json()["bike"] is False
    assert api.post("/bike-route", json={"origin": ORIGIN, "destination": DESTINATION}).status_code == 503


def test_walking_routes_through_the_api(tmp_path, google):
    walk_map = tmp_path / "grid-walk.json.gz"
    build_graph(grid_city().osm.elements, area="grid", mode="walk").save(walk_map)
    app = create_app(make_settings(tmp_path, map="osm", walk_graph_path=walk_map), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.get("/app-config").json()["walk"] is True
        trip = client.post("/walk-route", json={"origin": ORIGIN, "destination": DESTINATION, "profile": "careful"}).json()
        assert client.post("/walk-route", json={"origin": ORIGIN, "destination": DESTINATION, "profile": "family"}).status_code == 422
    winner = trip["plan"]["winner"]
    assert winner["features"]["duration_min"] >= 9  # 600 m or so at a careful walker's pace
    assert trip["attribution"][0].startswith("Walking routes")


def test_bus_trips_through_the_api(tmp_path, google):
    from tests.test_timetable import feed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=feed(point(1, 1))) if request.url.host == "transit.example" else google(request)

    walk_map = tmp_path / "grid-walk.json.gz"
    build_graph(grid_city().osm.elements, area="grid", mode="walk").save(walk_map)
    settings = make_settings(tmp_path, map="osm", walk_graph_path=walk_map, bus_routes_url="https://transit.example/gtfs.zip")
    app = create_app(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with TestClient(app) as client:
        assert client.get("/app-config").json()["transit"] is True
        client.app.state.engine.schedule.load()  # read in the background at startup: here, wait for it
        assert client.get("/health").json()["transit"]["ready"] is True
        tuesday_10 = "2026-11-10T10:00:00"
        far_east = {"lat": point(1, 1)[0], "lng": point(1, 1)[1] + 0.0367}  # 3 km east, past the GreenLine's last stop
        trip = client.post("/transit-route", json={"origin": ORIGIN, "destination": far_east, "leave_at": tuesday_10}).json()
        sunday = client.post("/transit-route", json={"origin": ORIGIN, "destination": far_east, "leave_at": "2026-11-08T10:00:00"})
    winner = trip["plan"]["winner"]
    assert [leg["kind"] for leg in winner["legs"]] == ["walk", "ride", "walk"]
    to_the_bus = winner["legs"][0]
    # Planned on the walking map: 200 m along Main St, a busy street with no sidewalks mapped.
    assert 150 < to_the_bus["meters"] < 300 and to_the_bus["minutes_by_stress"][2] > 0
    assert winner["legs"][1]["route"]["name"] == "GRE" and trip["scheduled"] is True
    assert sunday.status_code == 404 and "No bus" in sunday.json()["detail"]


def test_without_the_schedule_no_bus_trips(tmp_path, google):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if request.url.host == "transit.example" else google(request)

    settings = make_settings(tmp_path, bus_routes_url="https://transit.example/gtfs.zip")
    with TestClient(create_app(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))) as client:
        client.app.state.engine.schedule.load()
        response = client.post("/transit-route", json={"origin": ORIGIN, "destination": DESTINATION})
        assert response.status_code == 503 and "couldn't be loaded" in response.json()["detail"]
    with TestClient(create_app(make_settings(tmp_path, bus_routes_url="https://transit.example/gtfs.zip", transit=False), http=httpx.Client(transport=httpx.MockTransport(handler)))) as client:
        assert client.get("/app-config").json()["transit"] is False
        assert client.post("/transit-route", json={"origin": ORIGIN, "destination": DESTINATION}).status_code == 503


def test_a_walk_over_ten_miles_suggests_another_mode(tmp_path, google):
    walk_map = tmp_path / "grid-walk.json.gz"
    build_graph(grid_city().osm.elements, area="grid", mode="walk").save(walk_map)
    app = create_app(make_settings(tmp_path, map="osm", walk_graph_path=walk_map), http=httpx.Client(transport=httpx.MockTransport(google)))
    far = {"lat": ORIGIN["lat"] + 0.1462, "lng": ORIGIN["lng"]}  # about 10.1 miles north
    within = {"lat": ORIGIN["lat"] + 0.1433, "lng": ORIGIN["lng"]}  # about 9.9 miles north: past the test city's streets
    halfway = {"lat": ORIGIN["lat"] + 0.08, "lng": ORIGIN["lng"]}
    with TestClient(app) as client:
        too_far = client.post("/walk-route", json={"origin": ORIGIN, "destination": far})
        assert too_far.status_code == 422 and "over 10 miles" in too_far.json()["detail"]
        off_the_map = client.post("/walk-route", json={"origin": ORIGIN, "destination": within})
        assert off_the_map.status_code == 422 and off_the_map.json()["detail"].startswith("The destination isn't near any road on this map")
        # There and back past a stop counts all the way.
        with_a_stop = client.post("/walk-route", json={"origin": ORIGIN, "stops": [halfway], "destination": ORIGIN})
        assert with_a_stop.status_code == 422
        assert client.post("/walk-route", json={"origin": ORIGIN, "destination": DESTINATION}).status_code == 200


def test_the_signs_along_a_route_come_in_order_for_its_direction_of_travel(tmp_path, google):
    controls = tmp_path / "controls.json.gz"
    # Along Main St, west to east: a light on a junction, then a stop sign for eastbound traffic, then one for westbound.
    west, middle, east = point(1, 0), point(1, 1), point(1, 2)
    eastbound = initial_bearing_deg(west, east)
    near_east = (east[0], east[1] - 0.0002)
    RoadControls([
        Control("signal", *middle),
        Control("stop", *near_east, (eastbound,)),
        Control("stop", *near_east, ((eastbound + 180) % 360,)),
    ]).save(controls)
    app = create_app(make_settings(tmp_path, road_controls_path=controls), http=httpx.Client(transport=httpx.MockTransport(google)))
    with TestClient(app) as client:
        assert client.get("/health").json()["road_controls"] == {"stop": 2, "signal": 1}
        met = client.post("/road-controls", json={"encoded_polyline": encode_polyline([west, middle, east])}).json()["controls"]
        assert [control["kind"] for control in met] == ["signal", "stop"]
        assert met[0]["along_m"] < met[1]["along_m"]
        back = client.post("/road-controls", json={"encoded_polyline": encode_polyline([east, middle, west])}).json()["controls"]
        assert [control["kind"] for control in back] == ["stop", "signal"]


def test_without_the_signs_file_a_route_has_no_signs_and_the_code_still_counts(tmp_path, google):
    app = create_app(make_settings(tmp_path, access_code="expo"), http=httpx.Client(transport=httpx.MockTransport(google)))
    line = {"encoded_polyline": encode_polyline([point(1, 0), point(1, 1)])}
    with TestClient(app) as client:
        assert client.post("/road-controls", json=line).status_code == 401
        answer = client.post("/road-controls", json=line, headers={"X-Access-Code": "expo"}).json()
        assert answer["controls"] == []


def test_a_route_says_which_road_it_is_on_all_the_way_along(api):
    east_then_north = [point(1, 0), point(1, 1), point(1, 2), point(2, 2)]
    roads = api.post("/road-controls", json={"encoded_polyline": encode_polyline(east_then_north)}).json()["roads"]
    assert roads[0] == {"name": "Main St", "along_m": 0.0}
    assert len(roads) == 2 and roads[1]["name"] and roads[1]["name"] != "Main St"
    assert abs(roads[1]["along_m"] - haversine_m(point(1, 0), point(1, 2))) < 40  # about where it turns


def test_a_cross_street_passed_doesnt_name_the_road_for_a_moment():
    class Map:
        def road_name_at(self, spot, heading):
            return "Cross St" if 43.0 + 0.0020 < spot[0] < 43.0 + 0.0023 else "Long Rd"

    assert roads_along(Map(), [(43.0, -88.0), (43.005, -88.0)]) == [{"name": "Long Rd", "along_m": 0.0}]


def test_a_weight_too_big_to_be_a_number_is_a_bad_request(tmp_path, google):
    """JSON reads 1e999 as infinity. It's turned away, rather than reaching a search that would make no sense of it."""
    app = create_app(make_settings(tmp_path), http=httpx.Client(transport=httpx.MockTransport(google)))
    body = '{"origin": {"lat": %s, "lng": %s}, "destination": {"lat": %s, "lng": %s}, "weights": {"busy_crossing_weight": 1e999}}' % (
        ORIGIN["lat"],
        ORIGIN["lng"],
        DESTINATION["lat"],
        DESTINATION["lng"],
    )
    with TestClient(app) as client:
        assert client.post("/route", content=body, headers={"Content-Type": "application/json"}).status_code == 422
