import pytest

from app.geo import haversine_m
from app.roadgraph import (
    DEFAULT_SPEED_FACTOR,
    MPH_TO_KMH,
    SIGNAL_DELAY_S,
    RoadGraph,
    _direction,
    _speed_kmh,
    build_graph,
)
from tests.osm_factory import grid_city, junction, point


def node_at(graph: RoadGraph, row: int, col: int) -> int:
    node = graph.nearest_node(point(row, col), 5)
    assert node is not None
    return node


def edge_between(graph: RoadGraph, a: tuple[int, int], b: tuple[int, int]) -> int:
    start, end = node_at(graph, *a), node_at(graph, *b)
    (edge,) = [e for e in graph.out_edges[start] if graph.edge_to[e] == end]
    return edge


def test_grid_becomes_junctions_and_two_way_segments():
    graph = build_graph(grid_city().osm.elements)
    assert graph.node_count == 10  # the bend node is geometry, not a junction
    assert graph.edge_count == 26
    assert graph.stats["junctions_dropped"] == 0


def test_shape_nodes_stay_in_the_geometry():
    graph = build_graph(grid_city().osm.elements)
    edge = edge_between(graph, (0, 1), (0, 2))
    assert len(graph.edge_points(edge)) == 3
    assert graph.length_m[edge] > haversine_m(point(0, 1), point(0, 2))


def test_oneway_streets_get_one_direction():
    graph = build_graph(grid_city(north={"oneway": "yes"}).osm.elements)
    assert graph.edge_count == 24
    assert graph.oneway[edge_between(graph, (2, 1), (2, 2))]
    with pytest.raises(ValueError):
        edge_between(graph, (2, 2), (2, 1))


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"highway": "residential", "oneway": "-1"}, (False, True)),
        ({"highway": "motorway"}, (True, False)),
        ({"highway": "primary", "junction": "roundabout"}, (True, False)),
        ({"highway": "motorway", "oneway": "no"}, (True, True)),
        ({"highway": "primary", "oneway": "reversible"}, None),
    ],
)
def test_direction(tags, expected):
    assert _direction(tags) == expected


@pytest.mark.parametrize(
    ("tags", "kmh"),
    [
        ({"highway": "residential", "maxspeed": "30 mph"}, 30 * MPH_TO_KMH),
        ({"highway": "residential", "maxspeed": "50"}, 50),
        ({"highway": "residential", "maxspeed": "none"}, 25 * MPH_TO_KMH),
        ({"highway": "primary"}, 40 * MPH_TO_KMH),
    ],
)
def test_speed_parsing(tags, kmh):
    assert _speed_kmh(tags) == pytest.approx(kmh)


def test_private_and_parking_ways_are_left_out():
    city = grid_city()
    extra = city.osm.node(3000, (point(2, 3)[0] + 0.001, point(2, 3)[1]))
    city.osm.way([junction(2, 3), extra], highway="residential", access="private")
    city.osm.way([junction(2, 3), extra], highway="service", service="parking_aisle")
    graph = build_graph(city.osm.elements)
    assert graph.edge_count == 26


def test_unreachable_islands_are_dropped():
    city = grid_city()
    a = city.osm.node(4000, (43.2, -88.2))
    b = city.osm.node(4001, (43.201, -88.2))
    city.osm.way([a, b], highway="residential")
    graph = build_graph(city.osm.elements)
    assert graph.node_count == 10
    assert graph.stats["junctions_dropped"] == 2


def test_signals_add_delay_on_arrival():
    city = grid_city()
    signal = next(e for e in city.osm.elements if e["type"] == "node" and e["id"] == junction(1, 2))
    signal["tags"] = {"highway": "traffic_signals"}
    graph = build_graph(city.osm.elements)

    arriving = edge_between(graph, (1, 1), (1, 2))
    leaving = edge_between(graph, (1, 2), (1, 3))
    assert (graph.signals[arriving], graph.signals[leaving]) == (1, 0)
    free_flow = graph.length_m[arriving] / (graph.speed_kmh[arriving] / 3.6)
    assert graph.time_s[arriving] == pytest.approx(free_flow + SIGNAL_DELAY_S)
    assert graph.speed_kmh[arriving] == pytest.approx(40 * MPH_TO_KMH * DEFAULT_SPEED_FACTOR)


def test_no_turn_restriction_bans_the_transition():
    city = grid_city()
    city.osm.restriction("no_left_turn", city.main, junction(1, 2), city.avenues[2])
    graph = build_graph(city.osm.elements)

    eastbound = edge_between(graph, (1, 1), (1, 2))
    westbound = edge_between(graph, (1, 3), (1, 2))
    north = edge_between(graph, (1, 2), (2, 2))
    south = edge_between(graph, (1, 2), (0, 2))
    # The relation names ways, not directions, so both approaches lose both turns.
    assert {north, south} <= graph.banned[eastbound]
    assert {north, south} <= graph.banned[westbound]
    assert graph.stats["restrictions_applied"] == 1


def test_only_restriction_bans_every_other_exit():
    city = grid_city()
    city.osm.restriction("only_straight_on", city.main, junction(1, 2), city.main)
    graph = build_graph(city.osm.elements)

    eastbound = edge_between(graph, (1, 1), (1, 2))
    allowed = edge_between(graph, (1, 2), (1, 3))
    assert allowed not in graph.banned[eastbound]
    assert edge_between(graph, (1, 2), (2, 2)) in graph.banned[eastbound]
    assert edge_between(graph, (1, 2), (0, 2)) in graph.banned[eastbound]


def test_unsupported_and_exempt_restrictions_are_counted_not_applied():
    city = grid_city()
    city.osm.restriction("no_left_turn", city.main, junction(1, 2), city.avenues[2], **{"except": "motorcar"})
    city.osm.restriction("no_u_turn", city.main, junction(1, 2), city.main)
    city.osm.elements[-1]["members"][1] = {"type": "way", "ref": city.avenues[2], "role": "via"}
    graph = build_graph(city.osm.elements)
    assert graph.banned == {}
    assert graph.stats["restrictions_ignored"] == 1
    assert graph.stats["restrictions_unsupported"] == 1


def test_save_and_load_round_trip(tmp_path):
    city = grid_city()
    city.osm.restriction("no_left_turn", city.main, junction(1, 2), city.avenues[2])
    graph = build_graph(city.osm.elements, area="test")
    path = tmp_path / "graph.json.gz"
    graph.save(path)

    loaded = RoadGraph.load(path)
    assert (loaded.node_count, loaded.edge_count, loaded.area) == (10, 26, "test")
    assert loaded.banned == graph.banned
    assert loaded.names == graph.names
    assert loaded.time_s == pytest.approx(graph.time_s, abs=0.1)


def test_nearest_node_skips_ramps_and_respects_the_limit():
    graph = build_graph(grid_city(main={"highway": "trunk_link"}).osm.elements)
    # (1, 0) only touches Main St, now a ramp, so the nearest usable junction is (1, 1).
    assert graph.nearest_node(point(1, 0), 500) == node_at(graph, 1, 1)
    assert graph.nearest_node((43.5, -88.5), 1_000) is None
