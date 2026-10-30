import pytest

from app.geo import decode_polyline, destination_point, encode_polyline, haversine_m
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


def long_street_router(**road_tags: str) -> Router:
    """A 5 km straight road with a short side street every 250 m (so every 250 m is a junction)."""
    from tests.osm_factory import Osm

    osm = Osm()
    main_nodes = []
    for i in range(21):
        at = destination_point((43.0, -88.0), 90, 250 * i)
        main_nodes.append(osm.node(5000 + i, at))
        spur = osm.node(6000 + i, destination_point(at, 0, 50))
        osm.way([main_nodes[-1], spur], highway="residential")
    osm.way(main_nodes, **{"highway": "residential", **road_tags})
    return Router(build_graph(osm.elements))


def test_straight_streets_get_a_pin_every_few_blocks():
    router = long_street_router()
    (path,) = router.routes((43.0, -88.0), destination_point((43.0, -88.0), 90, 5000), FASTEST, count=1)
    assert all(m in ("DEPART", "STRAIGHT") for m in path.maneuvers)
    pins = via_points(router.graph, path)
    assert len(pins) == 5  # 5 km of street, one pin per ~800 m (rounded up to the next junction)


def test_freeways_get_sparser_pins():
    router = long_street_router(highway="trunk", maxspeed="65 mph")  # freeway speed, but two-way like the test needs
    (path,) = router.routes((43.0, -88.0), destination_point((43.0, -88.0), 90, 5000), FASTEST, count=1)
    assert len(via_points(router.graph, path)) == 1


def test_pins_after_turns_win_over_spacers_at_the_limit():
    from app.pricing import _spread

    assert _spread([1, 2, 3, 4, 5], 3) == [1, 3, 5]
    assert _spread([1, 2], 5) == [1, 2]
    assert _spread([1, 2, 3], 1) == [1]
    assert _spread([1, 2, 3], 0) == []


class StrayThenFollow:
    """Plays Google: the first answer cuts a corner, the second follows the design."""

    def __init__(self, stray, follow) -> None:
        self.answers = [stray, follow]
        self.requests: list[RouteRequest] = []

    def compute_routes(self, request: RouteRequest) -> list[dict]:
        self.requests.append(request)
        return [self.answers[min(len(self.requests), 2) - 1]]


def test_google_straying_gets_one_repinned_retry():
    router = long_street_router()
    start, end = (43.0, -88.0), destination_point((43.0, -88.0), 90, 5000)
    (path,) = router.routes(start, end, FASTEST, count=1)
    design = router.to_route_json(path)
    # Google's first answer leaves the street for 2 km in the middle.
    points = router.path_points(path)
    detour = [destination_point(p, 0, 300) if 1500 < haversine_m(start, p) < 3500 else p for p in points]
    stray = {"polyline": {"encodedPolyline": encode_polyline(detour)}}
    google = StrayThenFollow(stray, design)

    origin, destination = LatLng(lat=start[0], lng=start[1]), LatLng(lat=end[0], lng=end[1])
    priced = price_route(google, router, origin, destination, path)
    assert len(google.requests) == 2
    assert len(google.requests[1].via) == len(google.requests[0].via) + 1  # a pin in the skipped stretch
    assert priced.attempts == 2
    assert priced.design_match == pytest.approx(1.0)


def test_no_retry_when_nothing_new_can_be_pinned(router):
    # On the small grid every segment of this loop is already pinned, so a retry couldn't help.
    loop = router.routes(point(1, 0), point(2, 2), FASTEST.model_copy(update={"busy_crossing_weight": 5}))[0]
    shortcut = router.to_route_json(router.routes(point(1, 0), point(2, 2), FASTEST)[0])
    google = StrayThenFollow(shortcut, router.to_route_json(loop))
    priced = price_route(google, router, ORIGIN, DESTINATION, loop)
    assert len(google.requests) == 1
    assert priced.attempts == 1 and priced.design_match < 0.99


def test_no_retry_when_google_follows(router, path):
    google = EchoFetcher(router.to_route_json(path))
    assert price_route(google, router, ORIGIN, DESTINATION, path).attempts == 1
    assert len(google.requests) == 1


def test_no_pins_on_service_roads():
    from app.router import RoutedPath
    from tests.test_router import grid_with_service_avenue

    graph = grid_with_service_avenue()
    node = lambda r, c: graph.nearest_node(point(r, c), 5)  # noqa: E731
    edge = lambda a, b: next(e for e in graph.out_edges[node(*a)] if graph.edge_to[e] == node(*b))  # noqa: E731
    # East on Main St, then left into the service road: the turn lands on a segment we must not pin.
    path = RoutedPath(
        edges=(edge((1, 0), (1, 1)), edge((1, 1), (1, 2)), edge((1, 2), (2, 2))),
        maneuvers=("DEPART", "STRAIGHT", "TURN_LEFT"),
        cost=0.0,
    )
    assert via_points(graph, path) == []
