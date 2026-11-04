//! Finding the cheapest way through a trip's costs. Nothing here knows about profiles or road events: it sees numbers.
//!
//! * Every way on from a segment is worked out once, into arrays (build_moves, build_steps), instead of being judged
//!   again as the search reaches it. Millions of steps come in a few thousand kinds (the same turn, the same
//!   crossings), so a step keeps only its kind, and a traveler's costs are one short table.
//! * The search knows how far it still has to go. Before searching forward, reverse_bound searches back from the
//!   destination along the roads, ignoring turns. Turns and zones only add to a route, and an alternative's penalties
//!   only raise a road's cost, so what it finds never overstates what's left. That's what A* needs to still find the
//!   cheapest route, and it's far closer to the truth than a straight line at freeway speed.

use crate::geometry::haversine;
use crate::{Arr, Out};

const SEEN: u32 = 1;
const SETTLED: u32 = 2;
const TARGET: u32 = 4;
const MARK_BITS: u32 = 3;
const ARRIVAL: i32 = -2;
const NO_TURN: u8 = 30;

const STRAIGHT: u8 = 0;
const DEPART: u8 = 1;
const SLIGHT_LEFT: u8 = 2;
const LEFT: u8 = 3;
const SHARP_LEFT: u8 = 4;
const SLIGHT_RIGHT: u8 = 6;
const RIGHT: u8 = 7;
const SHARP_RIGHT: u8 = 8;
const RAMP_LEFT: u8 = 10;
const RAMP_RIGHT: u8 = 11;
const MERGE: u8 = 12;
const FORK_LEFT: u8 = 13;
const FORK_RIGHT: u8 = 14;

const KIND_BUSY: u8 = 1;
const KIND_INTERCHANGE: u8 = 2;
const KIND_ONE_WAY: u8 = 3;

const CROSSES: u8 = 1;
const BUSY: u8 = 2;
const FIRST_CROSSES: u8 = 1;
const FIRST_BUSY: u8 = 2;
const SECOND_CROSSES: u8 = 4;
const SECOND_BUSY: u8 = 8;

#[repr(C)]
pub struct Zones {
    pub index: Arr<i32>,
    pub ptr: Arr<i64>,
    pub ids: Arr<i32>,
    pub cost: Arr<f64>,
    pub school: Arr<i8>,
    pub school_cost: f64,
}

#[repr(C)]
pub struct StepTable {
    pub edge_to: Arr<i32>,
    pub ptr: Arr<i64>,
    pub first: Arr<i32>,
    pub second: Arr<i32>,
    pub kind: Arr<u16>,
    pub maneuver_first: Arr<u8>,
    pub maneuver_second: Arr<u8>,
}

#[repr(C)]
pub struct Roads {
    pub edge_from: Arr<i32>,
    pub edge_to: Arr<i32>,
    pub out_ptr: Arr<i64>,
    pub out_edges: Arr<i32>,
    pub banned_ptr: Arr<i64>,
    pub banned: Arr<i32>,
    pub bearing_start: Arr<f64>,
    pub bearing_end: Arr<f64>,
    pub link: Arr<u8>,
    pub freeway: Arr<u8>,
    pub way_id: Arr<i64>,
    pub name: Arr<i32>,
    pub speed_kmh: Arr<f64>,
    pub kinds: Arr<u8>,
}

#[repr(C)]
pub struct TurnRules {
    pub crossing: Arr<u8>,
    pub crossing_ramp: Arr<u8>,
    pub freeway_speed_kmh: f64,
    pub uturn: i64,
    pub uturn_min_deg: f64,
}

#[repr(C)]
pub struct Crossings {
    pub edge_to: Arr<i32>,
    pub out_ptr: Arr<i64>,
    pub out_edges: Arr<i32>,
    pub in_ptr: Arr<i64>,
    pub in_edges: Arr<i32>,
    pub way_id: Arr<i64>,
    pub name: Arr<i32>,
    pub cross: Arr<i8>,
    pub signal_node: Arr<u8>,
    pub left: Arr<u8>,
    pub right: Arr<u8>,
}

/// What's filled in about each step, and each kind of step.
#[repr(C)]
pub struct StepColumns {
    pub ptr: Arr<i64>,
    pub first: Arr<i32>,
    pub second: Arr<i32>,
    pub kind: Arr<u16>,
    pub maneuver_first: Arr<u8>,
    pub maneuver_second: Arr<u8>,
    pub turn_first: Arr<u8>,
    pub turn_second: Arr<u8>,
    pub flags: Arr<u8>,
    pub busy_plain: Arr<u8>,
    pub busy_signal: Arr<u8>,
    pub hotspots: Arr<u8>,
    pub cut_through: Arr<u8>,
    pub cross_3: Arr<u8>,
    pub cross_4: Arr<u8>,
    pub crash_spots: Arr<u8>,
}

/// A route: for Python to copy and free.
#[repr(C)]
pub struct RouteOut {
    pub path: Out<i32>,
    pub maneuvers: Out<u8>,
    pub steps: Out<i32>,
    pub opened: i64,
}

// ---- Scratch space kept between searches: a generation number says what's current, so nothing has to be cleared.

#[derive(Copy, Clone)]
#[repr(C)]
struct NodeRecord {
    distance: f64,
    mark: u32, // generation << MARK_BITS, and the SEEN, SETTLED and TARGET bits
    _pad: u32,
}

#[derive(Copy, Clone)]
#[repr(C)]
struct StateRecord {
    best: f64,
    reached_by: i32, // the step reaching it plus 2, or 1 for a departure
    came_from: i32,
    stamp: u32,
    _pad: u32,
}

#[derive(Copy, Clone)]
struct Entry {
    priority: f64,
    cost: f64,
    item: i32,
}

/// One trip's scratch. Records are zeroed by the system and untouched until used, so a short trip costs little memory.
pub struct Workspace {
    node_generation: u32,
    state_generation: u32,
    nodes: Vec<u64>,
    states: Vec<u64>,
    #[cfg_attr(not(feature = "checked"), allow(dead_code))]
    node_count: usize,
    #[cfg_attr(not(feature = "checked"), allow(dead_code))]
    state_count: usize,
    heap: Vec<Entry>,
    buckets: Vec<Vec<(u64, i32)>>,
    settled: i64,
}

const NODE_WORDS: usize = std::mem::size_of::<NodeRecord>() / 8;
const STATE_WORDS: usize = std::mem::size_of::<StateRecord>() / 8;

impl Workspace {
    #[inline(always)]
    unsafe fn node(&mut self, i: usize) -> &mut NodeRecord {
        #[cfg(feature = "checked")]
        assert!(i < self.node_count, "junction {} of {}", i, self.node_count);
        &mut *(self.nodes.as_mut_ptr() as *mut NodeRecord).add(i)
    }

    #[inline(always)]
    unsafe fn state(&mut self, i: usize) -> &mut StateRecord {
        #[cfg(feature = "checked")]
        assert!(i < self.state_count, "segment {} of {}", i, self.state_count);
        &mut *(self.states.as_mut_ptr() as *mut StateRecord).add(i)
    }

    /// What's left from a junction, as the last search back found it.
    #[inline(always)]
    unsafe fn left(&self, node: usize, radius: f64) -> f64 {
        #[cfg(feature = "checked")]
        assert!(node < self.node_count, "junction {} of {}", node, self.node_count);
        let record = &*(self.nodes.as_ptr() as *const NodeRecord).add(node);
        if record.mark >> MARK_BITS == self.node_generation && record.mark & SETTLED != 0 {
            record.distance
        } else {
            radius
        }
    }
}

#[no_mangle]
pub extern "C" fn workspace_new(nodes: i64, edges: i64) -> *mut Workspace {
    Box::into_raw(Box::new(Workspace {
        node_generation: 0,
        state_generation: 0,
        nodes: vec![0u64; nodes as usize * NODE_WORDS],
        states: vec![0u64; edges as usize * STATE_WORDS],
        node_count: nodes as usize,
        state_count: edges as usize,
        heap: Vec::with_capacity(4096),
        buckets: (0..65).map(|_| Vec::new()).collect(),
        settled: 0,
    }))
}

#[no_mangle]
pub unsafe extern "C" fn workspace_free(workspace: *mut Workspace) {
    drop(Box::from_raw(workspace));
}

/// What's left from a junction: the distance if the search back settled it, else negative.
#[no_mangle]
pub unsafe extern "C" fn workspace_left(workspace: *const Workspace, node: i64) -> f64 {
    (*workspace).left(node as usize, -1.0)
}

#[no_mangle]
pub unsafe extern "C" fn workspace_settled(workspace: *const Workspace) -> i64 {
    (*workspace).settled
}

// ---- The heap: 4-ary, of (priority, cost, item), compared in that order.

#[inline(always)]
fn before(a: &Entry, b: &Entry) -> bool {
    if a.priority != b.priority {
        return a.priority < b.priority;
    }
    if a.cost != b.cost {
        return a.cost < b.cost;
    }
    a.item < b.item
}

#[inline(always)]
unsafe fn heap_push(heap: &mut Vec<Entry>, entry: Entry) {
    heap.push(entry);
    let items = heap.as_mut_ptr();
    let mut i = heap.len() - 1;
    while i > 0 {
        let parent = (i - 1) >> 2;
        if !before(&entry, &*items.add(parent)) {
            break;
        }
        *items.add(i) = *items.add(parent);
        i = parent;
    }
    *items.add(i) = entry;
}

#[inline(always)]
unsafe fn heap_pop(heap: &mut Vec<Entry>) -> Entry {
    let top = *heap.as_ptr();
    let last = heap.pop().unwrap_unchecked();
    let size = heap.len();
    if size == 0 {
        return top;
    }
    let items = heap.as_mut_ptr();
    let mut i = 0usize;
    loop {
        let child = 4 * i + 1;
        if child >= size {
            break;
        }
        let mut best = child;
        let stop = (child + 4).min(size);
        let mut other = child + 1;
        while other < stop {
            if before(&*items.add(other), &*items.add(best)) {
                best = other;
            }
            other += 1;
        }
        if !before(&*items.add(best), &last) {
            break;
        }
        *items.add(i) = *items.add(best);
        i = best;
    }
    *items.add(i) = last;
    top
}

/// With the sun low, segments heading toward it cost per_s more, for each second they take.
#[no_mangle]
pub unsafe extern "C" fn add_glare(edge_costs: *mut f64, bearings: *const f64, time_s: *const f64, edges: i64, azimuth: f64, per_s: f64, glare_deg: f64) {
    let (edge_costs, bearings, time_s) = (Arr::new(edge_costs, edges), Arr::new(bearings, edges), Arr::new(time_s, edges));
    for edge in 0..edges as usize {
        if ((bearings.get(edge) - azimuth + 180.0).rem_euclid(360.0) - 180.0).abs() <= glare_deg {
            edge_costs.set(edge, edge_costs.get(edge) + per_s * time_s.get(edge));
        }
    }
}

// ---- The search back

/// What's left to the destination from each junction, along the roads alone, turns ignored. Returns the distance
/// searched to: every junction not settled is at least that far from the destination. It goes on until every
/// junction in targets is settled and reach times the farthest of them is covered. If the roads run out first the
/// answer is infinity: what wasn't reached can't reach the destination at all.
///
/// Distances only grow, so a radix heap on their bits does (a float's bits sort as it does).
#[no_mangle]
pub unsafe extern "C" fn reverse_bound(
    workspace: *mut Workspace,
    end: i32,
    targets: *const i32,
    targets_len: i64,
    reach: f64,
    edge_costs: *const f64,
    edge_costs_len: i64,
    in_ptr: *const i64,
    in_ptr_len: i64,
    in_edges: *const i32,
    in_edges_len: i64,
    edge_from: *const i32,
    edge_from_len: i64,
) -> f64 {
    let ws = &mut *workspace;
    let targets = Arr::new(targets, targets_len);
    let edge_costs = Arr::new(edge_costs, edge_costs_len);
    let in_ptr = Arr::new(in_ptr, in_ptr_len);
    let in_edges = Arr::new(in_edges, in_edges_len);
    let edge_from = Arr::new(edge_from, edge_from_len);

    ws.node_generation += 1;
    let generation = ws.node_generation;
    let fresh = generation << MARK_BITS;
    let mut buckets = std::mem::take(&mut ws.buckets);
    for bucket in buckets.iter_mut() {
        bucket.clear();
    }
    let mut settled = 0i64;
    let mut last = 0u64;
    let mut count = 1usize;
    buckets[0].push((0, end));
    ws.node(end as usize).mark = fresh | SEEN;
    let mut waiting = 0i64;
    for i in 0..targets.len() {
        let node = ws.node(targets.get(i) as usize);
        let current = if node.mark >> MARK_BITS == generation { node.mark } else { fresh };
        if current & TARGET == 0 {
            node.mark = current | TARGET;
            waiting += 1;
        }
    }
    let mut limit = f64::INFINITY;
    let mut answer = f64::INFINITY;
    while count > 0 {
        if buckets[0].is_empty() {
            let mut b = 1;
            while buckets[b].is_empty() {
                b += 1;
            }
            let mut least = u64::MAX;
            for &(key, _) in buckets[b].iter() {
                least = least.min(key);
            }
            last = least;
            let moved = std::mem::take(&mut buckets[b]);
            for &(key, item) in moved.iter() {
                let index = (64 - (key ^ last).leading_zeros()) as usize;
                buckets[index].push((key, item));
            }
            let mut emptied = moved;
            emptied.clear();
            buckets[b] = emptied;
        }
        let (key, item) = buckets[0].pop().unwrap_unchecked();
        count -= 1;
        let done = f64::from_bits(key);
        let node_at = item as usize;
        let node = ws.node(node_at);
        if node.mark & SETTLED != 0 {
            continue;
        }
        if done >= limit {
            answer = done;
            break;
        }
        let was_target = node.mark & TARGET != 0;
        node.mark |= SETTLED;
        node.distance = done;
        settled += 1;
        if was_target {
            waiting -= 1;
            if waiting == 0 {
                limit = done * reach;
            }
        }
        for i in in_ptr.get(node_at) as usize..in_ptr.get(node_at + 1) as usize {
            let edge = in_edges.get(i) as usize;
            let cost = edge_costs.get(edge);
            if !(cost < f64::INFINITY) {
                continue; // closed
            }
            let start = edge_from.get(edge);
            let other = ws.node(start as usize);
            let known = other.mark >> MARK_BITS == generation;
            if known && other.mark & SETTLED != 0 {
                continue;
            }
            let new = done + cost;
            if !known || other.mark & SEEN == 0 || new < other.distance {
                other.mark = (if known { other.mark } else { fresh }) | SEEN;
                other.distance = new;
                let bits = new.to_bits();
                let index = (64 - (bits ^ last).leading_zeros()) as usize;
                buckets[index].push((bits, start));
                count += 1;
            }
        }
    }
    ws.buckets = buckets;
    ws.settled = settled;
    answer
}

// ---- The search forward

/// What driving into a segment's zones costs: once per zone, not again from a segment in the same one.
#[inline(always)]
unsafe fn zones_entered(previous: usize, entered: usize, zones: &Zones) -> f64 {
    let mut cost = 0.0;
    if zones.school_cost > 0.0 && zones.school.get(entered) != 0 && zones.school.get(previous) == 0 {
        cost += zones.school_cost;
    }
    if zones.ptr.n > 1 {
        let row = zones.index.get(entered) as usize;
        if row != 0 {
            let row_before = zones.index.get(previous) as usize;
            for i in zones.ptr.get(row - 1) as usize..zones.ptr.get(row) as usize {
                let zone = zones.ids.get(i);
                let mut inside = false;
                if row_before != 0 {
                    for j in zones.ptr.get(row_before - 1) as usize..zones.ptr.get(row_before) as usize {
                        inside = inside || zones.ids.get(j) == zone;
                    }
                }
                if !inside {
                    cost += zones.cost.get(zone as usize);
                }
            }
        }
    }
    cost
}

#[inline(always)]
unsafe fn forward<F: Fn(&Workspace, usize) -> f64>(
    ws: &mut Workspace,
    departures: Arr<i32>,
    end: i32,
    edge_costs: Arr<f64>,
    step_costs: Arr<f64>,
    table: &StepTable,
    zones: &Zones,
    left_from: F,
    out: *mut RouteOut,
) {
    ws.state_generation += 1;
    let stamp = ws.state_generation;
    let mut heap = std::mem::take(&mut ws.heap);
    heap.clear();
    for i in 0..departures.len() {
        let edge = departures.get(i);
        let edge_at = edge as usize;
        let cost = edge_costs.get(edge_at);
        let left = left_from(ws, table.edge_to.get(edge_at) as usize);
        if left == f64::INFINITY || cost.is_nan() {
            continue;
        }
        let state = ws.state(edge_at);
        state.stamp = stamp;
        state.best = cost;
        state.reached_by = 1;
        heap_push(&mut heap, Entry { priority: cost + left, cost, item: edge });
    }

    let mut opened = 0i64;
    let mut final_edge = -1i32;
    while !heap.is_empty() {
        let top = heap_pop(&mut heap);
        let (cost, edge) = (top.cost, top.item);
        let edge_at = edge as usize;
        if cost > ws.state(edge_at).best {
            continue; // a stale entry
        }
        opened += 1;
        if table.edge_to.get(edge_at) == end {
            final_edge = edge;
            break;
        }
        for s in table.ptr.get(edge_at) as usize..table.ptr.get(edge_at + 1) as usize {
            let first = table.first.get(s);
            let second = table.second.get(s);
            let first_at = first as usize;
            if second == ARRIVAL && table.edge_to.get(first_at) != end {
                continue;
            }
            let mut new = cost + step_costs.get(table.kind.get(s) as usize) + edge_costs.get(first_at) + zones_entered(edge_at, first_at, zones);
            let mut last = first;
            if second >= 0 {
                new += edge_costs.get(second as usize) + zones_entered(first_at, second as usize, zones);
                last = second;
            }
            let last_at = last as usize;
            if !(new < f64::INFINITY) {
                continue;
            }
            let known = ws.state(last_at);
            if known.stamp == stamp && new >= known.best {
                continue;
            }
            let left = left_from(ws, table.edge_to.get(last_at) as usize);
            if left == f64::INFINITY {
                continue;
            }
            let state = ws.state(last_at);
            state.stamp = stamp;
            state.best = new;
            state.reached_by = s as i32 + 2;
            state.came_from = edge;
            heap_push(&mut heap, Entry { priority: new + left, cost: new, item: last });
        }
    }
    ws.heap = heap;
    finish_route(ws, final_edge, table, opened, out);
}

/// Back along the steps. Each enters one or two of the route's segments.
unsafe fn finish_route(ws: &mut Workspace, final_edge: i32, table: &StepTable, opened: i64, out: *mut RouteOut) {
    (*out).opened = opened;
    if final_edge < 0 {
        (*out).path = Out::from_vec(Vec::new());
        (*out).maneuvers = Out::from_vec(Vec::new());
        (*out).steps = Out::from_vec(Vec::new());
        return;
    }
    let (mut taken, mut entered) = (0usize, 1usize);
    let mut edge = final_edge as usize;
    loop {
        let state = *ws.state(edge);
        if state.reached_by == 1 {
            break;
        }
        entered += if table.second.get((state.reached_by - 2) as usize) >= 0 { 2 } else { 1 };
        taken += 1;
        edge = state.came_from as usize;
    }
    let mut path = vec![0i32; entered];
    let mut maneuvers = vec![0u8; entered];
    let mut steps = vec![0i32; taken];
    let mut edge = final_edge as usize;
    let mut position = entered;
    for t in (0..taken).rev() {
        let state = *ws.state(edge);
        let s = (state.reached_by - 2) as usize;
        let kind = table.kind.get(s) as usize;
        steps[t] = s as i32;
        if table.second.get(s) >= 0 {
            position -= 1;
            path[position] = table.second.get(s);
            maneuvers[position] = table.maneuver_second.get(kind);
        }
        position -= 1;
        path[position] = table.first.get(s);
        maneuvers[position] = table.maneuver_first.get(kind);
        edge = state.came_from as usize;
    }
    path[0] = edge as i32;
    maneuvers[0] = DEPART;
    (*out).path = Out::from_vec(path);
    (*out).maneuvers = Out::from_vec(maneuvers);
    (*out).steps = Out::from_vec(steps);
}

/// A* over segments: a state is "arrived at this junction along this segment", which is what makes turn costs and
/// turn restrictions possible. It finds the cheapest route.
///
/// With straight_per_km above 0, what's left is instead that much per km in a straight line to the goal. Set above a
/// true lower bound, it heads for the goal more directly than the cheapest route does: the way to find direct routes.
#[no_mangle]
pub unsafe extern "C" fn search(
    workspace: *mut Workspace,
    departures: *const i32,
    departures_len: i64,
    end: i32,
    edge_costs: *const f64,
    edge_costs_len: i64,
    step_costs: *const f64,
    step_costs_len: i64,
    table: *const StepTable,
    zones: *const Zones,
    radius: f64,
    straight_per_km: f64,
    node_lat: *const f64,
    node_lng: *const f64,
    node_count: i64,
    out: *mut RouteOut,
) {
    let ws = &mut *workspace;
    let departures = Arr::new(departures, departures_len);
    let edge_costs = Arr::new(edge_costs, edge_costs_len);
    let step_costs = Arr::new(step_costs, step_costs_len);
    let (table, zones) = (&*table, &*zones);
    if straight_per_km > 0.0 {
        let (lat, lng) = (Arr::new(node_lat, node_count), Arr::new(node_lng, node_count));
        let (goal_lat, goal_lng) = (lat.get(end as usize), lng.get(end as usize));
        forward(ws, departures, end, edge_costs, step_costs, table, zones, |ws, node| {
            if ws.left(node, radius) == f64::INFINITY {
                return f64::INFINITY;
            }
            straight_per_km * haversine(lat.get(node), lng.get(node), goal_lat, goal_lng) / 1000.0
        }, out);
    } else {
        forward(ws, departures, end, edge_costs, step_costs, table, zones, |ws, node| ws.left(node, radius), out);
    }
}

#[no_mangle]
pub unsafe extern "C" fn free_route(out: *mut RouteOut) {
    (*out).path.free();
    (*out).maneuvers.free();
    (*out).steps.free();
}

/// What a route costs, by the steps it took: without the penalties it may have been searched with.
#[no_mangle]
pub unsafe extern "C" fn path_cost(
    departure: i32,
    steps: *const i32,
    steps_len: i64,
    edge_costs: *const f64,
    edge_costs_len: i64,
    step_costs: *const f64,
    step_costs_len: i64,
    first: *const i32,
    second: *const i32,
    kind: *const u16,
    step_count: i64,
    zones: *const Zones,
) -> f64 {
    let zones = &*zones;
    let steps = Arr::new(steps, steps_len);
    let edge_costs = Arr::new(edge_costs, edge_costs_len);
    let step_costs = Arr::new(step_costs, step_costs_len);
    let (first, second, kind) = (Arr::new(first, step_count), Arr::new(second, step_count), Arr::new(kind, step_count));
    let mut cost = edge_costs.get(departure as usize);
    let mut previous = departure as usize;
    for i in 0..steps.len() {
        let s = steps.get(i) as usize;
        let entered = first.get(s) as usize;
        let after = second.get(s);
        cost += step_costs.get(kind.get(s) as usize) + edge_costs.get(entered) + zones_entered(previous, entered, zones);
        previous = entered;
        if after >= 0 {
            cost += edge_costs.get(after as usize) + zones_entered(entered, after as usize, zones);
            previous = after as usize;
        }
    }
    cost
}

// ---- The map's turns, worked out once

#[inline(always)]
fn angle_between(a: f64, b: f64) -> f64 {
    let diff = (a - b).abs() % 360.0;
    diff.min(360.0 - diff)
}

/// Classify the move from edge onto nxt, using the Routes API's maneuver names.
unsafe fn maneuver(edge: usize, nxt: usize, roads: &Roads, uturn: u8) -> u8 {
    if roads.edge_to.get(nxt) == roads.edge_from.get(edge) {
        return uturn;
    }
    let angle = (roads.bearing_start.get(nxt) - roads.bearing_end.get(edge) + 540.0).rem_euclid(360.0) - 180.0; // positive turns right
    let right = angle > 0.0;
    let turn = angle.abs();
    let (link_e, link_n) = (roads.link.get(edge) != 0, roads.link.get(nxt) != 0);
    let (free_e, free_n) = (roads.freeway.get(edge) != 0, roads.freeway.get(nxt) != 0);

    if link_n && !link_e {
        if turn > 10.0 {
            return if right { RAMP_RIGHT } else { RAMP_LEFT };
        }
        return STRAIGHT;
    }
    if link_e && free_n {
        return MERGE;
    }
    if (link_e || free_e) && (link_n || free_n) {
        if turn > 10.0 {
            return if right { FORK_RIGHT } else { FORK_LEFT };
        }
        return STRAIGHT;
    }
    let same_road = roads.way_id.get(edge) == roads.way_id.get(nxt) || (roads.name.get(edge) != 0 && roads.name.get(edge) == roads.name.get(nxt));
    if turn <= 30.0 || (same_road && turn < 60.0) {
        return STRAIGHT;
    }
    if turn >= 165.0 {
        return uturn;
    }
    if turn < 60.0 {
        return if right { SLIGHT_RIGHT } else { SLIGHT_LEFT };
    }
    if turn > 120.0 {
        return if right { SHARP_RIGHT } else { SHARP_LEFT };
    }
    if right {
        RIGHT
    } else {
        LEFT
    }
}

/// Whether a maneuver at a junction of this kind cuts across oncoming traffic, given the speed of the road it leaves.
#[inline(always)]
unsafe fn crosses(code: u8, speed_kmh: f64, kind: u8, rules: &TurnRules) -> bool {
    if kind == KIND_INTERCHANGE || kind == KIND_ONE_WAY {
        return false; // no oncoming stream to cross
    }
    rules.crossing.get(code as usize) != 0 || (rules.crossing_ramp.get(code as usize) != 0 && speed_kmh < rules.freeway_speed_kmh)
}

/// As many moves as there could be, for Python to make room.
#[no_mangle]
pub unsafe extern "C" fn moves_capacity(roads: *const Roads) -> i64 {
    let roads = &*roads;
    let mut capacity = 0i64;
    for edge in 0..roads.edge_to.len() {
        let node = roads.edge_to.get(edge) as usize;
        capacity += roads.out_ptr.get(node + 1) - roads.out_ptr.get(node);
    }
    capacity
}

/// Legal next segments from the end of every segment, with how each turn is classified. Returns how many.
#[no_mangle]
pub unsafe extern "C" fn build_moves(
    roads: *const Roads,
    rules: *const TurnRules,
    ptr: *mut i64,
    following: *mut i32,
    codes: *mut u8,
    flags: *mut u8,
    capacity: i64,
) -> i64 {
    let (roads, rules) = (&*roads, &*rules);
    let edges = roads.edge_to.n;
    let (ptr, following) = (Arr::new(ptr, edges + 1), Arr::new(following, capacity));
    let (codes, flags) = (Arr::new(codes, capacity), Arr::new(flags, capacity));
    let uturn = rules.uturn as u8;
    let mut count = 0usize;
    ptr.set(0, 0);
    for edge in 0..edges as usize {
        let node = roads.edge_to.get(edge) as usize;
        let (start, stop) = (roads.out_ptr.get(node) as usize, roads.out_ptr.get(node + 1) as usize);
        let kind = roads.kinds.get(node);
        for i in start..stop {
            let nxt = roads.out_edges.get(i);
            let mut banned = false;
            for j in roads.banned_ptr.get(edge) as usize..roads.banned_ptr.get(edge + 1) as usize {
                banned = banned || roads.banned.get(j) == nxt;
            }
            if banned {
                continue;
            }
            if roads.edge_to.get(nxt as usize) == roads.edge_from.get(edge) && stop - start > 1 {
                continue; // U-turns only at dead ends
            }
            let code = maneuver(edge, nxt as usize, roads, uturn);
            following.set(count, nxt);
            codes.set(count, code);
            let crossing = crosses(code, roads.speed_kmh.get(edge), kind, rules);
            flags.set(count, (if crossing { CROSSES } else { 0 }) | (if kind == KIND_BUSY { BUSY } else { 0 }));
            count += 1;
        }
        ptr.set(edge + 1, count as i64);
    }
    count as i64
}

/// How many steps build_steps writes: a connector's are a pass through it to each move on, and an arrival.
#[no_mangle]
pub unsafe extern "C" fn count_steps(edges: i64, ptr: *const i64, following: *const i32, moves: i64, connector: *const u8) -> i64 {
    let (ptr, following, connector) = (Arr::new(ptr, edges + 1), Arr::new(following, moves), Arr::new(connector, edges));
    let mut count = 0i64;
    for edge in 0..edges as usize {
        for i in ptr.get(edge) as usize..ptr.get(edge + 1) as usize {
            let nxt = following.get(i) as usize;
            if connector.get(nxt) != 0 {
                count += ptr.get(nxt + 1) - ptr.get(nxt) + 1;
            } else {
                count += 1;
            }
        }
    }
    count
}

/// The stress of the traffic crossed going from edge onto nxt: the worst road met at the junction that the rider isn't
/// riding along (going straight across or turning left across it), and the road being left when turning left across
/// its own oncoming lanes. 0 where a signal controls the junction, or for a right turn, which crosses no traffic.
#[no_mangle]
pub unsafe extern "C" fn crossing_stress(edge: i32, nxt: i32, code: u8, c: *const Crossings) -> i64 {
    let c = &*c;
    let (edge, nxt) = (edge as usize, nxt as usize);
    let node = c.edge_to.get(edge) as usize;
    if c.signal_node.get(node) != 0 || c.right.get(code as usize) != 0 {
        return 0;
    }
    let mut stress = 0i64;
    let (way_a, way_b) = (c.way_id.get(edge), c.way_id.get(nxt));
    let (name_a, name_b) = (c.name.get(edge), c.name.get(nxt));
    for (edges, ptr) in [(c.out_edges, c.out_ptr), (c.in_edges, c.in_ptr)] {
        for i in ptr.get(node) as usize..ptr.get(node + 1) as usize {
            let other = edges.get(i) as usize;
            let way = c.way_id.get(other);
            if way == way_a || way == way_b {
                continue;
            }
            let name = c.name.get(other);
            if name != 0 && (name == name_a || name == name_b) {
                continue;
            }
            stress = stress.max(c.cross.get(other) as i64);
        }
    }
    if c.left.get(code as usize) != 0 {
        stress = stress.max(c.cross.get(edge) as i64);
    }
    stress
}

/// One step, as it's being put together.
#[derive(Copy, Clone, Default)]
struct StepFacts {
    maneuver_first: u8,
    maneuver_second: u8,
    turn_first: u8,
    turn_second: u8,
    flags: u8,
    busy_plain: u8,
    busy_signal: u8,
    hotspots: u8,
    cut_through: u8,
    cross_3: u8,
    cross_4: u8,
    crash_spots: u8,
}

impl StepFacts {
    #[inline(always)]
    unsafe fn count_turn(&mut self, at_node: usize, crossing: bool, busy: bool, signal_node: Arr<u8>, hotspot_node: Arr<u8>) {
        if crossing {
            if busy {
                if signal_node.get(at_node) != 0 {
                    self.busy_signal += 1;
                } else {
                    self.busy_plain += 1;
                }
            }
            if hotspot_node.get(at_node) != 0 {
                self.hotspots += 1;
            }
        }
    }

    #[inline(always)]
    unsafe fn count_entry(&mut self, previous: i32, entered: i32, code: u8, crossings: *const Crossings, crash_node: Arr<u8>) {
        let stress = crossing_stress(previous, entered, code, crossings);
        if stress == 3 {
            self.cross_3 += 1;
        } else if stress >= 4 {
            self.cross_4 += 1;
        }
        if crash_node.get((*crossings).edge_to.get(previous as usize) as usize) != 0 {
            self.crash_spots += 1;
        }
    }

    /// Everything about it in one number, for telling kinds apart. Maneuvers take 4 bits, turns' rows 5, the rest 4.
    #[inline(always)]
    fn key(&self) -> u64 {
        let mut key = 0u64;
        for (value, bits) in [
            (self.maneuver_first, 4),
            (self.maneuver_second, 4),
            (self.turn_first, 5),
            (self.turn_second, 5),
            (self.flags, 4),
            (self.busy_plain, 4),
            (self.busy_signal, 4),
            (self.hotspots, 4),
            (self.cut_through, 4),
            (self.cross_3, 4),
            (self.cross_4, 4),
            (self.crash_spots, 4),
        ] {
            key = (key << bits) | value as u64;
        }
        key
    }
}

/// The kinds seen so far: an open-addressed table, as there are only a few thousand of them among millions of steps.
struct KindTable {
    slots: Vec<(u64, u16)>,
    count: usize,
}

const EMPTY_SLOT: u64 = u64::MAX;

impl KindTable {
    fn new() -> Self {
        KindTable { slots: vec![(EMPTY_SLOT, 0); 1 << 18], count: 0 }
    }

    /// The kind with this key, adding it if it's new. None if there's no room for another.
    #[inline(always)]
    unsafe fn kind(&mut self, facts: &StepFacts, out: &StepColumns) -> Option<u16> {
        let key = facts.key();
        let mask = self.slots.len() - 1;
        let mut slot = (key.wrapping_mul(0x9E37_79B9_7F4A_7C15) >> 40) as usize & mask;
        loop {
            let (found, kind) = *self.slots.get_unchecked(slot);
            if found == key {
                return Some(kind);
            }
            if found == EMPTY_SLOT {
                if self.count >= out.maneuver_first.len() {
                    return None;
                }
                let kind = self.count as u16;
                self.slots[slot] = (key, kind);
                self.count += 1;
                let k = kind as usize;
                out.maneuver_first.set(k, facts.maneuver_first);
                out.maneuver_second.set(k, facts.maneuver_second);
                out.turn_first.set(k, facts.turn_first);
                out.turn_second.set(k, facts.turn_second);
                out.flags.set(k, facts.flags);
                out.busy_plain.set(k, facts.busy_plain);
                out.busy_signal.set(k, facts.busy_signal);
                out.hotspots.set(k, facts.hotspots);
                out.cut_through.set(k, facts.cut_through);
                out.cross_3.set(k, facts.cross_3);
                out.cross_4.set(k, facts.cross_4);
                out.crash_spots.set(k, facts.crash_spots);
                return Some(kind);
            }
            slot = (slot + 1) & mask;
        }
    }
}

/// Ways on from every segment, each costed as one decision: usually one segment, and through a short connector two,
/// judged together (a median opening is part of the turn through it). Coming out pointing back the way the car came
/// is one U-turn across the oncoming carriageway, not a ramp followed by a slight turn.
/// Returns how many kinds of step there are, or -1 if there were too many for the room given.
#[no_mangle]
pub unsafe extern "C" fn build_steps(
    moves_ptr: *const i64,
    following: *const i32,
    codes: *const u8,
    flags: *const u8,
    moves: i64,
    roads: *const Roads,
    rules: *const TurnRules,
    connector: *const u8,
    signal_node: *const u8,
    hotspot_node: *const u8,
    cut_through: *const u8,
    crossings: *const Crossings,
    crash_node: *const u8,
    with_crossings: bool,
    out: *const StepColumns,
) -> i64 {
    let (roads, rules, out) = (&*roads, &*rules, &*out);
    let (edges, nodes) = (roads.edge_to.n, roads.out_ptr.n - 1);
    let moves_ptr = Arr::new(moves_ptr, edges + 1);
    let (following, codes, flags) = (Arr::new(following, moves), Arr::new(codes, moves), Arr::new(flags, moves));
    let (connector, cut_through) = (Arr::new(connector, edges), Arr::new(cut_through, edges));
    let (signal_node, hotspot_node, crash_node) = (Arr::new(signal_node, nodes), Arr::new(hotspot_node, nodes), Arr::new(crash_node, nodes));
    let uturn = rules.uturn as u8;
    let mut kinds = KindTable::new();
    let mut s = 0usize;
    out.ptr.set(0, 0);
    for edge in 0..edges as usize {
        let node = roads.edge_to.get(edge) as usize;
        let kind = roads.kinds.get(node);
        for i in moves_ptr.get(edge) as usize..moves_ptr.get(edge + 1) as usize {
            let nxt = following.get(i);
            let nxt_at = nxt as usize;
            let (code, flag) = (codes.get(i), flags.get(i));
            let (crosses_now, busy) = (flag & CROSSES != 0, flag & BUSY != 0);
            let through_stop = if connector.get(nxt_at) != 0 { moves_ptr.get(nxt_at + 1) } else { moves_ptr.get(nxt_at) };
            for through in moves_ptr.get(nxt_at) as usize..through_stop as usize {
                let after = following.get(through);
                let after_at = after as usize;
                let (code_after, flag_after) = (codes.get(through), flags.get(through));
                let mut facts = StepFacts::default();
                if angle_between(roads.bearing_end.get(edge), roads.bearing_start.get(after_at)) >= rules.uturn_min_deg {
                    let crossing = crosses(uturn, roads.speed_kmh.get(edge), kind, rules);
                    facts.maneuver_first = uturn;
                    facts.maneuver_second = STRAIGHT;
                    facts.turn_first = uturn * 2 + crossing as u8;
                    facts.turn_second = NO_TURN;
                    facts.flags = (if crossing { FIRST_CROSSES } else { 0 }) | (if kind == KIND_BUSY { FIRST_BUSY } else { 0 });
                    facts.count_turn(node, crossing, kind == KIND_BUSY, signal_node, hotspot_node);
                } else {
                    let node_after = roads.edge_to.get(nxt_at) as usize;
                    let (crosses_after, busy_after) = (flag_after & CROSSES != 0, flag_after & BUSY != 0);
                    facts.maneuver_first = code;
                    facts.maneuver_second = code_after;
                    facts.turn_first = code * 2 + crosses_now as u8;
                    facts.turn_second = code_after * 2 + crosses_after as u8;
                    facts.flags = (if crosses_now { FIRST_CROSSES } else { 0 })
                        | (if busy { FIRST_BUSY } else { 0 })
                        | (if crosses_after { SECOND_CROSSES } else { 0 })
                        | (if busy_after { SECOND_BUSY } else { 0 });
                    facts.count_turn(node, crosses_now, busy, signal_node, hotspot_node);
                    facts.count_turn(node_after, crosses_after, busy_after, signal_node, hotspot_node);
                }
                let (cut_e, cut_n, cut_a) = (cut_through.get(edge) != 0, cut_through.get(nxt_at) != 0, cut_through.get(after_at) != 0);
                facts.cut_through = (cut_n && !cut_e) as u8 + (cut_a && !cut_n) as u8;
                if with_crossings {
                    facts.count_entry(edge as i32, nxt, facts.maneuver_first, crossings, crash_node);
                    facts.count_entry(nxt, after, facts.maneuver_second, crossings, crash_node);
                }
                out.first.set(s, nxt);
                out.second.set(s, after);
                match kinds.kind(&facts, out) {
                    Some(found) => out.kind.set(s, found),
                    None => return -1,
                }
                s += 1;
            }
            // The move itself: a plain step, or a connector that's a way to arrive.
            let mut facts = StepFacts {
                maneuver_first: code,
                maneuver_second: STRAIGHT,
                turn_first: code * 2 + crosses_now as u8,
                turn_second: NO_TURN,
                flags: (if crosses_now { FIRST_CROSSES } else { 0 }) | (if busy { FIRST_BUSY } else { 0 }),
                cut_through: (cut_through.get(nxt_at) != 0 && cut_through.get(edge) == 0) as u8,
                ..StepFacts::default()
            };
            facts.count_turn(node, crosses_now, busy, signal_node, hotspot_node);
            if with_crossings {
                facts.count_entry(edge as i32, nxt, code, crossings, crash_node);
            }
            out.first.set(s, nxt);
            out.second.set(s, if connector.get(nxt_at) != 0 { ARRIVAL } else { -1 });
            match kinds.kind(&facts, out) {
                Some(found) => out.kind.set(s, found),
                None => return -1,
            }
            s += 1;
        }
        out.ptr.set(edge + 1, s as i64);
    }
    kinds.count as i64
}

/// For each junction, the road classes meeting there (a bit each), and whether all of its roads are one-way.
#[no_mangle]
pub unsafe extern "C" fn class_masks(
    nodes: i64,
    edges: i64,
    out_ptr: *const i64,
    out_edges: *const i32,
    in_ptr: *const i64,
    in_edges: *const i32,
    road_class: *const i32,
    oneway: *const i8,
    masks: *mut u64,
    all_oneway: *mut u8,
) {
    let (out_ptr, in_ptr) = (Arr::new(out_ptr, nodes + 1), Arr::new(in_ptr, nodes + 1));
    let (out_edges, in_edges) = (Arr::new(out_edges, edges), Arr::new(in_edges, edges));
    let (road_class, oneway) = (Arr::new(road_class, edges), Arr::new(oneway, edges));
    let (masks, all_oneway) = (Arr::new(masks, nodes), Arr::new(all_oneway, nodes));
    for node in 0..nodes as usize {
        let mut mask = 0u64;
        let mut every = 1u8;
        for (list, ptr) in [(out_edges, out_ptr), (in_edges, in_ptr)] {
            for i in ptr.get(node) as usize..ptr.get(node + 1) as usize {
                let edge = list.get(i) as usize;
                mask |= 1u64 << road_class.get(edge);
                if oneway.get(edge) == 0 {
                    every = 0;
                }
            }
        }
        masks.set(node, mask);
        all_oneway.set(node, every);
    }
}
