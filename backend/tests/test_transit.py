"""Bus routes from a GTFS feed, for the OpenStreetMap map's Transit layer."""

import csv
import io
import os
import zipfile

import httpx

from app.geo import decode_polyline, destination_point, haversine_m
from app.transit import REFRESH, BusRoutes, read_agency, read_gtfs

START = (43.04, -87.91)
URL = "https://transit.example/gtfs.zip"


def table(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def shape(shape_id: str, bearing: float, length_m: int = 1000) -> list[dict]:
    return [
        {"shape_id": shape_id, "shape_pt_lat": lat, "shape_pt_lon": lng, "shape_pt_sequence": i}
        for i, (lat, lng) in enumerate(destination_point(START, bearing, d) for d in range(0, length_m + 1, 100))
    ]


def feed() -> bytes:
    """Two routes. Route 30 runs a main shape east (three trips) and a short-turn (one trip); 4 runs north."""
    routes = [
        {"route_id": "30", "route_short_name": "30", "route_long_name": "Sherman  -  Wisconsin", "route_color": "863399"},
        {"route_id": "4", "route_short_name": "4", "route_long_name": "Port Washington", "route_color": "FFFFFF"},
    ]
    trips = [
        *({"route_id": "30", "direction_id": "0", "shape_id": "30-main", "trip_id": f"t{i}"} for i in range(3)),
        {"route_id": "30", "direction_id": "0", "shape_id": "30-short", "trip_id": "t3"},
        {"route_id": "4", "direction_id": "1", "shape_id": "4-north", "trip_id": "t4"},
    ]
    shapes = shape("30-main", 90) + shape("30-short", 90, 300) + shape("4-north", 0)
    shapes.reverse()  # rows in any order: the sequence numbers decide
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as gtfs:
        gtfs.writestr("agency.txt", table([{"agency_id": "MCTS", "agency_name": "Milwaukee County Transit System"}]))
        gtfs.writestr("routes.txt", "﻿" + table(routes))  # some feeds start with a byte-order mark
        gtfs.writestr("trips.txt", table(trips))
        gtfs.writestr("shapes.txt", table(shapes))
    return data.getvalue()


def test_each_route_is_drawn_by_its_most_run_shape_in_each_direction():
    four, thirty = read_gtfs(feed())  # in route number order
    assert (thirty.name, thirty.long_name, thirty.color) == ("30", "Sherman - Wisconsin", "#863399")
    (path,) = thirty.paths  # the main shape, not the short-turn
    points = decode_polyline(path)
    assert len(points) == 2  # a straight line needs only its ends
    assert haversine_m(points[0], START) < 2 and 990 < haversine_m(points[0], points[-1]) < 1010
    assert four.color == "#1a73e8"  # white would vanish on the map
    assert read_agency(feed()) == "Milwaukee County Transit System"


def test_bus_routes_are_fetched_once_and_kept_on_disk(tmp_path):
    requests = []

    def mcts(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=feed())

    saved = tmp_path / "bus-routes.json"
    http = httpx.Client(transport=httpx.MockTransport(mcts))
    buses = BusRoutes(http, URL, saved)
    assert [route.name for route in buses.routes()] == ["4", "30"]
    assert buses.routes() and len(requests) == 1
    # A restart reads them back instead of fetching the feed again.
    assert [route.name for route in BusRoutes(http, URL, saved).routes()] == ["4", "30"]
    assert len(requests) == 1


def test_old_bus_routes_are_served_when_the_feed_is_down(tmp_path):
    saved = tmp_path / "bus-routes.json"
    BusRoutes(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=feed()))), URL, saved).routes()
    old = saved.stat().st_mtime - REFRESH.total_seconds() - 60
    os.utime(saved, (old, old))
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    old_lines = BusRoutes(down, URL, saved)
    assert [route.name for route in old_lines.routes()] == ["4", "30"]  # at once, while a new fetch is tried
    assert old_lines.attribution == "Bus routes: Milwaukee County Transit System"
    assert BusRoutes(down, URL, tmp_path / "never-saved.json").routes() == []
