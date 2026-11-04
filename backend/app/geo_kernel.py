"""The geometry a plan repeats thousands of times, compiled (Numba): what the route passes, and what's near it.

Great-circle sums, polylines, and lookups in a grid of cells. Each works on flat arrays, and each lets go of
Python's lock (nogil), so trips judged at once don't queue for it. The formulas are app.geo's, whose functions
call these for whole paths and do single sums themselves.

A grid (Grid) is a sorted list of cells: the things in it, ordered by the cell each falls in, found by a binary
search. It's far smaller than a dictionary of lists, and it's what makes these lookups quick.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np
from numba import njit

EARTH_RADIUS_M = 6_371_008.8
_RADIANS = math.pi / 180
_DEGREES = 180 / math.pi


class Grid(NamedTuple):
    keys: np.ndarray  # each thing's cell, sorted
    items: np.ndarray  # what it is: its place in the list the grid was made from
    lat: np.ndarray  # where it is
    lng: np.ndarray
    cell_deg: float


class RouteLine(NamedTuple):
    """A route's points in a grid, with the direction of travel at each."""

    grid: Grid
    bearings: np.ndarray  # as grid.items: per thing in the grid
    reach_lng: int  # cells to search east and west: they narrow away from the equator
    south: float
    west: float
    north: float
    east: float


class EventColumns(NamedTuple):
    """Road events, each a line of points, as flat columns."""

    ptr: np.ndarray  # an event's points are ptr[event] up to ptr[event + 1]
    lat: np.ndarray
    lng: np.ndarray
    south: np.ndarray
    west: np.ndarray
    north: np.ndarray
    east: np.ndarray
    has_direction: np.ndarray
    direction: np.ndarray
    along: np.ndarray
    ramp: np.ndarray


class Matching(NamedTuple):
    """The rules for whether a route drives an event: see app.road_events."""

    match_m: float
    on_route_m: float
    event_on_route_m: float
    match_deg: float
    parallel_deg: float
    margin_lat: float
    margin_lng: float


@njit(cache=True, inline="always")
def haversine(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = lat1 * _RADIANS, lng1 * _RADIANS, lat2 * _RADIANS, lng2 * _RADIANS
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


@njit(cache=True, inline="always")
def bearing(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = lat1 * _RADIANS, lng1 * _RADIANS, lat2 * _RADIANS, lng2 * _RADIANS
    dlng = lng2 - lng1
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.atan2(x, y) * _DEGREES + 360) % 360


@njit(cache=True, inline="always")
def _angle_between(a, b):
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


@njit(cache=True, nogil=True)
def densified(lat, lng, spacing_m):
    """The path's points, plus extra ones so no gap is longer than spacing_m."""
    points = lat.size
    if points == 0:
        return np.empty(0), np.empty(0)
    count = 1
    steps_of = np.empty(points - 1, dtype=np.int64)
    lengths = np.empty(points - 1)
    for i in range(points - 1):
        segment = haversine(lat[i], lng[i], lat[i + 1], lng[i + 1])
        steps = max(1, math.ceil(segment / spacing_m))
        lengths[i], steps_of[i] = segment, steps
        count += steps
    dense_lat = np.empty(count)
    dense_lng = np.empty(count)
    dense_lat[0], dense_lng[0] = lat[0], lng[0]
    at = 1
    for i in range(points - 1):
        steps = steps_of[i]
        if steps > 1:
            segment = lengths[i]
            lat1, lng1 = lat[i] * _RADIANS, lng[i] * _RADIANS
            heading = bearing(lat[i], lng[i], lat[i + 1], lng[i + 1]) * _RADIANS
            sin_lat1, cos_lat1 = math.sin(lat1), math.cos(lat1)
            sin_heading, cos_heading = math.sin(heading), math.cos(heading)
            for k in range(1, steps):
                angular = (segment * k / steps) / EARTH_RADIUS_M
                sin_angular, cos_angular = math.sin(angular), math.cos(angular)
                lat2 = math.asin(sin_lat1 * cos_angular + cos_lat1 * sin_angular * cos_heading)
                lng2 = lng1 + math.atan2(sin_heading * sin_angular * cos_lat1, cos_angular - sin_lat1 * math.sin(lat2))
                dense_lat[at] = lat2 * _DEGREES
                dense_lng[at] = (lng2 * _DEGREES + 540) % 360 - 180
                at += 1
        dense_lat[at], dense_lng[at] = lat[i + 1], lng[i + 1]
        at += 1
    return dense_lat, dense_lng


@njit(cache=True, nogil=True)
def path_length(lat, lng):
    total = 0.0
    for i in range(lat.size - 1):
        total += haversine(lat[i], lng[i], lat[i + 1], lng[i + 1])
    return total


@njit(cache=True, nogil=True)
def decoded(encoded, start, stop, factor):
    """A polyline's points, from its bytes in encoded, start up to stop."""
    capacity = stop - start  # at least one byte per number: more than enough
    lat_out = np.empty(capacity // 2 + 1)
    lng_out = np.empty(capacity // 2 + 1)
    index = start
    lat = lng = 0
    count = 0
    while index < stop:
        for which in range(2):
            shift = 0
            result = 0
            while True:
                if index >= stop:
                    raise ValueError("Truncated polyline string")
                chunk = int(encoded[index]) - 63
                index += 1
                result |= (chunk & 0x1F) << shift
                shift += 5
                if chunk < 0x20:
                    break
            # The lowest bit carries the sign; the value is stored inverted when negative.
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lng += delta
        lat_out[count], lng_out[count] = lat / factor, lng / factor
        count += 1
    return lat_out[:count].copy(), lng_out[:count].copy()


@njit(cache=True, nogil=True)
def encoded(lat, lng, factor):
    """The points as a polyline's bytes."""
    out = np.empty(lat.size * 2 * 13, dtype=np.uint8)  # 13 bytes hold any 64-bit number
    at = 0
    previous_lat = previous_lng = 0
    for i in range(lat.size):
        ilat, ilng = int(np.rint(lat[i] * factor)), int(np.rint(lng[i] * factor))
        for which in range(2):
            delta = ilat - previous_lat if which == 0 else ilng - previous_lng
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out[at] = (0x20 | (value & 0x1F)) + 63
                at += 1
                value >>= 5
            out[at] = value + 63
            at += 1
        previous_lat, previous_lng = ilat, ilng
    return out[:at].copy()


@njit(cache=True, nogil=True)
def path_points(edges, ptr, blob, factor):
    """A route's points, from its segments' polylines (the first point of each after the first is the last one's end)."""
    size = 0
    for edge in edges:
        size += (ptr[edge + 1] - ptr[edge]) // 2 + 1
    lat = np.empty(size)
    lng = np.empty(size)
    at = 0
    for number, edge in enumerate(edges):
        part_lat, part_lng = decoded(blob, ptr[edge], ptr[edge + 1], factor)
        for i in range(0 if number == 0 else 1, part_lat.size):
            lat[at], lng[at] = part_lat[i], part_lng[i]
            at += 1
    return lat[:at].copy(), lng[:at].copy()


@njit(cache=True, nogil=True)
def heading_change(lat, lng, min_segment_m):
    """Sum of absolute heading changes along a path, skipping points too close to the last kept: see app.geo."""
    if lat.size == 0:
        return 0.0
    kept = 0
    total = 0.0
    previous = np.nan
    for i in range(1, lat.size):
        if haversine(lat[kept], lng[kept], lat[i], lng[i]) < min_segment_m:
            continue
        heading = bearing(lat[kept], lng[kept], lat[i], lng[i])
        if previous == previous:
            total += _angle_between(previous, heading)
        previous = heading
        kept = i
    return total


@njit(cache=True, nogil=True)
def share_into_sun(lat, lng, azimuth, glare_deg):
    """The share of a stretch's length driven toward the sun."""
    total = into = 0.0
    for i in range(lat.size - 1):
        length = haversine(lat[i], lng[i], lat[i + 1], lng[i + 1])
        total += length
        if length > 1:
            if _angle_between(bearing(lat[i], lng[i], lat[i + 1], lng[i + 1]), azimuth) <= glare_deg:
                into += length
    return into / total if total else 0.0


# ---- Grids


@njit(cache=True, inline="always")
def _key(row, column):
    return (row << 32) ^ (column & 0xFFFFFFFF)


@njit(cache=True, nogil=True)
def cell_keys(lat, lng, cell_deg):
    keys = np.empty(lat.size, dtype=np.int64)
    for i in range(lat.size):
        keys[i] = _key(int(math.floor(lat[i] / cell_deg)), int(math.floor(lng[i] / cell_deg)))
    return keys


@njit(cache=True, inline="always")
def _first_at(keys, key):
    low, high = 0, keys.size
    while low < high:
        middle = (low + high) >> 1
        if keys[middle] < key:
            low = middle + 1
        else:
            high = middle
    return low


def make_grid(lat: np.ndarray, lng: np.ndarray, cell_deg: float, items: np.ndarray | None = None) -> Grid:
    keys = cell_keys(lat, lng, cell_deg)
    order = np.argsort(keys, kind="stable")
    return Grid(
        keys[order],
        (np.arange(lat.size, dtype=np.int32) if items is None else items)[order].astype(np.int32),
        lat[order],
        lng[order],
        float(cell_deg),
    )


@njit(cache=True, inline="always")
def _reach_lng(lat):
    return int(math.ceil(1 / max(math.cos(lat * _RADIANS), 0.01)))


def reach_lng_at(lat: float) -> int:
    """Cells to search east and west of a point at this latitude: they narrow away from the equator."""
    return math.ceil(1 / max(math.cos(math.radians(lat)), 0.01))


@njit(cache=True, nogil=True)
def any_within(grid, lat, lng, within_m, reach_lng):
    """Whether anything in the cells around a point is within within_m of it."""
    row, column = int(math.floor(lat / grid.cell_deg)), int(math.floor(lng / grid.cell_deg))
    for i in range(row - 1, row + 2):
        for j in range(column - reach_lng, column + reach_lng + 1):
            key = _key(i, j)
            at = _first_at(grid.keys, key)
            while at < grid.keys.size and grid.keys[at] == key:
                if haversine(lat, lng, grid.lat[at], grid.lng[at]) <= within_m:
                    return True
                at += 1
    return False


@njit(cache=True, nogil=True)
def share_near(grid, samples_lat, samples_lng, within_m, reach_lng):
    """How many of the samples have something in the grid within within_m."""
    count = 0
    for i in range(samples_lat.size):
        if any_within(grid, samples_lat[i], samples_lng[i], within_m, reach_lng):
            count += 1
    return count


@njit(cache=True, nogil=True)
def mark_near_stretches(grid, ptr, lat, lng, spacing_m, within_m, found):
    """Set found[item] for everything in the grid within within_m of the stretches of road given (ptr: where each starts)."""
    for stretch in range(ptr.size - 1):
        start, stop = ptr[stretch], ptr[stretch + 1]
        if start == stop:
            continue
        reach_lng = _reach_lng(lat[start])
        dense_lat, dense_lng = densified(lat[start:stop], lng[start:stop], spacing_m)
        for s in range(dense_lat.size):
            point_lat, point_lng = dense_lat[s], dense_lng[s]
            row, column = int(math.floor(point_lat / grid.cell_deg)), int(math.floor(point_lng / grid.cell_deg))
            for i in range(row - 1, row + 2):
                for j in range(column - reach_lng, column + reach_lng + 1):
                    key = _key(i, j)
                    at = _first_at(grid.keys, key)
                    while at < grid.keys.size and grid.keys[at] == key:
                        item = grid.items[at]
                        if not found[item] and haversine(point_lat, point_lng, grid.lat[at], grid.lng[at]) <= within_m:
                            found[item] = True
                        at += 1


@njit(cache=True, inline="always")
def _node_reach(lat, distance_m, cell_deg, m_per_deg):
    reach_lat = int(math.ceil(distance_m / (m_per_deg * cell_deg)))
    lng_cell_m = m_per_deg * cell_deg * max(math.cos(lat * _RADIANS), 0.01)
    return reach_lat, int(math.ceil(distance_m / lng_cell_m))


@njit(cache=True, nogil=True)
def items_near(grid, lat, lng, distance_m, m_per_deg):
    """What's in the grid cells within distance_m of a point: a superset of what's within distance_m."""
    row, column = int(math.floor(lat / grid.cell_deg)), int(math.floor(lng / grid.cell_deg))
    reach_lat, reach_lng = _node_reach(lat, distance_m, grid.cell_deg, m_per_deg)
    total = 0
    for i in range(row - reach_lat, row + reach_lat + 1):
        for j in range(column - reach_lng, column + reach_lng + 1):
            key = _key(i, j)
            at = _first_at(grid.keys, key)
            while at < grid.keys.size and grid.keys[at] == key:
                total += 1
                at += 1
    found = np.empty(total, dtype=np.int32)
    total = 0
    for i in range(row - reach_lat, row + reach_lat + 1):
        for j in range(column - reach_lng, column + reach_lng + 1):
            key = _key(i, j)
            at = _first_at(grid.keys, key)
            while at < grid.keys.size and grid.keys[at] == key:
                found[total] = grid.items[at]
                total += 1
                at += 1
    return found


@njit(cache=True, nogil=True)
def nearest_item(grid, lat, lng, max_distance_m, m_per_deg):
    """The closest thing in the grid within max_distance_m of a point, or -1."""
    row, column = int(math.floor(lat / grid.cell_deg)), int(math.floor(lng / grid.cell_deg))
    reach_lat, reach_lng = _node_reach(lat, max_distance_m, grid.cell_deg, m_per_deg)
    best = -1
    best_m = max_distance_m
    for i in range(row - reach_lat, row + reach_lat + 1):
        for j in range(column - reach_lng, column + reach_lng + 1):
            key = _key(i, j)
            at = _first_at(grid.keys, key)
            while at < grid.keys.size and grid.keys[at] == key:
                distance = haversine(lat, lng, grid.lat[at], grid.lng[at])
                if distance <= best_m:
                    best, best_m = grid.items[at], distance
                at += 1
    return best


@njit(cache=True, nogil=True)
def edges_along(grid, lat, lng, within_m, m_per_deg, out_ptr, out_edges, edge_to, on_route):
    """Segments a route's points follow: both of their junctions lie on it. on_route is all False on the way in."""
    touched = np.empty(64, dtype=np.int32)
    count = 0
    for s in range(lat.size):
        row, column = int(math.floor(lat[s] / grid.cell_deg)), int(math.floor(lng[s] / grid.cell_deg))
        reach_lat, reach_lng = _node_reach(lat[s], within_m, grid.cell_deg, m_per_deg)
        for i in range(row - reach_lat, row + reach_lat + 1):
            for j in range(column - reach_lng, column + reach_lng + 1):
                key = _key(i, j)
                at = _first_at(grid.keys, key)
                while at < grid.keys.size and grid.keys[at] == key:
                    node = grid.items[at]
                    if not on_route[node] and haversine(lat[s], lng[s], grid.lat[at], grid.lng[at]) <= within_m:
                        on_route[node] = True
                        if count == touched.size:
                            bigger = np.empty(count * 2, dtype=np.int32)
                            bigger[:count] = touched
                            touched = bigger
                        touched[count] = node
                        count += 1
                    at += 1
    total = 0
    for n in range(count):
        node = touched[n]
        for i in range(out_ptr[node], out_ptr[node + 1]):
            if on_route[edge_to[out_edges[i]]]:
                total += 1
    found = np.empty(total, dtype=np.int32)
    total = 0
    for n in range(count):
        node = touched[n]
        for i in range(out_ptr[node], out_ptr[node + 1]):
            edge = out_edges[i]
            if on_route[edge_to[edge]]:
                found[total] = edge
                total += 1
    return found


# ---- A route and the road events on it


@njit(cache=True, nogil=True)
def bearings_of(lat, lng):
    """The direction of travel at each point of a path."""
    out = np.empty(lat.size)
    for i in range(lat.size):
        if i + 1 < lat.size:
            out[i] = bearing(lat[i], lng[i], lat[i + 1], lng[i + 1])
        else:
            out[i] = bearing(lat[i - 1], lng[i - 1], lat[i], lng[i])
    return out


@njit(cache=True, inline="always")
def _carries(travel, has_line, line, has_direction, direction, along, rules):
    """Whether traffic heading along travel, beside an event's line (bearing line there), is the traffic it affects."""
    if along and has_line:
        return _angle_between(travel, line) <= rules.match_deg
    if has_line:
        angle = _angle_between(travel, line)
        if min(angle, 180 - angle) > rules.parallel_deg:
            return False  # crossing the event's road, not driving along it
    return not has_direction or _angle_between(travel, direction) <= rules.match_deg


@njit(cache=True, nogil=True)
def _route_carries(route, lat, lng, within_m, has_line, line, has_direction, direction, along, rules):
    """Whether the route passes within within_m of a point, going the way an event's traffic goes."""
    grid = route.grid
    row, column = int(math.floor(lat / grid.cell_deg)), int(math.floor(lng / grid.cell_deg))
    for i in range(row - 1, row + 2):
        for j in range(column - route.reach_lng, column + route.reach_lng + 1):
            key = _key(i, j)
            at = _first_at(grid.keys, key)
            while at < grid.keys.size and grid.keys[at] == key:
                if haversine(lat, lng, grid.lat[at], grid.lng[at]) <= within_m and _carries(
                    route.bearings[at], has_line, line, has_direction, direction, along, rules
                ):
                    return True
                at += 1
    return False


@njit(cache=True, inline="always")
def _overlaps(route, events, event, rules):
    """A quick test first, as a trip can have a hundred events around it."""
    return (
        events.south[event] <= route.north + rules.margin_lat
        and events.north[event] >= route.south - rules.margin_lat
        and events.west[event] <= route.east + rules.margin_lng
        and events.east[event] >= route.west - rules.margin_lng
    )


@njit(cache=True, nogil=True)
def runs_along_each(route, events, wanted, rules, out):
    """Set out[event] for each wanted event the route drives through, the way its traffic goes."""
    for event in range(events.ptr.size - 1):
        if not wanted[event] or not _overlaps(route, events, event, rules):
            continue
        start, stop = events.ptr[event], events.ptr[event + 1]
        has_direction, direction, along = events.has_direction[event], events.direction[event], events.along[event]
        if stop - start == 1:
            # A source's point may sit a little off the road: the looser match.
            out[event] = _route_carries(route, events.lat[start], events.lng[start], rules.match_m, False, 0.0, has_direction, direction, along, rules)
            continue
        if events.ramp[event] and not (
            _route_carries(route, events.lat[start], events.lng[start], rules.on_route_m, False, 0.0, False, 0.0, False, rules)
            and _route_carries(route, events.lat[stop - 1], events.lng[stop - 1], rules.on_route_m, False, 0.0, False, 0.0, False, rules)
        ):
            continue  # a ramp runs beside its freeway for a while: using it means passing both its ends
        spacing = rules.match_m / 2
        path_lat, path_lng = events.lat[start:stop], events.lng[start:stop]
        samples_lat, samples_lng = densified(path_lat, path_lng, spacing)
        lines = bearings_of(samples_lat, samples_lng)
        longest = run = misses = 0
        for s in range(samples_lat.size):
            if _route_carries(route, samples_lat[s], samples_lng[s], rules.on_route_m, True, lines[s], has_direction, direction, along, rules):
                run += 1 + misses  # a single missed sample (a curve drawn differently) doesn't break the run
                misses = 0
            elif run != 0 and misses == 0:
                misses = 1
            else:
                run = misses = 0
            longest = max(longest, run)
        out[event] = longest * spacing >= min(rules.event_on_route_m, path_length(path_lat, path_lng) / 2)


@njit(cache=True, nogil=True)
def share_along_each(route, events, wanted, rules, out):
    """Set out[event] for each wanted event: the share of its stretch the route drives, the way its traffic goes."""
    for event in range(events.ptr.size - 1):
        if not wanted[event] or not _overlaps(route, events, event, rules):
            continue
        start, stop = events.ptr[event], events.ptr[event + 1]
        has_direction, direction, along = events.has_direction[event], events.direction[event], events.along[event]
        samples_lat, samples_lng = densified(events.lat[start:stop], events.lng[start:stop], rules.match_m / 2)
        lines = bearings_of(samples_lat, samples_lng)
        matched = 0
        for s in range(samples_lat.size):
            if _route_carries(route, samples_lat[s], samples_lng[s], rules.on_route_m, True, lines[s], has_direction, direction, along, rules):
                matched += 1
        out[event] = matched / samples_lat.size


@njit(cache=True, nogil=True)
def mark_carried(route, samples_lat, samples_lng, lines, has_direction, direction, along, rules, covered):
    """Set covered[sample] for each sample of an event's line this other line runs beside, the way its traffic goes."""
    for s in range(samples_lat.size):
        if not covered[s] and _route_carries(route, samples_lat[s], samples_lng[s], rules.on_route_m, True, lines[s], has_direction, direction, along, rules):
            covered[s] = True


# ---- Segments near a point


@njit(cache=True, inline="always")
def _distance_to_path(lat, lng, path_lat, path_lng, m_per_deg):
    """Distance from a point to the nearest spot on a path, and the path's bearing there: see app.geo."""
    m_per_deg_lng = m_per_deg * math.cos(lat * _RADIANS)
    best_m, best_bearing = np.inf, 0.0
    for i in range(path_lat.size - 1):
        ax, ay = (path_lng[i] - lng) * m_per_deg_lng, (path_lat[i] - lat) * m_per_deg
        bx, by = (path_lng[i + 1] - lng) * m_per_deg_lng, (path_lat[i + 1] - lat) * m_per_deg
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
        distance = math.hypot(ax + t * dx, ay + t * dy)
        if distance < best_m and length_sq > 0:
            best_m, best_bearing = distance, bearing(path_lat[i], path_lng[i], path_lat[i + 1], path_lng[i + 1])
    return best_m, best_bearing


@njit(cache=True, nogil=True)
def segment_cells(edge_from, edge_to, lat, lng, length_m, sample_m, cell_deg):
    """Every (cell, segment) a segment's straight line falls in, sampled about every sample_m: each pair once."""
    capacity = edge_from.size * 3 + 16
    keys = np.empty(capacity, dtype=np.int64)
    edges = np.empty(capacity, dtype=np.int32)
    count = 0
    for edge in range(edge_from.size):
        lat1, lng1, lat2, lng2 = lat[edge_from[edge]], lng[edge_from[edge]], lat[edge_to[edge]], lng[edge_to[edge]]
        steps = max(1, math.ceil(length_m[edge] / sample_m))
        start = count
        for k in range(steps + 1):
            key = _key(
                int(math.floor((lat1 + (lat2 - lat1) * k / steps) / cell_deg)),
                int(math.floor((lng1 + (lng2 - lng1) * k / steps) / cell_deg)),
            )
            filed = False
            for i in range(start, count):
                filed = filed or keys[i] == key
            if filed:
                continue
            if count == capacity:
                capacity *= 2
                bigger_keys = np.empty(capacity, dtype=np.int64)
                bigger_edges = np.empty(capacity, dtype=np.int32)
                bigger_keys[:count] = keys[:count]
                bigger_edges[:count] = edges[:count]
                keys, edges = bigger_keys, bigger_edges
            keys[count], edges[count] = key, edge
            count += 1
    return keys[:count].copy(), edges[:count].copy()


@njit(cache=True, nogil=True)
def roads_at(
    lat,
    lng,
    direction,
    has_direction,
    want_ramp,
    any_road,
    keys,
    items,
    cell_deg,
    is_link,
    edge_from,
    edge_to,
    node_lat,
    node_lng,
    length_m,
    ptr,
    blob,
    factor,
    rules,
    m_per_deg,
    m_per_deg_flat,
    edge_sample_m,
    same_road_m,
):
    """Segments of the road nearest a point (both ways of it unless direction says), within match_m: see RoadGraph."""
    row, column = int(math.floor(lat / cell_deg)), int(math.floor(lng / cell_deg))
    candidates = np.empty(64, dtype=np.int32)
    count = 0
    for i in range(row - 1, row + 2):
        for j in range(column - 1, column + 2):
            key = _key(i, j)
            at = _first_at(keys, key)
            while at < keys.size and keys[at] == key:
                if count == candidates.size:
                    bigger = np.empty(count * 2, dtype=np.int32)
                    bigger[:count] = candidates
                    candidates = bigger
                candidates[count] = items[at]
                count += 1
                at += 1
    if count == 0:
        return np.empty(0, dtype=np.int32)
    candidates = np.unique(candidates[:count])
    m_per_deg_lng = m_per_deg_flat * math.cos(lat * _RADIANS)
    offsets = np.empty(candidates.size)
    kept = np.empty(candidates.size, dtype=np.int32)
    found = 0
    nearest = np.inf
    for edge in candidates:
        if not any_road and bool(is_link[edge]) != want_ramp:
            continue
        a, b = edge_from[edge], edge_to[edge]
        # Far even allowing for a curve: skip decoding its shape.
        ax, ay = (node_lng[a] - lng) * m_per_deg_lng, (node_lat[a] - lat) * m_per_deg_flat
        dx, dy = (node_lng[b] - lng) * m_per_deg_lng - ax, (node_lat[b] - lat) * m_per_deg_flat - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
        if math.hypot(ax + t * dx, ay + t * dy) > rules.match_m + min(length_m[edge] / 2, edge_sample_m):
            continue
        path_lat, path_lng = decoded(blob, ptr[edge], ptr[edge + 1], factor)
        offset, heading = _distance_to_path(lat, lng, path_lat, path_lng, m_per_deg)
        if offset <= rules.match_m and _carries(heading, False, 0.0, has_direction, direction, False, rules):
            offsets[found], kept[found] = offset, edge
            found += 1
            nearest = min(nearest, offset)
    result = np.empty(found, dtype=np.int32)
    count = 0
    for i in range(found):
        if offsets[i] <= nearest + same_road_m:
            result[count] = kept[i]
            count += 1
    return result[:count].copy()


@njit(cache=True, nogil=True)
def segments_under(
    lat,
    lng,
    heading,
    any_road,
    want_ramp,
    is_link,
    radii,
    match_m,
    match_deg,
    sure_m,
    same_road_m,
    grid_fine,
    grid_coarse,
    fine_limit_m,
    in_ptr,
    in_edges,
    node_lat,
    node_lng,
    ptr,
    blob,
    factor,
    m_per_deg_lookup,
    m_per_deg_geo,
    checked_marks,
    edge_offsets,
):
    """Segments within match_m of a point running along heading, nearest first: those about as near as the nearest.

    checked_marks and edge_offsets are scratch, all zeros on the way in, so only what's touched costs memory."""
    edge_list = np.empty(64, dtype=np.int32)
    edge_count = 0
    nearest = np.inf
    for radius in radii:
        grid = grid_fine if radius <= fine_limit_m else grid_coarse
        row, column = int(math.floor(lat / grid.cell_deg)), int(math.floor(lng / grid.cell_deg))
        reach_lat, reach_lng = _node_reach(lat, radius, grid.cell_deg, m_per_deg_lookup)
        for i in range(row - reach_lat, row + reach_lat + 1):
            for j in range(column - reach_lng, column + reach_lng + 1):
                key = _key(i, j)
                at = _first_at(grid.keys, key)
                while at < grid.keys.size and grid.keys[at] == key:
                    node = grid.items[at]
                    at_this = at
                    at += 1
                    if checked_marks[node]:
                        continue
                    # The car drives toward the end of its segment, so skip junctions behind it.
                    if haversine(lat, lng, grid.lat[at_this], grid.lng[at_this]) > match_m and (
                        _angle_between(bearing(lat, lng, grid.lat[at_this], grid.lng[at_this]), heading) > 90
                    ):
                        continue
                    checked_marks[node] = 1
                    for e in range(in_ptr[node], in_ptr[node + 1]):
                        edge = in_edges[e]
                        if not any_road and bool(is_link[edge]) != want_ramp:
                            continue
                        path_lat, path_lng = decoded(blob, ptr[edge], ptr[edge + 1], factor)
                        offset, along = _distance_to_path(lat, lng, path_lat, path_lng, m_per_deg_geo)
                        if offset <= match_m and _angle_between(along, heading) <= match_deg:
                            if edge_count == edge_list.size:
                                bigger_edges = np.empty(edge_count * 2, dtype=np.int32)
                                bigger_edges[:edge_count] = edge_list
                                edge_list = bigger_edges
                            edge_list[edge_count] = edge
                            edge_count += 1
                            edge_offsets[edge] = offset
                            nearest = min(nearest, offset)
        if edge_count > 0 and nearest <= sure_m:
            break  # a wider search could only find roads farther from the car
    # Nearest first: a stable insertion sort, as there are only ever a few.
    kept = 0
    result = np.empty(edge_count, dtype=np.int32)
    for n in range(edge_count):
        edge = edge_list[n]
        offset = edge_offsets[edge]
        if offset > nearest + same_road_m:
            continue
        place = kept
        while place > 0 and edge_offsets[result[place - 1]] > offset:
            result[place] = result[place - 1]
            place -= 1
        result[place] = edge
        kept += 1
    return result[:kept].copy()
