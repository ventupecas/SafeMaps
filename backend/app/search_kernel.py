"""The router's search as compiled code (Numba): the map's turns as flat arrays, and A* over them.

Router (app.router) decides what a trip costs; this module finds the cheapest way through those costs.
Nothing here knows about profiles or road events: it sees arrays of numbers. Three ideas:

* Every way on from a segment is worked out once, into arrays (build_moves, build_steps), instead of
  being judged again as the search reaches it. Millions of steps come in a few thousand kinds (the same turn, the
  same crossings), so a step keeps only its kind, and a traveler's costs are one short table (Router._step_costs).
* The search knows how far it still has to go. Before searching forward, reverse_bound searches back from
  the destination along the roads, ignoring turns. Turns and zones only add to a route, and an alternative's
  penalties only raise a road's cost, so what it finds never overstates what's left. That's what A* needs to
  still find the cheapest route, and it's far closer to the truth than a straight line at freeway speed.
* The compiled functions let go of Python's lock (nogil), so trips can plan side by side on the computer's cores.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
from numba import njit

from app.geo_kernel import haversine

# How a segment is entered. The codes index the tables Router builds from these names.
MANEUVERS = (
    "STRAIGHT",
    "DEPART",
    "TURN_SLIGHT_LEFT",
    "TURN_LEFT",
    "TURN_SHARP_LEFT",
    "UTURN_LEFT",
    "TURN_SLIGHT_RIGHT",
    "TURN_RIGHT",
    "TURN_SHARP_RIGHT",
    "UTURN_RIGHT",
    "RAMP_LEFT",
    "RAMP_RIGHT",
    "MERGE",
    "FORK_LEFT",
    "FORK_RIGHT",
)
MANEUVER_CODES = {name: code for code, name in enumerate(MANEUVERS)}
_STRAIGHT = MANEUVER_CODES["STRAIGHT"]
_DEPART = MANEUVER_CODES["DEPART"]
_SLIGHT_LEFT, _SLIGHT_RIGHT = MANEUVER_CODES["TURN_SLIGHT_LEFT"], MANEUVER_CODES["TURN_SLIGHT_RIGHT"]
_LEFT, _RIGHT = MANEUVER_CODES["TURN_LEFT"], MANEUVER_CODES["TURN_RIGHT"]
_SHARP_LEFT, _SHARP_RIGHT = MANEUVER_CODES["TURN_SHARP_LEFT"], MANEUVER_CODES["TURN_SHARP_RIGHT"]
_RAMP_LEFT, _RAMP_RIGHT = MANEUVER_CODES["RAMP_LEFT"], MANEUVER_CODES["RAMP_RIGHT"]
_MERGE = MANEUVER_CODES["MERGE"]
_FORK_LEFT, _FORK_RIGHT = MANEUVER_CODES["FORK_LEFT"], MANEUVER_CODES["FORK_RIGHT"]

# A turn's row in a profile's table: its maneuver code * 2, plus 1 when it crosses traffic.
NO_TURN = len(MANEUVERS) * 2  # the table's last row, which costs nothing: a step with one turn
TURN_TABLE_SIZE = NO_TURN + 1

# Junction kinds, as RoadGraph.node_kinds gives them.
KIND_QUIET, KIND_BUSY, KIND_INTERCHANGE, KIND_ONE_WAY = 0, 1, 2, 3

# step_second: the second segment a step enters, or one of these.
NO_SECOND = -1  # the step enters one segment
ARRIVAL = -2  # a short connector that ends at the destination: a way to arrive, only then a step at all

# A move's flags.
_CROSSES = 1
_BUSY = 2
# A step's flags: the same two for its first turn, then for its second.
FIRST_CROSSES, FIRST_BUSY, SECOND_CROSSES, SECOND_BUSY = 1, 2, 4, 8

# A junction's marks in the search back from the destination.
_SEEN, _SETTLED, _TARGET = 1, 2, 4

# The bits each of a step's columns takes, in build_steps' order, for packing them into one number: its kind.
STEP_COLUMN_BITS = (4, 4, 5, 5, 4, 2, 2, 2, 2, 2, 2, 2)

_HEAP_START = 4096


class Roads(NamedTuple):
    """What a move is judged by, for build_moves: the map's columns."""

    edge_from: np.ndarray
    edge_to: np.ndarray
    out_ptr: np.ndarray
    out_edges: np.ndarray
    banned_ptr: np.ndarray
    banned: np.ndarray
    bearing_start: np.ndarray
    bearing_end: np.ndarray
    link: np.ndarray
    freeway: np.ndarray
    way_id: np.ndarray
    name: np.ndarray
    speed_kmh: np.ndarray
    kinds: np.ndarray


class TurnRules(NamedTuple):
    """Which maneuvers cross traffic on this side of the road, and how a U-turn is named."""

    crossing: np.ndarray  # per maneuver code
    crossing_ramp: np.ndarray  # per maneuver code: crosses when leaving a surface street
    freeway_speed_kmh: float
    uturn: int
    uturn_min_deg: float


class Crossings(NamedTuple):
    """What crossing_stress reads."""

    edge_to: np.ndarray
    out_ptr: np.ndarray
    out_edges: np.ndarray
    in_ptr: np.ndarray
    in_edges: np.ndarray
    way_id: np.ndarray
    name: np.ndarray
    cross: np.ndarray
    signal_node: np.ndarray
    left: np.ndarray  # per maneuver code
    right: np.ndarray  # per maneuver code


class Steps(NamedTuple):
    """Every way on from every segment. A segment's are ptr[edge] up to ptr[edge + 1]."""

    ptr: np.ndarray
    first: np.ndarray  # the segment it enters
    second: np.ndarray  # then this one, through a short connector; or NO_SECOND, or ARRIVAL
    kind: np.ndarray  # which kind of step it is: its row in the rest of these columns
    maneuver_first: np.ndarray  # how each is entered
    maneuver_second: np.ndarray
    turn_first: np.ndarray  # its turns' rows in a profile's table (see NO_TURN)
    turn_second: np.ndarray
    flags: np.ndarray
    # What a profile's weights multiply, counted once per step:
    busy_plain: np.ndarray  # busy crossing turns without a signal
    busy_signal: np.ndarray  # and with one
    hotspots: np.ndarray  # crossing turns at crash spots
    cut_through: np.ndarray  # turns onto a service road
    # The bike and walking maps' (none on the car map): crossings of stress 3 and 4 traffic, and crash spots passed.
    cross_3: np.ndarray
    cross_4: np.ndarray
    crash_spots: np.ndarray


class Zones(NamedTuple):
    """A trip's zones, which cost once when driven into. Lane closures and hazards, by event number; school zones."""

    index: np.ndarray  # per segment: 0, or 1 + its row in ptr (empty when the trip has none)
    ptr: np.ndarray
    ids: np.ndarray
    cost: np.ndarray  # per zone number
    school: np.ndarray  # per segment
    school_cost: float  # 0 outside school hours


@njit(cache=True)
def class_masks(out_ptr, out_edges, in_ptr, in_edges, road_class, oneway):
    """For each junction, the road classes meeting there (a bit each), and whether all of its roads are one-way."""
    nodes = out_ptr.size - 1
    masks = np.zeros(nodes, dtype=np.uint64)
    all_oneway = np.ones(nodes, dtype=np.bool_)
    for node in range(nodes):
        for edges, ptr in ((out_edges, out_ptr), (in_edges, in_ptr)):
            for i in range(ptr[node], ptr[node + 1]):
                edge = edges[i]
                masks[node] |= np.uint64(1) << np.uint64(road_class[edge])
                if not oneway[edge]:
                    all_oneway[node] = False
    return masks, all_oneway


@njit(cache=True, inline="always")
def _angle_between(a, b):
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


@njit(cache=True)
def _maneuver(edge, nxt, roads, uturn):
    """Classify the move from edge onto nxt, using the Routes API's maneuver names."""
    if roads.edge_to[nxt] == roads.edge_from[edge]:
        return uturn
    angle = (roads.bearing_start[nxt] - roads.bearing_end[edge] + 540.0) % 360.0 - 180.0  # positive turns right
    right = angle > 0
    turn = abs(angle)
    link, freeway = roads.link, roads.freeway

    if link[nxt] and not link[edge]:
        if turn > 10:
            return _RAMP_RIGHT if right else _RAMP_LEFT
        return _STRAIGHT
    if link[edge] and freeway[nxt]:
        return _MERGE
    if (link[edge] or freeway[edge]) and (link[nxt] or freeway[nxt]):
        if turn > 10:
            return _FORK_RIGHT if right else _FORK_LEFT
        return _STRAIGHT

    same_road = roads.way_id[edge] == roads.way_id[nxt] or (roads.name[edge] != 0 and roads.name[edge] == roads.name[nxt])
    if turn <= 30 or (same_road and turn < 60):
        return _STRAIGHT
    if turn >= 165:
        return uturn
    if turn < 60:
        return _SLIGHT_RIGHT if right else _SLIGHT_LEFT
    if turn > 120:
        return _SHARP_RIGHT if right else _SHARP_LEFT
    return _RIGHT if right else _LEFT


@njit(cache=True)
def _crosses(code, speed_kmh, kind, rules):
    """Whether a maneuver at a junction of this kind cuts across oncoming traffic, given the speed of the road it leaves."""
    if kind == KIND_INTERCHANGE or kind == KIND_ONE_WAY:
        return False  # no oncoming stream to cross
    return rules.crossing[code] or (rules.crossing_ramp[code] and speed_kmh < rules.freeway_speed_kmh)


@njit(cache=True)
def build_moves(roads, rules):
    """Legal next segments from the end of every segment, with how each turn is classified."""
    edges = roads.edge_to.size
    ptr = np.zeros(edges + 1, dtype=np.int64)
    capacity = 0
    for edge in range(edges):
        node = roads.edge_to[edge]
        capacity += roads.out_ptr[node + 1] - roads.out_ptr[node]
    following = np.empty(capacity, dtype=np.int32)
    codes = np.empty(capacity, dtype=np.uint8)
    flags = np.empty(capacity, dtype=np.uint8)
    count = 0
    for edge in range(edges):
        node = roads.edge_to[edge]
        start, stop = roads.out_ptr[node], roads.out_ptr[node + 1]
        kind = roads.kinds[node]
        for i in range(start, stop):
            nxt = roads.out_edges[i]
            banned = False
            for j in range(roads.banned_ptr[edge], roads.banned_ptr[edge + 1]):
                banned = banned or roads.banned[j] == nxt
            if banned:
                continue
            if roads.edge_to[nxt] == roads.edge_from[edge] and stop - start > 1:
                continue  # U-turns only at dead ends
            code = _maneuver(edge, nxt, roads, rules.uturn)
            following[count] = nxt
            codes[count] = code
            flags[count] = (_CROSSES if _crosses(code, roads.speed_kmh[edge], kind, rules) else 0) | (_BUSY if kind == KIND_BUSY else 0)
            count += 1
        ptr[edge + 1] = count
    return ptr, following[:count].copy(), codes[:count].copy(), flags[:count].copy()


@njit(cache=True)
def _count_steps(ptr, following, connector):
    """How many steps build_steps writes: a connector's are a pass through it to each move on, and an arrival."""
    count = 0
    for edge in range(ptr.size - 1):
        for i in range(ptr[edge], ptr[edge + 1]):
            nxt = following[i]
            if connector[nxt]:
                count += ptr[nxt + 1] - ptr[nxt] + 1
            else:
                count += 1
    return count


@njit(cache=True)
def crossing_stress(edge, nxt, code, c):
    """The stress of the traffic crossed going from edge onto nxt: the worst road met at the junction that the
    rider isn't riding along (going straight across or turning left across it), and the road being left when
    turning left across its own oncoming lanes. 0 where a signal controls the junction, or for a right turn,
    which crosses no traffic."""
    node = c.edge_to[edge]
    if c.signal_node[node] or c.right[code]:
        return 0
    stress = 0
    way_a, way_b = c.way_id[edge], c.way_id[nxt]
    name_a, name_b = c.name[edge], c.name[nxt]
    for edges, ptr in ((c.out_edges, c.out_ptr), (c.in_edges, c.in_ptr)):
        for i in range(ptr[node], ptr[node + 1]):
            other = edges[i]
            if c.way_id[other] == way_a or c.way_id[other] == way_b:
                continue
            name = c.name[other]
            if name != 0 and (name == name_a or name == name_b):
                continue
            stress = max(stress, int(c.cross[other]))
    if c.left[code]:
        stress = max(stress, int(c.cross[edge]))
    return stress


@njit(cache=True, inline="always")
def _count_turn(s, at, crossing, busy, signal_node, hotspot_node, busy_plain, busy_signal, hotspots):
    """Add one turn at junction `at` to what step s's busy crossing and crash spot weights multiply."""
    if crossing:
        if busy:
            if signal_node[at]:
                busy_signal[s] += 1
            else:
                busy_plain[s] += 1
        if hotspot_node[at]:
            hotspots[s] += 1


@njit(cache=True, inline="always")
def _count_entry(s, previous, entered, code, crossings, crash_node, cross_3, cross_4, crash_spots):
    """Add entering a segment to what a traveler's crossing and crash spot minutes multiply."""
    stress = crossing_stress(previous, entered, code, crossings)
    if stress == 3:
        cross_3[s] += 1
    elif stress >= 4:
        cross_4[s] += 1
    if crash_node[crossings.edge_to[previous]]:
        crash_spots[s] += 1


@njit(cache=True)
def build_steps(
    moves_ptr,
    following,
    codes,
    flags,
    roads,
    rules,
    connector,
    signal_node,
    hotspot_node,
    cut_through,
    crossings,
    crash_node,
    with_crossings,
):
    """Ways on from every segment, each costed as one decision: usually one segment, and through a short
    connector two, judged together (a median opening is part of the turn through it). Coming out pointing back
    the way the car came is one U-turn across the oncoming carriageway, not a ramp followed by a slight turn."""
    edges = moves_ptr.size - 1
    count = _count_steps(moves_ptr, following, connector)
    ptr = np.zeros(edges + 1, dtype=np.int64)
    first = np.empty(count, dtype=np.int32)
    second = np.empty(count, dtype=np.int32)
    maneuver_first = np.empty(count, dtype=np.uint8)
    maneuver_second = np.empty(count, dtype=np.uint8)
    turn_first = np.empty(count, dtype=np.uint8)
    turn_second = np.empty(count, dtype=np.uint8)
    step_flags = np.zeros(count, dtype=np.uint8)
    busy_plain = np.zeros(count, dtype=np.uint8)
    busy_signal = np.zeros(count, dtype=np.uint8)
    hotspots = np.zeros(count, dtype=np.uint8)
    cut = np.zeros(count, dtype=np.uint8)
    cross_3 = np.zeros(count, dtype=np.uint8)
    cross_4 = np.zeros(count, dtype=np.uint8)
    crash_spots = np.zeros(count, dtype=np.uint8)

    s = 0
    for edge in range(edges):
        node = roads.edge_to[edge]
        kind = roads.kinds[node]
        for i in range(moves_ptr[edge], moves_ptr[edge + 1]):
            nxt, code, flag = following[i], codes[i], flags[i]
            crosses, busy = (flag & _CROSSES) != 0, (flag & _BUSY) != 0
            # Through a connector, a step to each move on from it.
            through_stop = moves_ptr[nxt + 1] if connector[nxt] else moves_ptr[nxt]
            for through in range(moves_ptr[nxt], through_stop):
                after, code_after, flag_after = following[through], codes[through], flags[through]
                first[s], second[s] = nxt, after
                if _angle_between(roads.bearing_end[edge], roads.bearing_start[after]) >= rules.uturn_min_deg:
                    uturn = rules.uturn
                    crossing = _crosses(uturn, roads.speed_kmh[edge], kind, rules)
                    maneuver_first[s], maneuver_second[s] = uturn, _STRAIGHT
                    turn_first[s], turn_second[s] = uturn * 2 + int(crossing), NO_TURN
                    step_flags[s] = (FIRST_CROSSES if crossing else 0) | (FIRST_BUSY if kind == KIND_BUSY else 0)
                    _count_turn(s, node, crossing, kind == KIND_BUSY, signal_node, hotspot_node, busy_plain, busy_signal, hotspots)
                else:
                    node_after = roads.edge_to[nxt]
                    crosses_after, busy_after = (flag_after & _CROSSES) != 0, (flag_after & _BUSY) != 0
                    maneuver_first[s], maneuver_second[s] = code, code_after
                    turn_first[s], turn_second[s] = code * 2 + int(crosses), code_after * 2 + int(crosses_after)
                    step_flags[s] = (
                        (FIRST_CROSSES if crosses else 0)
                        | (FIRST_BUSY if busy else 0)
                        | (SECOND_CROSSES if crosses_after else 0)
                        | (SECOND_BUSY if busy_after else 0)
                    )
                    _count_turn(s, node, crosses, busy, signal_node, hotspot_node, busy_plain, busy_signal, hotspots)
                    _count_turn(s, node_after, crosses_after, busy_after, signal_node, hotspot_node, busy_plain, busy_signal, hotspots)
                cut[s] = int(cut_through[nxt] and not cut_through[edge]) + int(cut_through[after] and not cut_through[nxt])
                if with_crossings:
                    _count_entry(s, edge, nxt, maneuver_first[s], crossings, crash_node, cross_3, cross_4, crash_spots)
                    _count_entry(s, nxt, after, maneuver_second[s], crossings, crash_node, cross_3, cross_4, crash_spots)
                s += 1
            # The move itself: a plain step, or a connector that's a way to arrive.
            first[s], second[s] = nxt, (ARRIVAL if connector[nxt] else NO_SECOND)
            maneuver_first[s], maneuver_second[s] = code, _STRAIGHT
            turn_first[s], turn_second[s] = code * 2 + int(crosses), NO_TURN
            step_flags[s] = (FIRST_CROSSES if crosses else 0) | (FIRST_BUSY if busy else 0)
            _count_turn(s, node, crosses, busy, signal_node, hotspot_node, busy_plain, busy_signal, hotspots)
            cut[s] = int(cut_through[nxt] and not cut_through[edge])
            if with_crossings:
                _count_entry(s, edge, nxt, code, crossings, crash_node, cross_3, cross_4, crash_spots)
            s += 1
        ptr[edge + 1] = s
    return (
        ptr,
        first,
        second,
        maneuver_first,
        maneuver_second,
        turn_first,
        turn_second,
        step_flags,
        busy_plain,
        busy_signal,
        hotspots,
        cut,
        cross_3,
        cross_4,
        crash_spots,
    )


# ---- The heap: a 4-ary heap of (priority, cost, item), compared in that order.


@njit(cache=True, inline="always")
def _before(priority_a, cost_a, item_a, priority_b, cost_b, item_b):
    if priority_a != priority_b:
        return priority_a < priority_b
    if cost_a != cost_b:
        return cost_a < cost_b
    return item_a < item_b


@njit(cache=True)
def _push(priorities, costs, items, size, priority, cost, item):
    i = size
    while i > 0:
        parent = (i - 1) >> 2
        if not _before(priority, cost, item, priorities[parent], costs[parent], items[parent]):
            break
        priorities[i], costs[i], items[i] = priorities[parent], costs[parent], items[parent]
        i = parent
    priorities[i], costs[i], items[i] = priority, cost, item


@njit(cache=True)
def _sift_last_down(priorities, costs, items, size):
    """Remove the top: the last entry (at index size) goes in its place, and down to where it belongs."""
    priority, cost, item = priorities[size], costs[size], items[size]
    i = 0
    while True:
        child = 4 * i + 1
        if child >= size:
            break
        best = child
        for other in range(child + 1, min(child + 4, size)):
            if _before(priorities[other], costs[other], items[other], priorities[best], costs[best], items[best]):
                best = other
        if not _before(priorities[best], costs[best], items[best], priority, cost, item):
            break
        priorities[i], costs[i], items[i] = priorities[best], costs[best], items[best]
        i = best
    priorities[i], costs[i], items[i] = priority, cost, item


@njit(cache=True)
def _grown(array):
    bigger = np.empty(array.size * 2, dtype=array.dtype)
    bigger[: array.size] = array
    return bigger


# ---- The search


@njit(cache=True, nogil=True)
def reverse_bound(end, targets, reach, edge_costs, in_ptr, in_edges, edge_from, distance, marks):
    """What's left to the destination from each junction, along the roads alone, turns ignored.

    Fills distance and marks (both all zeros on the way in, so only what's touched costs memory). Returns the
    distance searched to: every junction not settled is at least that far from the destination. It goes on until
    every junction in targets is settled and reach times the farthest of them is covered. If the roads run out
    first the answer is infinity: what wasn't reached can't reach the destination at all.
    """
    capacity = _HEAP_START
    priorities = np.empty(capacity, dtype=np.float64)
    costs = np.empty(capacity, dtype=np.float64)
    items = np.empty(capacity, dtype=np.int32)
    size = 1
    priorities[0], costs[0], items[0] = 0.0, 0.0, end
    marks[end] |= _SEEN
    waiting = 0
    for node in targets:
        if not marks[node] & _TARGET:
            marks[node] |= _TARGET
            waiting += 1
    limit = np.inf
    while size > 0:
        done, node = priorities[0], items[0]
        size -= 1
        if size > 0:
            _sift_last_down(priorities, costs, items, size)
        if marks[node] & _SETTLED:
            continue
        if done >= limit:
            return done
        marks[node] |= _SETTLED
        distance[node] = done
        if marks[node] & _TARGET:
            waiting -= 1
            if waiting == 0:
                limit = done * reach
        for i in range(in_ptr[node], in_ptr[node + 1]):
            edge = in_edges[i]
            cost = edge_costs[edge]
            if cost == np.inf:
                continue
            start = edge_from[edge]
            if marks[start] & _SETTLED:
                continue
            new = done + cost
            if not marks[start] & _SEEN or new < distance[start]:
                marks[start] |= _SEEN
                distance[start] = new
                if size == capacity:
                    priorities, costs, items = _grown(priorities), _grown(costs), _grown(items)
                    capacity *= 2
                _push(priorities, costs, items, size, new, new, start)
                size += 1
    return np.inf


@njit(cache=True, inline="always")
def _zones_entered(previous, entered, zones):
    """What driving into a segment's zones costs: once per zone, not again from a segment in the same one."""
    cost = 0.0
    if zones.school_cost > 0.0 and zones.school[entered] and not zones.school[previous]:
        cost += zones.school_cost
    if zones.ptr.size > 1:
        row = zones.index[entered]
        if row != 0:
            row_before = zones.index[previous]
            for i in range(zones.ptr[row - 1], zones.ptr[row]):
                zone = zones.ids[i]
                inside = False
                if row_before != 0:
                    for j in range(zones.ptr[row_before - 1], zones.ptr[row_before]):
                        inside = inside or zones.ids[j] == zone
                if not inside:
                    cost += zones.cost[zone]
    return cost


@njit(cache=True, nogil=True)
def search(
    departures,
    end,
    edge_costs,
    step_costs,
    edge_to,
    step_ptr,
    step_first,
    step_second,
    step_kind,
    maneuver_first,
    maneuver_second,
    zones,
    distance,
    marks,
    radius,
    straight_per_km,
    goal_lat,
    goal_lng,
    node_lat,
    node_lng,
    best,
    reached_by,
    came_from,
):
    """A* over segments: a state is "arrived at this junction along this segment", which is what makes turn costs
    and turn restrictions possible. It finds the cheapest route.

    With straight_per_km above 0, what's left is instead that much per km in a straight line to the goal. Set above
    a true lower bound, it heads for the goal more directly than the cheapest route does: the way to find direct routes.

    best, reached_by and came_from are all zeros on the way in, so only what's touched costs memory. reached_by
    holds, per segment, the step reaching it plus 2 (1 for a departure, 0 for not reached). Returns the route: its
    segments, how each is entered, and the steps taken; all empty if none. Also how many states were opened.
    """
    capacity = _HEAP_START
    while capacity < departures.size:
        capacity *= 2
    priorities = np.empty(capacity, dtype=np.float64)
    costs = np.empty(capacity, dtype=np.float64)
    items = np.empty(capacity, dtype=np.int32)
    size = 0
    for edge in departures:
        cost = edge_costs[edge]
        node = edge_to[edge]
        left = distance[node] if marks[node] & _SETTLED else radius
        if left == np.inf:
            continue
        if straight_per_km > 0.0:
            left = straight_per_km * haversine(node_lat[node], node_lng[node], goal_lat, goal_lng) / 1000
        best[edge] = cost
        reached_by[edge] = 1
        _push(priorities, costs, items, size, cost + left, cost, edge)
        size += 1

    opened = 0
    final = -1
    while size > 0:
        cost, edge = costs[0], items[0]
        size -= 1
        if size > 0:
            _sift_last_down(priorities, costs, items, size)
        if cost > best[edge]:
            continue  # a stale entry
        opened += 1
        if edge_to[edge] == end:
            final = edge
            break
        for s in range(step_ptr[edge], step_ptr[edge + 1]):
            first, second = step_first[s], step_second[s]
            if second == ARRIVAL and edge_to[first] != end:
                continue
            new = cost + step_costs[step_kind[s]] + edge_costs[first] + _zones_entered(edge, first, zones)
            last = first
            if second >= 0:
                new += edge_costs[second] + _zones_entered(first, second, zones)
                last = second
            if new == np.inf or (reached_by[last] != 0 and new >= best[last]):
                continue
            node = edge_to[last]
            left = distance[node] if marks[node] & _SETTLED else radius
            if left == np.inf:
                continue
            if straight_per_km > 0.0:
                left = straight_per_km * haversine(node_lat[node], node_lng[node], goal_lat, goal_lng) / 1000
            best[last] = new
            reached_by[last] = s + 2
            came_from[last] = edge
            if size == capacity:
                priorities, costs, items = _grown(priorities), _grown(costs), _grown(items)
                capacity *= 2
            _push(priorities, costs, items, size, new + left, new, last)
            size += 1

    if final < 0:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.int32), opened

    # Back along the steps. Each enters one or two of the route's segments.
    taken = 0
    edge = final
    entered = 1
    while reached_by[edge] != 1:
        s = reached_by[edge] - 2
        entered += 2 if step_second[s] >= 0 else 1
        taken += 1
        edge = came_from[edge]
    path = np.empty(entered, dtype=np.int32)
    maneuvers = np.empty(entered, dtype=np.uint8)
    steps = np.empty(taken, dtype=np.int32)
    edge = final
    at = entered
    for t in range(taken - 1, -1, -1):
        s = reached_by[edge] - 2
        steps[t] = s
        kind = step_kind[s]
        if step_second[s] >= 0:
            at -= 1
            path[at], maneuvers[at] = step_second[s], maneuver_second[kind]
        at -= 1
        path[at], maneuvers[at] = step_first[s], maneuver_first[kind]
        edge = came_from[edge]
    path[0], maneuvers[0] = edge, _DEPART
    return path, maneuvers, steps, opened


@njit(cache=True, nogil=True)
def path_cost(departure, steps, edge_costs, step_costs, step_first, step_second, step_kind, zones):
    """What a route costs, by the steps it took: without the penalties it may have been searched with."""
    cost = edge_costs[departure]
    previous = departure
    for s in steps:
        first, second = step_first[s], step_second[s]
        cost += step_costs[step_kind[s]] + edge_costs[first] + _zones_entered(previous, first, zones)
        previous = first
        if second >= 0:
            cost += edge_costs[second] + _zones_entered(first, second, zones)
            previous = second
    return cost
