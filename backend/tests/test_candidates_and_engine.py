import pytest

from app.candidates import CandidateOptions, gather_candidates, via_points
from app.driving_side import DrivingSide
from app.engine import NoRouteFoundError, plan_route
from app.features import TurnContext
from app.geo import haversine_m, initial_bearing_deg, midpoint
from app.models import LatLng
from app.profiles import PRESETS
from app.routes_client import RouteRequest
from tests.factories import END, START, highway_route, junction_route, local_route, winding_route

ORIGIN = LatLng(lat=START[0], lng=START[1])
DESTINATION = LatLng(lat=END[0], lng=END[1])


class FakeFetcher:
    """Answers by request kind: plain, avoid-highways, or via (one queued route per via request)."""

    def __init__(self, base=(), avoid_highways=(), via=()) -> None:
        self.base = list(base)
        self.avoid_highways = list(avoid_highways)
        self.via = list(via)
        self.requests: list[RouteRequest] = []

    def compute_routes(self, request: RouteRequest) -> list[dict]:
        self.requests.append(request)
        if request.via:
            return [self.via.pop(0)] if self.via else []
        if request.avoid_highways:
            return list(self.avoid_highways)
        return list(self.base)


def test_via_points_sit_at_right_angles_to_the_trip():
    a, b = (37.0, -122.0), (37.0, -121.0)  # due east, about 89 km
    span = haversine_m(a, b)
    mid = midpoint(a, b)
    right, left = via_points(LatLng(lat=a[0], lng=a[1]), LatLng(lat=b[0], lng=b[1]), (0.25, -0.25), 1e9)

    for point in (right, left):
        assert haversine_m(mid, point.as_tuple()) == pytest.approx(0.25 * span, rel=1e-3)
    assert right.lat < mid[0] < left.lat  # right of eastward travel is south
    heading = initial_bearing_deg(mid, b)
    assert initial_bearing_deg(mid, right.as_tuple()) == pytest.approx((heading + 90) % 360, abs=0.1)


def test_via_offset_is_capped():
    (point,) = via_points(LatLng(lat=37.0, lng=-122.0), LatLng(lat=37.0, lng=-121.0), (0.25,), 15_000)
    assert haversine_m(midpoint((37.0, -122.0), (37.0, -121.0)), point.as_tuple()) == pytest.approx(15_000, rel=1e-3)


def test_enough_google_routes_means_no_via_requests():
    fetcher = FakeFetcher(base=[highway_route(), local_route()], avoid_highways=[winding_route()])
    raw = gather_candidates(fetcher, ORIGIN, DESTINATION)
    assert [c.id for c in raw] == ["google-1", "google-2", "avoid-highways-1"]
    assert len(fetcher.requests) == 2


def test_duplicates_are_dropped_and_via_fills_the_pool():
    hw = highway_route()
    fetcher = FakeFetcher(base=[hw], avoid_highways=[hw], via=[local_route(), winding_route(), local_route()])
    raw = gather_candidates(fetcher, ORIGIN, DESTINATION, CandidateOptions(via_offset_fractions=(0.2, -0.2, 0.4)))
    assert [c.id for c in raw] == ["google-1", "via-offset-1", "via-offset-2"]
    assert len(fetcher.requests) == 4  # stopped before the third via offset


def test_no_base_route_short_circuits():
    fetcher = FakeFetcher(base=[])
    assert gather_candidates(fetcher, ORIGIN, DESTINATION) == []
    assert len(fetcher.requests) == 1


def test_plan_route_flags_when_it_overrides_google():
    fetcher = FakeFetcher(base=[highway_route(), local_route()], avoid_highways=[winding_route()])

    fastest = plan_route(fetcher, ORIGIN, DESTINATION, PRESETS["fastest"])
    assert fastest.winner.id == "google-1"
    assert fastest.winner.is_google_default
    assert not fastest.overrides_google

    scenic = plan_route(fetcher, ORIGIN, DESTINATION, PRESETS["scenic"])
    assert scenic.winner.id == "avoid-highways-1"
    assert not scenic.winner.is_google_default  # labeled DEFAULT_ROUTE, but only for its own request
    assert scenic.overrides_google
    assert scenic.winner.encoded_polyline == winding_route()["polyline"]["encodedPolyline"]
    assert len(scenic.candidates) == 3


def test_plan_route_applies_the_trips_driving_side():
    left = junction_route("TURN_LEFT")
    right = junction_route("TURN_RIGHT", labels=("DEFAULT_ROUTE_ALTERNATE",))
    fetcher = FakeFetcher(base=[left, right])

    keep_right = plan_route(fetcher, ORIGIN, DESTINATION, PRESETS["calm"], context=TurnContext(DrivingSide.RIGHT))
    keep_left = plan_route(fetcher, ORIGIN, DESTINATION, PRESETS["calm"], context=TurnContext(DrivingSide.LEFT))
    assert keep_right.winner.id == "google-2"  # the right turn
    assert keep_left.winner.id == "google-1"  # the left turn


def test_plan_route_raises_when_no_route_exists():
    with pytest.raises(NoRouteFoundError):
        plan_route(FakeFetcher(base=[]), ORIGIN, DESTINATION, PRESETS["fastest"])
