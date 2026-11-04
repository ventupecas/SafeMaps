"""Compile the search and the geometry ahead of any trip: python -m app.compile_kernels

Numba compiles each function when it's first called, for the kinds of arguments it's called with, and keeps the
result on disk. So this plans trips on a tiny made-up city, reaching what real trips reach. The server does it at
startup (a second or so once compiled), and the Docker image while it's built, so a container starts ready.
"""

from __future__ import annotations

import logging
import time

from app import bike
from app.engine import build_router_candidates, name_routes, plan_from_candidates
from app.features import TurnContext
from app.geo import decode_polyline
from app.hotspots import CrashHotspots
from app.models import LatLng
from app.pricing import _NearIndex, design_match
from app.profiles import DEFAULT_PROFILE, PRESETS
from app.road_events import RoadEvent, near_routes, without_repeats
from app.roadgraph import build_graph
from app.router import Router

logger = logging.getLogger(__name__)

_LAT, _LNG = 43.04, -87.91
_STEP = 0.002


def _spot(row: int, column: int) -> tuple[float, float]:
    return _LAT + row * _STEP, _LNG + column * _STEP


def _tiny_city() -> list[dict]:
    """Three streets by three, a median opening, a school, a signal and a rail crossing."""
    elements: list[dict] = []
    for row in range(3):
        for column in range(3):
            lat, lng = _spot(row, column)
            tags = {"highway": "traffic_signals"} if (row, column) == (1, 1) else {"railway": "level_crossing"} if (row, column) == (2, 1) else {}
            elements.append({"type": "node", "id": 100 + 10 * row + column, "lat": lat, "lon": lng, "tags": tags})
    next_way = 1

    def way(nodes: list[int], **tags: str) -> None:
        nonlocal next_way
        elements.append({"type": "way", "id": next_way, "nodes": nodes, "tags": tags})
        next_way += 1

    for row in range(3):
        way([100 + 10 * row + column for column in range(3)], highway="primary" if row == 1 else "residential", name=f"Row {row}")
    for column in range(3):
        way([100 + 10 * row + column for row in range(3)], highway="residential", name=f"Column {column}")
    lat, lng = _spot(3, 0)
    elements.append({"type": "node", "id": 200, "lat": lat, "lon": lng})
    elements.append({"type": "node", "id": 201, "lat": lat, "lon": lng + 0.0002})
    way([120, 200], highway="secondary", name="Glen Ave", oneway="yes")
    way([200, 201], highway="secondary_link", oneway="yes")
    way([201, 122], highway="secondary", name="Glen Ave", oneway="yes")
    lat, lng = _spot(1, 1)
    elements.append({"type": "way", "id": 900, "tags": {"amenity": "school"}, "center": {"lat": lat, "lon": lng}})
    return elements


def compile_kernels() -> None:
    """Plan a car trip, a ride and a walk, as a real request does, with every kind of road event on the way."""
    elements = _tiny_city()
    origin, destination = LatLng(lat=_spot(0, 0)[0], lng=_spot(0, 0)[1]), LatLng(lat=_spot(2, 2)[0], lng=_spot(2, 2)[1])
    hotspots = CrashHotspots([(_spot(1, 2), 3.0)])
    line = (_spot(1, 0), _spot(1, 1), _spot(1, 2))
    events = without_repeats(
        [
            RoadEvent("wzdx", "lanes", line, along=True, workers=True, speed_limit_kmh=24.0),
            RoadEvent("tomtom", "lanes", line, along=True),
            RoadEvent("tomtom", "closed", (_spot(2, 0), _spot(2, 1)), along=True),
            RoadEvent("wi511", "closed", (_spot(0, 1),), direction=90.0),
            RoadEvent("tomtom", "hazard", (_spot(0, 1), _spot(0, 2)), direction=90.0),
            RoadEvent("wi511", "jam", (_spot(0, 2), _spot(1, 2)), along=True, delay_min=3.0),
            RoadEvent("wzdx", "closed", (_spot(1, 2), _spot(2, 2)), road="on-ramp"),
        ]
    )
    graph = build_graph(elements, area="tiny")
    router = Router(graph, TurnContext(hotspots=hotspots))
    for school_hours, sun in ((True, (90.0, 10.0)), (False, None)):
        context = TurnContext(hotspots=hotspots, junctions=graph, events=tuple(events), school_hours=school_hours, sun=sun)
        pool = build_router_candidates(
            None, router, origin, destination, [PRESETS[DEFAULT_PROFILE]], price=False, context=context, baseline=PRESETS["fastest"]
        )
        plan = name_routes(plan_from_candidates(pool.candidates, PRESETS[DEFAULT_PROFILE]), graph)
        routes = [decode_polyline(route.encoded_polyline) for route in plan.candidates]
        near_routes(events, routes, 100.0)
        design_match(routes[0], routes[-1])
        _NearIndex(routes[0], 30.0).near(routes[-1][0])
    router.routes(origin.as_tuple(), destination.as_tuple(), PRESETS["fastest"], heading=45, events=events, count=1)
    graph.event_edges(events[0])
    graph.snap_events(events)
    graph.edges_in_box(_LAT, _LNG, _LAT + _STEP, _LNG + _STEP)
    graph.nearest_node(origin.as_tuple(), 500.0)

    for mode, profiles in (("bike", bike.PROFILES), ("walk", bike.WALK_PROFILES)):
        rider = bike.BikeRouter(build_graph(elements, area="tiny", mode=mode), TurnContext(hotspots=hotspots))
        for sun in (None, (0.0, -30.0)):
            bike.plan(rider, origin, destination, profiles[bike.DEFAULT_PROFILE], events=events, sun=sun)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    started = time.perf_counter()
    compile_kernels()
    print(f"Compiled in {time.perf_counter() - started:.1f} s")
