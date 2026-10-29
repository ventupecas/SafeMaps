import pytest

from app.driving_side import DrivingSide
from app.features import TurnContext, extract_features
from app.hotspots import CrashHotspots
from app.models import ScoringProfile
from app.roadgraph import RoadGraph, build_graph
from app.router import OutsideGraphError, Router
from tests.osm_factory import grid_city, junction, point

FASTEST = ScoringProfile(busy_crossing_weight=0, hotspot_crossing_weight=0)
AVOID_BUSY_CROSSINGS = ScoringProfile(busy_crossing_weight=5, hotspot_crossing_weight=0)


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
    routes = Router(graph).routes(point(1, 0), point(2, 2), FASTEST, count=3)
    assert len(routes) >= 2
    assert len({r.edges for r in routes}) == len(routes)
    assert routes[0].cost == min(r.cost for r in routes)


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
