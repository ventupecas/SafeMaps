import json

import httpx
import pytest

from app.cache import CacheMode, ResponseCache
from app.models import LatLng
from app.routes_client import FIELD_MASK, ROUTES_ENDPOINT, RouteRequest, RoutesApiError, RoutesClient

ORIGIN = LatLng(lat=37.7749, lng=-122.4194)
DESTINATION = LatLng(lat=37.3382, lng=-121.8863)
ROUTES = {"routes": [{"distanceMeters": 1000, "polyline": {"encodedPolyline": "abc"}}]}


class Recorder:
    """httpx MockTransport handler that records requests and returns a canned reply."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.payload = ROUTES if payload is None else payload

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


def make_client(tmp_path, handler, *, api_key="test-key", mode=CacheMode.RECORD) -> RoutesClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return RoutesClient(api_key, ResponseCache(tmp_path, mode), http=http)


def test_sends_field_mask_key_and_alternatives_request(tmp_path):
    handler = Recorder()
    routes = make_client(tmp_path, handler).compute_routes(RouteRequest(ORIGIN, DESTINATION))

    assert routes == ROUTES["routes"]
    (sent,) = handler.requests
    assert str(sent.url) == ROUTES_ENDPOINT
    assert sent.headers["X-Goog-Api-Key"] == "test-key"
    assert sent.headers["X-Goog-FieldMask"] == FIELD_MASK
    body = json.loads(sent.content)
    assert body["computeAlternativeRoutes"] is True
    assert body["routingPreference"] == "TRAFFIC_UNAWARE"
    assert body["origin"] == {"location": {"latLng": {"latitude": 37.7749, "longitude": -122.4194}}}
    assert "intermediates" not in body and "routeModifiers" not in body


def test_via_and_avoid_highways_body():
    via = LatLng(lat=37.5, lng=-122.2)
    body = RouteRequest(ORIGIN, DESTINATION, via=(via,), avoid_highways=True, traffic_aware=True).to_body()
    assert body["intermediates"] == [{"location": {"latLng": {"latitude": 37.5, "longitude": -122.2}}, "via": True}]
    assert body["computeAlternativeRoutes"] is False  # Google ignores it with intermediates
    assert body["routeModifiers"] == {"avoidHighways": True}
    assert body["routingPreference"] == "TRAFFIC_AWARE"


def test_repeat_request_is_served_from_cache_without_storing_the_key(tmp_path):
    handler = Recorder()
    client = make_client(tmp_path, handler)
    client.compute_routes(RouteRequest(ORIGIN, DESTINATION))
    client.compute_routes(RouteRequest(ORIGIN, DESTINATION))

    assert len(handler.requests) == 1
    assert client.live_requests == 1
    assert client.cache.hits == 1
    (entry,) = tmp_path.glob("*.json")
    assert "test-key" not in entry.read_text(encoding="utf-8")


def test_replay_works_without_an_api_key(tmp_path):
    make_client(tmp_path, Recorder()).compute_routes(RouteRequest(ORIGIN, DESTINATION))

    handler = Recorder()
    replay = make_client(tmp_path, handler, api_key=None, mode=CacheMode.REPLAY)
    assert replay.compute_routes(RouteRequest(ORIGIN, DESTINATION)) == ROUTES["routes"]
    assert handler.requests == []


def test_google_error_is_surfaced_and_not_cached(tmp_path):
    error = {"error": {"code": 403, "message": "API key not valid.", "status": "PERMISSION_DENIED"}}
    client = make_client(tmp_path, Recorder(status=403, payload=error))
    with pytest.raises(RoutesApiError, match="API key not valid") as caught:
        client.compute_routes(RouteRequest(ORIGIN, DESTINATION))
    assert caught.value.status_code == 403
    assert list(tmp_path.glob("*.json")) == []


def test_missing_api_key_fails_before_any_request(tmp_path):
    handler = Recorder()
    with pytest.raises(RoutesApiError, match="GOOGLE_MAPS_API_KEY"):
        make_client(tmp_path, handler, api_key=None).compute_routes(RouteRequest(ORIGIN, DESTINATION))
    assert handler.requests == []


def test_network_failure_becomes_routes_api_error(tmp_path):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RoutesApiError, match="Could not reach"):
        make_client(tmp_path, unreachable).compute_routes(RouteRequest(ORIGIN, DESTINATION))


def test_no_route_found_returns_empty_list(tmp_path):
    client = make_client(tmp_path, Recorder(payload={}))
    assert client.compute_routes(RouteRequest(ORIGIN, DESTINATION)) == []
