"""End to end: record synthetic responses through the real client, then run the CLI in replay mode."""

import json

import httpx
import pytest

from app import cli
from app.cache import CacheMode, ResponseCache
from app.candidates import gather_candidates
from app.engine import build_router_candidates
from app.geo import decode_polyline
from app.models import LatLng
from app.profiles import PRESETS
from app.roadgraph import build_graph
from app.router import Router
from app.routes_client import RoutesClient
from tests.factories import END, START, highway_route, junction_route, local_route, winding_route
from tests.osm_factory import grid_city, point

FROM = f"{START[0]},{START[1]}"
TO = f"{END[0]},{END[1]}"


def fake_google(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("routeModifiers", {}).get("avoidHighways"):
        return httpx.Response(200, json={"routes": [winding_route()]})
    return httpx.Response(200, json={"routes": [highway_route(), local_route()]})


def isolate_env(monkeypatch, cache_dir, graph_path) -> None:
    # Set every variable explicitly so a developer's backend/.env can't leak in.
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setenv("ROUTES_CACHE_MODE", "replay")
    monkeypatch.setenv("ROUTES_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("ROUTES_CACHE_MAX_AGE_DAYS", "30")
    monkeypatch.setenv("ROUTES_TRAFFIC_AWARE", "false")
    monkeypatch.setenv("ROUTES_DEFAULT_COUNTRY", "")
    monkeypatch.setenv("ROUTES_CRASH_DATA", "")
    monkeypatch.setenv("ROUTES_CRASH_MIN_COUNT", "3")
    monkeypatch.setenv("ROUTES_GRAPH", str(graph_path))


@pytest.fixture
def recorded_cache(tmp_path, monkeypatch):
    http = httpx.Client(transport=httpx.MockTransport(fake_google))
    with RoutesClient("test-key", ResponseCache(tmp_path, CacheMode.RECORD), http=http) as client:
        gather_candidates(client, LatLng.parse(FROM), LatLng.parse(TO))
    isolate_env(monkeypatch, tmp_path, tmp_path / "no-graph.json.gz")
    return tmp_path


GRID_FROM = f"{point(1, 0)[0]},{point(1, 0)[1]}"
GRID_TO = f"{point(2, 2)[0]},{point(2, 2)[1]}"


@pytest.fixture
def recorded_router_trip(tmp_path, monkeypatch):
    """A built grid graph plus recorded Google answers for a calm-profile trip across it."""
    graph = build_graph(grid_city().osm.elements, area="grid")
    graph.save(tmp_path / "grid.json.gz")
    priced_versions = [winding_route(), local_route(), junction_route("TURN_RIGHT")]
    via_requests = []

    def google(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "intermediates" in body:
            # None of these follow the design, so each route is also re-pinned and asked again.
            via_requests.append(body)
            return httpx.Response(200, json={"routes": [priced_versions[len(via_requests) % 3]]})
        return httpx.Response(200, json={"routes": [highway_route()]})

    http = httpx.Client(transport=httpx.MockTransport(google))
    with RoutesClient("test-key", ResponseCache(tmp_path, CacheMode.RECORD), http=http) as client:
        for price in (True, False):
            build_router_candidates(
                client, Router(graph), LatLng.parse(GRID_FROM), LatLng.parse(GRID_TO), [PRESETS["calm"]], price=price
            )
    isolate_env(monkeypatch, tmp_path, tmp_path / "grid.json.gz")
    return tmp_path


def test_json_output_is_the_route_plan(recorded_cache, capsys):
    assert cli.main(["--from", FROM, "--to", TO, "--profile", "scenic", "--json"]) == 0
    out, err = capsys.readouterr()
    plan = json.loads(out)
    assert plan["winner"]["id"] == "avoid-highways-1"
    assert plan["overrides_google"] is True
    assert "0 live request(s), 2 served from cache" in err


def test_all_profiles_table(recorded_cache, capsys):
    assert cli.main(["--from", FROM, "--to", TO, "--all-profiles"]) == 0
    out = capsys.readouterr().out
    for name in ("fastest", "highway-averse", "scenic", "calm", "eco"):
        assert name in out


def test_weight_override_changes_the_winner(recorded_cache, capsys):
    assert cli.main(["--from", FROM, "--to", TO, "--set", "highway_weight=1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["winner"]["id"] != "google-1"


def test_country_sets_the_driving_side(recorded_cache, capsys):
    assert cli.main(["--from", FROM, "--to", TO, "--country", "GB", "--turns"]) == 0
    out = capsys.readouterr().out
    assert "Traffic keeps left (GB)" in out
    assert "Turns across oncoming traffic:" in out
    assert "TURN_RIGHT" in out  # rights cross traffic in the UK


def test_crash_data_marks_hotspot_turns(recorded_cache, tmp_path, monkeypatch, capsys):
    # The local route's first left turn starts its third step.
    first_left = decode_polyline(local_route()["legs"][0]["steps"][2]["polyline"]["encodedPolyline"])[0]
    crashes = tmp_path / "crashes.csv"
    crashes.write_text(f"lat,lng,crashes\n{first_left[0]},{first_left[1]},5\n", encoding="utf-8")
    monkeypatch.setenv("ROUTES_CRASH_DATA", str(crashes))

    assert cli.main(["--from", FROM, "--to", TO, "--json"]) == 0
    local = next(c for c in json.loads(capsys.readouterr().out)["candidates"] if c["id"] == "google-2")
    assert local["features"]["hotspot_crossing_turns"] == 1


def test_bad_country_fails_cleanly(recorded_cache, capsys):
    assert cli.main(["--from", FROM, "--to", TO, "--country", "USA"]) == 1
    assert "two-letter" in capsys.readouterr().err


def test_unrecorded_trip_fails_cleanly_in_replay(recorded_cache, capsys):
    assert cli.main(["--from", "40.0,-74.0", "--to", "40.1,-74.1"]) == 1
    assert "No recorded response" in capsys.readouterr().err


def test_bad_override_fails_cleanly(recorded_cache, capsys):
    assert cli.main(["--from", FROM, "--to", TO, "--set", "highway_weight=lots"]) == 1
    assert "not a number" in capsys.readouterr().err


def test_router_mode_adds_our_priced_designs(recorded_router_trip, capsys):
    assert cli.main(["--from", GRID_FROM, "--to", GRID_TO, "--profile", "calm", "--json"]) == 0
    out, err = capsys.readouterr()
    candidates = json.loads(out)["candidates"]
    own = [c for c in candidates if c["source"] == "own"]
    assert own and all(c["live_traffic"] and c["design_match"] is not None for c in own)
    assert "0 live request(s)" in err


def test_router_mode_table_says_how_routes_were_priced(recorded_router_trip, capsys):
    assert cli.main(["--from", GRID_FROM, "--to", GRID_TO, "--profile", "calm"]) == 0
    out = capsys.readouterr().out
    assert "Own router: grid graph, 10 junctions; priced by Google with live traffic" in out
    assert "own-1" in out


def test_no_pricing_keeps_estimates(recorded_router_trip, capsys):
    assert cli.main(["--from", GRID_FROM, "--to", GRID_TO, "--profile", "calm", "--no-pricing"]) == 0
    out = capsys.readouterr().out
    assert "estimated times only" in out
    assert "own-1 ~" in out


def test_no_router_flag(recorded_cache, capsys):
    assert cli.main(["--from", FROM, "--to", TO, "--no-router"]) == 0
    assert "Own router: off (--no-router)" in capsys.readouterr().out
