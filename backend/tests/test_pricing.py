import pytest

from app.geo import decode_polyline, destination_point, encode_polyline
from app.models import LatLng
from app.pricing import design_match, price_route, via_points
from app.roadgraph import build_graph
from app.router import Router
from app.routes_client import RouteRequest
from tests.osm_factory import grid_city, point
from tests.test_router import FASTEST

ORIGIN = LatLng(lat=point(1, 0)[0], lng=point(1, 0)[1])
DESTINATION = LatLng(lat=point(2, 2)[0], lng=point(2, 2)[1])


@pytest.fixture
def router() -> Router:
    return Router(build_graph(grid_city().osm.elements, area="grid"))


@pytest.fixture
def path(router):
    # (1,0) east along Main St, left at (1,2), north to (2,2).
    return router.routes(point(1, 0), point(2, 2), FASTEST)[0]


class EchoFetcher:
    """Plays Google: answers every request with a fixed route and records the request."""

    def __init__(self, route) -> None:
        self.route = route
        self.requests: list[RouteRequest] = []

    def compute_routes(self, request: RouteRequest) -> list[dict]:
        self.requests.append(request)
        return [self.route] if self.route else []


def test_via_point_sits_mid_block_after_each_turn(router, path):
    (via,) = via_points(router.graph, path)
    mid = (point(1, 2)[0] + point(2, 2)[0]) / 2
    assert via.point.lat == pytest.approx(mid, abs=1e-4)
    assert via.point.lng == pytest.approx(point(1, 2)[1], abs=1e-4)
    assert via.heading in (0, 359, 1)  # heading north


def test_via_points_respect_the_limit(router):
    loop = router.routes(point(1, 0), point(2, 2), FASTEST.model_copy(update={"busy_crossing_weight": 5}))[0]
    assert len(via_points(router.graph, loop)) >= 2
    assert len(via_points(router.graph, loop, limit=1)) == 1


def test_design_match():
    line = [destination_point((43.04, -87.9), 90, d) for d in range(0, 2001, 100)]
    parallel = [destination_point(p, 0, 200) for p in line]
    assert design_match(line, line) == pytest.approx(1.0)
    assert design_match(line, parallel) == 0.0
    half = line[:11] + [destination_point(p, 0, 200) for p in line[11:]]
    assert 0.4 < design_match(line, half) < 0.65


def test_price_route_asks_for_live_traffic_through_the_via_points(router, path):
    google_version = router.to_route_json(path)  # Google follows the design exactly
    fetcher = EchoFetcher(google_version)
    priced = price_route(fetcher, router, ORIGIN, DESTINATION, path)

    (request,) = fetcher.requests
    assert request.traffic_aware
    assert len(request.via) == len(request.via_headings) == priced.via_count == 1
    body = request.to_body()
    assert body["intermediates"][0]["via"] is True
    assert "heading" in body["intermediates"][0]["location"]
    assert body["computeAlternativeRoutes"] is False
    assert priced.route is google_version
    assert priced.design_match == pytest.approx(1.0)


def test_price_route_reports_when_google_wanders(router, path):
    designed = router.path_points(path)
    elsewhere = [destination_point(p, 180, 400) for p in designed]
    detour = {"polyline": {"encodedPolyline": encode_polyline(elsewhere)}}
    priced = price_route(EchoFetcher(detour), router, ORIGIN, DESTINATION, path)
    assert priced.design_match < 0.1
    assert decode_polyline(priced.route["polyline"]["encodedPolyline"])


def test_price_route_returns_none_when_google_finds_nothing(router, path):
    assert price_route(EchoFetcher(None), router, ORIGIN, DESTINATION, path) is None


def test_headings_must_match_via_points():
    with pytest.raises(ValueError, match="one heading per via"):
        RouteRequest(ORIGIN, DESTINATION, via=(ORIGIN,), via_headings=(0, 90))
