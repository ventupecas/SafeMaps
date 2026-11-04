"""The compiled geometry against the plain Python it replaced (tests/reference_geometry.py), on random inputs.

Floats may differ by rounding: within 1e-6 m, or 1e-9 relative. Every yes or no, count, id and name must match
exactly. The inputs lean on what goes wrong: empty lists, single points, the same point twice, tiny and huge
segments, points on cell boundaries, places far off the map, and both sides of the equator and the date line.
"""

import math
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

import app.search_kernel as kernel
from app import geo, pricing, road_events
from app.features import extract_features, is_winding, _share_into_sun, TurnContext
from app.hotspots import CrashHotspots
from app.models import ScoringProfile
from app.road_events import RoadEvent
from app.roadgraph import RoadGraph, build_graph
from app.router import MAX_START_TURN_DEG, OutsideGraphError, Router
from tests import reference_geometry as old
from tests.test_search_kernel import plain_search, random_city

SEEDS = range(6)
CELLS = (0.0005, 0.002, 0.005, 20 / 111_320, 30 / 111_320, 50 / 111_320, 200 / 111_320, 0.001)
# (lat, lng) to build paths around: the map, then each way a sign or a wrap could go wrong.
PLACES = (
    *(((43.0389, -87.9065),) * 6),
    (43.5, -88.6),  # past the map's far corner
    (0.00001, -0.00001),  # both sides of the equator and the prime meridian
    (-33.9, 151.2),  # south and east
    (51.5, 179.9999),  # at the date line
    (64.1, -21.9),  # where longitude cells narrow
    (85.0, 10.0),
)


def close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def same_points(new, expected) -> bool:
    """Within 1e-10 degrees: about 10 micrometers. Longitudes may wrap at the date line."""
    return len(new) == len(expected) and all(
        abs(a[0] - b[0]) <= 1e-10 and (abs(a[1] - b[1]) <= 1e-10 or abs(abs(a[1] - b[1]) - 360) <= 1e-10) for a, b in zip(new, expected, strict=True)
    )


def on_a_cell_boundary(rng: random.Random, value: float) -> float:
    cell = rng.choice(CELLS)
    return round(value / cell) * cell + rng.choice((0.0, 0.0, 1e-12, -1e-12, 1e-9, -1e-9))


def nasty_path(rng: random.Random, around: tuple[float, float] | None = None, most: int = 40) -> list[tuple[float, float]]:
    """A path with whatever a path can have wrong with it. May be empty."""
    lat, lng = around or rng.choice(PLACES)
    lat += rng.uniform(-0.01, 0.01)
    lng += rng.uniform(-0.01, 0.01)
    points: list[tuple[float, float]] = []
    for _ in range(rng.choice((0, 1, 2, 2, 3, 5, 12, most))):
        kind = rng.random()
        if points and kind < 0.15:
            pass  # the same point again
        elif kind < 0.3:
            lat += rng.uniform(-1e-6, 1e-6)  # under a meter
            lng += rng.uniform(-1e-6, 1e-6)
        elif kind < 0.75:
            lat += rng.uniform(-0.002, 0.002)  # an ordinary stretch of road
            lng += rng.uniform(-0.002, 0.002)
        elif kind < 0.85:
            lat += rng.uniform(-0.04, 0.04)  # kilometers
            lng += rng.uniform(-0.04, 0.04)
        else:
            lat, lng = on_a_cell_boundary(rng, lat), on_a_cell_boundary(rng, lng)
        lat = max(-89.99, min(89.99, lat))
        lng = (lng + 540) % 360 - 180
        points.append((lat, lng))
    return points


def random_drive(graph: RoadGraph, rng: random.Random, segments: int) -> list[tuple[float, float]]:
    """A drive along the map's roads, as the points a route has."""
    edge = rng.randrange(graph.edge_count)
    edges = [edge]
    while len(edges) < segments:
        onward = graph.out_edges[graph.edge_to[edges[-1]]]
        if not onward:
            break
        edges.append(rng.choice(onward))
    return graph.path_points(edges)


def random_event(rng: random.Random, route: list[tuple[float, float]]) -> RoadEvent:
    """An event on the route, beside it, against it, across it or nowhere near it. Never with an empty path."""
    path = nasty_path(rng, route[0] if route else None, most=30) if not route or rng.random() < 0.15 else []
    if route and not path:
        start = rng.randrange(len(route))
        path = route[start : start + rng.choice((1, 2, 3, 8, 30, 200))]
        path = path[:: rng.choice((1, 1, 1, 2, 5))]
        if rng.random() < 0.3:
            path = path[::-1]
        shift_lat, shift_lng = rng.choice(((0.0, 0.0), (0.0, 0.0), (0.00003, 0.0), (0.0, 0.0002), (0.00012, 0.00012), (0.0003, 0.0)))
        path = [(lat + shift_lat, lng + shift_lng) for lat, lng in path]
    path = path or [(43.0389, -87.9065)]
    heading = geo.initial_bearing_deg(path[0], path[-1]) if len(path) > 1 else rng.uniform(0, 360)
    return RoadEvent(
        rng.choice(("wzdx", "wi511", "tomtom", "google", "apify")),
        rng.choice(("closed", "lanes", "lanes", "hazard", "jam", "slow")),
        tuple(path),
        direction=rng.choice((None, None, 0.0, 90.0, 180.0, 270.0, 359.9, rng.uniform(0, 360), heading, (heading + 180) % 360)),
        along=rng.random() < 0.5,
        road=rng.choice(("any", "any", "any", "mainline", "ramp", "on-ramp", "off-ramp")),
        workers=rng.random() < 0.4,
        delay_min=rng.choice((0.0, 0.0, 2.5, 11.0)),
        slowdown=rng.choice((None, 2.0)),
        speed_limit_kmh=rng.choice((None, 40.0)),
    )


def points_on_the_map(graph: RoadGraph, rng: random.Random, count: int) -> list[tuple[float, float]]:
    """On junctions and roads, a little off them, on cell boundaries, and past the map's edges."""
    lats, lngs = list(graph.lat), list(graph.lng)
    south, north, west, east = min(lats), max(lats), min(lngs), max(lngs)
    found = []
    for _ in range(count):
        kind = rng.random()
        if kind < 0.2:
            lat, lng = graph.point(rng.randrange(graph.node_count))
        elif kind < 0.45:
            lat, lng = rng.choice(graph.edge_points(rng.randrange(graph.edge_count)))
        elif kind < 0.7:
            lat, lng = rng.choice(graph.edge_points(rng.randrange(graph.edge_count)))
            lat += rng.uniform(-0.0005, 0.0005)
            lng += rng.uniform(-0.0007, 0.0007)
        elif kind < 0.8:
            lat, lng = on_a_cell_boundary(rng, rng.uniform(south, north)), on_a_cell_boundary(rng, rng.uniform(west, east))
        elif kind < 0.9:
            lat, lng = rng.uniform(south, north), rng.choice((west, east)) + rng.uniform(-0.03, 0.03)
        elif kind < 0.97:
            lat, lng = rng.choice((south, north)) + rng.uniform(-0.03, 0.03), rng.uniform(west, east)
        else:
            lat, lng = rng.uniform(-80, 80), rng.uniform(-179, 179)
        found.append((lat, lng))
    return found


@pytest.fixture(scope="module", params=SEEDS)
def city(request):
    """A random city, as the compiled code has it and as the plain code does."""
    graph = random_city(request.param)
    return graph, old.ReferenceMap(graph), random.Random(request.param * 31 + 7)


# ---- Polylines and whole paths


@pytest.mark.parametrize("seed", SEEDS)
def test_polylines_decode_and_encode_as_they_did(seed):
    rng = random.Random(seed)
    for _ in range(300):
        path = nasty_path(rng)
        for precision in (5, 6):
            encoded = old.encode_polyline(path, precision)
            assert geo.encode_polyline(path, precision) == encoded
            assert geo.encode_polyline(iter(path), precision) == encoded
            assert geo.decode_polyline(encoded, precision) == old.decode_polyline(encoded, precision)


@pytest.mark.parametrize("value", [0.000005, 0.000015, 0.000025, -0.000005, -0.000015, 1.5e-5, 43.038885, -87.906505, 2.5e-6, 0.0])
def test_rounding_halves_when_encoding_is_as_it_was(value):
    points = [(value, -value), (value, value), (0.0, value)]
    assert geo.encode_polyline(points) == old.encode_polyline(points)
    assert geo.encode_polyline(points, 6) == old.encode_polyline(points, 6)


@pytest.mark.parametrize("seed", SEEDS)
def test_broken_polylines_fail_as_they_did(seed):
    rng = random.Random(seed)
    alphabet = [chr(code) for code in (*range(63, 127), 0, 1, 40, 62, 200, 255)]
    failed = 0
    for _ in range(1500):
        text, run = "", 0
        for _ in range(rng.randrange(0, 40)):
            char = rng.choice(alphabet)
            run = run + 1 if ord(char) - 63 >= 0x20 else 0
            if run > 11:  # a number too long for 64 bits: the plain code's integers never overflowed (see the report)
                char, run = "?", 0
            text += char
        try:
            expected = old.decode_polyline(text)
        except ValueError:
            failed += 1
            with pytest.raises(ValueError):
                geo.decode_polyline(text)
            continue
        # Junk decodes to numbers too big to be places: beyond 2**53 they may round differently, by the last digit.
        new = geo.decode_polyline(text)
        assert len(new) == len(expected) and all(
            math.isclose(a[0], b[0], rel_tol=1e-12) and math.isclose(a[1], b[1], rel_tol=1e-12) for a, b in zip(new, expected, strict=True)
        ), repr(text)
    assert failed > 100


@pytest.mark.parametrize("seed", SEEDS)
def test_paths_measure_as_they_did(seed):
    rng = random.Random(seed)
    for _ in range(300):
        path = nasty_path(rng)
        assert close(geo.path_length_m(path), old.path_length_m(path))
        for spacing in (5.0, 15.0, 25.0, 100.0, 5000.0):
            assert same_points(geo.densify(path, spacing), old.densify(path, spacing)), (path, spacing)
        for minimum in (30.0, 0.0, 5.0, 1000.0):
                # To a thousandth of a degree: over a segment a tenth of a millimeter long, a bearing is mostly rounding.
            assert math.isclose(geo.total_heading_change_deg(path, minimum), old.total_heading_change_deg(path, minimum), abs_tol=1e-3), (path, minimum)
        if path:
            assert is_winding(path) == _old_is_winding(path)


def _old_is_winding(points) -> bool:
    from app.features import MIN_WINDING_STEP_M, WINDING_DEG_PER_KM

    length = old.path_length_m(points)
    return length >= MIN_WINDING_STEP_M and old.total_heading_change_deg(points) / (length / 1000) >= WINDING_DEG_PER_KM


@pytest.mark.parametrize("seed", SEEDS)
def test_the_share_driven_into_a_low_sun_is_as_it_was(seed):
    rng = random.Random(seed)
    for _ in range(400):
        path = nasty_path(rng)
        sun = (rng.choice((0.0, 359.9, 180.0, rng.uniform(0, 360))), rng.uniform(0.1, 24.9))
        assert close(_share_into_sun(path, sun), old.share_into_sun(path, sun)), (path, sun)
        assert _share_into_sun(path, None) == 0.0


def test_a_high_sun_or_none_adds_no_glare_to_a_route():
    """_share_into_sun itself doesn't look at how high the sun is any more: extract_features has to, and does."""
    points = [(43.0, -87.9), (43.01, -87.9)]
    step = {
        "distanceMeters": 1111,
        "staticDuration": "60s",
        "polyline": {"encodedPolyline": geo.encode_polyline(points)},
        "navigationInstruction": {"maneuver": "STRAIGHT"},
    }
    route = {"distanceMeters": 1111, "duration": "60s", "staticDuration": "60s", "legs": [{"steps": [step]}]}
    assert extract_features(route, TurnContext(sun=(0.0, 60.0))).sun_glare_min == 0.0
    assert extract_features(route, TurnContext(sun=(0.0, -3.0))).sun_glare_min == 0.0
    assert extract_features(route, TurnContext(sun=None)).sun_glare_min == 0.0
    assert extract_features(route, TurnContext(sun=(0.0, 10.0))).sun_glare_min == pytest.approx(1.0)


# ---- Which events a route drives through


def check_events(route, events, label) -> tuple[int, int, int]:
    expected = old.count_on_route(route, events)
    assert road_events.count_on_route(route, events) == expected, label
    assert road_events.weather_on_route(route, events) == old.weather_on_route(route, events), label
    assert close(road_events.delay_on_route(route, events), old.delay_on_route(route, events)), label
    return expected


def test_events_on_routes_count_as_they_did(city):
    graph, _, rng = city
    totals = [0, 0, 0]
    hazards = 0
    delays = 0
    for trial in range(150):
        route = random_drive(graph, rng, rng.choice((1, 2, 4, 10, 25)))
        events = [random_event(rng, route) for _ in range(rng.randrange(0, 9))]
        counted = check_events(route, events, trial)
        # The same events judged again, as a tuple, the way a trip's are: they're flattened once and kept.
        check_events(route, tuple(events), trial)
        for number in range(3):
            totals[number] += counted[number]
        hazards += old.weather_on_route(route, events)
        delays += old.delay_on_route(route, events) > 0
    assert all(totals) and hazards and delays, "the random events should hit something of each kind"


def test_events_on_nasty_routes_count_as_they_did():
    rng = random.Random(4)
    for trial in range(1500):
        route = nasty_path(rng)
        events = [random_event(rng, route) for _ in range(rng.randrange(0, 6))]
        check_events(route, events, (trial, route, events))


def test_each_route_a_trip_judges_gets_its_own_answer_from_the_kept_events():
    rng = random.Random(11)
    graph = random_city(11)
    routes = [random_drive(graph, rng, 12) for _ in range(6)]
    events = tuple(random_event(rng, routes[0]) for _ in range(12)) + tuple(random_event(rng, routes[1]) for _ in range(12))
    for _ in range(3):  # around again: the routes' indexes and the events' table are kept
        for route in routes:
            check_events(route, events, route)
    for _ in range(road_events._event_tables.limit + 3):  # past what's kept, and new tuples with the ids of old ones
        fresh = tuple(random_event(rng, routes[0]) for _ in range(3))
        check_events(routes[0], fresh, fresh)
        del fresh


def test_official_closures_hide_tomtoms_repeats_as_they_did():
    rng = random.Random(21)
    graph = random_city(21)
    kept = hidden = 0
    for _ in range(250):
        route = random_drive(graph, rng, rng.choice((2, 5, 15)))
        events = []
        for _ in range(rng.randrange(0, 8)):
            event = random_event(rng, route)
            events.append(RoadEvent(rng.choice(("wzdx", "wi511", "tomtom", "tomtom")), event.kind, event.path, direction=event.direction, along=event.along))
        expected = old.without_repeats(events)
        assert road_events.without_repeats(events) == expected
        kept += len(expected)
        hidden += len(events) - len(expected)
    assert kept and hidden


# ---- How well a route follows a design


@pytest.mark.parametrize("seed", SEEDS)
def test_design_match_is_as_it_was(seed):
    rng = random.Random(seed)
    graph = random_city(seed)
    for _ in range(120):
        designed = random_drive(graph, rng, rng.choice((1, 3, 12))) if rng.random() < 0.7 else nasty_path(rng)
        actual = designed[:: rng.choice((1, 2))] if rng.random() < 0.5 and designed else random_drive(graph, rng, rng.choice((1, 3, 12)))
        for tolerance in (30.0, 10.0, 60.0, 120.0):
            assert close(pricing.design_match(designed, actual, tolerance), old.design_match(designed, actual, tolerance))


@pytest.mark.parametrize("seed", SEEDS)
def test_the_near_index_answers_as_it_did(seed):
    rng = random.Random(seed)
    for _ in range(80):
        path = nasty_path(rng)
        tolerance = rng.choice((10.0, 30.0, 75.0))
        new = pricing._NearIndex(path, tolerance)
        expected = old.NearIndex(old.densify(path, tolerance / 2), tolerance)
        probes = [(lat + rng.uniform(-0.0006, 0.0006), lng + rng.uniform(-0.0008, 0.0008)) for lat, lng in path[:8]] + nasty_path(rng, path[0] if path else None)
        for probe in probes:
            assert new.near(probe) == expected.near(probe), (path, tolerance, probe)


# ---- A map's lookups


def test_nearest_junctions_are_as_they_were(city):
    graph, plain, rng = city
    for point in points_on_the_map(graph, rng, 300):
        for distance in (30.0, 100.0, 100.5, 1000.0, 5.0):
            for snappable in (True, False):
                assert graph._nearest(point, distance, snappable_only=snappable) == plain.nearest(point, distance, snappable_only=snappable), (point, distance, snappable)
        assert graph.nearest_node(point) == plain.nearest(point, 1000.0, snappable_only=True)


def test_junctions_near_a_point_come_in_the_same_order(city):
    graph, plain, rng = city
    for point in points_on_the_map(graph, rng, 150):
        for distance in (35.0, 50.0, 100.0, 300.0, 1500.0):
            for snappable in (True, False):
                assert graph._nodes_near(point, distance, snappable_only=snappable) == list(plain.nodes_near(point, distance, snappable_only=snappable))


def test_junction_kinds_are_as_they_were(city):
    graph, plain, rng = city
    for node in range(graph.node_count):
        assert graph.node_kind(node) == plain.node_kind(node), node
    for point in points_on_the_map(graph, rng, 200):
        assert graph.junction_kind(point) == plain.junction_kind(point), point


def test_signals_at_a_point_are_as_they_were(city):
    graph, plain, rng = city
    for point in points_on_the_map(graph, rng, 400):
        assert graph.signal_at(point) == plain.signal_at(point), point


def test_what_stretches_of_road_pass_is_as_it_was(city):
    graph, plain, rng = city
    seen = [0, 0, 0]
    for _ in range(80):
        stretches = []
        for _ in range(rng.choice((0, 1, 1, 2, 5))):
            kind = rng.random()
            if kind < 0.1:
                stretches.append([])
            elif kind < 0.2:
                stretches.append([rng.choice(points_on_the_map(graph, rng, 1))])
            elif kind < 0.8:
                stretches.append(random_drive(graph, rng, rng.choice((1, 3, 10))))
            else:
                stretches.append(nasty_path(rng, graph.point(0)))
        signals = plain.signals_along(stretches)
        rails, schools = plain.rail_crossings_at(stretches), plain.schools_at(stretches)
        assert graph.signals_along(stretches) == signals, stretches
        assert graph.rail_crossings_at(stretches) == rails, stretches
        assert graph.schools_at(stretches) == schools, stretches
        assert graph.rail_crossings_along(stretches) == len(rails)
        assert graph.schools_along(stretches) == len(schools)
        seen = [seen[0] + signals, seen[1] + len(rails), seen[2] + len(schools)]
    assert seen[0] and seen[2], "the stretches should pass signals and schools"


def test_segments_a_route_follows_are_as_they_were(city):
    graph, plain, rng = city
    for _ in range(80):
        points = random_drive(graph, rng, rng.choice((1, 3, 15))) if rng.random() < 0.7 else nasty_path(rng, graph.point(0))
        for within in (20.0, 5.0, 100.0, 150.0):
            assert graph.edges_along(points, within) == plain.edges_along(points, within), (points, within)


def nearest_first(plain: old.ReferenceMap, point, new: list[int], expected: list[int]) -> bool:
    """The same segments, nearest first. Two that end at the same junction are exactly as near as each other, and which
    comes first is down to rounding in the last digit of a distance: within a micrometer, they may swap."""
    if sorted(new) != sorted(expected):
        return False
    offset = {edge: old.distance_to_path_m(point, plain.edge_points(edge))[0] for edge in new}
    return all(a == b or abs(offset[a] - offset[b]) <= 1e-6 for a, b in zip(new, expected, strict=True))


def test_the_segment_under_a_car_is_as_it_was(city):
    graph, plain, rng = city
    matched = 0
    for point in points_on_the_map(graph, rng, 200):
        heading = rng.choice((0.0, 90.0, 180.0, 270.0, 359.9, rng.uniform(0, 360)))
        for road in ("any", "ramp", "on-ramp", "off-ramp", "mainline"):
            expected = plain.segments_under(point, heading, road)
            assert nearest_first(plain, point, graph._segments_under(point, heading, road), expected), (point, heading, road, expected)
            matched += bool(expected)
    assert matched > 100


def test_a_heading_in_whole_degrees_answers_like_any_other(city):
    """The app's headings are whole numbers (engine: heading: int), and compiled code is compiled again for each type."""
    graph, plain, rng = city
    for point in points_on_the_map(graph, rng, 60):
        heading = rng.randrange(360)
        assert nearest_first(plain, point, graph._segments_under(point, heading), plain.segments_under(point, float(heading)))
        assert graph._roads_at(point, heading) == plain.roads_at(point, float(heading))
        assert graph.segment_under(point, heading) == graph.segment_under(point, float(heading))


def test_the_road_at_a_point_is_as_it_was(city):
    graph, plain, rng = city
    matched = 0
    for point in points_on_the_map(graph, rng, 300):
        for direction in (None, 0.0, 90.0, 225.5, 359.9, rng.uniform(0, 360)):
            for road in ("any", "ramp", "on-ramp", "off-ramp", "mainline"):
                expected = plain.roads_at(point, direction, road)
                assert graph._roads_at(point, direction, road) == expected, (point, direction, road)
                matched += bool(expected)
        heading = rng.uniform(0, 360)
        names = plain.road_names_at(point, heading)
        name = graph.road_name_at(point, heading)
        assert name in names if names else name == "", (point, heading)
    assert matched > 100


def test_segments_in_a_box_are_as_they_were(city):
    graph, plain, rng = city
    for _ in range(200):
        south, west = rng.choice(points_on_the_map(graph, rng, 3))
        height, width = rng.choice((0.0, 0.0001, 0.002, 0.02, 0.2)), rng.choice((0.0, 0.0001, 0.002, 0.02, 0.2))
        assert graph.edges_in_box(south, west, south + height, west + width) == plain.edges_in_box(south, west, south + height, west + width)


def test_crashes_near_each_junction_are_as_they_were(city):
    graph, plain, rng = city
    crashes = []
    for _ in range(60):
        lat, lng = graph.point(rng.randrange(graph.node_count))
        # Around the 35 m that counts as near, each way, a few millimeters clear of it so rounding can't decide.
        away = rng.choice((0.0, 5.0, 34.99, 35.01, 60.0, 200.0))
        lat, lng = old.destination_point((lat, lng), rng.uniform(0, 360), away)
        crashes.append(((lat, lng), rng.choice((1.0, 1.0, 2.0, 0.5, 7.0))))
    hotspots = CrashHotspots(crashes)
    router = Router(graph, TurnContext(hotspots=hotspots))
    for node in range(graph.node_count):
        assert close(router.crashes_at(node), plain.crashes_at(node, hotspots)), node


@pytest.mark.parametrize("base", [(-0.004, -0.008), (-33.87, 151.2), (51.5, -0.01)], ids=["equator-and-meridian", "south-and-east", "greenwich"])
def test_a_map_anywhere_on_earth_answers_as_it_did(base, monkeypatch):
    """A grid cell's key mixes its row and column: maps where they're negative, or change sign, have to file and find alike."""
    monkeypatch.setattr("tests.test_search_kernel.BASE", base)
    graph = random_city(2)
    plain = old.ReferenceMap(graph)
    rng = random.Random(2)
    for point in points_on_the_map(graph, rng, 150):
        heading = rng.uniform(0, 360)
        assert graph.nearest_node(point) == plain.nearest(point, 1000.0, snappable_only=True)
        assert graph.junction_kind(point) == plain.junction_kind(point)
        assert graph.signal_at(point) == plain.signal_at(point)
        assert nearest_first(plain, point, graph._segments_under(point, heading), plain.segments_under(point, heading))
        assert graph._roads_at(point, heading) == plain.roads_at(point, heading)
        assert graph._nodes_near(point, 300.0, snappable_only=False) == list(plain.nodes_near(point, 300.0, snappable_only=False))
    for _ in range(40):
        drive = random_drive(graph, rng, rng.choice((1, 4, 12)))
        assert graph.edges_along(drive) == plain.edges_along(drive)
        assert graph.signals_along([drive]) == plain.signals_along([drive])
        assert graph.schools_at([drive]) == plain.schools_at([drive])
        assert graph.rail_crossings_at([drive]) == plain.rail_crossings_at([drive])
        south, west = rng.choice(drive)
        assert graph.edges_in_box(south, west, south + 0.01, west + 0.01) == plain.edges_in_box(south, west, south + 0.01, west + 0.01)
    assert [graph.node_kind(node) for node in range(graph.node_count)] == [plain.node_kind(node) for node in range(graph.node_count)]


def test_map_columns_read_as_lists_did(city):
    graph, plain, _ = city
    for edge in range(graph.edge_count):
        assert graph.edge_points(edge) == plain.edge_points(edge)
        assert graph.geometry[edge] == plain.geometry[edge]
    assert list(graph.geometry) == plain.geometry and len(graph.geometry) == graph.edge_count
    assert list(graph.out_edges) == plain.out_edges and list(graph.in_edges) == plain.in_edges
    for node in range(graph.node_count):
        assert graph.out_edges[node] == plain.out_edges[node] and graph.in_edges[node] == plain.in_edges[node]


def test_a_map_saved_and_loaded_is_the_same_map(city, tmp_path):
    graph, _, _ = city
    path = tmp_path / "map.json.gz"
    graph.save(path)
    again = RoadGraph.load(path)
    assert list(again.geometry) == list(graph.geometry)
    for name in ("edge_from", "edge_to", "signals", "road_class", "name", "way_id", "oneway", "winding", "rail_crossings", "school_zone", "stress", "lit", "cross"):
        assert list(getattr(again, name)) == list(getattr(graph, name)), name
    for name, digits in (("lat", 7), ("lng", 7), ("length_m", 1), ("speed_kmh", 1), ("bearing_start", 1), ("bearing_end", 1)):
        assert list(getattr(again, name)) == [round(x, digits) for x in getattr(graph, name)], name
    assert again.banned == graph.banned and again.names == graph.names and again.classes == graph.classes
    second = tmp_path / "again.json.gz"
    again.save(second)
    assert list(RoadGraph.load(second).geometry) == list(graph.geometry)


# ---- A moving car, and readings that aren't numbers


def test_a_moving_car_off_every_road_starts_ahead_from_the_nearest_junction():
    """No segment under the car: it starts from the nearest junction, but not back the way it came."""
    graph = random_city(3)
    router = Router(graph)
    plain = old.ReferenceMap(graph)
    rng = random.Random(3)
    checked = 0
    for point in points_on_the_map(graph, rng, 300):
        heading = rng.uniform(0, 360)
        start = plain.nearest(point, 1000.0, snappable_only=True)
        if plain.segments_under(point, heading) or start is None:
            continue
        edges = plain.out_edges[start]
        ahead = [edge for edge in edges if old.angle_between_deg(graph.bearing_start[edge], heading) <= MAX_START_TURN_DEG]
        assert router._departures(point, heading) == (ahead or edges), (point, heading)
        checked += 1
    assert checked > 10


@pytest.mark.parametrize("weight", ["rail_crossing_weight", "time_weight", "busy_crossing_weight", "hotspot_crossing_weight"])
@pytest.mark.parametrize("value", [math.inf, math.nan])
def test_a_weight_that_is_not_a_real_number_is_turned_away(weight, value):
    """A request's weights can be 1e999, which JSON reads as infinity: 0 * infinity would make costs that aren't numbers."""
    with pytest.raises(ValueError):
        ScoringProfile(**{weight: value})


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_a_search_through_costs_that_are_not_numbers_ends_and_uses_none_of_them(bad):
    """Whatever else goes wrong upstream, the search stays on real costs: this one is a road closed in every direction."""
    graph = random_city(1)
    router = Router(graph)
    rng = random.Random(1)
    costs = router._trip_costs(ScoringProfile(), [], keep=[], school_hours=False, sun=None)
    blocked = np.flatnonzero(rng.choices([True, False], weights=[1, 3], k=graph.edge_count))
    costs.edges[blocked] = bad
    table = router._steps()
    step_costs = router._step_costs(costs)
    zones = router._zone_table(costs)
    workspace = kernel.Workspace(graph.node_count, graph.edge_count)
    for _ in range(40):
        start, end = rng.randrange(graph.edge_count), rng.randrange(graph.node_count)
        starts = np.array([start], dtype=np.int32)
        radius = workspace.reverse_bound(end, graph.column("edge_to")[starts], 1.1, costs.edges, graph.in_edges.column_ptr(), graph.in_edges.column_edges(), graph.column("edge_from"))
        path, _, _, _ = workspace.search(starts, end, costs.edges, step_costs, router._step_table(), zones, radius, 0.0, graph.column("lat"), graph.column("lng"))
        assert not set(path.tolist()[1:]) & set(blocked.tolist())
    assert table.first.size


# ---- Maps with next to nothing on them


def tiny_map(lat, lng, edge_from=(), edge_to=(), geometry=()) -> RoadGraph:
    count = len(edge_from)
    return RoadGraph(
        area="tiny",
        bbox=None,
        lat=list(lat),
        lng=list(lng),
        edge_from=list(edge_from),
        edge_to=list(edge_to),
        length_m=[10.0] * count,
        speed_kmh=[40.0] * count,
        signals=[0] * count,
        road_class=[0] * count,
        name=[0] * count,
        way_id=[1] * count,
        oneway=[False] * count,
        winding=[False] * count,
        bearing_start=[0.0] * count,
        bearing_end=[0.0] * count,
        geometry=list(geometry),
        classes=["residential"],
        names=[""],
        banned={},
    )


STRAIGHT = [(43.0, -87.9), (43.001, -87.9)]
TINY_MAPS = {
    "no-junctions": dict(lat=[], lng=[]),
    "one-junction": dict(lat=[43.0], lng=[-87.9]),
    "zero-length-segment": dict(lat=[43.0, 43.0], lng=[-87.9, -87.9], edge_from=[0], edge_to=[1], geometry=[old.encode_polyline(STRAIGHT[:1] * 2)]),
    "two-way-street": dict(
        lat=[43.0, 43.001], lng=[-87.9, -87.9], edge_from=[0, 1], edge_to=[1, 0], geometry=[old.encode_polyline(STRAIGHT), old.encode_polyline(STRAIGHT[::-1])]
    ),
}


@pytest.mark.parametrize("name", TINY_MAPS)
def test_a_map_with_next_to_nothing_answers_every_lookup_as_it_did(name):
    graph = tiny_map(**TINY_MAPS[name])
    plain = old.ReferenceMap(graph)
    router = Router(graph)
    for point in [(43.0, -87.9), (43.0005, -87.9001), (0.0, 0.0), (-89.0, 179.9), (89.0, -179.9)]:
        assert graph.nearest_node(point) == plain.nearest(point, 1000.0, snappable_only=True)
        assert graph.junction_kind(point) == plain.junction_kind(point)
        assert graph.signal_at(point) == plain.signal_at(point)
        assert graph._segments_under(point, 0.0) == plain.segments_under(point, 0.0)
        assert graph._roads_at(point, None) == plain.roads_at(point, None)
        assert graph.road_name_at(point, 0.0) == ""
        assert graph.edges_along([point, point]) == plain.edges_along([point, point])
        assert graph.signals_along([[point]]) == plain.signals_along([[point]])
        assert graph.rail_crossings_at([[point]]) == [] and graph.schools_at([[point]]) == []
        assert graph.edges_in_box(point[0], point[1], point[0] + 0.001, point[1] + 0.001) == plain.edges_in_box(point[0], point[1], point[0] + 0.001, point[1] + 0.001)
    assert graph.signals_along([]) == 0 and graph.edges_along([]) == set()
    assert [graph.node_kind(node) for node in range(graph.node_count)] == [plain.node_kind(node) for node in range(graph.node_count)]
    for edge in range(graph.edge_count):
        assert graph.edge_points(edge) == plain.edge_points(edge)
        assert router.steps(edge) is not None
    if graph.edge_count == 0:
        with pytest.raises(OutsideGraphError):
            router.routes((43.0, -87.9), (43.0, -87.9), ScoringProfile())


def test_a_segment_with_a_shape_of_one_point_is_under_nothing():
    """Not on a real map: the plain code raised here. The compiled code says no road is there, which is as good."""
    graph = tiny_map([43.0, 43.0], [-87.9, -87.9], [0], [1], [old.encode_polyline(STRAIGHT[:1])])
    assert graph._segments_under((43.0, -87.9), 0.0) == []
    assert graph._roads_at((43.0, -87.9), None) == set()


def test_a_trip_with_no_events_or_no_route_has_nothing_on_it():
    line = [(43.0, -87.9), (43.001, -87.9)]
    closed = RoadEvent("wzdx", "closed", tuple(line))
    for events in ([], ()):
        assert road_events.count_on_route(line, events) == (0, 0, 0)
        assert road_events.weather_on_route(line, events) == 0
        assert road_events.delay_on_route(line, events) == 0.0
    for route in ([], line[:1]):
        assert road_events.count_on_route(route, [closed]) == (0, 0, 0)
        assert road_events.weather_on_route(route, [closed]) == 0
        assert road_events.delay_on_route(route, [closed]) == 0.0
    assert road_events.without_repeats([]) == [] and pricing.design_match([], line) == 0.0 and pricing.design_match(line, []) == 0.0


@pytest.mark.parametrize("seed", [0, 3])
def test_a_reading_that_is_not_a_number_blocks_the_road_as_it_did(seed):
    """A feed's "NaN" reaches a road's cost through a slowdown (road_events._wi511_traffic). The plain search never takes such a road.

    On a small map the search still ends. On a bigger one it doesn't: see the report."""
    graph = random_city(seed, rows=4, cols=4)
    router = Router(graph)
    rng = random.Random(seed)
    checked = 0
    for _ in range(12):
        origin, destination = graph.point(rng.randrange(graph.node_count)), graph.point(rng.randrange(graph.node_count))
        departures = router._departures(origin, None)
        end = graph.nearest_node(destination, 1000)
        jammed = rng.choice([edge for edge in range(graph.edge_count) if edge not in departures])
        events = [RoadEvent("wi511", "slow", tuple(graph.edge_points(jammed)), along=True, slowdown=math.nan, delay_min=math.nan)]
        profile = ScoringProfile()
        costs = router._trip_costs(profile, events, keep=departures, school_hours=False, sun=None)
        expected = plain_search(router, departures, end, costs)
        if graph.edge_from[departures[0]] == end or expected is None:
            continue
        found = router.routes(origin, destination, profile, count=1, events=events)
        assert found and found[0].cost == pytest.approx(expected, rel=1e-9, abs=1e-9), (origin, destination, jammed)
        checked += 1
    assert checked


# ---- Many trips at once


def test_threads_building_a_maps_indexes_at_once_all_get_the_right_answers():
    """Compiled code runs without Python's lock, and a map's indexes are built by whichever trip needs them first."""
    rng = random.Random(5)
    graph = random_city(5)
    plain = old.ReferenceMap(graph)
    points = points_on_the_map(graph, rng, 60)
    drives = [random_drive(graph, rng, 8) for _ in range(20)]
    hotspots = CrashHotspots([(graph.point(rng.randrange(graph.node_count)), 2.0) for _ in range(10)])
    router = Router(graph, TurnContext(hotspots=hotspots))
    expected = (
        [plain.nearest(point, 1000.0, snappable_only=True) for point in points],
        [set(plain.segments_under(point, 90.0)) for point in points],  # as a set: which of those as near comes first is no matter
        [plain.roads_at(point, None) for point in points],
        [plain.signal_at(point) for point in points],
        [plain.edges_along(drive) for drive in drives],
        [plain.signals_along([drive]) for drive in drives],
        [plain.schools_at([drive]) for drive in drives],
        [plain.rail_crossings_at([drive]) for drive in drives],
        [plain.node_kind(node) for node in range(graph.node_count)],
        [plain.crashes_at(node, hotspots) for node in range(graph.node_count)],
    )

    def work(_: int):
        return (
            [graph.nearest_node(point) for point in points],
            [set(graph._segments_under(point, 90.0)) for point in points],
            [graph._roads_at(point, None) for point in points],
            [graph.signal_at(point) for point in points],
            [graph.edges_along(drive) for drive in drives],
            [graph.signals_along([drive]) for drive in drives],
            [graph.schools_at([drive]) for drive in drives],
            [graph.rail_crossings_at([drive]) for drive in drives],
            [graph.node_kind(node) for node in range(graph.node_count)],
            [router.crashes_at(node) for node in range(graph.node_count)],
        )

    with ThreadPoolExecutor(8) as pool:
        for answers in pool.map(work, range(8)):
            assert answers == expected


def test_threads_judging_routes_at_once_get_the_answers_one_at_a_time_gives():
    rng = random.Random(6)
    graph = random_city(6)
    trips = []
    for _ in range(40):
        route = random_drive(graph, rng, 10)
        trips.append((route, tuple(random_event(rng, route) for _ in range(6))))
    expected = [
        (old.count_on_route(route, events), old.weather_on_route(route, events), round(old.delay_on_route(route, events), 6)) for route, events in trips
    ]

    def work(_: int):
        return [
            (road_events.count_on_route(route, events), road_events.weather_on_route(route, events), round(float(road_events.delay_on_route(route, events)), 6))
            for route, events in trips
        ]

    with ThreadPoolExecutor(8) as pool:
        for answers in pool.map(work, range(8)):
            assert answers == expected


def test_threads_replacing_what_is_kept_do_not_fail():
    """Past its limit, a trip drops the oldest of what's kept with next(iter(kept)), which raises if another thread adds
    one in between. Rare at Python's usual pace, so this hands the lock over as often as it can."""
    graph = random_city(0, rows=3, cols=3)
    router = Router(graph)
    line = random_drive(graph, random.Random(0), 3)
    errors: list[str] = []
    until = time.monotonic() + 2.0

    def trips(seed: int) -> None:
        rng = random.Random(seed)
        while time.monotonic() < until:
            try:
                road_events.count_on_route(line, (RoadEvent("wzdx", "closed", (line[0], (line[1][0] + rng.uniform(0, 1e-5), line[1][1]))),))
                costs = router._trip_costs(ScoringProfile(time_weight=1.0 + rng.randrange(40) / 10), [], keep=[], school_hours=False, sun=None)
                router._step_costs(costs)
            except RuntimeError as exc:
                errors.append(str(exc))

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(trips, range(8)))
    finally:
        sys.setswitchinterval(interval)
    assert not errors, f"{len(errors)} trips failed: {errors[0]}"


def test_build_graph_with_nothing_in_it_makes_an_empty_map():
    graph = build_graph([], area="nothing")
    assert graph.node_count == 0 and graph.edge_count == 0 and graph.nearest_node((43.0, -87.9)) is None
