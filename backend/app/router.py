"""The engine's own router: personalized shortest paths on the OSM road graph.

Costs are the same perceived minutes the scorer uses, applied per road
segment and per turn instead of per whole route:

* a segment costs its expected no-traffic time, distance and fuel (a stop
  at each traffic signal included), plus the highway penalty or minus the
  winding-road refund, weighted by the profile;
* a turn costs its physical time plus the maneuver weight, and a turn across
  oncoming traffic adds the busy and crash-prone penalties. "Busy" and
  "interchange" come from RoadGraph.node_kind, the same rule the scorer uses
  when it has the graph.

The search runs over segments rather than junctions (a state is "arrived at
junction v along segment e"), which is what makes turn costs and turn
restrictions possible. Live traffic isn't in the graph; the pricing step asks
Google for it. The search itself is compiled (app.search_kernel): this module
turns a trip into the arrays it searches, and what it finds back into a route.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any

import numpy as np

import app.search_kernel as kernel
from app.driving_side import DrivingSide
from app.features import (
    CROSSING_MANEUVERS,
    CROSSING_RAMPS,
    DECISION_MANEUVERS,
    HIGHWAY_SPEED_KMH,
    HOTSPOT_RADIUS_M,
    TurnContext,
)
from app.fuel import SIGNAL_L, TURN_L, cruise_liters_each
from app.geo import Point, encode_polyline, haversine_m, initial_bearings_deg
from app.models import SIGNALED_CROSSING_SHARE, ScoringProfile
from app.road_events import MAX_SLOWDOWN, WORKERS_PRESENT_FACTOR, RoadEvent
from app.roadgraph import DEFAULT_SPEED_FACTOR, RoadGraph
from app.sun import GLARE_ANGLE_DEG, Sun, low_sun

LINK_CLASSES = frozenset({"motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"})
FREEWAY_CLASSES = frozenset({"motorway", "trunk"})

# Alternatives: after each route is found, its segments cost ALTERNATIVE_PENALTY
# times more in the next search. A new route is kept only if it shares at most
# MAX_OVERLAP of its length with each earlier one, so it takes a different
# corridor rather than a block-long detour, and its true cost is within
# MAX_COST_RATIO of the best route's. Alternatives may be much slower: the
# ranking says so, and the driver chooses.
ALTERNATIVE_PENALTY = 2.0
MAX_OVERLAP = 0.5
MAX_COST_RATIO = 2.0
ALTERNATIVE_SEARCHES_PER_ROUTE = 3
# The longer the trip, the longer each search: fewer alternatives, so a long trip stays worth waiting for.
# The first search, the best route, is always made. (Straight-line km, at most routes, at most searches.)
SEARCH_BUDGETS = ((20.0, 2, 3), (8.0, 3, 5))
EDGE_COSTS_KEPT = 6  # segment and step costs kept per router, by profile: about 15 MB each on Milwaukee's car map
# Searches for alternatives inflate the A* estimate by this factor: much
# faster on penalized costs, and an alternative needs to be good and
# different, not provably the best. The first design's search stays exact.
ALTERNATIVE_SEARCH_GREED = 1.5
# How far past the trip's start the search back from the destination goes (see kernel.reverse_bound), as a
# multiple of the road-only cost from there: farther, and more is settled than the forward search ever looks at.
BOUND_REACH = 1.5
MAX_SNAP_M = 1_000.0
# A moving car whose road can't be matched starts from the nearest junction,
# but not back the way it came.
MAX_START_TURN_DEG = 100.0
# Turning onto a service road (alley, parking-lot connector) mid-trip costs this
# many perceived minutes. Real drivers, and Google, don't cut through them to
# save a turn. A trip that starts or ends on one pays it on every route alike.
CUT_THROUGH_PENALTY = 10.0
CUT_THROUGH_CLASSES = frozenset({"service"})
# For a profile that ranks by priority (safety, then fuel, then time), designs
# lean harder on safety and fuel, so the routes proposed suit that order.
PRIORITY_SAFETY_BOOST = 4.0
PRIORITY_FUEL_BOOST = 2.5
# A segment shorter than this, such as a median opening between the two
# carriageways of a divided road, is part of the turn through it: the turn is
# judged from the road before it to the road after it. Coming out pointing
# back the way the car came is one U-turn across the oncoming carriageway,
# not a ramp followed by a slight turn.
CONNECTOR_MAX_M = 30.0
UTURN_MIN_DEG = 150.0
# Road events (app.road_events): a jammed segment takes about this many times
# its free-flow time, a slow one or one with a lane closed less so, unless the
# source measures it (Router._slowdown). Driving
# into a lane closure also costs lane_closure_weight once: a forced merge.
JAM_FACTOR = 3.0
SLOW_FACTOR = 1.5
LANE_CLOSURE_FACTOR = 1.5
WEATHER_FACTOR = 1.3  # a stretch with fog, ice or flooding reported is driven this much slower
# OSM hazards: a railroad crossing at grade adds RAIL_CROSSING_S (slowing down,
# and now and then a train) plus rail_crossing_weight. During school hours a
# school zone is driven at SCHOOL_ZONE_KMH (20 mph), and entering one costs
# school_zone_weight once.
RAIL_CROSSING_S = 20.0
SCHOOL_ZONE_KMH = 32.0

_ALL_MANEUVERS = DECISION_MANEUVERS | {"STRAIGHT"}
_EMPTY_INT32 = np.empty(0, dtype=np.int32)
_EMPTY_INT64 = np.empty(0, dtype=np.int64)

Move = tuple[int, str, bool, bool]  # next segment, maneuver, crosses traffic, busy junction
Turn = tuple[int, str, bool, bool]  # junction, maneuver, crosses traffic, busy junction


@dataclass(frozen=True)
class Step:
    """One decision in the search: the segments it enters (two through a short connector) and the turns it costs."""

    edges: tuple[int, ...]
    maneuvers: tuple[str, ...]  # how each segment is entered
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class _Costs:
    """One trip's costs: per segment (with its road events), per turn, and which lane closures each segment is in."""

    profile: ScoringProfile
    edges: np.ndarray
    table: dict[tuple[str, bool], float]
    zones: dict[int, frozenset[int]]  # segment -> the lane closures and hazards it's in, by event number
    zone_costs: dict[int, float]  # zone -> what driving into it costs
    school_cost: float = 0.0  # what driving into a school zone costs: 0 outside school hours
    key: Any = None  # what the step costs depend on: trips with the same key share them
    extra: Any = None  # the bike router's own (app.bike): the rider's profile, and whether it's dark


class OutsideGraphError(LookupError):
    """The trip starts or ends too far from any road in the graph."""


@dataclass(frozen=True)
class RoutedPath:
    edges: tuple[int, ...]
    maneuvers: tuple[str, ...]  # how each segment is entered; the first is DEPART
    cost: float  # perceived minutes, without alternative-route penalties


def design_profile(profile: ScoringProfile) -> ScoringProfile:
    """The weights the router designs with: the profile's own, boosted for priority ranking."""
    if profile.ranking != "priority":
        return profile
    return profile.model_copy(
        update={
            "busy_crossing_weight": profile.busy_crossing_weight * PRIORITY_SAFETY_BOOST,
            "hotspot_crossing_weight": profile.hotspot_crossing_weight * PRIORITY_SAFETY_BOOST,
            "lane_closure_weight": profile.lane_closure_weight * PRIORITY_SAFETY_BOOST,
            "school_zone_weight": profile.school_zone_weight * PRIORITY_SAFETY_BOOST,
            "rail_crossing_weight": profile.rail_crossing_weight * PRIORITY_SAFETY_BOOST,
            "weather_hazard_weight": profile.weather_hazard_weight * PRIORITY_SAFETY_BOOST,
            "sun_glare_weight": profile.sun_glare_weight * PRIORITY_SAFETY_BOOST,
            "fuel_weight": profile.fuel_weight * PRIORITY_FUEL_BOOST,
        }
    )


def turn_time_s(maneuver: str, crossing: bool) -> float:
    """Physical time a maneuver adds: slowing, turning and, across traffic, waiting for a gap."""
    if maneuver in ("STRAIGHT", "DEPART"):
        return 0.0
    if maneuver.startswith("UTURN"):
        return 25.0
    if crossing:
        return 10.0
    if maneuver.startswith(("TURN_SLIGHT", "RAMP", "MERGE", "FORK")):
        return 2.0
    return 5.0


LEFT_TURNS = ("TURN_LEFT", "TURN_SLIGHT_LEFT", "TURN_SHARP_LEFT", "UTURN_LEFT")
RIGHT_TURNS = ("TURN_RIGHT", "TURN_SLIGHT_RIGHT", "TURN_SHARP_RIGHT", "UTURN_RIGHT")
KNOWN_ROUTES_KEPT = 5000  # routes whose road lengths the graph remembers (graph.known_road_lengths)


def _search_budget(km: float, count: int) -> tuple[int, int]:
    """How many of the count routes asked for to look for on a trip this long, in how many searches."""
    searches = count * ALTERNATIVE_SEARCHES_PER_ROUTE
    for longer_than, most_routes, most_searches in SEARCH_BUDGETS:
        if km >= longer_than:
            return min(count, most_routes), min(searches, most_searches)
    return count, searches


class Router:
    _with_crossings = False  # the bike and walking maps' steps also count crossings and crash spots (app.bike)
    _crash_spot_min = math.inf

    def __init__(self, graph: RoadGraph, context: TurnContext = TurnContext()) -> None:
        self.graph = graph
        self.context = context

        def flags(names: frozenset[str]) -> np.ndarray:
            by_class = np.array([name in names for name in graph.classes], dtype=np.bool_)
            return by_class[graph.column("road_class")]

        self._link = flags(LINK_CLASSES)
        self._freeway = flags(FREEWAY_CLASSES)
        self._cut_through = flags(CUT_THROUGH_CLASSES)
        self._connector = graph.column("length_m") < CONNECTOR_MAX_M
        self._kept_costs: dict[str, np.ndarray] = {}
        self._kept_step_costs: dict[Any, np.ndarray] = {}
        self._crashes: np.ndarray | None = None
        self._event_edges: dict[tuple[Any, ...], frozenset[int]] = {}
        self._bearings: np.ndarray | None = None
        # Junctions with a traffic signal: a busy crossing turn there costs SIGNALED_CROSSING_SHARE of one.
        self._signal_node = np.zeros(graph.node_count, dtype=np.bool_)
        self._signal_node[graph.column("edge_to")[graph.column("signals") > 0]] = True
        self._build_lock = Lock()
        self._moves_table: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None
        self._steps_table: kernel.Steps | None = None

    def routes(
        self,
        origin: Point,
        destination: Point,
        profile: ScoringProfile,
        *,
        count: int = 3,
        avoid: Sequence[Sequence[Point]] = (),
        heading: float | None = None,
        events: Sequence[RoadEvent] = (),
        school_hours: bool = False,
        sun: Sun | None = None,
    ) -> list[RoutedPath]:
        """Up to count distinct routes for this profile, best first.

        The first is the best design, whatever it shares with other routes.
        Each alternative shares at most MAX_OVERLAP of its length with every
        earlier one and with each route in avoid (routes already on offer,
        such as Google's suggestions). heading is the direction a moving car is
        driving, in degrees clockwise from north: routes then start along the
        road it's on, without turning around. events are closures, lane
        closures and jams on the roads now: closed roads are left out (except
        under the car), and the rest cost more (see JAM_FACTOR and below).
        During school_hours, school zones cost more too (see SCHOOL_ZONE_KMH),
        and with sun (its azimuth and elevation) low, so do roads heading into it.
        """
        g = self.graph
        departures = self._departures(origin, heading)
        end = g.nearest_node(destination, MAX_SNAP_M)
        if not departures or end is None:
            which = "start" if not departures else "end"
            raise OutsideGraphError(f"trip {which} is more than {MAX_SNAP_M:.0f} m from the {g.area} road graph")
        if heading is None and g.edge_from[departures[0]] == end:
            return []

        costs = self._trip_costs(profile, events, keep=departures, school_hours=school_hours, sun=sun)
        count, searches = _search_budget(haversine_m(origin, destination) / 1000, count)
        on_offer = [edges for edges in (g.edges_along(points) for points in avoid) if edges]
        table = self._steps()
        step_costs = self._step_costs(costs)
        zones = self._zone_table(costs)
        starts = np.array(departures, dtype=np.int32)
        edge_to = g.column("edge_to")
        # Only once: penalties only raise costs, so it stays a true lower bound for the alternatives.
        left = np.zeros(g.node_count)
        marks = np.zeros(g.node_count, dtype=np.uint8)
        radius = kernel.reverse_bound(
            end,
            edge_to[starts],
            BOUND_REACH,
            costs.edges,
            g.in_edges.column_ptr(),
            g.in_edges.column_edges(),
            g.column("edge_from"),
            left,
            marks,
        )
        searched = costs.edges  # with the penalties, a copy, once there are any
        found: list[RoutedPath] = []
        for _ in range(searches):
            path, codes, taken, _opened = kernel.search(
                starts,
                end,
                searched,
                step_costs,
                edge_to,
                table.ptr,
                table.first,
                table.second,
                table.maneuver_first,
                table.maneuver_second,
                zones,
                left,
                marks,
                radius,
                ALTERNATIVE_SEARCH_GREED if found else 1.0,
                np.zeros(g.edge_count),
                np.zeros(g.edge_count, dtype=np.int32),
                np.zeros(g.edge_count, dtype=np.int32),
            )
            if path.size == 0:
                break
            edges = path.tolist()
            maneuvers = [kernel.MANEUVERS[code] for code in codes.tolist()]
            cost = float(kernel.path_cost(edges[0], taken, costs.edges, step_costs, table.first, table.second, zones))
            if found and cost > found[0].cost * MAX_COST_RATIO:
                break
            first = not found
            if first or (
                all(self._shared(edges, path.edges) <= MAX_OVERLAP for path in found)
                and all(self._overlap(edges, offered) <= MAX_OVERLAP for offered in on_offer)
            ):
                found.append(RoutedPath(tuple(edges), tuple(maneuvers), cost))
                if len(found) == count:
                    break
            # Push the next search off this route, and after the best design, off the routes on offer.
            if first:
                searched = costs.edges.copy()
            for used in [edges, *on_offer] if first else [edges]:
                np.multiply.at(searched, np.fromiter(used, dtype=np.int64), ALTERNATIVE_PENALTY)
        return found

    def _moves(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        self._steps()
        return self._moves_table

    def _steps(self) -> kernel.Steps:
        """Every legal turn on the map, classified and costed as far as a profile doesn't matter: made once."""
        if self._steps_table is None:
            with self._build_lock:
                if self._steps_table is None:
                    self._steps_table = self._build_steps()
        return self._steps_table

    def _build_steps(self) -> kernel.Steps:
        g = self.graph
        side = self.context.driving_side
        banned_ptr = np.zeros(g.edge_count + 1, dtype=np.int64)
        banned: list[int] = []
        for edge, others in sorted(g.banned.items()):
            banned_ptr[edge + 1] = len(others)
            banned.extend(sorted(others))
        roads = kernel.Roads(
            g.column("edge_from"),
            g.column("edge_to"),
            g.out_edges.column_ptr(),
            g.out_edges.column_edges(),
            np.cumsum(banned_ptr),
            np.array(banned, dtype=np.int32),
            g.column("bearing_start"),
            g.column("bearing_end"),
            self._link,
            self._freeway,
            g.column("way_id"),
            g.column("name"),
            g.column("speed_kmh"),
            g.node_kinds(),  # the same junction judgement the scorer uses
        )
        rules = kernel.TurnRules(
            np.array([name in CROSSING_MANEUVERS[side] for name in kernel.MANEUVERS], dtype=np.bool_),
            np.array([name == CROSSING_RAMPS[side] for name in kernel.MANEUVERS], dtype=np.bool_),
            HIGHWAY_SPEED_KMH,
            kernel.MANEUVER_CODES[self._uturn],
            UTURN_MIN_DEG,
        )
        self._moves_table = kernel.build_moves(roads, rules)
        crashes = self._node_crashes()
        return kernel.Steps(
            *kernel.build_steps(
                *self._moves_table,
                roads,
                rules,
                self._connector,
                self._signal_node,
                crashes >= self.context.min_hotspot_crashes,
                self._cut_through,
                self._crossing_columns(),
                crashes >= self._crash_spot_min,
                self._with_crossings,
            )
        )

    def _crossing_columns(self) -> kernel.Crossings:
        g = self.graph
        return kernel.Crossings(
            g.column("edge_to"),
            g.out_edges.column_ptr(),
            g.out_edges.column_edges(),
            g.in_edges.column_ptr(),
            g.in_edges.column_edges(),
            g.column("way_id"),
            g.column("name"),
            g.column("cross"),
            self._signal_node,
            np.array([name.startswith(LEFT_TURNS) for name in kernel.MANEUVERS], dtype=np.bool_),
            np.array([name.startswith(RIGHT_TURNS) for name in kernel.MANEUVERS], dtype=np.bool_),
        )

    def _step_costs(self, costs: _Costs) -> np.ndarray:
        """What each step on the map costs for this trip's traveler, before the segments it enters: kept for the next trips."""
        kept = self._kept_step_costs.get(costs.key)
        if kept is None:
            kept = self._build_step_costs(costs)
            if len(self._kept_step_costs) >= EDGE_COSTS_KEPT:
                self._kept_step_costs.pop(next(iter(self._kept_step_costs)), None)
            self._kept_step_costs[costs.key] = kept
        return kept

    def _build_step_costs(self, costs: _Costs) -> np.ndarray:
        steps = self._steps()
        profile = costs.profile
        table = np.zeros(kernel.TURN_TABLE_SIZE)
        for (maneuver, crossing), cost in costs.table.items():
            code = kernel.MANEUVER_CODES.get(maneuver)
            if code is not None:  # the router makes no roundabouts: nothing's filed under them
                table[code * 2 + crossing] = cost
        return (
            table[steps.turn_first]
            + table[steps.turn_second]
            + steps.busy_plain * profile.busy_crossing_weight
            + steps.busy_signal * (profile.busy_crossing_weight * SIGNALED_CROSSING_SHARE)
            + steps.hotspots * profile.hotspot_crossing_weight
            + steps.cut_through * CUT_THROUGH_PENALTY
        )

    def _zone_table(self, costs: _Costs) -> kernel.Zones:
        """The trip's zones, for the search. Few segments are in a lane closure or a hazard, and a trip without any has none."""
        g = self.graph
        index, ptr, ids = _EMPTY_INT32, _EMPTY_INT64, _EMPTY_INT32
        if costs.zones:
            index = np.zeros(g.edge_count, dtype=np.int32)
            ends = [0]
            numbers: list[int] = []
            for row, (edge, zones) in enumerate(costs.zones.items(), 1):
                index[edge] = row
                numbers.extend(zones)
                ends.append(len(numbers))
            ptr = np.array(ends, dtype=np.int64)
            ids = np.array(numbers, dtype=np.int32)
        cost = np.zeros(max(costs.zone_costs, default=-1) + 1)
        for zone, value in costs.zone_costs.items():
            cost[zone] = value
        return kernel.Zones(index, ptr, ids, cost, g.column("school_zone"), float(costs.school_cost))

    def moves(self, edge: int) -> list[Move]:
        """Legal next segments from the end of edge, with how each turn is classified."""
        ptr, following, codes, flags = self._moves()
        return [
            (int(following[i]), kernel.MANEUVERS[codes[i]], bool(flags[i] & 1), bool(flags[i] & 2))
            for i in range(ptr[edge], ptr[edge + 1])
        ]

    def steps(self, edge: int) -> list[Step]:
        """Ways on from the end of edge, each costed as one decision.

        Usually one segment. Through a short connector it's two, judged
        together (see CONNECTOR_MAX_M).
        """
        table = self._steps()
        return [self._step(edge, s) for s in range(table.ptr[edge], table.ptr[edge + 1]) if table.second[s] != kernel.ARRIVAL]

    def _step(self, edge: int, s: int) -> Step:
        g, table = self.graph, self._steps()
        first, second = int(table.first[s]), int(table.second[s])
        flags = table.flags[s]
        maneuver = kernel.MANEUVERS[table.maneuver_first[s]]
        node = g.edge_to[edge]
        turn_maneuver = kernel.MANEUVERS[table.turn_first[s] // 2]
        turns = [(node, turn_maneuver, bool(flags & kernel.FIRST_CROSSES), bool(flags & kernel.FIRST_BUSY))]
        if second < 0:
            return Step((first,), (maneuver,), tuple(turns))
        if table.turn_second[s] != kernel.NO_TURN:
            turns.append(
                (
                    g.edge_to[first],
                    kernel.MANEUVERS[table.turn_second[s] // 2],
                    bool(flags & kernel.SECOND_CROSSES),
                    bool(flags & kernel.SECOND_BUSY),
                )
            )
        return Step((first, second), (maneuver, kernel.MANEUVERS[table.maneuver_second[s]]), tuple(turns))

    @property
    def _uturn(self) -> str:
        return "UTURN_LEFT" if self.context.driving_side is DrivingSide.RIGHT else "UTURN_RIGHT"

    def _node_crashes(self) -> np.ndarray:
        """Recorded crashes within HOTSPOT_RADIUS_M of each junction, from the crashes' side: there are far fewer of them."""
        if self._crashes is None:
            g = self.graph
            crashes = np.zeros(g.node_count)
            if self.context.hotspots is not None:
                for spot, count in self.context.hotspots.spots():
                    for node in g._nodes_near(spot, HOTSPOT_RADIUS_M, snappable_only=False):
                        if haversine_m(g.point(node), spot) <= HOTSPOT_RADIUS_M:
                            crashes[node] += count
            self._crashes = crashes
        return self._crashes

    def crashes_at(self, node: int) -> float:
        return float(self._node_crashes()[node])

    def path_points(self, path: RoutedPath) -> list[Point]:
        return self._points(path.edges)

    def to_route_json(self, path: RoutedPath) -> dict[str, Any]:
        """The path in the Routes API response shape, so features and scoring treat it like any route.

        Times are the graph's no-traffic estimates.
        """
        g = self.graph
        groups: list[tuple[str, list[int]]] = []
        for edge, maneuver in zip(path.edges, path.maneuvers, strict=True):
            if not groups or maneuver != "STRAIGHT":
                groups.append((maneuver, []))
            groups[-1][1].append(edge)

        steps = [
            {
                "distanceMeters": round(sum(g.length_m[e] for e in edges)),
                "staticDuration": f"{round(sum(g.time_s[e] for e in edges))}s",
                "polyline": {"encodedPolyline": encode_polyline(self._points(edges))},
                "navigationInstruction": {"maneuver": maneuver},
            }
            for maneuver, edges in groups
        ]

        road_lengths: Counter[int] = Counter()
        for edge in path.edges:
            road_lengths[g.name[edge]] += g.length_m[edge]
        named = [(length, name) for name, length in road_lengths.items() if name != 0]
        seconds = round(sum(g.time_s[e] for e in path.edges))
        encoded = encode_polyline(self.path_points(path))
        if len(g.known_road_lengths) > KNOWN_ROUTES_KEPT:
            g.known_road_lengths.clear()
        g.known_road_lengths[encoded] = {g.names[name]: length for length, name in named}
        return {
            "distanceMeters": round(sum(g.length_m[e] for e in path.edges)),
            "duration": f"{seconds}s",
            "staticDuration": f"{seconds}s",
            "description": g.names[max(named)[1]] if named else "",
            "routeLabels": [],
            "polyline": {"encodedPolyline": encoded},
            "legs": [{"steps": steps}],
        }

    def _departures(self, origin: Point, heading: float | None) -> list[int]:
        """Segments a trip may begin with; empty when the origin is off the graph.

        A moving car begins on the segment it's driving along. If none matches,
        or the car isn't moving, the trip begins at the nearest junction.
        """
        g = self.graph
        if heading is not None:
            under = g.segment_under(origin, heading)
            if under is not None:
                return [under]
        start = g.nearest_node(origin, MAX_SNAP_M)
        if start is None:
            return []
        edges = g.out_edges[start]
        if heading is None:
            return edges
        ahead = [edge for edge in edges if angle_between_deg(g.bearing_start[edge], heading) <= MAX_START_TURN_DEG]
        return ahead or edges

    def event_edges(self, event: RoadEvent) -> frozenset[int]:
        """The segments a road event lies on, remembered by place: the same stretches come back trip after trip."""
        key = event.geometry_key()
        edges = self._event_edges.get(key)
        if edges is None:
            if len(self._event_edges) > 10_000:
                self._event_edges.clear()
            edges = frozenset(self.graph.event_edges(event))
            self._event_edges[key] = edges
        return edges

    def _trip_costs(
        self, profile: Any, events: Sequence[RoadEvent], *, keep: Iterable[int], school_hours: bool, sun: Sun | None
    ) -> _Costs:
        """What a trip costs to search: a driver's, from their profile. The bike router (app.bike) costs it its own way."""
        return self._costs(design_profile(profile), events, keep=keep, school_hours=school_hours, sun=sun)

    def _costs(
        self,
        profile: ScoringProfile,
        events: Sequence[RoadEvent],
        *,
        keep: Iterable[int],
        school_hours: bool = False,
        sun: Sun | None = None,
    ) -> _Costs:
        """What every segment and turn costs this trip, with its road events (and school zones, in school hours) applied.

        Segments in keep (where the car is) are never closed off: it's already there.
        With the sun low, segments heading toward it cost sun_glare_weight per minute more.
        """
        g = self.graph
        edge_costs = self._kept_edge_costs(profile).copy()  # a copy: this trip's zones and events change it
        zones: dict[int, set[int]] = {}
        zone_costs: dict[int, float] = {}
        if school_hours:
            in_zone = g.column("school_zone") != 0
            edge_costs[in_zone] *= np.maximum(1.0, g.column("speed_kmh")[in_zone] / SCHOOL_ZONE_KMH)
        if low_sun(sun) and profile.sun_glare_weight:
            # app.sun.glare, for every segment of the map at once.
            azimuth, per_s = sun[0], profile.sun_glare_weight / 60
            into_sun = np.abs((self._edge_bearings() - azimuth + 180) % 360 - 180) <= GLARE_ANGLE_DEG
            edge_costs[into_sun] += per_s * g.column("time_s")[into_sun]
        keep = set(keep)
        for number, event in enumerate(events):
            edges = self.event_edges(event)
            slowdown = self._slowdown(event, edges) if event.kind in ("jam", "slow") else 1.0
            for edge in edges:
                if event.kind == "closed":
                    if edge not in keep:
                        edge_costs[edge] = math.inf
                elif event.kind == "lanes":
                    # Slower by the reduced speed limit if there is one (the expected speed is
                    # below the posted one, hence the factor), and by LANE_CLOSURE_FACTOR at least.
                    slower = LANE_CLOSURE_FACTOR
                    if event.speed_limit_kmh:
                        slower = max(slower, g.speed_kmh[edge] / (event.speed_limit_kmh * DEFAULT_SPEED_FACTOR))
                    edge_costs[edge] *= slower
                    zones.setdefault(edge, set()).add(number)
                    zone_costs[number] = profile.lane_closure_weight * (
                        WORKERS_PRESENT_FACTOR if event.workers else 1.0
                    )
                elif event.kind == "hazard":
                    # Fog, ice, flooding: driven slower, and entering it costs weather_hazard_weight once.
                    edge_costs[edge] *= WEATHER_FACTOR
                    zones.setdefault(edge, set()).add(number)
                    zone_costs[number] = profile.weather_hazard_weight
                else:
                    edge_costs[edge] *= slowdown
        return _Costs(
            profile=profile,
            edges=edge_costs,
            table=self._turn_table(profile),
            zones={edge: frozenset(numbers) for edge, numbers in zones.items()},
            zone_costs=zone_costs,
            school_cost=profile.school_zone_weight if school_hours else 0.0,
            key=profile.model_dump_json(),
        )

    def _edge_bearings(self) -> np.ndarray:
        """Each segment's compass direction, end to end, worked out once."""
        if self._bearings is None:
            g = self.graph
            lat, lng, start, end = g.column("lat"), g.column("lng"), g.column("edge_from"), g.column("edge_to")
            self._bearings = initial_bearings_deg(lat[start], lng[start], lat[end], lng[end])
        return self._bearings

    def _slowdown(self, event: RoadEvent, edges: Iterable[int]) -> float:
        """How many times its usual time a slow or jammed stretch takes.

        Measured where the source says (511 travel times); from the delay over
        the stretch's usual time where it gives that (TomTom's jams); typical
        otherwise (JAM_FACTOR, SLOW_FACTOR).
        """
        if event.slowdown:
            return event.slowdown
        usual_s = sum(self.graph.time_s[edge] for edge in edges)
        if event.delay_min > 0 and usual_s > 0:
            return min(1 + event.delay_min * 60 / usual_s, MAX_SLOWDOWN)
        return JAM_FACTOR if event.kind == "jam" else SLOW_FACTOR

    def _arrivals(self, edge: int, end: int) -> list[Step]:
        """A short connector that ends at the destination: a way to arrive, not part of a turn."""
        g, table = self.graph, self._steps()
        return [
            self._step(edge, s)
            for s in range(table.ptr[edge], table.ptr[edge + 1])
            if table.second[s] == kernel.ARRIVAL and g.edge_to[table.first[s]] == end
        ]

    def _step_cost(self, edge: int, step: Step, costs: _Costs, penalties: dict[int, float]) -> float:
        """A step's cost worked out from its parts: the arrays the search reads are checked against this."""
        cost, previous = 0.0, edge
        school = self.graph.school_zone
        for nxt in step.edges:
            cost += costs.edges[nxt] * penalties.get(nxt, 1.0) + self._cut_through_cost(previous, nxt)
            if costs.school_cost and school[nxt] and not school[previous]:
                cost += costs.school_cost
            entered = costs.zones.get(nxt)
            if entered:
                # Driving into a lane closure (a forced merge) or a school zone costs once per zone, like a risky turn.
                newly = entered - costs.zones.get(previous, frozenset())
                cost += sum(costs.zone_costs[zone] for zone in newly)
            previous = nxt
        for node, maneuver, crossing, busy in step.turns:
            cost += self._turn_cost(costs.table, costs.profile, node, maneuver, crossing, busy)
        return cost

    def _kept_edge_costs(self, profile: ScoringProfile) -> np.ndarray:
        """_edge_costs, a pass over every segment of the map, made once per profile."""
        key = profile.model_dump_json()
        kept = self._kept_costs.get(key)
        if kept is None:
            kept = self._edge_costs(profile)
            if len(self._kept_costs) >= EDGE_COSTS_KEPT:
                self._kept_costs.pop(next(iter(self._kept_costs)), None)
            self._kept_costs[key] = kept
        return kept

    def _edge_costs(self, profile: ScoringProfile) -> np.ndarray:
        g = self.graph
        length, speed = g.column("length_m"), g.column("speed_kmh")
        minutes = g.column("time_s") / 60
        fuel = cruise_liters_each(length, speed) + g.column("signals") * SIGNAL_L
        costs = profile.time_weight * minutes + profile.distance_weight * length / 1000 + profile.fuel_weight * fuel
        highway = speed >= HIGHWAY_SPEED_KMH
        costs[highway] += profile.highway_weight * minutes[highway]
        winding = ~highway & (g.column("winding") != 0)
        costs[winding] -= profile.scenic_weight * minutes[winding]  # never negative: scenic_weight <= time_weight
        rails = g.column("rail_crossings")
        costs += rails * (profile.time_weight * RAIL_CROSSING_S / 60 + profile.rail_crossing_weight)
        return costs

    @staticmethod
    def _turn_table(profile: ScoringProfile) -> dict[tuple[str, bool], float]:
        return {
            (maneuver, crossing): profile.time_weight * turn_time_s(maneuver, crossing) / 60
            + ((profile.maneuver_weight + profile.fuel_weight * TURN_L) if maneuver in DECISION_MANEUVERS else 0.0)
            for maneuver in _ALL_MANEUVERS
            for crossing in (False, True)
        }

    def _turn_cost(
        self,
        table: dict[tuple[str, bool], float],
        profile: ScoringProfile,
        node: int,
        maneuver: str,
        crossing: bool,
        busy: bool,
    ) -> float:
        cost = table[maneuver, crossing]
        if crossing:
            if busy:
                cost += profile.busy_crossing_weight * (SIGNALED_CROSSING_SHARE if self._signal_node[node] else 1.0)
            if self.crashes_at(node) >= self.context.min_hotspot_crashes:
                cost += profile.hotspot_crossing_weight
        return cost

    def _path_cost(self, edges: list[int], costs: _Costs) -> float:
        cost = costs.edges[edges[0]]
        index = 0
        while index + 1 < len(edges):
            step = self._step_from(edges, index)
            cost += self._step_cost(edges[index], step, costs, {})
            index += len(step.edges)
        return cost

    def _step_from(self, edges: Sequence[int], index: int) -> Step:
        """The step a found path takes after edges[index]."""
        ahead = tuple(edges[index + 1 : index + 3])
        for step in self.steps(edges[index]):
            if step.edges == ahead[: len(step.edges)]:
                return step
        (arrival,) = [step for step in self._arrivals(edges[index], self.graph.edge_to[ahead[0]]) if step.edges == ahead[:1]]
        return arrival

    def _cut_through_cost(self, edge: int, nxt: int) -> float:
        return CUT_THROUGH_PENALTY if self._cut_through[nxt] and not self._cut_through[edge] else 0.0

    def _shared(self, edges: Sequence[int], other: Sequence[int]) -> float:
        """Overlap either way: a long route containing all of a short one counts as shared too."""
        return max(self._overlap(edges, other), self._overlap(other, edges))

    def _overlap(self, edges: Iterable[int], other: Iterable[int]) -> float:
        """Share of edges' length that also lies on other."""
        edges = list(edges)
        shared = set(other)
        total = sum(self.graph.length_m[e] for e in edges)
        return sum(self.graph.length_m[e] for e in edges if e in shared) / total if total else 1.0

    def _points(self, edges: Iterable[int]) -> list[Point]:
        return self.graph.path_points(list(edges))

