"""Bike mode: the bike map's rules and stress, the bike router, and the plan (see BIKE_MODE.md)."""

import pytest

from app.bike import PROFILES, SHORTEST, BikeRouter, plan, safety
from app.models import LatLng
from app.roadgraph import _bikeable, build_graph, traffic_stress
from tests.osm_factory import grid_city, junction, point
from tests.test_router import node_at, visited


@pytest.mark.parametrize(
    ("tags", "stress"),
    [
        ({"highway": "cycleway"}, 1),
        ({"highway": "residential"}, 1),  # 25 mph, two lanes: quiet
        ({"highway": "tertiary", "maxspeed": "30 mph", "lanes": "2"}, 2),
        ({"highway": "secondary", "maxspeed": "30 mph", "lanes": "2", "cycleway:both": "lane"}, 2),
        ({"highway": "secondary", "maxspeed": "35 mph", "cycleway": "lane"}, 3),
        ({"highway": "primary"}, 4),  # 40 mph, lanes unknown: assumed four
        ({"highway": "primary", "cycleway:right": "track"}, 1),  # protected
    ],
)
def test_traffic_stress_from_a_ways_tags(tags, stress):
    assert traffic_stress(tags) == stress


def test_who_may_ride_where():
    assert not _bikeable({"highway": "motorway"})
    assert not _bikeable({"highway": "footway", "footway": "sidewalk", "bicycle": "yes"})  # sidewalks aren't used
    assert not _bikeable({"highway": "footway"})
    assert _bikeable({"highway": "footway", "bicycle": "designated"})
    assert _bikeable({"highway": "path"})
    assert not _bikeable({"highway": "primary", "bicycle": "no"})
    assert _bikeable({"highway": "residential", "access": "no", "bicycle": "yes"})


def city_with_a_trail():
    """The test city, with a trail from 1 Ave's south end east to 3 Ave's, beside South St."""
    city = grid_city()
    trail_points = [(point(0, c)[0] - 0.0005, point(0, c)[1]) for c in (1, 2, 3)]
    trail = [city.osm.node(3000 + i, at) for i, at in enumerate(trail_points)]
    city.osm.way([junction(0, 1), trail[0], trail[1], trail[2], junction(0, 3)], highway="cycleway", name="Lakeside Trail")
    return city


def test_the_bike_map_has_the_trail_and_the_car_map_does_not():
    elements = city_with_a_trail().osm.elements
    bikes, cars = build_graph(elements, mode="bike"), build_graph(elements)
    assert bikes.mode == "bike" and "cycleway" in [bikes.class_name(e) for e in range(bikes.edge_count)]
    assert "cycleway" not in [cars.class_name(e) for e in range(cars.edge_count)]
    assert cars.mode == "car" and set(cars.stress) == {0}
    main = next(e for e in range(bikes.edge_count) if bikes.names[bikes.name[e]] == "Main St")
    assert bikes.stress[main] == 4 and bikes.speed_kmh[main] == 16.0


def test_a_family_rides_around_the_arterial_that_the_shortest_route_takes():
    graph = build_graph(grid_city().osm.elements, area="grid", mode="bike")
    router = BikeRouter(graph)
    (shortest,) = router.bike_routes(point(1, 0), point(1, 3), SHORTEST, count=1)
    assert visited(graph, shortest) == [(1, 0), (1, 1), (1, 2), (1, 3)]  # straight along Main St
    (family, *_) = router.bike_routes(point(1, 0), point(1, 3), PROFILES["family"])
    assert (1, 2) not in visited(graph, family)  # up the avenues and along North St instead


def test_a_signal_takes_the_stress_out_of_crossing():
    city = grid_city()
    graph = build_graph(city.osm.elements, area="grid", mode="bike")
    router = BikeRouter(graph)
    # Riding north up 2 Ave across Main St at (1, 2): straight across a stress 4 road.
    up = next(e for e in graph.in_edges[node_at(graph, 1, 2)] if graph.edge_from[e] == node_at(graph, 0, 2))
    on = next(e for e in graph.out_edges[node_at(graph, 1, 2)] if graph.edge_to[e] == node_at(graph, 2, 2))
    assert router.crossing_stress(up, on, "STRAIGHT") == 4
    signal = next(e for e in city.osm.elements if e["type"] == "node" and e["id"] == junction(1, 2))
    signal["tags"] = {"highway": "traffic_signals"}
    signaled = build_graph(city.osm.elements, area="grid", mode="bike")
    up = next(e for e in signaled.in_edges[node_at(signaled, 1, 2)] if signaled.edge_from[e] == node_at(signaled, 0, 2))
    on = next(e for e in signaled.out_edges[node_at(signaled, 1, 2)] if signaled.edge_to[e] == node_at(signaled, 2, 2))
    assert BikeRouter(signaled).crossing_stress(up, on, "STRAIGHT") == 0


def test_a_bike_plan_ranks_the_lower_stress_route_first_and_explains_it():
    graph = build_graph(grid_city().osm.elements, area="grid", mode="bike")
    result = plan(BikeRouter(graph), LatLng(lat=point(1, 0)[0], lng=point(1, 0)[1]), LatLng(lat=point(1, 3)[0], lng=point(1, 3)[1]), PROFILES["family"])
    winner = result["winner"]
    standard = next(c for c in result["candidates"] if c["is_standard"])
    assert winner["safety_score"] > standard["safety_score"] and winner["reason"].startswith("lowest stress")
    assert winner["stress_segments"] and winner["maneuvers"] and winner["via"]
    assert standard["features"]["minutes_by_stress"][3] > 0 and winner["features"]["minutes_by_stress"][3] < standard["features"]["minutes_by_stress"][3]


def test_the_bike_safety_score_falls_with_stress_and_crossings():
    calm = {"minutes_by_stress": [10, 2, 0, 0], "crossings": [], "crash_spots": 0, "rail_crossings": 0, "lane_closures": 0, "closures": 0, "after_dark": False}
    busy = {**calm, "minutes_by_stress": [2, 2, 4, 6], "crossings": [{"stress": 4}]}
    assert safety(calm) == (100, {})
    score, risks = safety(busy)
    assert score == round(100 / (1 + (4 * 0.5 + 6 * 1.5 + 4) / 10)) and set(risks) == {"stress_3_minutes", "stress_4_minutes", "busy_crossings"}


def test_trails_count_as_unlit_and_streets_as_lit_unless_osm_says():
    from app.roadgraph import _lit

    assert _lit({"highway": "residential"}) and not _lit({"highway": "cycleway"})
    assert _lit({"highway": "cycleway", "lit": "yes"}) and not _lit({"highway": "residential", "lit": "no"})


def test_after_dark_unlit_minutes_count_in_the_score():
    unlit = {"minutes_by_stress": [10, 0, 0, 0], "crossings": [], "crash_spots": 0, "rail_crossings": 0, "lane_closures": 0,
             "closures": 0, "unlit_minutes": 10.0}  # fmt: skip
    assert safety({**unlit, "after_dark": False})[0] == 100
    score, risks = safety({**unlit, "after_dark": True})
    assert score < 100 and set(risks) == {"unlit_minutes"}


def test_a_bike_trip_with_a_stop_goes_through_it():
    from app.bike import plan_with_stops
    from app.geo import decode_polyline, haversine_m

    graph = build_graph(grid_city().osm.elements, area="grid", mode="bike")
    stop = point(2, 1)
    places = [LatLng(lat=lat, lng=lng) for lat, lng in (point(1, 0), stop, point(1, 3))]
    result = plan_with_stops(BikeRouter(graph), places, PROFILES["everyday"])
    winner = result["winner"]
    assert min(haversine_m(p, stop) for p in decode_polyline(winner["encoded_polyline"])) < 30
    assert [m["instruction"] for m in winner["maneuvers"] if m["maneuver"] == "WAYPOINT"] == ["Arrive at stop 1"]


def test_walking_stress_comes_from_sidewalks():
    from app.roadgraph import walking_stress

    assert walking_stress({"highway": "footway"}) == 1
    assert walking_stress({"highway": "primary", "sidewalk": "both"}) == 1
    assert walking_stress({"highway": "primary", "sidewalk": "no"}) == 4
    assert walking_stress({"highway": "residential", "sidewalk": "no"}) == 2
    assert walking_stress({"highway": "residential"}) == 1 and walking_stress({"highway": "primary"}) == 3


def test_the_walking_map_goes_both_ways_and_crosses_by_traffic():
    city = grid_city(main={"oneway": "yes", "sidewalk": "both"})
    walk = build_graph(city.osm.elements, area="grid", mode="walk")
    main = [e for e in range(walk.edge_count) if walk.names[walk.name[e]] == "Main St"]
    assert len(main) == 6  # three blocks, both ways: one-way is for vehicles
    assert {walk.stress[e] for e in main} == {1} and {walk.cross[e] for e in main} == {4}  # easy along, hard to cross
    router = BikeRouter(walk)
    up = next(e for e in walk.in_edges[node_at(walk, 1, 2)] if walk.edge_from[e] == node_at(walk, 0, 2))
    on = next(e for e in walk.out_edges[node_at(walk, 1, 2)] if walk.edge_to[e] == node_at(walk, 2, 2))
    assert router.crossing_stress(up, on, "STRAIGHT") == 4


def test_step_free_walking_rules_out_steps():
    import math

    from app.bike import WALK_PROFILES

    city = grid_city()
    city.osm.way([junction(1, 1), junction(2, 2)], highway="steps")
    walk = build_graph(city.osm.elements, area="grid", mode="walk")
    router = BikeRouter(walk)
    steps = [e for e in range(walk.edge_count) if walk.class_name(e) == "steps"]
    assert steps

    def cost(profile):
        return router._trip_costs(WALK_PROFILES[profile], [], keep=[], school_hours=False, sun=None).edges

    everyday, stepfree = cost("everyday"), cost("stepfree")
    assert all(math.isfinite(everyday[e]) for e in steps) and all(stepfree[e] == math.inf for e in steps)
