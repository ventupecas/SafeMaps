"""Which events a trip looks at, held to the plain way of finding them, on random lines."""

import math
import random

import pytest

from app.geo import densify
from app.overpass import BBox
from app.road_events import RoadEvent, in_area, near_routes

CENTER = (43.04, -87.91)


def random_line(rng: random.Random, points: int) -> tuple[tuple[float, float], ...]:
    lat, lng = CENTER[0] + rng.uniform(-0.05, 0.05), CENTER[1] + rng.uniform(-0.05, 0.05)
    line = []
    for _ in range(points):
        line.append((lat, lng))
        lat += rng.uniform(-0.002, 0.002)
        lng += rng.uniform(-0.002, 0.002)
    return tuple(line)


def random_events(rng: random.Random, count: int) -> list[RoadEvent]:
    return [RoadEvent("wzdx", rng.choice(("closed", "lanes", "hazard")), random_line(rng, rng.randint(1, 9))) for _ in range(count)]


def plain_near_routes(events, routes, within_m):
    if not any(routes):
        return []
    cell_lat = within_m / 111_320.0
    cell_lng = cell_lat / max(math.cos(math.radians(next(route for route in routes if route)[0][0])), 0.01)

    def cell(point):
        return math.floor(point[0] / cell_lat), math.floor(point[1] / cell_lng)

    on_routes = {cell(point) for route in routes for point in densify(route, within_m / 2)}
    reach = {(i + a, j + b) for i, j in on_routes for a in (-1, 0, 1) for b in (-1, 0, 1)}
    return [event for event in events if any(cell(point) in reach for point in densify(event.path, within_m / 2))]


@pytest.mark.parametrize("seed", range(20))
def test_events_in_an_area_are_those_with_a_point_inside(seed):
    rng = random.Random(seed)
    events = random_events(rng, rng.randint(0, 40))
    south, west = CENTER[0] + rng.uniform(-0.05, 0.02), CENTER[1] + rng.uniform(-0.05, 0.02)
    area = BBox(south, west, south + rng.uniform(0.001, 0.05), west + rng.uniform(0.001, 0.05))
    expected = [event for event in events if any(area.contains(lat, lng) for lat, lng in event.path)]
    assert in_area(events, area) == expected
    assert in_area(tuple(events), area) == expected
    assert in_area(tuple(events), area) == expected  # and again, from what was kept


def test_a_kept_list_of_events_is_not_trusted_after_it_changes():
    area = BBox(CENTER[0] - 1, CENTER[1] - 1, CENTER[0] + 1, CENTER[1] + 1)
    events = [RoadEvent("wzdx", "closed", (CENTER,))]
    assert in_area(events, area) == events
    events.append(RoadEvent("wzdx", "lanes", (CENTER,)))
    assert in_area(events, area) == events


@pytest.mark.parametrize("seed", range(20))
def test_events_near_routes_are_those_a_plain_search_finds(seed):
    rng = random.Random(100 + seed)
    events = random_events(rng, rng.randint(0, 40))
    routes = [list(random_line(rng, rng.randint(0, 40))) for _ in range(rng.randint(1, 4))]
    within = rng.choice((30.0, 100.0, 400.0))
    assert near_routes(events, routes, within) == plain_near_routes(events, routes, within)
