"""The router's search, in native code (backend/rust_kernel/src/search.rs): the map's turns as flat arrays, and A* over them.

Router (app.router) decides what a trip costs; this finds the cheapest way through those costs. These are thin
wrappers: see search.rs for how it works.
"""

from __future__ import annotations

import ctypes as C
from typing import NamedTuple

import numpy as np

from app.native import Arr, Out, address, arr, boolean, copied, declare, f64, i32, i64, lib, pointer, u8

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

# A turn's row in a profile's table: its maneuver code * 2, plus 1 when it crosses traffic.
NO_TURN = len(MANEUVERS) * 2  # the table's last row, which costs nothing: a step with one turn
TURN_TABLE_SIZE = NO_TURN + 1

# step.second: the second segment a step enters, or one of these.
NO_SECOND = -1  # the step enters one segment
ARRIVAL = -2  # a short connector that ends at the destination: a way to arrive, only then a step at all

# A step's flags: whether its first turn crosses traffic and is at a busy junction, then the same for its second.
FIRST_CROSSES, FIRST_BUSY, SECOND_CROSSES, SECOND_BUSY = 1, 2, 4, 8

_MOST_KINDS = 65_536  # of step: a step's kind is a 16-bit number


class _RoadsC(C.Structure):
    _fields_ = [(name, Arr) for name in ("edge_from", "edge_to", "out_ptr", "out_edges", "banned_ptr", "banned", "bearing_start", "bearing_end", "link", "freeway", "way_id", "name", "speed_kmh", "kinds")]


class _TurnRulesC(C.Structure):
    _fields_ = [("crossing", Arr), ("crossing_ramp", Arr), ("freeway_speed_kmh", f64), ("uturn", i64), ("uturn_min_deg", f64)]


class _CrossingsC(C.Structure):
    _fields_ = [(name, Arr) for name in ("edge_to", "out_ptr", "out_edges", "in_ptr", "in_edges", "way_id", "name", "cross", "signal_node", "left", "right")]


class _ZonesC(C.Structure):
    _fields_ = [("index", Arr), ("ptr", Arr), ("ids", Arr), ("cost", Arr), ("school", Arr), ("school_cost", f64)]


class _StepTableC(C.Structure):
    _fields_ = [(name, Arr) for name in ("edge_to", "ptr", "first", "second", "kind", "maneuver_first", "maneuver_second")]


class _StepColumnsC(C.Structure):
    _fields_ = [
        (name, Arr)
        for name in ("ptr", "first", "second", "kind", "maneuver_first", "maneuver_second", "turn_first", "turn_second", "flags", "busy_plain", "busy_signal", "hotspots", "cut_through", "cross_3", "cross_4", "crash_spots")
    ]


class _RouteOutC(C.Structure):
    _fields_ = [("path", Out), ("maneuvers", Out), ("steps", Out), ("opened", i64)]


declare("workspace_new", [i64, i64], pointer)
declare("workspace_free", [pointer])
declare("workspace_left", [pointer, i64], f64)
declare("workspace_settled", [pointer], i64)
declare("reverse_bound", [pointer, i32, pointer, i64, f64, pointer, i64, pointer, i64, pointer, i64, pointer, i64], f64)
declare("search", [pointer, pointer, i64, i32, pointer, i64, pointer, i64, pointer, pointer, f64, f64, pointer, pointer, i64, pointer])
declare("free_route", [pointer])
declare("path_cost", [i32, pointer, i64, pointer, i64, pointer, i64, pointer, pointer, pointer, i64, pointer], f64)
declare("moves_capacity", [pointer], i64)
declare("build_moves", [pointer, pointer, pointer, pointer, pointer, pointer, i64], i64)
declare("count_steps", [i64, pointer, pointer, i64, pointer], i64)
declare("crossing_stress", [i32, i32, u8, pointer], i64)
declare("build_steps", [pointer, pointer, pointer, pointer, i64, pointer, pointer, pointer, pointer, pointer, pointer, pointer, pointer, boolean, pointer], i64)
declare("class_masks", [i64, i64, pointer, pointer, pointer, pointer, pointer, pointer, pointer, pointer])


class Roads:
    """What a move is judged by, for build_moves: the map's columns."""

    def __init__(self, *columns: np.ndarray) -> None:
        self.columns = columns
        self.c = _RoadsC(*map(arr, columns))


class TurnRules:
    """Which maneuvers cross traffic on this side of the road, and how a U-turn is named."""

    def __init__(self, crossing: np.ndarray, crossing_ramp: np.ndarray, freeway_speed_kmh: float, uturn: int, uturn_min_deg: float) -> None:
        self.columns = (crossing, crossing_ramp)
        self.c = _TurnRulesC(arr(crossing), arr(crossing_ramp), freeway_speed_kmh, uturn, uturn_min_deg)


class Crossings:
    """What crossing_stress reads."""

    def __init__(self, *columns: np.ndarray) -> None:
        self.columns = columns
        self.c = _CrossingsC(*map(arr, columns))
        self.address = C.addressof(self.c)


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


class StepTable:
    """What the search reads of the steps."""

    def __init__(self, steps: Steps, edge_to: np.ndarray) -> None:
        self.columns = (steps, edge_to)
        self.c = _StepTableC(arr(edge_to), arr(steps.ptr), arr(steps.first), arr(steps.second), arr(steps.kind), arr(steps.maneuver_first), arr(steps.maneuver_second))
        self.address = C.addressof(self.c)


class Zones:
    """A trip's zones, which cost once when driven into. Lane closures and hazards, by event number; school zones."""

    def __init__(self, index: np.ndarray, ptr: np.ndarray, ids: np.ndarray, cost: np.ndarray, school: np.ndarray, school_cost: float) -> None:
        self.columns = (index, ptr, ids, cost, school)
        self.c = _ZonesC(arr(index), arr(ptr), arr(ids), arr(cost), arr(school), school_cost)
        self.address = C.addressof(self.c)


class Workspace:
    """One trip's scratch, kept for the next: what it holds is told apart by a number, so nothing has to be cleared."""

    def __init__(self, nodes: int, edges: int) -> None:
        self._address = lib.workspace_new(nodes, edges)
        self._free = lib.workspace_free  # still here when the interpreter's shutting down

    def __del__(self) -> None:
        self._free(self._address)

    def reverse_bound(self, end: int, targets: np.ndarray, reach: float, edge_costs: np.ndarray, in_ptr: np.ndarray, in_edges: np.ndarray, edge_from: np.ndarray) -> float:
        return lib.reverse_bound(
            self._address,
            end,
            address(targets),
            targets.size,
            reach,
            address(edge_costs),
            edge_costs.size,
            address(in_ptr),
            in_ptr.size,
            address(in_edges),
            in_edges.size,
            address(edge_from),
            edge_from.size,
        )

    def left(self, node: int) -> float:
        """What's left from a junction, as the search back found it; negative if it wasn't settled."""
        return lib.workspace_left(self._address, node)

    @property
    def settled(self) -> int:
        return lib.workspace_settled(self._address)

    def search(
        self,
        departures: np.ndarray,
        end: int,
        edge_costs: np.ndarray,
        step_costs: np.ndarray,
        table: StepTable,
        zones: Zones,
        radius: float,
        straight_per_km: float,
        node_lat: np.ndarray,
        node_lng: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        """The route: its segments, how each is entered, and the steps taken, all empty if there's none; and how many states were opened."""
        out = _RouteOutC()
        lib.search(
            self._address,
            address(departures),
            departures.size,
            end,
            address(edge_costs),
            edge_costs.size,
            address(step_costs),
            step_costs.size,
            table.address,
            zones.address,
            radius,
            straight_per_km,
            address(node_lat),
            address(node_lng),
            node_lat.size,
            C.addressof(out),
        )
        try:
            return copied(out.path, np.int32), copied(out.maneuvers, np.uint8), copied(out.steps, np.int32), out.opened
        finally:
            lib.free_route(C.addressof(out))


def path_cost(departure: int, steps: np.ndarray, edge_costs: np.ndarray, step_costs: np.ndarray, first: np.ndarray, second: np.ndarray, kind: np.ndarray, zones: Zones) -> float:
    """What a route costs, by the steps it took: without the penalties it may have been searched with."""
    return lib.path_cost(
        departure, address(steps), steps.size, address(edge_costs), edge_costs.size, address(step_costs), step_costs.size, address(first), address(second), address(kind), first.size, zones.address
    )


def class_masks(out_ptr: np.ndarray, out_edges: np.ndarray, in_ptr: np.ndarray, in_edges: np.ndarray, road_class: np.ndarray, oneway: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each junction, the road classes meeting there (a bit each), and whether all of its roads are one-way."""
    nodes = out_ptr.size - 1
    masks = np.zeros(nodes, dtype=np.uint64)
    all_oneway = np.zeros(nodes, dtype=np.bool_)
    lib.class_masks(nodes, out_edges.size, address(out_ptr), address(out_edges), address(in_ptr), address(in_edges), address(road_class), address(oneway), address(masks), address(all_oneway))
    return masks, all_oneway


def build_moves(roads: Roads, rules: TurnRules) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Legal next segments from the end of every segment, with how each turn is classified."""
    capacity = lib.moves_capacity(C.addressof(roads.c))
    ptr = np.zeros(roads.columns[1].size + 1, dtype=np.int64)
    following = np.empty(capacity, dtype=np.int32)
    codes = np.empty(capacity, dtype=np.uint8)
    flags = np.empty(capacity, dtype=np.uint8)
    count = lib.build_moves(C.addressof(roads.c), C.addressof(rules.c), address(ptr), address(following), address(codes), address(flags), capacity)
    return ptr, following[:count].copy(), codes[:count].copy(), flags[:count].copy()


def crossing_stress(edge: int, nxt: int, code: int, crossings: Crossings) -> int:
    return lib.crossing_stress(edge, nxt, code, crossings.address)


def build_steps(
    moves_ptr: np.ndarray,
    following: np.ndarray,
    codes: np.ndarray,
    flags: np.ndarray,
    roads: Roads,
    rules: TurnRules,
    connector: np.ndarray,
    signal_node: np.ndarray,
    hotspot_node: np.ndarray,
    cut_through: np.ndarray,
    crossings: Crossings,
    crash_node: np.ndarray,
    with_crossings: bool,
) -> Steps:
    """Every way on from every segment, each costed as one decision."""
    edges = moves_ptr.size - 1
    count = lib.count_steps(edges, address(moves_ptr), address(following), following.size, address(connector))
    ptr = np.zeros(edges + 1, dtype=np.int64)
    first = np.empty(count, dtype=np.int32)
    second = np.empty(count, dtype=np.int32)
    kind = np.empty(count, dtype=np.uint16)
    room = [np.zeros(_MOST_KINDS, dtype=np.uint8) for _ in range(12)]
    columns = _StepColumnsC(arr(ptr), arr(first), arr(second), arr(kind), *map(arr, room))
    kinds = lib.build_steps(
        address(moves_ptr),
        address(following),
        address(codes),
        address(flags),
        following.size,
        C.addressof(roads.c),
        C.addressof(rules.c),
        address(connector),
        address(signal_node),
        address(hotspot_node),
        address(cut_through),
        crossings.address,
        address(crash_node),
        with_crossings,
        C.addressof(columns),
    )
    if kinds < 0:
        raise RuntimeError(f"More than {_MOST_KINDS} kinds of step on this map")
    return Steps(ptr, first, second, kind, *(column[:kinds].copy() for column in room))
