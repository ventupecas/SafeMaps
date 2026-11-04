from dataclasses import replace

import pytest

from app.geo import destination_point, haversine_m, initial_bearing_deg, midpoint
from app.road_events import RoadEvent
from app.roadgraph import (
    DEFAULT_SPEED_FACTOR,
    MPH_TO_KMH,
    SIGNAL_DELAY_S,
    RoadGraph,
    _direction,
    _speed_kmh,
    build_graph,
)
from tests.osm_factory import MEDIAN_HALF_LNG, divided_road, freeway_with_exits, grid_city, junction, point


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


@pytest.mark.parametrize(
    ("classes", "kind"),
    [
        ({"motorway", "motorway_link"}, "interchange"),  # a freeway fork or exit
        ({"motorway"}, "interchange"),
        ({"trunk", "trunk_link"}, "interchange"),
        ({"trunk"}, "busy"),  # two trunk roads alone may cross at grade
        ({"motorway_link", "secondary"}, "busy"),  # the end of a ramp at a surface street
        ({"primary", "residential"}, "busy"),
        ({"residential", "service"}, "quiet"),
    ],
)
def test_classify_junction(classes, kind):
    from app.roadgraph import classify_junction

    assert classify_junction(classes) == kind


def test_junction_kind_by_location():
    graph = build_graph(grid_city().osm.elements)
    assert graph.junction_kind(point(1, 2)) == "busy"  # on Main St
    assert graph.junction_kind(point(0, 2)) == "quiet"
    assert graph.junction_kind((point(0, 2)[0], point(0, 2)[1] + 0.001)) is None  # mid-block, ~80 m away


def test_junction_of_only_one_way_roads():
    # North St one-way eastbound, 3 Ave one-way southbound: where they meet, nothing comes the other way.
    graph = build_graph(grid_city(north={"oneway": "yes"}).osm.elements)
    assert graph.node_kind(node_at(graph, 2, 2)) == "quiet"  # 2 Ave is still two-way here
    city = grid_city(north={"oneway": "yes"})
    way = next(e for e in city.osm.elements if e["type"] == "way" and e["id"] == city.avenues[3])
    way["tags"]["oneway"] = "-1"
    graph = build_graph(city.osm.elements)
    assert graph.node_kind(node_at(graph, 2, 3)) == "one-way"


def test_signals_along_a_route():
    city = grid_city()
    signal = next(e for e in city.osm.elements if e["type"] == "node" and e["id"] == junction(1, 2))
    signal["tags"] = {"highway": "traffic_signals"}
    graph = build_graph(city.osm.elements)
    along_main = [point(1, 0), point(1, 3)]  # passes the signal at (1, 2)
    along_north = [point(2, 1), point(2, 3)]
    assert graph.signals_along([along_main]) == 1
    assert graph.signals_along([along_north]) == 0
    assert graph.signals_along([along_main, along_main]) == 1  # each signal counts once


def test_segment_under_a_moving_car_follows_its_heading():
    graph = build_graph(grid_city().osm.elements)
    on_main = midpoint(point(1, 1), point(1, 2))  # on Main St between 1 Ave and 2 Ave
    assert graph.segment_under(on_main, 85) == edge_between(graph, (1, 1), (1, 2))
    assert graph.segment_under(on_main, 265) == edge_between(graph, (1, 2), (1, 1))
    assert graph.segment_under(on_main, 0) is None  # no road runs north here


def test_edges_along_a_drawn_route():
    graph = build_graph(grid_city().osm.elements)
    drawn = [point(1, 0), point(1, 2), point(2, 2)]  # along Main St, then up 2 Ave
    along = graph.edges_along(drawn)
    followed = [((1, 0), (1, 1)), ((1, 1), (1, 2)), ((1, 2), (2, 2))]
    assert {edge_between(graph, a, b) for a, b in followed} <= along
    assert edge_between(graph, (1, 1), (2, 1)) not in along  # it crosses the route, doesn't follow it


def test_a_divided_road_is_not_a_one_way_street():
    graph = build_graph(divided_road().elements)
    median_opening = graph.nearest_node((point(1, 1)[0], point(1, 1)[1] + MEDIAN_HALF_LNG), 5)
    # Every road there is one-way, but oncoming traffic is just across the median.
    assert graph.node_kind(median_opening) == "busy"


def test_a_closed_stretch_follows_its_road_not_the_cross_streets():
    graph = build_graph(grid_city().osm.elements)
    main_st = (point(1, 0), point(1, 3))  # just the two ends, as 511 often gives them
    eastbound = {edge_between(graph, a, b) for a, b in [((1, 0), (1, 1)), ((1, 1), (1, 2)), ((1, 2), (1, 3))]}
    westbound = {edge_between(graph, b, a) for a, b in [((1, 0), (1, 1)), ((1, 1), (1, 2)), ((1, 2), (1, 3))]}
    assert graph.event_edges(RoadEvent("wi511", "closed", main_st)) == eastbound | westbound
    assert graph.event_edges(RoadEvent("wi511", "closed", main_st, direction=90)) == eastbound
    assert graph.event_edges(RoadEvent("wi511", "closed", main_st, direction=270)) == westbound


def test_a_stretch_whose_ends_only_a_long_drive_connects_matches_nothing():
    graph = build_graph(grid_city().osm.elements)
    # From Main St's dead end to North St: the straight line crosses no road, and driving takes a detour.
    across = (point(1, 0), (point(2, 1)[0], point(1, 0)[1] + 0.0001))
    assert graph.event_edges(RoadEvent("wi511", "closed", across)) == set()


def test_a_closure_at_a_spot_is_the_nearest_road_there():
    graph = build_graph(grid_city().osm.elements)
    middle_of_main = midpoint(point(1, 1), point(1, 2))
    assert graph.event_edges(RoadEvent("wi511", "lanes", (middle_of_main,))) == {
        edge_between(graph, (1, 1), (1, 2)),
        edge_between(graph, (1, 2), (1, 1)),
    }


def test_snapped_events_follow_the_road_one_per_direction():
    graph = build_graph(grid_city().osm.elements)
    closure = RoadEvent("wi511", "closed", (point(1, 0), point(1, 3)))
    east, west = graph.snap_events([closure])
    assert east.along and len(east.path) == 4 and east.path[0] == pytest.approx(point(1, 0))
    assert west.path[0] == pytest.approx(point(1, 3))


def test_a_ramp_closure_on_the_freeway_closes_the_named_ramp_not_the_freeway():
    graph = build_graph(freeway_with_exits().elements)
    beside_hawley = (point(0, 0)[0], point(0, 0)[1] + 1_000 / 81_400)  # placed on the freeway, 400 m past the exit
    closure = RoadEvent(
        "wzdx", "closed", (beside_hawley,), direction=90.0, road="off-ramp", ramp_road="hawley",
        description="Ramp Full Closure on I-94 EB at OFF RAMP TO HAWLEY RD",
    )  # fmt: skip
    (ramp,) = graph.event_edges(closure)
    assert graph.class_name(ramp) == "motorway_link"
    assert graph.names[graph.name[next(iter(graph.out_edges[graph.edge_to[ramp]]))]] == "North Hawley Road"
    # Unnamed, the nearest exit within RAMP_SEARCH_M wins: the 68th St one is 400 m away, too far.
    assert graph.event_edges(replace(closure, ramp_road="")) == set()
    # A ramp that can't be found is left out, rather than closing the freeway.
    assert graph.snap_events([replace(closure, ramp_road="nowhere")]) == []


def city_with_hazards():
    """The grid with a railroad crossing on Main St between 1 Ave and 2 Ave, and a school by North St and 3 Ave."""
    city = grid_city()
    crossing = city.osm.node(5000, midpoint(point(1, 1), point(1, 2)), railway="level_crossing")
    main = next(e for e in city.osm.elements if e["type"] == "way" and e["id"] == city.main)
    main["nodes"].insert(2, crossing)
    city.osm.elements.append({"type": "way", "id": 900, "tags": {"amenity": "school"}, "center": {"lat": point(2, 3)[0] + 0.0012, "lon": point(2, 3)[1]}})
    return city


def test_railroad_crossings_and_school_zones_are_built_into_the_map(tmp_path):
    graph = build_graph(city_with_hazards().osm.elements)
    crossing_both_ways = {edge_between(graph, (1, 1), (1, 2)), edge_between(graph, (1, 2), (1, 1))}
    assert {e for e in range(graph.edge_count) if graph.rail_crossings[e]} == crossing_both_ways
    assert len(graph.rail_crossing_points) == 1 and len(graph.school_points) == 1
    assert graph.school_zone[edge_between(graph, (2, 2), (2, 3))]  # North St by the school
    assert not graph.school_zone[edge_between(graph, (1, 0), (1, 1))]  # far away
    assert graph.stats["schools"] == 1 and graph.stats["rail_crossings"] == 1
    graph.save(tmp_path / "g.json.gz")
    again = RoadGraph.load(tmp_path / "g.json.gz")
    assert again.rail_crossings == graph.rail_crossings and again.school_zone == graph.school_zone
    assert again.rail_crossings_along([[point(1, 0), point(1, 3)]]) == 1  # Main St passes the crossing
    assert again.rail_crossings_at([[point(1, 0), point(1, 3)]]) == again.rail_crossing_points  # where, for the map
    assert again.schools_along([[point(2, 1), point(2, 3)]]) == 1


def test_a_map_built_before_hazards_still_loads(tmp_path):
    import gzip
    import json

    build_graph(grid_city().osm.elements).save(tmp_path / "old.json.gz")
    with gzip.open(tmp_path / "old.json.gz", "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    for key in ("rail_crossings", "school"):
        del data["edges"][key]
    del data["rail_crossing_points"], data["school_points"]
    with gzip.open(tmp_path / "old.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(data, handle)
    graph = RoadGraph.load(tmp_path / "old.json.gz")
    assert list(graph.rail_crossings) == [0] * graph.edge_count and not any(graph.school_zone)


def test_a_traffic_segment_is_matched_waypoint_by_waypoint():
    graph = build_graph(grid_city().osm.elements)
    # Along Main St, then up 2 Ave: a few waypoints, as 511 travel times give them.
    waypoints = (point(1, 0), point(1, 1), point(1, 2), midpoint(point(1, 2), point(2, 2)), point(2, 2))
    jam = RoadEvent("wi511", "jam", waypoints, along=True, category="traffic")
    followed = [((1, 0), (1, 1)), ((1, 1), (1, 2)), ((1, 2), (2, 2))]
    assert graph.event_edges(jam) == {edge_between(graph, a, b) for a, b in followed}


def test_a_short_line_drawn_with_many_points_is_matched_from_its_ends():
    graph = build_graph(grid_city().osm.elements)
    # 48 m of Main St in nine points, as TomTom draws a short closure: no two of them a waypoint apart.
    east = initial_bearing_deg(point(1, 1), point(1, 2))
    line = tuple(destination_point(midpoint(point(1, 1), point(1, 2)), east, 6 * k) for k in range(9))
    assert graph.event_edges(RoadEvent("tomtom", "closed", line, along=True)) == {edge_between(graph, (1, 1), (1, 2))}
