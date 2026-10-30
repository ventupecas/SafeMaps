from datetime import date, timedelta

import httpx
import pytest

from app.cache import CacheMode, ResponseCache
from app.models import LatLng
from app.routes_client import RouteRequest, RoutesApiError, RoutesClient
from app.usage import DailyUsage

A = LatLng(lat=43.0389, lng=-87.9065)
B = LatLng(lat=43.0117, lng=-88.2315)
C = LatLng(lat=42.9476, lng=-87.8966)


class Today:
    def __init__(self) -> None:
        self.value = date(2026, 10, 29)

    def __call__(self) -> date:
        return self.value


def test_counts_until_the_limit_then_refuses(tmp_path):
    usage = DailyUsage(tmp_path / "usage.json", limit=2)
    assert usage.take() and usage.take()
    assert not usage.take()
    assert usage.used_today() == 2


def test_count_survives_restarts_and_resets_the_next_day(tmp_path):
    today = Today()
    DailyUsage(tmp_path / "usage.json", limit=1, today=today).take()
    assert not DailyUsage(tmp_path / "usage.json", limit=1, today=today).take()

    today.value += timedelta(days=1)
    assert DailyUsage(tmp_path / "usage.json", limit=1, today=today).take()


def test_no_limit_still_counts(tmp_path):
    usage = DailyUsage(tmp_path / "usage.json", limit=None)
    for _ in range(5):
        assert usage.take()
    assert usage.used_today() == 5


def test_client_stops_live_calls_at_the_limit_but_serves_the_cache(tmp_path):
    calls = []

    def google(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"routes": [{"polyline": {"encodedPolyline": "abc"}}]})

    client = RoutesClient(
        "test-key",
        ResponseCache(tmp_path, CacheMode.RECORD),
        http=httpx.Client(transport=httpx.MockTransport(google)),
        usage=DailyUsage(tmp_path / "daily-usage.json", limit=1),
    )
    client.compute_routes(RouteRequest(A, B))
    with pytest.raises(RoutesApiError, match="ROUTES_DAILY_LIMIT"):
        client.compute_routes(RouteRequest(A, C))
    assert client.compute_routes(RouteRequest(A, B))  # cached, so still allowed
    assert len(calls) == 1
