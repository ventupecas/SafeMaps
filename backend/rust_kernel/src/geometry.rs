//! The geometry a plan repeats thousands of times: what the route passes, and what's near it.
//!
//! Great-circle sums, polylines, and lookups in a grid of cells, on flat arrays. The formulas are app.geo's, whose
//! functions call these for whole paths and do single sums themselves.
//!
//! A grid (Grid) is a sorted list of cells: the things in it, ordered by the cell each falls in, found by a binary
//! search. It's far smaller than a dictionary of lists, and it's what makes these lookups quick.

use crate::{Arr, Out};

const EARTH_RADIUS_M: f64 = 6_371_008.8;
const RADIANS: f64 = std::f64::consts::PI / 180.0;
const DEGREES: f64 = 180.0 / std::f64::consts::PI;

#[repr(C)]
pub struct Grid {
    pub keys: Arr<i64>,
    pub items: Arr<i32>,
    pub lat: Arr<f64>,
    pub lng: Arr<f64>,
    pub cell_deg: f64,
}

#[repr(C)]
pub struct RouteLine {
    pub grid: Grid,
    pub bearings: Arr<f64>,
    pub reach_lng: i64,
    pub south: f64,
    pub west: f64,
    pub north: f64,
    pub east: f64,
    // Which big cells the route is in or next to: most of what's looked up isn't near it, and one read says so.
    pub cells: Arr<u8>,
    pub cell_rows: i64,
    pub cell_columns: i64,
    pub cell_south: f64,
    pub cell_west: f64,
    pub cell_lat: f64,
    pub cell_lng: f64,
}

#[repr(C)]
pub struct EventColumns {
    pub ptr: Arr<i64>,
    pub lat: Arr<f64>,
    pub lng: Arr<f64>,
    pub south: Arr<f64>,
    pub west: Arr<f64>,
    pub north: Arr<f64>,
    pub east: Arr<f64>,
    pub has_direction: Arr<u8>,
    pub direction: Arr<f64>,
    pub along: Arr<u8>,
    pub ramp: Arr<u8>,
    // Each event's line, sampled once: sample_ptr[event] up to sample_ptr[event + 1].
    pub sample_ptr: Arr<i64>,
    pub sample_lat: Arr<f64>,
    pub sample_lng: Arr<f64>,
    pub sample_line: Arr<f64>,
    pub length_m: Arr<f64>,
}

#[repr(C)]
pub struct EventSamples {
    pub lat: Out<f64>,
    pub lng: Out<f64>,
    pub line: Out<f64>,
}

#[no_mangle]
pub unsafe extern "C" fn free_event_samples(samples: *mut EventSamples) {
    (*samples).lat.free();
    (*samples).lng.free();
    (*samples).line.free();
}

/// Each event's line sampled every spacing_m, with the bearing of the line at each sample, and its length.
#[no_mangle]
pub unsafe extern "C" fn event_samples(
    ptr: *const i64,
    ptr_len: i64,
    lat: *const f64,
    lng: *const f64,
    n: i64,
    spacing_m: f64,
    sample_ptr: *mut i64,
    length_m: *mut f64,
    out: *mut EventSamples,
) {
    let (ptr, lat, lng) = (Arr::new(ptr, ptr_len), Arr::new(lat, n), Arr::new(lng, n));
    let (sample_ptr, length_m) = (Arr::new(sample_ptr, ptr_len), Arr::new(length_m, (ptr_len - 1).max(0)));
    let (mut all_lat, mut all_lng, mut all_line) = (Vec::new(), Vec::new(), Vec::new());
    sample_ptr.set(0, 0);
    for event in 0..(ptr_len as usize).saturating_sub(1) {
        let (start, stop) = (ptr.get(event) as usize, ptr.get(event + 1) as usize);
        let (path_lat, path_lng) = (lat.part(start, stop), lng.part(start, stop));
        let (samples_lat, samples_lng) = densify(path_lat, path_lng, spacing_m);
        all_line.extend(bearings_along(&samples_lat, &samples_lng));
        all_lat.extend(samples_lat);
        all_lng.extend(samples_lng);
        sample_ptr.set(event + 1, all_lat.len() as i64);
        length_m.set(event, path_length(path_lat.p, path_lng.p, path_lat.n));
    }
    (*out).lat = Out::from_vec(all_lat);
    (*out).lng = Out::from_vec(all_lng);
    (*out).line = Out::from_vec(all_line);
}

/// Mark the big cells a route's points are in, and those next to them.
#[no_mangle]
pub unsafe extern "C" fn mark_route_cells(lat: *const f64, lng: *const f64, n: i64, south: f64, west: f64, cell_lat: f64, cell_lng: f64, rows: i64, columns: i64, cells: *mut u8) {
    let (lat, lng, cells) = (Arr::new(lat, n), Arr::new(lng, n), Arr::new(cells, rows * columns));
    for i in 0..n as usize {
        let row = ((lat.get(i) - south) / cell_lat).floor() as i64;
        let column = ((lng.get(i) - west) / cell_lng).floor() as i64;
        for r in (row - 1).max(0)..=(row + 1).min(rows - 1) {
            for c in (column - 1).max(0)..=(column + 1).min(columns - 1) {
                cells.set((r * columns + c) as usize, 1);
            }
        }
    }
}

#[repr(C)]
pub struct Matching {
    pub match_m: f64,
    pub on_route_m: f64,
    pub event_on_route_m: f64,
    pub match_deg: f64,
    pub parallel_deg: f64,
    pub margin_lat: f64,
    pub margin_lng: f64,
}

#[repr(C)]
pub struct Points {
    pub lat: Out<f64>,
    pub lng: Out<f64>,
}

#[no_mangle]
pub unsafe extern "C" fn free_points(points: *mut Points) {
    (*points).lat.free();
    (*points).lng.free();
}

#[inline(always)]
pub fn haversine(lat1: f64, lng1: f64, lat2: f64, lng2: f64) -> f64 {
    let (lat1, lng1, lat2, lng2) = (lat1 * RADIANS, lng1 * RADIANS, lat2 * RADIANS, lng2 * RADIANS);
    let (a, b) = (((lat2 - lat1) / 2.0).sin(), ((lng2 - lng1) / 2.0).sin());
    let h = a * a + lat1.cos() * lat2.cos() * (b * b);
    2.0 * EARTH_RADIUS_M * h.sqrt().min(1.0).asin()
}

#[inline(always)]
fn bearing(lat1: f64, lng1: f64, lat2: f64, lng2: f64) -> f64 {
    let (lat1, lng1, lat2, lng2) = (lat1 * RADIANS, lng1 * RADIANS, lat2 * RADIANS, lng2 * RADIANS);
    let dlng = lng2 - lng1;
    let x = dlng.sin() * lat2.cos();
    let y = lat1.cos() * lat2.sin() - lat1.sin() * lat2.cos() * dlng.cos();
    (x.atan2(y) * DEGREES + 360.0).rem_euclid(360.0)
}

#[inline(always)]
fn angle_between(a: f64, b: f64) -> f64 {
    let diff = (a - b).abs() % 360.0;
    diff.min(360.0 - diff)
}

#[inline(always)]
fn key_of(row: i64, column: i64) -> i64 {
    (row << 32) ^ (column & 0xFFFF_FFFF)
}

#[inline(always)]
fn cell_of(value: f64, size: f64) -> i64 {
    (value / size).floor() as i64
}

#[inline(always)]
fn reach_lng_at(lat: f64) -> i64 {
    (1.0 / (lat * RADIANS).cos().max(0.01)).ceil() as i64
}

/// The steps a segment is split into, so no gap is longer than spacing_m. A segment without a sensible length, such
/// as one from a coordinate that isn't a number, isn't split at all.
#[inline(always)]
fn steps_for(segment: f64, spacing_m: f64) -> i64 {
    let steps = (segment / spacing_m).ceil();
    if steps >= 1.0 && steps < MAX_STEPS {
        steps as i64
    } else if steps >= MAX_STEPS {
        MAX_STEPS as i64
    } else {
        1
    }
}

const MAX_STEPS: f64 = 1_000_000.0;

unsafe fn densify(lat: Arr<f64>, lng: Arr<f64>, spacing_m: f64) -> (Vec<f64>, Vec<f64>) {
    let points = lat.len();
    if points == 0 {
        return (Vec::new(), Vec::new());
    }
    let mut lengths = Vec::with_capacity(points - 1);
    let mut count = 1usize;
    for i in 0..points - 1 {
        let segment = haversine(lat.get(i), lng.get(i), lat.get(i + 1), lng.get(i + 1));
        let steps = steps_for(segment, spacing_m);
        lengths.push((segment, steps));
        count += steps as usize;
    }
    let mut dense_lat = Vec::with_capacity(count);
    let mut dense_lng = Vec::with_capacity(count);
    dense_lat.push(lat.get(0));
    dense_lng.push(lng.get(0));
    for i in 0..points - 1 {
        let (segment, steps) = lengths[i];
        if steps > 1 {
            let (lat1, lng1) = (lat.get(i) * RADIANS, lng.get(i) * RADIANS);
            let heading = bearing(lat.get(i), lng.get(i), lat.get(i + 1), lng.get(i + 1)) * RADIANS;
            let (sin_lat1, cos_lat1) = lat1.sin_cos();
            let (sin_heading, cos_heading) = heading.sin_cos();
            for k in 1..steps {
                let angular = (segment * k as f64 / steps as f64) / EARTH_RADIUS_M;
                let (sin_angular, cos_angular) = angular.sin_cos();
                let lat2 = (sin_lat1 * cos_angular + cos_lat1 * sin_angular * cos_heading).asin();
                let lng2 = lng1 + (sin_heading * sin_angular * cos_lat1).atan2(cos_angular - sin_lat1 * lat2.sin());
                dense_lat.push(lat2 * DEGREES);
                dense_lng.push((lng2 * DEGREES + 540.0).rem_euclid(360.0) - 180.0);
            }
        }
        dense_lat.push(lat.get(i + 1));
        dense_lng.push(lng.get(i + 1));
    }
    (dense_lat, dense_lng)
}

/// The path's points, plus extra ones so no gap is longer than spacing_m.
#[no_mangle]
pub unsafe extern "C" fn densified(lat: *const f64, lng: *const f64, n: i64, spacing_m: f64, out: *mut Points) {
    let (dense_lat, dense_lng) = densify(Arr::new(lat, n), Arr::new(lng, n), spacing_m);
    (*out).lat = Out::from_vec(dense_lat);
    (*out).lng = Out::from_vec(dense_lng);
}

#[no_mangle]
pub unsafe extern "C" fn path_length(lat: *const f64, lng: *const f64, n: i64) -> f64 {
    let (lat, lng) = (Arr::new(lat, n), Arr::new(lng, n));
    let mut total = 0.0;
    for i in 0..(n as usize).saturating_sub(1) {
        total += haversine(lat.get(i), lng.get(i), lat.get(i + 1), lng.get(i + 1));
    }
    total
}

/// A polyline's points, added to lat and lng. False if it ends in the middle of a number.
#[inline(always)]
unsafe fn decode_into(encoded: Arr<u8>, start: usize, stop: usize, factor: f64, lat_out: &mut Vec<f64>, lng_out: &mut Vec<f64>, skip_first: bool) -> bool {
    let mut index = start;
    let (mut lat, mut lng) = (0i64, 0i64);
    let mut first = true;
    while index < stop {
        for which in 0..2 {
            let (mut shift, mut result) = (0u32, 0i64);
            loop {
                if index >= stop {
                    return false;
                }
                let chunk = encoded.get(index) as i64 - 63;
                index += 1;
                if shift < 63 {
                    result |= (chunk & 0x1F) << shift;
                }
                shift += 5;
                if chunk < 0x20 {
                    break;
                }
            }
            // The lowest bit carries the sign; the value is stored inverted when negative.
            let delta = if result & 1 != 0 { !(result >> 1) } else { result >> 1 };
            if which == 0 {
                lat = lat.wrapping_add(delta);
            } else {
                lng = lng.wrapping_add(delta);
            }
        }
        if !(first && skip_first) {
            lat_out.push(lat as f64 / factor);
            lng_out.push(lng as f64 / factor);
        }
        first = false;
    }
    true
}

/// A polyline's points, from its bytes in encoded, start up to stop. Returns false if it's cut short.
#[no_mangle]
pub unsafe extern "C" fn decoded(encoded: *const u8, encoded_len: i64, start: i64, stop: i64, factor: f64, out: *mut Points) -> bool {
    let mut lat = Vec::with_capacity(((stop - start) / 2 + 1) as usize);
    let mut lng = Vec::with_capacity(((stop - start) / 2 + 1) as usize);
    let complete = decode_into(Arr::new(encoded, encoded_len), start as usize, stop as usize, factor, &mut lat, &mut lng, false);
    (*out).lat = Out::from_vec(lat);
    (*out).lng = Out::from_vec(lng);
    complete
}

/// The points as a polyline's bytes.
#[no_mangle]
pub unsafe extern "C" fn encoded(lat: *const f64, lng: *const f64, n: i64, factor: f64, out: *mut Out<u8>) {
    let (lat, lng) = (Arr::new(lat, n), Arr::new(lng, n));
    let mut bytes = Vec::with_capacity(n as usize * 6);
    let (mut previous_lat, mut previous_lng) = (0i64, 0i64);
    for i in 0..n as usize {
        // Rounded as Python rounds: a half goes to the even number.
        let (ilat, ilng) = ((lat.get(i) * factor).round_ties_even() as i64, (lng.get(i) * factor).round_ties_even() as i64);
        for delta in [ilat - previous_lat, ilng - previous_lng] {
            let mut value = if delta < 0 { !(delta << 1) } else { delta << 1 };
            while value >= 0x20 {
                bytes.push(((0x20 | (value & 0x1F)) + 63) as u8);
                value >>= 5;
            }
            bytes.push((value + 63) as u8);
        }
        previous_lat = ilat;
        previous_lng = ilng;
    }
    *out = Out::from_vec(bytes);
}

/// A route's points, from its segments' polylines (each after the first starts where the last one ended).
#[no_mangle]
pub unsafe extern "C" fn path_points(edges: *const i32, n: i64, ptr: *const i64, ptr_len: i64, blob: *const u8, blob_len: i64, factor: f64, out: *mut Points) {
    let (edges, ptr, blob) = (Arr::new(edges, n), Arr::new(ptr, ptr_len), Arr::new(blob, blob_len));
    let mut size = 0usize;
    for i in 0..n as usize {
        let edge = edges.get(i) as usize;
        size += ((ptr.get(edge + 1) - ptr.get(edge)) / 2 + 1) as usize;
    }
    let mut lat = Vec::with_capacity(size);
    let mut lng = Vec::with_capacity(size);
    for i in 0..n as usize {
        let edge = edges.get(i) as usize;
        decode_into(blob, ptr.get(edge) as usize, ptr.get(edge + 1) as usize, factor, &mut lat, &mut lng, i != 0);
    }
    (*out).lat = Out::from_vec(lat);
    (*out).lng = Out::from_vec(lng);
}

/// Sum of absolute heading changes along a path, skipping points too close to the last kept: see app.geo.
#[no_mangle]
pub unsafe extern "C" fn heading_change(lat: *const f64, lng: *const f64, n: i64, min_segment_m: f64) -> f64 {
    let (lat, lng) = (Arr::new(lat, n), Arr::new(lng, n));
    if n == 0 {
        return 0.0;
    }
    let mut kept = 0usize;
    let mut total = 0.0;
    let mut previous = f64::NAN;
    for i in 1..n as usize {
        if !(haversine(lat.get(kept), lng.get(kept), lat.get(i), lng.get(i)) >= min_segment_m) {
            continue;
        }
        let heading = bearing(lat.get(kept), lng.get(kept), lat.get(i), lng.get(i));
        if !previous.is_nan() {
            total += angle_between(previous, heading);
        }
        previous = heading;
        kept = i;
    }
    total
}

/// The share of a stretch's length driven toward the sun.
#[no_mangle]
pub unsafe extern "C" fn share_into_sun(lat: *const f64, lng: *const f64, n: i64, azimuth: f64, glare_deg: f64) -> f64 {
    let (lat, lng) = (Arr::new(lat, n), Arr::new(lng, n));
    let (mut total, mut into) = (0.0, 0.0);
    for i in 0..(n as usize).saturating_sub(1) {
        let length = haversine(lat.get(i), lng.get(i), lat.get(i + 1), lng.get(i + 1));
        total += length;
        if length > 1.0 && angle_between(bearing(lat.get(i), lng.get(i), lat.get(i + 1), lng.get(i + 1)), azimuth) <= glare_deg {
            into += length;
        }
    }
    if total != 0.0 {
        into / total
    } else {
        0.0
    }
}

// ---- Grids

#[no_mangle]
pub unsafe extern "C" fn cell_keys(lat: *const f64, lng: *const f64, n: i64, cell_deg: f64, keys: *mut i64) {
    let (lat, lng, keys) = (Arr::new(lat, n), Arr::new(lng, n), Arr::new(keys, n));
    for i in 0..n as usize {
        keys.set(i, key_of(cell_of(lat.get(i), cell_deg), cell_of(lng.get(i), cell_deg)));
    }
}

#[inline(always)]
unsafe fn first_at(keys: Arr<i64>, key: i64) -> usize {
    let (mut low, mut high) = (0usize, keys.len());
    while low < high {
        let middle = (low + high) >> 1;
        if keys.get(middle) < key {
            low = middle + 1;
        } else {
            high = middle;
        }
    }
    low
}

/// Call visit for each thing in the cells around a point, until it returns true. Returns whether it did.
#[inline(always)]
unsafe fn in_cells_around<F: FnMut(usize) -> bool>(grid: &Grid, lat: f64, lng: f64, reach_lat: i64, reach_lng: i64, mut visit: F) -> bool {
    let (row, column) = (cell_of(lat, grid.cell_deg), cell_of(lng, grid.cell_deg));
    for i in row - reach_lat..=row + reach_lat {
        for j in column - reach_lng..=column + reach_lng {
            let key = key_of(i, j);
            let mut at = first_at(grid.keys, key);
            while at < grid.keys.len() && grid.keys.get(at) == key {
                if visit(at) {
                    return true;
                }
                at += 1;
            }
        }
    }
    false
}

#[inline(always)]
unsafe fn any_near(grid: &Grid, lat: f64, lng: f64, within_m: f64, reach_lng: i64) -> bool {
    in_cells_around(grid, lat, lng, 1, reach_lng, |at| haversine(lat, lng, grid.lat.get(at), grid.lng.get(at)) <= within_m)
}

/// Whether anything in the cells around a point is within within_m of it.
#[no_mangle]
pub unsafe extern "C" fn any_within(grid: *const Grid, lat: f64, lng: f64, within_m: f64, reach_lng: i64) -> bool {
    any_near(&*grid, lat, lng, within_m, reach_lng)
}

/// How many of the samples have something in the grid within within_m.
#[no_mangle]
pub unsafe extern "C" fn share_near(grid: *const Grid, lat: *const f64, lng: *const f64, n: i64, within_m: f64, reach_lng: i64) -> i64 {
    let (grid, lat, lng) = (&*grid, Arr::new(lat, n), Arr::new(lng, n));
    let mut count = 0;
    for i in 0..n as usize {
        if any_near(grid, lat.get(i), lng.get(i), within_m, reach_lng) {
            count += 1;
        }
    }
    count
}

/// Set found[item] for everything in the grid within within_m of the stretches of road given (ptr: where each starts).
#[no_mangle]
pub unsafe extern "C" fn mark_near_stretches(
    grid: *const Grid,
    ptr: *const i64,
    ptr_len: i64,
    lat: *const f64,
    lng: *const f64,
    n: i64,
    spacing_m: f64,
    within_m: f64,
    found: *mut u8,
    found_len: i64,
) {
    let grid = &*grid;
    let (ptr, lat, lng, found) = (Arr::new(ptr, ptr_len), Arr::new(lat, n), Arr::new(lng, n), Arr::new(found, found_len));
    for stretch in 0..(ptr_len as usize).saturating_sub(1) {
        let (start, stop) = (ptr.get(stretch) as usize, ptr.get(stretch + 1) as usize);
        if start == stop {
            continue;
        }
        let reach_lng = reach_lng_at(lat.get(start));
        let (dense_lat, dense_lng) = densify(lat.part(start, stop), lng.part(start, stop), spacing_m);
        for s in 0..dense_lat.len() {
            let (point_lat, point_lng) = (dense_lat[s], dense_lng[s]);
            in_cells_around(grid, point_lat, point_lng, 1, reach_lng, |at| {
                let item = grid.items.get(at) as usize;
                if found.get(item) == 0 && haversine(point_lat, point_lng, grid.lat.get(at), grid.lng.get(at)) <= within_m {
                    found.set(item, 1);
                }
                false
            });
        }
    }
}

#[inline(always)]
fn node_reach(lat: f64, distance_m: f64, cell_deg: f64, m_per_deg: f64) -> (i64, i64) {
    let reach_lat = (distance_m / (m_per_deg * cell_deg)).ceil() as i64;
    let lng_cell_m = m_per_deg * cell_deg * (lat * RADIANS).cos().max(0.01);
    (reach_lat, (distance_m / lng_cell_m).ceil() as i64)
}

/// What's in the grid cells within distance_m of a point: a superset of what's within distance_m.
#[no_mangle]
pub unsafe extern "C" fn items_near(grid: *const Grid, lat: f64, lng: f64, distance_m: f64, m_per_deg: f64, out: *mut Out<i32>) {
    let grid = &*grid;
    let (reach_lat, reach_lng) = node_reach(lat, distance_m, grid.cell_deg, m_per_deg);
    let mut found = Vec::new();
    in_cells_around(grid, lat, lng, reach_lat, reach_lng, |at| {
        found.push(grid.items.get(at));
        false
    });
    *out = Out::from_vec(found);
}

/// The closest thing in the grid within max_distance_m of a point, or -1.
#[no_mangle]
pub unsafe extern "C" fn nearest_item(grid: *const Grid, lat: f64, lng: f64, max_distance_m: f64, m_per_deg: f64) -> i32 {
    let grid = &*grid;
    let (reach_lat, reach_lng) = node_reach(lat, max_distance_m, grid.cell_deg, m_per_deg);
    let (mut best, mut best_m) = (-1, max_distance_m);
    in_cells_around(grid, lat, lng, reach_lat, reach_lng, |at| {
        let distance = haversine(lat, lng, grid.lat.get(at), grid.lng.get(at));
        if distance <= best_m {
            best = grid.items.get(at);
            best_m = distance;
        }
        false
    });
    best
}

/// Segments a route's points follow: both of their junctions lie on it. on_route is all false on the way in.
#[no_mangle]
pub unsafe extern "C" fn edges_along(
    grid: *const Grid,
    lat: *const f64,
    lng: *const f64,
    n: i64,
    within_m: f64,
    m_per_deg: f64,
    out_ptr: *const i64,
    out_ptr_len: i64,
    out_edges: *const i32,
    edge_to: *const i32,
    edges: i64,
    on_route: *mut u8,
    out: *mut Out<i32>,
) {
    let grid = &*grid;
    let (lat, lng) = (Arr::new(lat, n), Arr::new(lng, n));
    let (out_ptr, out_edges, edge_to) = (Arr::new(out_ptr, out_ptr_len), Arr::new(out_edges, edges), Arr::new(edge_to, edges));
    let on_route = Arr::new(on_route, out_ptr_len - 1);
    let mut touched: Vec<i32> = Vec::new();
    for s in 0..n as usize {
        let (point_lat, point_lng) = (lat.get(s), lng.get(s));
        let (reach_lat, reach_lng) = node_reach(point_lat, within_m, grid.cell_deg, m_per_deg);
        in_cells_around(grid, point_lat, point_lng, reach_lat, reach_lng, |at| {
            let node = grid.items.get(at);
            if on_route.get(node as usize) == 0 && haversine(point_lat, point_lng, grid.lat.get(at), grid.lng.get(at)) <= within_m {
                on_route.set(node as usize, 1);
                touched.push(node);
            }
            false
        });
    }
    let mut found = Vec::new();
    for &node in touched.iter() {
        for i in out_ptr.get(node as usize) as usize..out_ptr.get(node as usize + 1) as usize {
            let edge = out_edges.get(i);
            if on_route.get(edge_to.get(edge as usize) as usize) != 0 {
                found.push(edge);
            }
        }
    }
    *out = Out::from_vec(found);
}

// ---- A route and the road events on it

/// The direction of travel at each point of a path.
unsafe fn bearings_along(lat: &[f64], lng: &[f64]) -> Vec<f64> {
    let n = lat.len();
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        if i + 1 < n {
            out.push(bearing(lat[i], lng[i], lat[i + 1], lng[i + 1]));
        } else if n >= 2 {
            out.push(bearing(lat[i - 1], lng[i - 1], lat[i], lng[i]));
        } else {
            out.push(bearing(lat[i], lng[i], lat[i], lng[i]));
        }
    }
    out
}

#[no_mangle]
pub unsafe extern "C" fn bearings_of(lat: *const f64, lng: *const f64, n: i64, out: *mut f64) {
    let (lat, lng) = (std::slice::from_raw_parts(lat, n as usize), std::slice::from_raw_parts(lng, n as usize));
    let values = bearings_along(lat, lng);
    std::ptr::copy_nonoverlapping(values.as_ptr(), out, n as usize);
}

/// Whether traffic heading along travel, beside an event's line (bearing line there), is the traffic it affects.
#[inline(always)]
fn carries(travel: f64, line: Option<f64>, direction: Option<f64>, along: bool, rules: &Matching) -> bool {
    if let (true, Some(line)) = (along, line) {
        return angle_between(travel, line) <= rules.match_deg;
    }
    if let Some(line) = line {
        let angle = angle_between(travel, line);
        if angle.min(180.0 - angle) > rules.parallel_deg {
            return false; // crossing the event's road, not driving along it
        }
    }
    match direction {
        None => true,
        Some(direction) => angle_between(travel, direction) <= rules.match_deg,
    }
}

/// Whether the route passes within within_m of a point, going the way an event's traffic goes.
#[inline(always)]
unsafe fn route_carries(route: &RouteLine, lat: f64, lng: f64, within_m: f64, line: Option<f64>, direction: Option<f64>, along: bool, rules: &Matching) -> bool {
    if route.cells.n > 0 {
        let row = ((lat - route.cell_south) / route.cell_lat).floor() as i64;
        let column = ((lng - route.cell_west) / route.cell_lng).floor() as i64;
        if row < 0 || row >= route.cell_rows || column < 0 || column >= route.cell_columns || route.cells.get((row * route.cell_columns + column) as usize) == 0 {
            return false;
        }
    }
    let grid = &route.grid;
    in_cells_around(grid, lat, lng, 1, route.reach_lng, |at| {
        haversine(lat, lng, grid.lat.get(at), grid.lng.get(at)) <= within_m && carries(route.bearings.get(at), line, direction, along, rules)
    })
}

/// A quick test first, as a trip can have a hundred events around it.
#[inline(always)]
unsafe fn overlaps(route: &RouteLine, events: &EventColumns, event: usize, rules: &Matching) -> bool {
    events.south.get(event) <= route.north + rules.margin_lat
        && events.north.get(event) >= route.south - rules.margin_lat
        && events.west.get(event) <= route.east + rules.margin_lng
        && events.east.get(event) >= route.west - rules.margin_lng
}

#[inline(always)]
unsafe fn event_direction(events: &EventColumns, event: usize) -> Option<f64> {
    if events.has_direction.get(event) != 0 {
        Some(events.direction.get(event))
    } else {
        None
    }
}

/// Set out[event] for each wanted event the route drives through, the way its traffic goes.
#[no_mangle]
pub unsafe extern "C" fn runs_along_each(route: *const RouteLine, events: *const EventColumns, wanted: *const u8, rules: *const Matching, out: *mut u8) {
    let (route, events, rules) = (&*route, &*events, &*rules);
    let count = (events.ptr.n - 1).max(0);
    let (wanted, out) = (Arr::new(wanted, count), Arr::new(out, count));
    for event in 0..count as usize {
        if wanted.get(event) == 0 || !overlaps(route, events, event, rules) {
            continue;
        }
        let (start, stop) = (events.ptr.get(event) as usize, events.ptr.get(event + 1) as usize);
        let direction = event_direction(events, event);
        let along = events.along.get(event) != 0;
        if stop - start == 1 {
            // A source's point may sit a little off the road: the looser match.
            out.set(event, route_carries(route, events.lat.get(start), events.lng.get(start), rules.match_m, None, direction, along, rules) as u8);
            continue;
        }
        if events.ramp.get(event) != 0
            && !(route_carries(route, events.lat.get(start), events.lng.get(start), rules.on_route_m, None, None, false, rules)
                && route_carries(route, events.lat.get(stop - 1), events.lng.get(stop - 1), rules.on_route_m, None, None, false, rules))
        {
            continue; // a ramp runs beside its freeway for a while: using it means passing both its ends
        }
        let spacing = rules.match_m / 2.0;
        let (mut longest, mut run, mut misses) = (0i64, 0i64, 0i64);
        for s in events.sample_ptr.get(event) as usize..events.sample_ptr.get(event + 1) as usize {
            if route_carries(route, events.sample_lat.get(s), events.sample_lng.get(s), rules.on_route_m, Some(events.sample_line.get(s)), direction, along, rules) {
                run += 1 + misses; // a single missed sample (a curve drawn differently) doesn't break the run
                misses = 0;
            } else if run != 0 && misses == 0 {
                misses = 1;
            } else {
                run = 0;
                misses = 0;
            }
            longest = longest.max(run);
        }
        out.set(event, (longest as f64 * spacing >= rules.event_on_route_m.min(events.length_m.get(event) / 2.0)) as u8);
    }
}

/// Set out[event] for each wanted event: the share of its stretch the route drives, the way its traffic goes.
#[no_mangle]
pub unsafe extern "C" fn share_along_each(route: *const RouteLine, events: *const EventColumns, wanted: *const u8, rules: *const Matching, out: *mut f64) {
    let (route, events, rules) = (&*route, &*events, &*rules);
    let count = (events.ptr.n - 1).max(0);
    let (wanted, out) = (Arr::new(wanted, count), Arr::new(out, count));
    for event in 0..count as usize {
        if wanted.get(event) == 0 || !overlaps(route, events, event, rules) {
            continue;
        }
        let direction = event_direction(events, event);
        let along = events.along.get(event) != 0;
        let (from, to) = (events.sample_ptr.get(event) as usize, events.sample_ptr.get(event + 1) as usize);
        let mut matched = 0i64;
        for s in from..to {
            if route_carries(route, events.sample_lat.get(s), events.sample_lng.get(s), rules.on_route_m, Some(events.sample_line.get(s)), direction, along, rules) {
                matched += 1;
            }
        }
        out.set(event, matched as f64 / (to - from) as f64);
    }
}

/// Set covered[sample] for each sample of an event's line this other line runs beside, the way its traffic goes.
#[no_mangle]
pub unsafe extern "C" fn mark_carried(
    route: *const RouteLine,
    lat: *const f64,
    lng: *const f64,
    lines: *const f64,
    n: i64,
    has_direction: bool,
    direction: f64,
    along: bool,
    rules: *const Matching,
    covered: *mut u8,
) {
    let (route, rules) = (&*route, &*rules);
    let (lat, lng, lines, covered) = (Arr::new(lat, n), Arr::new(lng, n), Arr::new(lines, n), Arr::new(covered, n));
    let direction = if has_direction { Some(direction) } else { None };
    for s in 0..n as usize {
        if covered.get(s) == 0 && route_carries(route, lat.get(s), lng.get(s), rules.on_route_m, Some(lines.get(s)), direction, along, rules) {
            covered.set(s, 1);
        }
    }
}

/// Set out[line] for each line that comes into a cell next to one that any of the near lines passes through.
#[no_mangle]
pub unsafe extern "C" fn lines_near_lines(
    lines_ptr: *const i64,
    lines_ptr_len: i64,
    lines_lat: *const f64,
    lines_lng: *const f64,
    lines_n: i64,
    near_ptr: *const i64,
    near_ptr_len: i64,
    near_lat: *const f64,
    near_lng: *const f64,
    near_n: i64,
    spacing_m: f64,
    cell_lat: f64,
    cell_lng: f64,
    out: *mut u8,
) {
    let (lines_ptr, lines_lat, lines_lng) = (Arr::new(lines_ptr, lines_ptr_len), Arr::new(lines_lat, lines_n), Arr::new(lines_lng, lines_n));
    let (near_ptr, near_lat, near_lng) = (Arr::new(near_ptr, near_ptr_len), Arr::new(near_lat, near_n), Arr::new(near_lng, near_n));
    let out = Arr::new(out, (lines_ptr_len - 1).max(0));
    let mut keys: Vec<i64> = Vec::new();
    for line in 0..(near_ptr_len as usize).saturating_sub(1) {
        let (start, stop) = (near_ptr.get(line) as usize, near_ptr.get(line + 1) as usize);
        if start == stop {
            continue;
        }
        let (dense_lat, dense_lng) = densify(near_lat.part(start, stop), near_lng.part(start, stop), spacing_m);
        for s in 0..dense_lat.len() {
            let (row, column) = (cell_of(dense_lat[s], cell_lat), cell_of(dense_lng[s], cell_lng));
            for i in row - 1..=row + 1 {
                for j in column - 1..=column + 1 {
                    keys.push(key_of(i, j));
                }
            }
        }
    }
    keys.sort_unstable();
    keys.dedup();
    for line in 0..(lines_ptr_len as usize).saturating_sub(1) {
        let (start, stop) = (lines_ptr.get(line) as usize, lines_ptr.get(line + 1) as usize);
        if start == stop {
            continue;
        }
        let (dense_lat, dense_lng) = densify(lines_lat.part(start, stop), lines_lng.part(start, stop), spacing_m);
        for s in 0..dense_lat.len() {
            let key = key_of(cell_of(dense_lat[s], cell_lat), cell_of(dense_lng[s], cell_lng));
            if keys.binary_search(&key).is_ok() {
                out.set(line, 1);
                break;
            }
        }
    }
}

// ---- Segments near a point

/// Distance from a point to the nearest spot on a path, and the path's bearing there: see app.geo.
#[inline(always)]
fn distance_to_path(lat: f64, lng: f64, path_lat: &[f64], path_lng: &[f64], m_per_deg: f64) -> (f64, f64) {
    let m_per_deg_lng = m_per_deg * (lat * RADIANS).cos();
    let (mut best_m, mut best_bearing) = (f64::INFINITY, 0.0);
    for i in 0..path_lat.len().saturating_sub(1) {
        let (ax, ay) = ((path_lng[i] - lng) * m_per_deg_lng, (path_lat[i] - lat) * m_per_deg);
        let (bx, by) = ((path_lng[i + 1] - lng) * m_per_deg_lng, (path_lat[i + 1] - lat) * m_per_deg);
        let (dx, dy) = (bx - ax, by - ay);
        let length_sq = dx * dx + dy * dy;
        let t = if length_sq == 0.0 { 0.0 } else { (-(ax * dx + ay * dy) / length_sq).min(1.0).max(0.0) };
        let distance = (ax + t * dx).hypot(ay + t * dy);
        if distance < best_m && length_sq > 0.0 {
            best_m = distance;
            best_bearing = bearing(path_lat[i], path_lng[i], path_lat[i + 1], path_lng[i + 1]);
        }
    }
    (best_m, best_bearing)
}

#[repr(C)]
pub struct SegmentCells {
    pub keys: Out<i64>,
    pub edges: Out<i32>,
}

#[no_mangle]
pub unsafe extern "C" fn free_segment_cells(cells: *mut SegmentCells) {
    (*cells).keys.free();
    (*cells).edges.free();
}

/// Every (cell, segment) a segment's straight line falls in, sampled about every sample_m: each pair once.
#[no_mangle]
pub unsafe extern "C" fn segment_cells(
    edge_from: *const i32,
    edge_to: *const i32,
    length_m: *const f64,
    edges: i64,
    lat: *const f64,
    lng: *const f64,
    nodes: i64,
    sample_m: f64,
    cell_deg: f64,
    out: *mut SegmentCells,
) {
    let (edge_from, edge_to, length_m) = (Arr::new(edge_from, edges), Arr::new(edge_to, edges), Arr::new(length_m, edges));
    let (lat, lng) = (Arr::new(lat, nodes), Arr::new(lng, nodes));
    let mut keys: Vec<i64> = Vec::with_capacity(edges as usize * 2);
    let mut owners: Vec<i32> = Vec::with_capacity(edges as usize * 2);
    for edge in 0..edges as usize {
        let (from, to) = (edge_from.get(edge) as usize, edge_to.get(edge) as usize);
        let (lat1, lng1, lat2, lng2) = (lat.get(from), lng.get(from), lat.get(to), lng.get(to));
        let steps = (length_m.get(edge) / sample_m).ceil().max(1.0) as i64;
        let start = keys.len();
        for k in 0..=steps {
            let key = key_of(
                cell_of(lat1 + (lat2 - lat1) * k as f64 / steps as f64, cell_deg),
                cell_of(lng1 + (lng2 - lng1) * k as f64 / steps as f64, cell_deg),
            );
            if !keys[start..].contains(&key) {
                keys.push(key);
                owners.push(edge as i32);
            }
        }
    }
    (*out).keys = Out::from_vec(keys);
    (*out).edges = Out::from_vec(owners);
}

#[repr(C)]
pub struct MapColumns {
    pub edge_from: Arr<i32>,
    pub edge_to: Arr<i32>,
    pub node_lat: Arr<f64>,
    pub node_lng: Arr<f64>,
    pub length_m: Arr<f64>,
    pub is_link: Arr<u8>,
    pub in_ptr: Arr<i64>,
    pub in_edges: Arr<i32>,
    pub geometry_ptr: Arr<i64>,
    pub geometry: Arr<u8>,
    pub factor: f64,
    pub m_per_deg_lookup: f64,
    pub m_per_deg_geo: f64,
}

#[inline(always)]
unsafe fn decode_segment(map: &MapColumns, edge: usize, lat: &mut Vec<f64>, lng: &mut Vec<f64>) {
    lat.clear();
    lng.clear();
    decode_into(map.geometry, map.geometry_ptr.get(edge) as usize, map.geometry_ptr.get(edge + 1) as usize, map.factor, lat, lng, false);
}

/// Segments of the road nearest a point (both ways of it unless direction says), within match_m: see RoadGraph.
/// Those running most nearly along direction come first, then the nearest: at a junction, roads meet at one point.
#[no_mangle]
pub unsafe extern "C" fn roads_at(
    lat: f64,
    lng: f64,
    direction: f64,
    has_direction: bool,
    want_ramp: bool,
    any_road: bool,
    keys: *const i64,
    items: *const i32,
    cells: i64,
    cell_deg: f64,
    map: *const MapColumns,
    rules: *const Matching,
    edge_sample_m: f64,
    same_road_m: f64,
    out: *mut Out<i32>,
) {
    let (map, rules) = (&*map, &*rules);
    let (keys, items) = (Arr::new(keys, cells), Arr::new(items, cells));
    let (row, column) = (cell_of(lat, cell_deg), cell_of(lng, cell_deg));
    let mut candidates: Vec<i32> = Vec::new();
    for i in row - 1..=row + 1 {
        for j in column - 1..=column + 1 {
            let key = key_of(i, j);
            let mut at = first_at(keys, key);
            while at < keys.len() && keys.get(at) == key {
                candidates.push(items.get(at));
                at += 1;
            }
        }
    }
    candidates.sort_unstable();
    candidates.dedup();
    let direction = if has_direction { Some(direction) } else { None };
    let m_per_deg_lng = map.m_per_deg_lookup * (lat * RADIANS).cos();
    let mut near: Vec<(i32, f64, f64)> = Vec::new();
    let mut nearest = f64::INFINITY;
    let (mut path_lat, mut path_lng) = (Vec::new(), Vec::new());
    for &edge in candidates.iter() {
        let edge_at = edge as usize;
        if !any_road && (map.is_link.get(edge_at) != 0) != want_ramp {
            continue;
        }
        let (a, b) = (map.edge_from.get(edge_at) as usize, map.edge_to.get(edge_at) as usize);
        // Far even allowing for a curve: skip decoding its shape.
        let (ax, ay) = ((map.node_lng.get(a) - lng) * m_per_deg_lng, (map.node_lat.get(a) - lat) * map.m_per_deg_lookup);
        let (dx, dy) = ((map.node_lng.get(b) - lng) * m_per_deg_lng - ax, (map.node_lat.get(b) - lat) * map.m_per_deg_lookup - ay);
        let length_sq = dx * dx + dy * dy;
        let t = if length_sq == 0.0 { 0.0 } else { (-(ax * dx + ay * dy) / length_sq).min(1.0).max(0.0) };
        if (ax + t * dx).hypot(ay + t * dy) > rules.match_m + (map.length_m.get(edge_at) / 2.0).min(edge_sample_m) {
            continue;
        }
        decode_segment(map, edge_at, &mut path_lat, &mut path_lng);
        let (offset, heading) = distance_to_path(lat, lng, &path_lat, &path_lng, map.m_per_deg_geo);
        if offset <= rules.match_m && carries(heading, None, direction, false, rules) {
            let off_course = direction.map_or(0.0, |wanted| (angle_between(heading, wanted) * 1000.0).round());
            near.push((edge, offset, off_course));
            nearest = nearest.min(offset);
        }
    }
    near.retain(|(_, offset, _)| *offset <= nearest + same_road_m);
    near.sort_by(|a, b| a.2.total_cmp(&b.2).then(a.1.total_cmp(&b.1)).then(a.0.cmp(&b.0)));
    *out = Out::from_vec(near.into_iter().map(|(edge, _, _)| edge).collect());
}

/// Segments within match_m of a point running along heading, nearest first: those about as near as the nearest.
#[no_mangle]
pub unsafe extern "C" fn segments_under(
    lat: f64,
    lng: f64,
    heading: f64,
    any_road: bool,
    want_ramp: bool,
    radii: *const f64,
    radii_len: i64,
    match_m: f64,
    match_deg: f64,
    sure_m: f64,
    same_road_m: f64,
    grid_fine: *const Grid,
    grid_coarse: *const Grid,
    fine_limit_m: f64,
    map: *const MapColumns,
    out: *mut Out<i32>,
) {
    let map = &*map;
    let radii = Arr::new(radii, radii_len);
    let mut checked = std::collections::HashSet::new();
    let mut offsets: Vec<(i32, f64)> = Vec::new();
    let mut nearest = f64::INFINITY;
    let (mut path_lat, mut path_lng) = (Vec::new(), Vec::new());
    for r in 0..radii.len() {
        let radius = radii.get(r);
        let grid = if radius <= fine_limit_m { &*grid_fine } else { &*grid_coarse };
        let (reach_lat, reach_lng) = node_reach(lat, radius, grid.cell_deg, map.m_per_deg_lookup);
        in_cells_around(grid, lat, lng, reach_lat, reach_lng, |at| {
            let node = grid.items.get(at);
            if checked.contains(&node) {
                return false;
            }
            // The car drives toward the end of its segment, so skip junctions behind it.
            let (node_lat, node_lng) = (grid.lat.get(at), grid.lng.get(at));
            if haversine(lat, lng, node_lat, node_lng) > match_m && angle_between(bearing(lat, lng, node_lat, node_lng), heading) > 90.0 {
                return false;
            }
            checked.insert(node);
            for e in map.in_ptr.get(node as usize) as usize..map.in_ptr.get(node as usize + 1) as usize {
                let edge = map.in_edges.get(e);
                if !any_road && (map.is_link.get(edge as usize) != 0) != want_ramp {
                    continue;
                }
                decode_segment(map, edge as usize, &mut path_lat, &mut path_lng);
                let (offset, along) = distance_to_path(lat, lng, &path_lat, &path_lng, map.m_per_deg_geo);
                if offset <= match_m && angle_between(along, heading) <= match_deg {
                    offsets.push((edge, offset));
                    nearest = nearest.min(offset);
                }
            }
            false
        });
        if !offsets.is_empty() && nearest <= sure_m {
            break; // a wider search could only find roads farther from the car
        }
    }
    let mut kept: Vec<(i32, f64)> = offsets.into_iter().filter(|(_, offset)| *offset <= nearest + same_road_m).collect();
    kept.sort_by(|a, b| a.1.total_cmp(&b.1)); // stable: those as near keep their order
    *out = Out::from_vec(kept.into_iter().map(|(edge, _)| edge).collect());
}
