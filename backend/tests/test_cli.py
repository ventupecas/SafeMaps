"""End to end: record synthetic responses through the real client, then run the CLI in replay mode."""

import json

import httpx
import pytest

from app import cli
from app.cache import CacheMode, ResponseCache
from app.candidates import gather_candidates
from app.models import LatLng
from app.geo import decode_polyline
from app.routes_client import RoutesClient
from tests.factories import END, START, highway_route, local_route, winding_route

FROM = f"{START[0]},{START[1]}"
TO = f"{END[0]},{END[1]}"


def fake_google(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("routeModifiers", {}).get("avoidHighways"):
        return httpx.Response(200, json={"routes": [winding_route()]})
    return httpx.Response(200, json={"routes": [highway_route(), local_route()]})


@pytest.fixture
def recorded_cache(tmp_path, monkeypatch):
    http = httpx.Client(transport=httpx.MockTransport(fake_google))
    with RoutesClient("test-key", ResponseCache(tmp_path, CacheMode.RECORD), http=http) as client:
        gather_candidates(client, LatLng.parse(FROM), LatLng.parse(TO))

    # Set every variable explicitly so a developer's backend/.env can't leak in.
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setenv("ROUTES_CACHE_MODE", "replay")
    monkeypatch.setenv("ROUTES_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("ROUTES_CACHE_MAX_AGE_DAYS", "30")
    monkeypatch.setenv("ROUTES_TRAFFIC_AWARE", "false")
    monkeypatch.setenv("ROUTES_DEFAULT_COUNTRY", "")
    monkeypatch.setenv("ROUTES_CRASH_DATA", "")
    monkeypatch.setenv("ROUTES_CRASH_MIN_COUNT", "3")
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
