"""The hybrid pipeline: Google's suggestions plus our router's designs, priced by Google."""

import pytest

from app.cache import CacheMiss
from app.engine import build_router_candidates, plan_from_candidates
from app.models import LatLng
from app.profiles import PRESETS
from app.roadgraph import build_graph
from app.router import Router
from app.routes_client import RouteRequest
from tests.factories import highway_route
from tests.osm_factory import grid_city, point

ORIGIN = LatLng(lat=point(1, 0)[0], lng=point(1, 0)[1])
DESTINATION = LatLng(lat=point(2, 2)[0], lng=point(2, 2)[1])


class FakeGoogle:
    """Plain requests get Google's own route; via requests get our design back, as if Google followed it."""

    def __init__(self, router: Router, *, fail_pricing: Exception | None = None) -> None:
        self.router = router
        self.fail_pricing = fail_pricing
        self.requests: list[RouteRequest] = []
        self.designs: list[dict] = []

    def compute_routes(self, request: RouteRequest) -> list[dict]:
        self.requests.append(request)
        if not request.via:
            return [highway_route()]
        if self.fail_pricing:
            raise self.fail_pricing
        design = self.designs.pop(0)
        return [design]


@pytest.fixture
def router() -> Router:
    return Router(build_graph(grid_city().osm.elements, area="grid"))


def expect_designs(google: FakeGoogle, router: Router, profile, count: int = 3) -> int:
    paths = router.routes(ORIGIN.as_tuple(), DESTINATION.as_tuple(), profile, count=count)
    google.designs = [router.to_route_json(p) for p in paths]
    return len(paths)


def test_pool_has_google_plus_priced_own_routes(router):
    google = FakeGoogle(router)
    designs = expect_designs(google, router, PRESETS["calm"])
    pool = build_router_candidates(google, router, ORIGIN, DESTINATION, [PRESETS["calm"]])

    assert [c.id for c in pool.candidates] == ["google-1"] + [f"own-{n}" for n in range(1, designs + 1)]
    assert all(r.traffic_aware for r in google.requests)  # one live-traffic call per route
    assert len(google.requests) == 1 + designs
    own = pool.candidates[1]
    assert own.source == "own" and own.live_traffic
    assert own.design_match == pytest.approx(1.0)

    plan = plan_from_candidates(pool.candidates, PRESETS["calm"])
    assert plan.winner.id.startswith("own-")  # the synthetic Google route is far longer


def test_a_moving_cars_heading_reaches_every_google_request(router):
    google = FakeGoogle(router)
    expect_designs(google, router, PRESETS["calm"])
    build_router_candidates(google, router, ORIGIN, DESTINATION, [PRESETS["calm"]], heading=90)
    assert len(google.requests) > 1 and all(request.origin_heading == 90 for request in google.requests)


def test_without_pricing_own_routes_keep_estimates_and_cost_nothing_extra(router):
    google = FakeGoogle(router)
    pool = build_router_candidates(google, router, ORIGIN, DESTINATION, [PRESETS["calm"]], price=False)
    assert len(google.requests) == 1
    assert not google.requests[0].traffic_aware
    own = [c for c in pool.candidates if c.source == "own"]
    assert own and all(not c.live_traffic and c.design_match is None for c in own)


def test_one_design_per_profile_when_ranking_several(router):
    profiles = list(PRESETS.values())
    best_paths = {}
    for profile in profiles:
        best = router.routes(ORIGIN.as_tuple(), DESTINATION.as_tuple(), profile, count=1)[0]
        best_paths.setdefault(best.edges, best)
    google = FakeGoogle(router)
    google.designs = [router.to_route_json(p) for p in best_paths.values()]

    pool = build_router_candidates(google, router, ORIGIN, DESTINATION, profiles)
    assert len([c for c in pool.candidates if c.source == "own"]) == len(best_paths)
    assert len(google.requests) == 1 + len(best_paths)


def test_pricing_failures_become_notes(router):
    google = FakeGoogle(router, fail_pricing=CacheMiss("No recorded response"))
    pool = build_router_candidates(google, router, ORIGIN, DESTINATION, [PRESETS["calm"]])
    assert [c.id for c in pool.candidates] == ["google-1"]
    assert pool.notes and all("not priced" in note for note in pool.notes)


def test_trip_outside_the_graph_falls_back_to_google(router):
    google = FakeGoogle(router)
    far = LatLng(lat=44.5, lng=-88.0)  # Green Bay
    pool = build_router_candidates(google, router, far, DESTINATION, [PRESETS["calm"]])
    assert [c.id for c in pool.candidates] == ["google-1"]
    assert "Own router skipped" in pool.notes[0]


class NoKeyGoogle:
    """A fetcher that fails like the real client does without an API key."""

    def __init__(self) -> None:
        self.requests = 0

    def compute_routes(self, request):
        from app.routes_client import RoutesApiError

        self.requests += 1
        raise RoutesApiError("GOOGLE_MAPS_API_KEY is not set")


def test_without_pricing_google_is_optional(router):
    pool = build_router_candidates(NoKeyGoogle(), router, ORIGIN, DESTINATION, [PRESETS["calm"]], price=False)
    assert pool.candidates and all(c.source == "own" for c in pool.candidates)
    assert "Google's suggestions skipped" in pool.notes[0]


def test_with_pricing_google_failure_is_fatal(router):
    from app.routes_client import RoutesApiError

    with pytest.raises(RoutesApiError):
        build_router_candidates(NoKeyGoogle(), router, ORIGIN, DESTINATION, [PRESETS["calm"]])


def test_nothing_at_all_raises_no_route(router):
    from app.engine import NoRouteFoundError

    far = LatLng(lat=44.5, lng=-88.0)
    with pytest.raises(NoRouteFoundError, match="skipped"):
        build_router_candidates(NoKeyGoogle(), router, far, DESTINATION, [PRESETS["calm"]], price=False)
