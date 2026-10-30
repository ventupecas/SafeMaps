import pytest

from app.driving_side import DrivingSide
from app.features import TurnContext, extract_features
from app.geo import midpoint
from app.hotspots import CrashHotspots
from app.models import ScoringProfile
from app.road_events import RoadEvent
from app.roadgraph import RoadGraph, build_graph
from app.router import MAX_OVERLAP, OutsideGraphError, Router
from tests.osm_factory import MEDIAN_HALF_LNG, divided_road, grid_city, junction, point

FASTEST = ScoringProfile(busy_crossing_weight=0, hotspot_crossing_weight=0, fuel_weight=0)
AVOID_BUSY_CROSSINGS = ScoringProfile(busy_crossing_weight=5, hotspot_crossing_weight=0, fuel_weight=0)


def node_at(graph: RoadGraph, row: int, col: int) -> int:
    return graph.nearest_node(point(row, col), 5)


def visited(graph: RoadGraph, path) -> list[tuple[int, int]]:
    """Grid (row, col) of every junction the path passes, in order."""
    lookup = {node_at(graph, r, c): (r, c) for r in range(3) for c in range(4) if r == 1 or c > 0}
    nodes = [graph.edge_from[path.edges[0]], *(graph.edge_to[e] for e in path.edges)]
    return [lookup[n] for n in nodes]


def busy_crossings(router: Router, path) -> int:
    count = 0
    for edge, nxt in zip(path.edges, path.edges[1:]):
        _, _, crossing, busy = next(m for m in router.moves(edge) if m[0] == nxt)
        count += crossing and busy
    return count


@pytest.fixture
def graph() -> RoadGraph:
    return build_graph(grid_city().osm.elements, area="grid")


def test_turn_classification(graph):
    router = Router(graph)
    east = next(e for e in graph.out_edges[node_at(graph, 1, 0)])  # (1,0) -> (1,1)
    moves = {graph.edge_to[nxt]: maneuver for nxt, maneuver, _, _ in router.moves(east)}
    assert moves[node_at(graph, 2, 1)] == "TURN_LEFT"
    assert moves[node_at(graph, 0, 1)] == "TURN_RIGHT"
    assert moves[node_at(graph, 1, 2)] == "STRAIGHT"


def test_u_turns_only_at_dead_ends(graph):
    router = Router(graph)
    west_end = node_at(graph, 1, 0)
    into_dead_end = next(e for e in graph.out_edges[node_at(graph, 1, 1)] if graph.edge_to[e] == west_end)
    out_of_dead_end = graph.out_edges[west_end][0]
    assert [m[1] for m in router.moves(into_dead_end)] == ["UTURN_LEFT"]
    assert all(graph.edge_to[m[0]] != west_end for m in router.moves(out_of_dead_end))


def test_fastest_route_turns_left_off_the_arterial(graph):
    router = Router(graph)
    (best, *_) = router.routes(point(1, 0), point(2, 2), FASTEST)
    assert visited(graph, best) == [(1, 0), (1, 1), (1, 2), (2, 2)]
    assert best.maneuvers == ("DEPART", "STRAIGHT", "TURN_LEFT")
    assert busy_crossings(router, best) == 1


def test_busy_crossing_penalty_makes_it_loop_with_right_turns(graph):
    router = Router(graph)
    (best, *_) = router.routes(point(1, 0), point(2, 2), AVOID_BUSY_CROSSINGS)
    assert busy_crossings(router, best) == 0
    assert visited(graph, best)[-1] == (2, 2)
    # The scorer, reading the same route, agrees there's no busy crossing.
    assert extract_features(router.to_route_json(best)).busy_crossing_turns == 0


def test_left_hand_traffic_avoids_busy_right_turns(graph):
    keep_left = Router(graph, TurnContext(DrivingSide.LEFT))
    (fastest, *_) = keep_left.routes(point(1, 0), point(0, 2), FASTEST)
    assert busy_crossings(keep_left, fastest) == 1  # the right turn at (1, 2)
    (careful, *_) = keep_left.routes(point(1, 0), point(0, 2), AVOID_BUSY_CROSSINGS)
    assert busy_crossings(keep_left, careful) == 0


def test_turn_restrictions_are_obeyed():
    city = grid_city()
    city.osm.restriction("no_left_turn", city.main, junction(1, 1), city.avenues[1])
    city.osm.restriction("no_left_turn", city.main, junction(1, 2), city.avenues[2])
    graph = build_graph(city.osm.elements)
    (best, *_) = Router(graph).routes(point(1, 0), point(2, 2), FASTEST)
    assert visited(graph, best) == [(1, 0), (1, 1), (1, 2), (1, 3), (2, 3), (2, 2)]


def test_crash_hotspot_pushes_the_turn_to_another_junction(graph):
    hotspots = CrashHotspots([(point(1, 2), 6)])
    router = Router(graph, TurnContext(hotspots=hotspots, min_hotspot_crashes=3))
    profile = ScoringProfile(busy_crossing_weight=0, hotspot_crossing_weight=5)
    (best, *_) = router.routes(point(1, 0), point(2, 2), profile)
    assert visited(graph, best) == [(1, 0), (1, 1), (2, 1), (2, 2)]  # turns left at (1, 1) instead


def test_alternatives_are_distinct_and_best_first(graph):
    router = Router(graph)
    routes = router.routes(point(1, 0), point(2, 2), FASTEST, count=3)
    assert len(routes) >= 2
    assert routes[0].cost == min(r.cost for r in routes)
    for i, route in enumerate(routes):
        for other in routes[i + 1 :]:
            assert router._overlap(route.edges, other.edges) <= MAX_OVERLAP
            assert router._overlap(other.edges, route.edges) <= MAX_OVERLAP


def test_alternatives_steer_clear_of_routes_already_on_offer(graph):
    router = Router(graph)
    best, second, *_ = router.routes(point(1, 0), point(2, 2), FASTEST, count=3)
    routes = router.routes(point(1, 0), point(2, 2), FASTEST, count=3, avoid=[router.path_points(second)])
    assert routes[0].edges == best.edges  # the best design stays, whatever is on offer
    assert all(router._overlap(r.edges, second.edges) <= MAX_OVERLAP for r in routes[1:])


def test_a_moving_car_starts_along_its_road(graph):
    router = Router(graph)
    on_main = midpoint(point(1, 1), point(1, 2))  # between 1 Ave and 2 Ave
    (west, *_) = router.routes(on_main, point(2, 2), FASTEST, heading=270)
    assert visited(graph, west)[:2] == [(1, 2), (1, 1)]  # carries on west, no U-turn
    (east, *_) = router.routes(on_main, point(2, 2), FASTEST, heading=90)
    assert visited(graph, east)[:2] == [(1, 1), (1, 2)]


def test_trip_outside_the_graph_is_refused(graph):
    with pytest.raises(OutsideGraphError, match="more than"):
        Router(graph).routes((43.5, -88.5), point(2, 2), FASTEST)


def test_route_json_reads_like_a_google_route(graph):
    router = Router(graph)
    (best, *_) = router.routes(point(1, 0), point(2, 2), FASTEST)
    route = router.to_route_json(best)
    steps = route["legs"][0]["steps"]
    assert [s["navigationInstruction"]["maneuver"] for s in steps] == ["DEPART", "TURN_LEFT"]
    assert route["distanceMeters"] == pytest.approx(600, abs=10)
    assert route["description"] == "Main St"

    features = extract_features(route)
    assert features.busy_crossing_turns == 1
    assert features.duration_min == pytest.approx(sum(graph.time_s[e] for e in best.edges) / 60, abs=0.02)


def grid_with_service_avenue() -> RoadGraph:
    """The grid, with 2 Ave turned into a service road as fast as a street (an alley or parking connector)."""
    city = grid_city()
    way = next(e for e in city.osm.elements if e["type"] == "way" and e["id"] == city.avenues[2])
    way["tags"] = {"highway": "service", "maxspeed": "25 mph"}
    return build_graph(city.osm.elements)


def test_service_roads_are_not_shortcuts():
    graph = grid_with_service_avenue()
    (best, *_) = Router(graph).routes(point(1, 0), point(2, 2), FASTEST)
    # Through the alley would be quickest; the real streets win.
    assert visited(graph, best) == [(1, 0), (1, 1), (2, 1), (2, 2)]


def test_a_trip_that_must_use_a_service_road_still_routes():
    city = grid_city()
    lot = city.osm.node(3000, (point(2, 2)[0] + 0.001, point(2, 2)[1]))  # a parking lot 110 m north of (2, 2)
    city.osm.way([junction(2, 2), lot], highway="service")
    graph = build_graph(city.osm.elements)
    routes = Router(graph).routes(point(1, 0), (point(2, 2)[0] + 0.001, point(2, 2)[1]), FASTEST)
    assert routes and graph.edge_to[routes[0].edges[-1]] == graph.nearest_node((point(2, 2)[0] + 0.001, point(2, 2)[1]), 5)


def test_a_turn_back_through_a_median_opening_is_one_u_turn():
    graph = build_graph(divided_road().elements)
    router = Router(graph)
    northbound_start = (point(0, 1)[0], point(0, 1)[1] + MEDIAN_HALF_LNG)
    southbound_end = (point(0, 1)[0], point(0, 1)[1] - MEDIAN_HALF_LNG)
    (best, *_) = router.routes(northbound_start, southbound_end, FASTEST)
    assert best.maneuvers == ("DEPART", "UTURN_LEFT", "STRAIGHT")  # not a left ramp and a slight left
    route = router.to_route_json(best)
    assert [s["navigationInstruction"]["maneuver"] for s in route["legs"][0]["steps"]] == ["DEPART", "UTURN_LEFT"]
    # The scorer, reading the same route with the graph, sees one U-turn across traffic at a busy junction.
    assert extract_features(route, TurnContext(junctions=graph)).busy_crossing_turns == 1


def test_a_closed_road_is_driven_around():
    graph = build_graph(grid_city().osm.elements)
    closed = RoadEvent("wi511", "closed", (point(1, 1), point(1, 2)))  # Main St between 1 Ave and 2 Ave
    (best, *_) = Router(graph).routes(point(1, 0), point(2, 2), FASTEST, events=[closed])
    assert visited(graph, best) == [(1, 0), (1, 1), (2, 1), (2, 2)]


def test_a_lane_closure_costs_like_a_risky_spot():
    graph = build_graph(grid_city().osm.elements)
    router = Router(graph)
    lanes = RoadEvent("wi511", "lanes", (point(1, 2), point(2, 2)), direction=0)  # 2 Ave northbound
    (plain, *_) = router.routes(point(1, 0), point(2, 2), FASTEST)
    (careful, *_) = router.routes(point(1, 0), point(2, 2), FASTEST, events=[lanes])
    assert visited(graph, plain)[-2:] == [(1, 2), (2, 2)]
    assert visited(graph, careful) == [(1, 0), (1, 1), (2, 1), (2, 2)]
    assert careful.cost < plain.cost + FASTEST.lane_closure_weight  # going round costs less than the merge


def test_a_car_already_on_a_closed_road_can_still_leave_it():
    graph = build_graph(grid_city().osm.elements)
    on_main = midpoint(point(1, 1), point(1, 2))
    closed = RoadEvent("wi511", "closed", (point(1, 1), point(1, 2)))
    (best, *_) = Router(graph).routes(on_main, point(2, 2), FASTEST, heading=90, events=[closed])
    assert visited(graph, best) == [(1, 1), (1, 2), (2, 2)]


def test_workers_on_site_make_a_lane_closure_cost_more():
    graph = build_graph(grid_city().osm.elements)
    router = Router(graph)
    on_2_ave = (point(1, 2), point(2, 2))
    empty = RoadEvent("wzdx", "lanes", on_2_ave, direction=0)
    crew = RoadEvent("wzdx", "lanes", on_2_ave, direction=0, workers=True, speed_limit_kmh=24)
    path = router.routes(point(1, 0), point(2, 2), FASTEST)[0]
    plain = router._path_cost(list(path.edges), router._costs(FASTEST, [], keep=()))
    with_empty = router._path_cost(list(path.edges), router._costs(FASTEST, [empty], keep=()))
    with_crew = router._path_cost(list(path.edges), router._costs(FASTEST, [crew], keep=()))
    assert plain < with_empty < with_crew


def test_a_railroad_crossing_is_driven_around_when_there_is_a_fair_alternative():
    from tests.test_roadgraph import city_with_hazards

    graph = build_graph(city_with_hazards().osm.elements)
    (best, *_) = Router(graph).routes(point(1, 0), point(2, 2), FASTEST)
    assert visited(graph, best) == [(1, 0), (1, 1), (2, 1), (2, 2)]  # not along Main St over the tracks


def test_school_zones_cost_more_only_during_school_hours():
    from tests.test_roadgraph import city_with_hazards

    graph = build_graph(city_with_hazards().osm.elements)
    router = Router(graph)
    path = router.routes(point(1, 0), point(2, 3), FASTEST)[0]
    after_school = router._path_cost(list(path.edges), router._costs(FASTEST, [], keep=()))
    drop_off = router._path_cost(list(path.edges), router._costs(FASTEST, [], keep=(), school_hours=True))
    assert drop_off > after_school + FASTEST.school_zone_weight - 0.01
    features = extract_features(router.to_route_json(path), TurnContext(junctions=graph, school_hours=True))
    assert features.school_zones == 1
