"""The geometry a plan repeats thousands of times, in native code (backend/rust_kernel/src/geometry.rs).

What the route passes, and what's near it. These are thin wrappers: see geometry.rs for how it works, and how a grid
of cells (Grid) makes those lookups quick. app.geo's functions call these for whole paths and do single sums themselves.
"""

from __future__ import annotations

import ctypes as C
import math

import numpy as np

from app.native import Arr, Out, address, arr, boolean, copied, declare, f64, i64, lib, pointer


class _GridC(C.Structure):
    _fields_ = [("keys", Arr), ("items", Arr), ("lat", Arr), ("lng", Arr), ("cell_deg", f64)]


class _RouteLineC(C.Structure):
    _fields_ = [("grid", _GridC), ("bearings", Arr), ("reach_lng", i64), ("south", f64), ("west", f64), ("north", f64), ("east", f64)]


class _EventColumnsC(C.Structure):
    _fields_ = [(name, Arr) for name in ("ptr", "lat", "lng", "south", "west", "north", "east", "has_direction", "direction", "along", "ramp")]


class _MatchingC(C.Structure):
    _fields_ = [(name, f64) for name in ("match_m", "on_route_m", "event_on_route_m", "match_deg", "parallel_deg", "margin_lat", "margin_lng")]


class _MapColumnsC(C.Structure):
    _fields_ = [
        *((name, Arr) for name in ("edge_from", "edge_to", "node_lat", "node_lng", "length_m", "is_link", "in_ptr", "in_edges", "geometry_ptr", "geometry")),
        ("factor", f64),
        ("m_per_deg_lookup", f64),
        ("m_per_deg_geo", f64),
    ]


class _PointsC(C.Structure):
    _fields_ = [("lat", Out), ("lng", Out)]


class _SegmentCellsC(C.Structure):
    _fields_ = [("keys", Out), ("edges", Out)]


declare("free_points", [pointer])
declare("free_i32", [pointer])
declare("free_u8", [pointer])
declare("free_segment_cells", [pointer])
declare("densified", [pointer, pointer, i64, f64, pointer])
declare("path_length", [pointer, pointer, i64], f64)
declare("decoded", [pointer, i64, i64, i64, f64, pointer], boolean)
declare("encoded", [pointer, pointer, i64, f64, pointer])
declare("path_points", [pointer, i64, pointer, i64, pointer, i64, f64, pointer])
declare("heading_change", [pointer, pointer, i64, f64], f64)
declare("share_into_sun", [pointer, pointer, i64, f64, f64], f64)
declare("cell_keys", [pointer, pointer, i64, f64, pointer])
declare("any_within", [pointer, f64, f64, f64, i64], boolean)
declare("share_near", [pointer, pointer, pointer, i64, f64, i64], i64)
declare("mark_near_stretches", [pointer, pointer, i64, pointer, pointer, i64, f64, f64, pointer, i64])
declare("items_near", [pointer, f64, f64, f64, f64, pointer])
declare("nearest_item", [pointer, f64, f64, f64, f64], C.c_int32)
declare("edges_along", [pointer, pointer, pointer, i64, f64, f64, pointer, i64, pointer, pointer, i64, pointer, pointer])
declare("bearings_of", [pointer, pointer, i64, pointer])
declare("runs_along_each", [pointer, pointer, pointer, pointer, pointer])
declare("share_along_each", [pointer, pointer, pointer, pointer, pointer])
declare("mark_carried", [pointer, pointer, pointer, pointer, i64, boolean, f64, boolean, pointer, pointer])
declare("lines_near_lines", [pointer, i64, pointer, pointer, i64, pointer, i64, pointer, pointer, i64, f64, f64, f64, pointer])
declare("segment_cells", [pointer, pointer, pointer, i64, pointer, pointer, i64, f64, f64, pointer])
declare("roads_at", [f64, f64, f64, boolean, boolean, boolean, pointer, pointer, i64, f64, pointer, pointer, f64, f64, pointer])
declare("segments_under", [f64, f64, f64, boolean, boolean, pointer, i64, f64, f64, f64, f64, pointer, pointer, f64, pointer, pointer])


class Grid:
    """Things sorted by the cell each falls in: see geometry.rs."""

    def __init__(self, keys: np.ndarray, items: np.ndarray, lat: np.ndarray, lng: np.ndarray, cell_deg: float) -> None:
        self.keys, self.items, self.lat, self.lng, self.cell_deg = keys, items, lat, lng, cell_deg
        self.c = _GridC(arr(keys), arr(items), arr(lat), arr(lng), cell_deg)
        self.address = C.addressof(self.c)


class RouteLine:
    """A route's points in a grid, with the direction of travel at each."""

    def __init__(self, grid: Grid, bearings: np.ndarray, reach_lng: int, south: float, west: float, north: float, east: float) -> None:
        self.grid, self.bearings = grid, bearings
        self.c = _RouteLineC(grid.c, arr(bearings), reach_lng, south, west, north, east)
        self.address = C.addressof(self.c)


class EventColumns:
    """Road events, each a line of points, as flat columns."""

    def __init__(self, *columns: np.ndarray) -> None:
        self.columns = columns
        self.c = _EventColumnsC(*map(arr, columns))
        self.address = C.addressof(self.c)


class Matching:
    """The rules for whether a route drives an event: see app.road_events."""

    def __init__(self, *rules: float) -> None:
        self.c = _MatchingC(*rules)
        self.address = C.addressof(self.c)


class MapColumns:
    """What finding the segments near a point reads of the map."""

    def __init__(self, *columns: np.ndarray, factor: float, m_per_deg_lookup: float, m_per_deg_geo: float) -> None:
        self.columns = columns
        self.c = _MapColumnsC(*map(arr, columns), factor, m_per_deg_lookup, m_per_deg_geo)
        self.address = C.addressof(self.c)


def _points(out: _PointsC) -> tuple[np.ndarray, np.ndarray]:
    try:
        return copied(out.lat, np.float64), copied(out.lng, np.float64)
    finally:
        lib.free_points(C.addressof(out))


def _ints(out: Out) -> np.ndarray:
    try:
        return copied(out, np.int32)
    finally:
        lib.free_i32(C.addressof(out))


def densified(lat: np.ndarray, lng: np.ndarray, spacing_m: float) -> tuple[np.ndarray, np.ndarray]:
    """The path's points, plus extra ones so no gap is longer than spacing_m."""
    out = _PointsC()
    lib.densified(address(lat), address(lng), lat.size, spacing_m, C.addressof(out))
    return _points(out)


def path_length(lat: np.ndarray, lng: np.ndarray) -> float:
    return lib.path_length(address(lat), address(lng), lat.size)


def decoded(data: np.ndarray, start: int, stop: int, factor: float) -> tuple[np.ndarray, np.ndarray]:
    """A polyline's points, from its bytes in data, start up to stop."""
    out = _PointsC()
    complete = lib.decoded(address(data), data.size, start, stop, factor, C.addressof(out))
    points = _points(out)
    if not complete:
        raise ValueError("Truncated polyline string")
    return points


def encoded(lat: np.ndarray, lng: np.ndarray, factor: float) -> np.ndarray:
    """The points as a polyline's bytes."""
    out = Out()
    lib.encoded(address(lat), address(lng), lat.size, factor, C.addressof(out))
    try:
        return copied(out, np.uint8)
    finally:
        lib.free_u8(C.addressof(out))


def path_points(edges: np.ndarray, ptr: np.ndarray, blob: np.ndarray, factor: float) -> tuple[np.ndarray, np.ndarray]:
    """A route's points, from its segments' polylines."""
    out = _PointsC()
    lib.path_points(address(edges), edges.size, address(ptr), ptr.size, address(blob), blob.size, factor, C.addressof(out))
    return _points(out)


def heading_change(lat: np.ndarray, lng: np.ndarray, min_segment_m: float) -> float:
    return lib.heading_change(address(lat), address(lng), lat.size, min_segment_m)


def share_into_sun(lat: np.ndarray, lng: np.ndarray, azimuth: float, glare_deg: float) -> float:
    return lib.share_into_sun(address(lat), address(lng), lat.size, azimuth, glare_deg)


def make_grid(lat: np.ndarray, lng: np.ndarray, cell_deg: float, items: np.ndarray | None = None) -> Grid:
    keys = np.empty(lat.size, dtype=np.int64)
    lib.cell_keys(address(lat), address(lng), lat.size, cell_deg, address(keys))
    order = np.argsort(keys, kind="stable")
    return Grid(
        keys[order],
        (np.arange(lat.size, dtype=np.int32) if items is None else items)[order].astype(np.int32),
        lat[order],
        lng[order],
        float(cell_deg),
    )


def reach_lng_at(lat: float) -> int:
    """Cells to search east and west of a point at this latitude: they narrow away from the equator."""
    return math.ceil(1 / max(math.cos(math.radians(lat)), 0.01))


def any_within(grid: Grid, lat: float, lng: float, within_m: float, reach_lng: int) -> bool:
    return lib.any_within(grid.address, lat, lng, within_m, reach_lng)


def share_near(grid: Grid, lat: np.ndarray, lng: np.ndarray, within_m: float, reach_lng: int) -> int:
    """How many of the samples have something in the grid within within_m."""
    return lib.share_near(grid.address, address(lat), address(lng), lat.size, within_m, reach_lng)


def mark_near_stretches(grid: Grid, ptr: np.ndarray, lat: np.ndarray, lng: np.ndarray, spacing_m: float, within_m: float, found: np.ndarray) -> None:
    """Set found[item] for everything in the grid within within_m of the stretches of road given."""
    lib.mark_near_stretches(grid.address, address(ptr), ptr.size, address(lat), address(lng), lat.size, spacing_m, within_m, address(found), found.size)


def items_near(grid: Grid, lat: float, lng: float, distance_m: float, m_per_deg: float) -> np.ndarray:
    """What's in the grid cells within distance_m of a point: a superset of what's within distance_m."""
    out = Out()
    lib.items_near(grid.address, lat, lng, distance_m, m_per_deg, C.addressof(out))
    return _ints(out)


def nearest_item(grid: Grid, lat: float, lng: float, max_distance_m: float, m_per_deg: float) -> int:
    """The closest thing in the grid within max_distance_m of a point, or -1."""
    return lib.nearest_item(grid.address, lat, lng, max_distance_m, m_per_deg)


def edges_along(grid: Grid, lat: np.ndarray, lng: np.ndarray, within_m: float, m_per_deg: float, out_ptr: np.ndarray, out_edges: np.ndarray, edge_to: np.ndarray, on_route: np.ndarray) -> np.ndarray:
    """Segments a route's points follow: both of their junctions lie on it. on_route is all False on the way in."""
    out = Out()
    lib.edges_along(grid.address, address(lat), address(lng), lat.size, within_m, m_per_deg, address(out_ptr), out_ptr.size, address(out_edges), address(edge_to), edge_to.size, address(on_route), C.addressof(out))
    return _ints(out)


def bearings_of(lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
    """The direction of travel at each point of a path."""
    out = np.empty(lat.size)
    lib.bearings_of(address(lat), address(lng), lat.size, address(out))
    return out


def runs_along_each(route: RouteLine, events: EventColumns, wanted: np.ndarray, rules: Matching, out: np.ndarray) -> None:
    """Set out[event] for each wanted event the route drives through, the way its traffic goes."""
    lib.runs_along_each(route.address, events.address, address(wanted), rules.address, address(out))


def share_along_each(route: RouteLine, events: EventColumns, wanted: np.ndarray, rules: Matching, out: np.ndarray) -> None:
    """Set out[event] for each wanted event: the share of its stretch the route drives, the way its traffic goes."""
    lib.share_along_each(route.address, events.address, address(wanted), rules.address, address(out))


def mark_carried(route: RouteLine, lat: np.ndarray, lng: np.ndarray, lines: np.ndarray, has_direction: bool, direction: float, along: bool, rules: Matching, covered: np.ndarray) -> None:
    """Set covered[sample] for each sample of an event's line this other line runs beside, the way its traffic goes."""
    lib.mark_carried(route.address, address(lat), address(lng), address(lines), lat.size, has_direction, direction, along, rules.address, address(covered))


def lines_near_lines(lines_ptr: np.ndarray, lines_lat: np.ndarray, lines_lng: np.ndarray, near_ptr: np.ndarray, near_lat: np.ndarray, near_lng: np.ndarray, spacing_m: float, cell_lat: float, cell_lng: float, out: np.ndarray) -> None:
    """Set out[line] for each line that comes into a cell next to one that any of the near lines passes through."""
    lib.lines_near_lines(
        address(lines_ptr), lines_ptr.size, address(lines_lat), address(lines_lng), lines_lat.size, address(near_ptr), near_ptr.size, address(near_lat), address(near_lng), near_lat.size, spacing_m, cell_lat, cell_lng, address(out)
    )


def segment_cells(edge_from: np.ndarray, edge_to: np.ndarray, lat: np.ndarray, lng: np.ndarray, length_m: np.ndarray, sample_m: float, cell_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Every (cell, segment) a segment's straight line falls in, sampled about every sample_m: each pair once."""
    out = _SegmentCellsC()
    lib.segment_cells(address(edge_from), address(edge_to), address(length_m), edge_from.size, address(lat), address(lng), lat.size, sample_m, cell_deg, C.addressof(out))
    try:
        return copied(out.keys, np.int64), copied(out.edges, np.int32)
    finally:
        lib.free_segment_cells(C.addressof(out))


def roads_at(lat: float, lng: float, direction: float, has_direction: bool, want_ramp: bool, any_road: bool, keys: np.ndarray, items: np.ndarray, cell_deg: float, columns: MapColumns, rules: Matching, edge_sample_m: float, same_road_m: float) -> np.ndarray:
    """Segments of the road nearest a point (both ways of it unless direction says), within rules.match_m."""
    out = Out()
    lib.roads_at(lat, lng, direction, has_direction, want_ramp, any_road, address(keys), address(items), keys.size, cell_deg, columns.address, rules.address, edge_sample_m, same_road_m, C.addressof(out))
    return _ints(out)


def segments_under(lat: float, lng: float, heading: float, any_road: bool, want_ramp: bool, radii: np.ndarray, match_m: float, match_deg: float, sure_m: float, same_road_m: float, grid_fine: Grid, grid_coarse: Grid, fine_limit_m: float, columns: MapColumns) -> np.ndarray:
    """Segments within match_m of a point running along heading, nearest first: those about as near as the nearest."""
    out = Out()
    lib.segments_under(lat, lng, heading, any_road, want_ramp, address(radii), radii.size, match_m, match_deg, sure_m, same_road_m, grid_fine.address, grid_coarse.address, fine_limit_m, columns.address, C.addressof(out))
    return _ints(out)
