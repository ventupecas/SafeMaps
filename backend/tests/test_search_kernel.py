"""The compiled search against the slow, plain way of doing the same thing, on random cities.

The router's steps and costs can be read one at a time (Router.steps, Router._step_cost), which is easy to
check by eye. These tests hold the compiled arrays and search to that: a route it calls cheapest must really
be the cheapest a plain search through those same steps can find, whatever the map and the traveler.
"""

import heapq
import math
import random
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

import app.search_kernel as kernel
from app import bike
from app.driving_side import DrivingSide
from app.features import HIGHWAY_SPEED_KMH, TurnContext, crosses_traffic
from app.fuel import SIGNAL_L, cruise_liters
from app.geo import angle_between_deg, initial_bearing_deg
from app.hotspots import CrashHotspots
from app.models import LatLng, ScoringProfile
from app.road_events import RoadEvent
from app.roadgraph import JUNCTION_KINDS, RoadGraph, build_graph, classify_junction
from app.router import BOUND_REACH, CONNECTOR_MAX_M, MAX_COST_RATIO, MAX_OVERLAP, RAIL_CROSSING_S, Router, UTURN_MIN_DEG
from tests.osm_factory import MEDIAN_HALF_LNG, Osm

CLASSES = ("residential", "residential", "residential", "tertiary", "secondary", "primary", "service", "unclassified", "living_street")
LAT_STEP, LNG_STEP = 0.0018, 0.00246
BASE = (43.0389, -87.9065)
SEEDS = range(12)


def spot(rng: random.Random, row: float, col: float) -> tuple[float, float]:
    jitter = 0.00025
    return (BASE[0] + row * LAT_STEP + rng.uniform(-jitter, jitter), BASE[1] + col * LNG_STEP + rng.uniform(-jitter, jitter))


def random_city(seed: int, rows: int = 7, cols: int = 7, mode: str = "car") -> RoadGraph:
    """A jittered grid with every kind of thing the search has to get right: mixed roads, one-ways, a freeway with
    ramps, divided roads with median openings, signals, rail crossings, schools and turn restrictions."""
    rng = random.Random(seed)
    osm = Osm()
    ids: dict[tuple[int, int], int] = {}
    for row in range(rows):
        for col in range(cols):
            ids[row, col] = 10_000 + 100 * row + col
            tags = {}
            if rng.random() < 0.15:
                tags["highway"] = "traffic_signals"
            elif rng.random() < 0.05:
                tags["railway"] = "level_crossing"
            osm.node(ids[row, col], spot(rng, row, col), **tags)

    ways: list[tuple[int, list[int]]] = []

    def street(nodes: list[int], number: int) -> None:
        highway = rng.choice(CLASSES)
        tags = {"highway": highway, "name": f"Street {number}"}
        if rng.random() < 0.2:
            tags["oneway"] = "yes"
        if mode != "car" and rng.random() < 0.3:
            tags["lit"] = "yes"
        if mode == "walk" and rng.random() < 0.4:
            tags["sidewalk"] = "both"
        ways.append((osm.way(nodes, **tags), nodes))

    for row in range(rows):
        street([ids[row, col] for col in range(cols)], row)
    for col in range(cols):
        street([ids[row, col] for row in range(rows)], 100 + col)
    # Paths for the bike and walking maps, stairs among them.
    if mode != "car":
        for number in range(6):
            a, b = (rng.randrange(rows - 1), rng.randrange(cols - 1)), None
            b = (a[0] + 1, a[1] + 1)
            osm.way([ids[a], ids[b]], highway=rng.choice(("cycleway", "footway", "path", "steps") if mode == "walk" else ("cycleway", "path")), name=f"Trail {number}")

    # A divided road along the top, with median openings: short connectors, judged with the turn through them.
    top = rows + 1
    east, west = [], []
    for col in range(cols):
        lat, lng = spot(rng, top, col)
        east.append(osm.node(20_000 + col, (lat, lng + MEDIAN_HALF_LNG)))
        west.append(osm.node(21_000 + col, (lat, lng - MEDIAN_HALF_LNG)))
    osm.way(east, highway="secondary", name="Glen Ave", oneway="yes")
    osm.way(west[::-1], highway="secondary", name="Glen Ave", oneway="yes")
    for col in range(cols):
        if rng.random() < 0.7:
            osm.way([east[col], west[col]], highway="secondary_link", oneway="yes")
        if rng.random() < 0.5:
            osm.way([west[col], east[col]], highway="secondary_link", oneway="yes")
    for col in rng.sample(range(cols), 3):
        osm.way([ids[rows - 1, col], east[col]], highway="tertiary", name=f"Up {col}")
        osm.way([west[col], ids[rows - 1, col]], highway="tertiary", name=f"Down {col}")

    # A freeway along the bottom, with ramps at each end and in the middle.
    if mode == "car":
        freeway = [osm.node(30_000 + col, spot(rng, -2, col)) for col in range(cols)]
        osm.way(freeway, highway="motorway", name="I-94", oneway="yes")
        osm.way(freeway[::-1][:0] or [osm.node(31_000 + col, spot(rng, -2.3, col)) for col in reversed(range(cols))], highway="motorway", name="I-94", oneway="yes")
        for col in (0, cols // 2, cols - 1):
            osm.way([ids[0, col], freeway[col]], highway="motorway_link", oneway="yes")
            osm.way([freeway[col], ids[0, col]], highway="motorway_link", oneway="yes")
            osm.way([ids[0, col], 31_000 + col], highway="motorway_link", oneway="yes")
            osm.way([31_000 + col, ids[0, col]], highway="motorway_link", oneway="yes")

    # Turn restrictions where two streets cross.
    for _ in range(5):
        row, col = rng.randrange(rows), rng.randrange(cols)
        kind = rng.choice(("no_left_turn", "no_right_turn", "no_straight_on", "only_straight_on"))
        across = [w for w, nodes in ways if ids[row, col] in nodes]
        if len(across) == 2:
            osm.restriction(kind, across[0], ids[row, col], across[1])

    # Schools, as the map builder finds them.
    for number in range(2):
        lat, lng = spot(rng, rng.randrange(rows), rng.randrange(cols))
        osm.elements.append({"type": "way", "id": 900_000 + number, "tags": {"amenity": "school"}, "center": {"lat": lat, "lon": lng}})
    return build_graph(osm.elements, area=f"random-{seed}", mode=mode)


def random_events(graph: RoadGraph, rng: random.Random) -> list[RoadEvent]:
    events = []
    for number in range(rng.randrange(0, 9)):
        edge = rng.randrange(graph.edge_count)
        stretch = tuple(graph.edge_points(edge))
        kind = rng.choice(("closed", "lanes", "lanes", "hazard", "jam", "slow"))
        events.append(
            RoadEvent(
                "tomtom",
                kind,
                stretch,
                along=True,
                workers=rng.random() < 0.3,
                speed_limit_kmh=rng.choice((None, 24.0)),
                slowdown=rng.choice((None, 2.5)),
                delay_min=rng.choice((0.0, 3.0)),
                description=str(number),
            )
        )
    return events


def random_profile(rng: random.Random) -> ScoringProfile:
    time_weight = rng.choice((1.0, 1.0, 0.5, 2.0))
    return ScoringProfile(
        time_weight=time_weight,
        distance_weight=rng.choice((0.0, 0.2)),
        fuel_weight=rng.choice((0.0, 8.0)),
        highway_weight=rng.choice((0.0, 0.5)),
        maneuver_weight=rng.choice((0.0, 0.3)),
        scenic_weight=rng.choice((0.0, 0.3)),
        busy_crossing_weight=rng.choice((0.0, 3.0, 12.0)),
        hotspot_crossing_weight=rng.choice((0.0, 5.0)),
        ranking=rng.choice(("weighted", "priority")),
    )


def plain_search(router: Router, departures: list[int], end: int, costs, penalties=None) -> float | None:
    """Dijkstra in plain Python: no estimate, nothing compiled, every step costed from its parts."""
    penalties = penalties or {}
    best = {edge: costs.edges[edge] * penalties.get(edge, 1.0) for edge in departures}
    heap = [(cost, edge) for edge, cost in best.items()]
    heapq.heapify(heap)
    while heap:
        cost, edge = heapq.heappop(heap)
        if cost > best[edge]:
            continue
        if router.graph.edge_to[edge] == end:
            return float(cost)
        for step in [*router.steps(edge), *router._arrivals(edge, end)]:
            new = cost + router._step_cost(edge, step, costs, penalties)
            if new < best.get(step.edges[-1], math.inf):
                best[step.edges[-1]] = new
                heapq.heappush(heap, (new, step.edges[-1]))
    return None


def trips(graph: RoadGraph, rng: random.Random, count: int) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Trips from one junction to another, the map's own points so they snap where they're meant to."""
    nodes = sorted({*graph.edge_from, *graph.edge_to})
    return [(graph.point(rng.choice(nodes)), graph.point(rng.choice(nodes))) for _ in range(count)]


def walk_path_and_check(router: Router, path, costs) -> None:
    """The route's segments join up, its turns are legal, and it costs what the plain sums say it does."""
    g = router.graph
    for a, b in zip(path.edges, path.edges[1:], strict=False):
        assert g.edge_to[a] == g.edge_from[b]
    assert path.maneuvers[0] == "DEPART" and len(path.maneuvers) == len(path.edges)
    assert path.cost == pytest.approx(router._path_cost(list(path.edges), costs), rel=1e-9, abs=1e-9)
    assert type(path.cost) is float and all(type(edge) is int for edge in path.edges)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_best_route_costs_what_a_plain_search_finds(seed):
    rng = random.Random(seed)
    graph = random_city(seed)
    hotspots = CrashHotspots([(graph.point(rng.randrange(graph.node_count)), rng.choice((1, 3, 6))) for _ in range(6)])
    router = Router(graph, TurnContext(rng.choice(tuple(DrivingSide)), hotspots=hotspots, min_hotspot_crashes=3))
    checked = 0
    for origin, destination in trips(graph, rng, 25):
        profile = random_profile(rng)
        events = random_events(graph, rng)
        school = rng.random() < 0.4
        sun = rng.choice((None, (rng.uniform(0, 360), 8.0), (90.0, -20.0)))
        departures = router._departures(origin, None)
        end = graph.nearest_node(destination, 1000)
        costs = router._trip_costs(profile, events, keep=departures, school_hours=school, sun=sun)
        found = router.routes(origin, destination, profile, count=1, events=events, school_hours=school, sun=sun)
        if graph.edge_from[departures[0]] == end:
            assert found == []
            continue
        expected = plain_search(router, departures, end, costs)
        if expected is None:
            assert found == []
            continue
        assert found[0].cost == pytest.approx(expected, rel=1e-9, abs=1e-9)
        walk_path_and_check(router, found[0], costs)
        checked += 1
    assert checked >= 10


@pytest.mark.parametrize("mode", ["bike", "walk"])
@pytest.mark.parametrize("seed", SEEDS[:6])
def test_the_best_ride_or_walk_costs_what_a_plain_search_finds(seed, mode):
    rng = random.Random(1000 + seed)
    graph = random_city(seed, mode=mode)
    hotspots = CrashHotspots([(graph.point(rng.randrange(graph.node_count)), 1) for _ in range(5)])
    router = bike.BikeRouter(graph, TurnContext(hotspots=hotspots))
    profiles = (*bike.PROFILES.values(), bike.SHORTEST) if mode == "bike" else (*bike.WALK_PROFILES.values(), bike.SHORTEST)
    checked = 0
    for origin, destination in trips(graph, rng, 25):
        profile = rng.choice(profiles)
        events = random_events(graph, rng)
        sun = rng.choice((None, (180.0, 30.0), (0.0, -30.0)))
        departures = router._departures(origin, None)
        end = graph.nearest_node(destination, 1000)
        costs = router._trip_costs(profile, events, keep=departures, school_hours=False, sun=sun)
        found = router.bike_routes(origin, destination, profile, count=1, events=events, sun=sun)
        if graph.edge_from[departures[0]] == end:
            continue
        expected = plain_search(router, departures, end, costs)
        if expected is None:
            assert found == []
            continue
        assert found[0].cost == pytest.approx(expected, rel=1e-9, abs=1e-9)
        walk_path_and_check(router, found[0], costs)
        checked += 1
    assert checked >= 10


@pytest.mark.parametrize("seed", SEEDS[:6])
def test_alternatives_are_real_routes_within_the_limits(seed):
    rng = random.Random(2000 + seed)
    graph = random_city(seed)
    router = Router(graph)
    for origin, destination in trips(graph, rng, 15):
        profile = random_profile(rng)
        events = random_events(graph, rng)
        departures = router._departures(origin, None)
        costs = router._trip_costs(profile, events, keep=departures, school_hours=False, sun=None)
        found = router.routes(origin, destination, profile, count=3, events=events)
        for number, path in enumerate(found):
            walk_path_and_check(router, path, costs)
            if number:
                assert path.cost <= found[0].cost * MAX_COST_RATIO + 1e-9
                assert path.cost >= found[0].cost - 1e-9  # the first is the cheapest there is
            for earlier in found[:number]:
                assert router._shared(path.edges, earlier.edges) <= MAX_OVERLAP


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_every_step_costs_in_the_arrays_what_its_parts_add_up_to(seed):
    rng = random.Random(3000 + seed)
    for mode, make in (("car", Router), ("bike", bike.BikeRouter), ("walk", bike.BikeRouter)):
        graph = random_city(seed, mode=mode)
        hotspots = CrashHotspots([(graph.point(rng.randrange(graph.node_count)), rng.choice((1, 4))) for _ in range(5)])
        router = make(graph, TurnContext(hotspots=hotspots, min_hotspot_crashes=3))
        profile = random_profile(rng) if mode == "car" else bike.PROFILES["family"]
        events = random_events(graph, rng)
        costs = router._trip_costs(profile, events, keep=[], school_hours=mode == "car", sun=(0.0, -30.0))
        table = router._steps()
        step_costs = router._step_costs(costs)
        zones = router._zone_table(costs)
        for edge in range(graph.edge_count):
            for s in range(table.ptr[edge], table.ptr[edge + 1]):
                step = router._step(edge, s)
                from_parts = router._step_cost(edge, step, costs, {})
                if math.isinf(from_parts) or math.isinf(costs.edges[edge]):
                    continue  # a closed road: nothing to compare
                taken = np.array([s], dtype=np.int32)
                from_arrays = kernel.path_cost(edge, taken, costs.edges, step_costs, table.first, table.second, table.kind, zones) - costs.edges[edge]
                assert from_arrays == pytest.approx(from_parts, rel=1e-9, abs=1e-9)


def expected_moves(router: Router, edge: int) -> list[tuple[int, str, bool, bool]]:
    """The moves from a segment, judged by the rules as written out in plain Python."""
    g = router.graph
    node = g.edge_to[edge]
    kind = g.node_kind(node)
    outs = g.out_edges[node]
    moves = []
    link, freeway = router._link, router._freeway
    uturn = router._uturn
    for nxt in outs:
        if nxt in g.banned.get(edge, frozenset()):
            continue
        if g.edge_to[nxt] == g.edge_from[edge] and len(outs) > 1:
            continue
        angle = (g.bearing_start[nxt] - g.bearing_end[edge] + 540.0) % 360.0 - 180.0
        side = "RIGHT" if angle > 0 else "LEFT"
        turn = abs(angle)
        same_road = g.way_id[edge] == g.way_id[nxt] or (g.name[edge] != 0 and g.name[edge] == g.name[nxt])
        if g.edge_to[nxt] == g.edge_from[edge]:
            maneuver = uturn
        elif link[nxt] and not link[edge]:
            maneuver = f"RAMP_{side}" if turn > 10 else "STRAIGHT"
        elif link[edge] and freeway[nxt]:
            maneuver = "MERGE"
        elif (link[edge] or freeway[edge]) and (link[nxt] or freeway[nxt]):
            maneuver = f"FORK_{side}" if turn > 10 else "STRAIGHT"
        elif turn <= 30 or (same_road and turn < 60):
            maneuver = "STRAIGHT"
        elif turn >= 165:
            maneuver = uturn
        elif turn < 60:
            maneuver = f"TURN_SLIGHT_{side}"
        else:
            maneuver = f"TURN_SHARP_{side}" if turn > 120 else f"TURN_{side}"
        crossing = kind not in ("interchange", "one-way") and crosses_traffic(maneuver, g.speed_kmh[edge], router.context.driving_side)
        moves.append((nxt, maneuver, crossing, kind == "busy"))
    return moves


@pytest.mark.parametrize("side", list(DrivingSide))
@pytest.mark.parametrize("seed", SEEDS[:4])
def test_moves_and_steps_are_judged_by_the_written_rules(seed, side):
    graph = random_city(seed)
    router = Router(graph, TurnContext(side))
    for edge in range(graph.edge_count):
        moves = expected_moves(router, edge)
        assert router.moves(edge) == moves
        expected = []
        for nxt, maneuver, crossing, busy in moves:
            turn = (graph.edge_to[edge], maneuver, crossing, busy)
            if graph.length_m[nxt] >= CONNECTOR_MAX_M:
                expected.append(((nxt,), (maneuver,), (turn,)))
                continue
            for after, maneuver_after, crossing_after, busy_after in expected_moves(router, nxt):
                if angle_between_deg(graph.bearing_end[edge], graph.bearing_start[after]) >= UTURN_MIN_DEG:
                    kind = graph.node_kind(graph.edge_to[edge])
                    uturn = router._uturn
                    across = kind not in ("interchange", "one-way") and crosses_traffic(uturn, graph.speed_kmh[edge], side)
                    expected.append(((nxt, after), (uturn, "STRAIGHT"), ((graph.edge_to[edge], uturn, across, kind == "busy"),)))
                else:
                    expected.append(((nxt, after), (maneuver, maneuver_after), (turn, (graph.edge_to[nxt], maneuver_after, crossing_after, busy_after))))
        found = [(step.edges, step.maneuvers, step.turns) for step in router.steps(edge)]
        assert sorted(found) == sorted(expected)


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_junction_kinds_for_the_whole_map_are_each_junctions_own(seed):
    graph = random_city(seed)
    for node in range(graph.node_count):
        edges = graph.out_edges[node] + graph.in_edges[node]
        kind = classify_junction(graph.class_name(edge) for edge in edges)
        if kind != "interchange" and edges and all(graph.oneway[edge] for edge in edges) and not graph._on_divided_road(node):
            kind = "one-way"
        assert graph.node_kind(node) == kind and JUNCTION_KINDS[graph.node_kinds()[node]] == kind


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_columns_at_once_match_one_segment_at_a_time(seed):
    rng = random.Random(4000 + seed)
    graph = random_city(seed, mode="bike")
    car = random_city(seed)
    router = Router(car)
    profile = random_profile(rng)
    costs = router._edge_costs(profile)
    bearings = router._edge_bearings()
    for edge in range(car.edge_count):
        minutes = car.time_s[edge] / 60
        fuel = cruise_liters(car.length_m[edge], car.speed_kmh[edge]) + car.signals[edge] * SIGNAL_L
        cost = profile.time_weight * minutes + profile.distance_weight * car.length_m[edge] / 1000 + profile.fuel_weight * fuel
        if car.speed_kmh[edge] >= HIGHWAY_SPEED_KMH:
            cost += profile.highway_weight * minutes
        elif car.winding[edge]:
            cost -= profile.scenic_weight * minutes
        if car.rail_crossings[edge]:
            cost += car.rail_crossings[edge] * (profile.time_weight * RAIL_CROSSING_S / 60 + profile.rail_crossing_weight)
        assert costs[edge] == pytest.approx(cost, rel=1e-12)
        assert bearings[edge] == pytest.approx(initial_bearing_deg(car.point(car.edge_from[edge]), car.point(car.edge_to[edge])), abs=1e-9)
    rider = bike.BikeRouter(graph)
    for traveler in bike.PROFILES.values():
        for night in (False, True):
            base = rider._base(traveler, night)
            slower = bike.MAP_SPEED_KMH[graph.mode] / traveler.speed_kmh
            for edge in range(graph.edge_count):
                weight = traveler.stress_weights[max(1, graph.stress[edge]) - 1] + (traveler.unlit_weight if night and not graph.lit[edge] else 0.0)
                expected = graph.time_s[edge] / 60 * slower * weight + graph.rail_crossings[edge] * traveler.rail_minutes
                assert base[edge] == pytest.approx(expected, rel=1e-12)


def test_crash_spots_found_from_the_crashes_match_looking_from_each_junction():
    graph = random_city(1)
    rng = random.Random(5)
    spots = []
    for _ in range(30):
        lat, lng = graph.point(rng.randrange(graph.node_count))
        spots.append(((lat + rng.uniform(-0.0004, 0.0004), lng + rng.uniform(-0.0004, 0.0004)), rng.choice((1, 2, 5))))
    hotspots = CrashHotspots(spots)
    router = Router(graph, TurnContext(hotspots=hotspots))
    for node in range(graph.node_count):
        assert router.crashes_at(node) == hotspots.crashes_near(graph.point(node), 35.0)


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_the_estimate_never_overstates_what_is_left(seed):
    """The whole of why the route found is the cheapest: nothing settled says more than the true cost from there,
    and everything else is at least as far as the distance searched to."""
    rng = random.Random(6000 + seed)
    graph = random_city(seed)
    router = Router(graph)
    for origin, destination in trips(graph, rng, 10):
        profile = random_profile(rng)
        events = random_events(graph, rng)
        departures = router._departures(origin, None)
        end = graph.nearest_node(destination, 1000)
        costs = router._trip_costs(profile, events, keep=departures, school_hours=False, sun=None)
        workspace = kernel.Workspace(graph.node_count, graph.edge_count)
        radius = workspace.reverse_bound(
            end,
            graph.column("edge_to")[np.array(departures, dtype=np.int32)],
            BOUND_REACH,
            costs.edges,
            graph.in_edges.column_ptr(),
            graph.in_edges.column_edges(),
            graph.column("edge_from"),
        )
        truth = {end: 0.0}
        heap = [(0.0, end)]
        while heap:
            done, node = heapq.heappop(heap)
            if done > truth[node]:
                continue
            for edge in graph.in_edges[node]:
                new = done + costs.edges[edge]
                if new < truth.get(graph.edge_from[edge], math.inf):
                    truth[graph.edge_from[edge]] = new
                    heapq.heappush(heap, (new, graph.edge_from[edge]))
        for node in range(graph.node_count):
            true_left = truth.get(node, math.inf)
            left = workspace.left(node)
            if left >= 0:  # settled
                assert left == pytest.approx(true_left, rel=1e-9, abs=1e-9)
            else:
                assert radius <= true_left + 1e-9


def test_trips_planned_at_once_on_one_router_get_the_same_routes():
    graph = random_city(3, rows=10, cols=10)
    router = Router(graph)
    rng = random.Random(9)
    requests = [(a, b, random_profile(rng)) for a, b in trips(graph, rng, 40)]
    alone = [router.routes(a, b, profile, count=3) for a, b, profile in requests]
    with ThreadPoolExecutor(8) as pool:
        together = list(pool.map(lambda request: router.routes(request[0], request[1], request[2], count=3), requests))
    assert together == alone


def test_a_map_can_be_saved_and_loaded_with_its_compact_columns(tmp_path):
    graph = random_city(2)
    path = tmp_path / "map.json.gz"
    graph.save(path)
    loaded = RoadGraph.load(path)
    assert list(loaded.edge_from) == list(graph.edge_from) and loaded.out_edges[3] == graph.out_edges[3]
    assert [loaded.oneway[e] for e in range(loaded.edge_count)] == [graph.oneway[e] for e in range(graph.edge_count)]
    origin, destination = graph.point(graph.edge_from[0]), graph.point(graph.edge_to[graph.edge_count // 2])
    best = Router(graph).routes(origin, destination, ScoringProfile())
    again = Router(loaded).routes(origin, destination, ScoringProfile())
    assert [path.edges for path in best] == [path.edges for path in again]


def test_every_way_a_short_connector_is_used_has_a_plain_search_behind_it():
    """A destination at the end of a median opening, the one place a connector is a way to arrive."""
    graph = random_city(4)
    router = Router(graph)
    connectors = [edge for edge in range(graph.edge_count) if graph.length_m[edge] < CONNECTOR_MAX_M]
    assert connectors
    profile = ScoringProfile()
    costs = router._trip_costs(profile, [], keep=[], school_hours=False, sun=None)
    for connector in connectors[:10]:
        end = graph.edge_to[connector]
        destination = graph.point(end)
        origin = graph.point(graph.edge_from[connector])
        found = router.routes(origin, destination, profile, count=1)
        departures = router._departures(origin, None)
        if graph.edge_from[departures[0]] != end:
            expected = plain_search(router, departures, end, costs)
            if expected is not None:
                assert found[0].cost == pytest.approx(expected, rel=1e-9, abs=1e-9)


@pytest.mark.parametrize("seed", SEEDS[:6])
def test_a_walks_direct_routes_only_ever_improve_the_recommendation(seed, monkeypatch):
    rng = random.Random(7000 + seed)
    graph = random_city(seed, mode="walk")
    router = bike.BikeRouter(graph, TurnContext(hotspots=CrashHotspots([(graph.point(rng.randrange(graph.node_count)), 1) for _ in range(4)])))
    direct_routes = router.direct_routes

    def rank(plan):
        winner = plan["winner"]
        return (not winner["eligible"], -winner["safety_score"], winner["features"]["duration_min"])

    for (a, b), profile in zip(trips(graph, rng, 15), [*bike.WALK_PROFILES.values()] * 5, strict=True):
        origin, destination = LatLng(lat=a[0], lng=a[1]), LatLng(lat=b[0], lng=b[1])
        sun = rng.choice((None, (0.0, -30.0)))
        try:
            monkeypatch.setattr(router, "direct_routes", lambda *args, **kwargs: [])
            without = bike.plan(router, origin, destination, profile, sun=sun)
        except LookupError:
            continue
        monkeypatch.setattr(router, "direct_routes", direct_routes)
        with_direct = bike.plan(router, origin, destination, profile, sun=sun)
        assert rank(with_direct) <= rank(without)
        ids = [candidate["id"] for candidate in with_direct["candidates"]]
        assert len(ids) == len(set(ids)) and len(ids) <= bike.ROUTES_SHOWN
        assert with_direct["candidates"][0] is with_direct["winner"]
        assert [c["id"] for c in with_direct["candidates"] if c["is_standard"]] == [c["id"] for c in without["candidates"] if c["is_standard"]]


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_direct_routes_are_real_routes(seed):
    rng = random.Random(8000 + seed)
    graph = random_city(seed, mode="walk")
    router = bike.BikeRouter(graph)
    profile = bike.WALK_PROFILES["everyday"]
    costs = router._trip_costs(profile, [], keep=[], school_hours=False, sun=None)
    for origin, destination in trips(graph, rng, 15):
        for path in router.direct_routes(origin, destination, profile):
            walk_path_and_check(router, path, costs)


def test_steps_of_a_kind_are_the_same_in_every_way_a_traveler_could_cost():
    graph = random_city(1, mode="walk")
    router = bike.BikeRouter(graph, TurnContext(hotspots=CrashHotspots([(graph.point(3), 1)])))
    table = router._steps()
    columns = table[4:]
    assert all(column.size == columns[0].size for column in columns) and columns[0].size == table.kind.max() + 1
    assert columns[0].size < table.first.size  # fewer kinds than steps: the point of them
    # No two kinds are the same: a kind that lost something in the packing would have merged with another.
    assert len({tuple(column[k] for column in columns) for k in range(columns[0].size)}) == columns[0].size
