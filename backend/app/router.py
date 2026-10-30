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
Google for it.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.driving_side import DrivingSide
from app.features import (
    DECISION_MANEUVERS,
    HIGHWAY_SPEED_KMH,
    HOTSPOT_RADIUS_M,
    NO_ONCOMING_KINDS,
    TurnContext,
    crosses_traffic,
)
from app.fuel import MIN_L_PER_KM, SIGNAL_L, TURN_L, cruise_liters
from app.geo import Point, angle_between_deg, encode_polyline, haversine_m
from app.models import ScoringProfile
from app.road_events import RoadEvent
from app.roadgraph import RoadGraph

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
# Searches for alternatives inflate the A* estimate by this factor: much
# faster on penalized costs, and an alternative needs to be good and
# different, not provably the best. The first design's search stays exact.
ALTERNATIVE_SEARCH_GREED = 1.5
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
# its free-flow time, a slow one or one with a lane closed less so. Driving
# into a lane closure also costs lane_closure_weight once: a forced merge.
JAM_FACTOR = 3.0
SLOW_FACTOR = 1.5
LANE_CLOSURE_FACTOR = 1.5

_ALL_MANEUVERS = DECISION_MANEUVERS | {"STRAIGHT"}

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
    edges: list[float]
    table: dict[tuple[str, bool], float]
    lane_closures: dict[int, frozenset[int]]


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


class Router:
    def __init__(self, graph: RoadGraph, context: TurnContext = TurnContext()) -> None:
        self.graph = graph
        self.context = context

        def flags(names: frozenset[str]) -> list[bool]:
            by_class = [name in names for name in graph.classes]
            return [by_class[c] for c in graph.road_class]

        self._link = flags(LINK_CLASSES)
        self._freeway = flags(FREEWAY_CLASSES)
        self._cut_through = flags(CUT_THROUGH_CLASSES)
        self._connector = [length < CONNECTOR_MAX_M for length in graph.length_m]
        self._moves: dict[int, list[Move]] = {}
        self._steps: dict[int, list[Step]] = {}
        self._crashes: dict[int, float] = {}
        self._event_edges: dict[RoadEvent, frozenset[int]] = {}

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
        """
        g = self.graph
        departures = self._departures(origin, heading)
        end = g.nearest_node(destination, MAX_SNAP_M)
        if not departures or end is None:
            which = "start" if not departures else "end"
            raise OutsideGraphError(f"trip {which} is more than {MAX_SNAP_M:.0f} m from the {g.area} road graph")
        if heading is None and g.edge_from[departures[0]] == end:
            return []

        costs = self._costs(design_profile(profile), events, keep=departures)
        on_offer = [edges for edges in (g.edges_along(points) for points in avoid) if edges]
        penalties: dict[int, float] = {}
        found: list[RoutedPath] = []
        for _ in range(count * ALTERNATIVE_SEARCHES_PER_ROUTE):
            greed = ALTERNATIVE_SEARCH_GREED if found else 1.0
            result = self._search(departures, end, costs, penalties, greed=greed)
            if result is None:
                break
            edges, maneuvers = result
            cost = self._path_cost(edges, costs)
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
            for used in [edges, *on_offer] if first else [edges]:
                for edge in used:
                    penalties[edge] = penalties.get(edge, 1.0) * ALTERNATIVE_PENALTY
        return found

    def moves(self, edge: int) -> list[Move]:
        """Legal next segments from the end of edge, with how each turn is classified."""
        moves = self._moves.get(edge)
        if moves is None:
            g = self.graph
            node = g.edge_to[edge]
            kind = g.node_kind(node)  # the same junction judgement the scorer uses
            outs = g.out_edges[node]
            banned = g.banned.get(edge, frozenset())
            moves = []
            for nxt in outs:
                if nxt in banned:
                    continue
                if g.edge_to[nxt] == g.edge_from[edge] and len(outs) > 1:
                    continue  # U-turns only at dead ends
                maneuver = self.maneuver(edge, nxt)
                # Interchanges and all-one-way junctions have no oncoming stream to cross.
                crossing = kind not in NO_ONCOMING_KINDS and crosses_traffic(
                    maneuver, g.speed_kmh[edge], self.context.driving_side
                )
                moves.append((nxt, maneuver, crossing, kind == "busy"))
            self._moves[edge] = moves
        return moves

    def steps(self, edge: int) -> list[Step]:
        """Ways on from the end of edge, each costed as one decision.

        Usually one segment. Through a short connector it's two, judged
        together (see CONNECTOR_MAX_M).
        """
        steps = self._steps.get(edge)
        if steps is None:
            g = self.graph
            steps = []
            for nxt, maneuver, crossing, busy in self.moves(edge):
                turn = (g.edge_to[edge], maneuver, crossing, busy)
                if not self._connector[nxt]:
                    steps.append(Step((nxt,), (maneuver,), (turn,)))
                    continue
                for after, maneuver_after, crossing_after, busy_after in self.moves(nxt):
                    if angle_between_deg(g.bearing_end[edge], g.bearing_start[after]) >= UTURN_MIN_DEG:
                        steps.append(Step((nxt, after), (self._uturn, "STRAIGHT"), (self._uturn_at(edge),)))
                    else:
                        turn_after = (g.edge_to[nxt], maneuver_after, crossing_after, busy_after)
                        steps.append(Step((nxt, after), (maneuver, maneuver_after), (turn, turn_after)))
            self._steps[edge] = steps
        return steps

    @property
    def _uturn(self) -> str:
        return "UTURN_LEFT" if self.context.driving_side is DrivingSide.RIGHT else "UTURN_RIGHT"

    def _uturn_at(self, edge: int) -> Turn:
        """A U-turn through a connector at the end of edge, judged like any turn at that junction."""
        g = self.graph
        node = g.edge_to[edge]
        kind = g.node_kind(node)
        crossing = kind not in NO_ONCOMING_KINDS and crosses_traffic(
            self._uturn, g.speed_kmh[edge], self.context.driving_side
        )
        return (node, self._uturn, crossing, kind == "busy")

    def maneuver(self, edge: int, nxt: int) -> str:
        """Classify the move from edge onto nxt using the Routes API's maneuver names."""
        g = self.graph
        uturn = self._uturn
        if g.edge_to[nxt] == g.edge_from[edge]:
            return uturn
        angle = (g.bearing_start[nxt] - g.bearing_end[edge] + 540.0) % 360.0 - 180.0  # positive turns right
        side = "RIGHT" if angle > 0 else "LEFT"
        turn = abs(angle)

        if self._link[nxt] and not self._link[edge]:
            return f"RAMP_{side}" if turn > 10 else "STRAIGHT"
        if self._link[edge] and self._freeway[nxt]:
            return "MERGE"
        if (self._link[edge] or self._freeway[edge]) and (self._link[nxt] or self._freeway[nxt]):
            return f"FORK_{side}" if turn > 10 else "STRAIGHT"

        same_road = g.way_id[edge] == g.way_id[nxt] or (g.name[edge] != 0 and g.name[edge] == g.name[nxt])
        if turn <= 30 or (same_road and turn < 60):
            return "STRAIGHT"
        if turn >= 165:
            return uturn
        if turn < 60:
            return f"TURN_SLIGHT_{side}"
        return f"TURN_SHARP_{side}" if turn > 120 else f"TURN_{side}"

    def crashes_at(self, node: int) -> float:
        hotspots = self.context.hotspots
        if hotspots is None:
            return 0.0
        crashes = self._crashes.get(node)
        if crashes is None:
            crashes = self._crashes[node] = hotspots.crashes_near(self.graph.point(node), HOTSPOT_RADIUS_M)
        return crashes

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
        return {
            "distanceMeters": round(sum(g.length_m[e] for e in path.edges)),
            "duration": f"{seconds}s",
            "staticDuration": f"{seconds}s",
            "description": g.names[max(named)[1]] if named else "",
            "routeLabels": [],
            "polyline": {"encodedPolyline": encode_polyline(self.path_points(path))},
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
        """The segments a road event lies on, remembered: the same events come back trip after trip."""
        edges = self._event_edges.get(event)
        if edges is None:
            if len(self._event_edges) > 10_000:
                self._event_edges.clear()
            edges = frozenset(self.graph.event_edges(event))
            self._event_edges[event] = edges
        return edges

    def _costs(self, profile: ScoringProfile, events: Sequence[RoadEvent], *, keep: Iterable[int]) -> _Costs:
        """What every segment and turn costs this trip, with its road events applied.

        Segments in keep (where the car is) are never closed off: it's already there.
        """
        edge_costs = self._edge_costs(profile)
        lane_closures: dict[int, set[int]] = {}
        keep = set(keep)
        for number, event in enumerate(events):
            for edge in self.event_edges(event):
                if event.kind == "closed":
                    if edge not in keep:
                        edge_costs[edge] = math.inf
                elif event.kind == "lanes":
                    edge_costs[edge] *= LANE_CLOSURE_FACTOR
                    lane_closures.setdefault(edge, set()).add(number)
                else:
                    edge_costs[edge] *= JAM_FACTOR if event.kind == "jam" else SLOW_FACTOR
        return _Costs(
            profile=profile,
            edges=edge_costs,
            table=self._turn_table(profile),
            lane_closures={edge: frozenset(numbers) for edge, numbers in lane_closures.items()},
        )

    def _search(
        self,
        departures: list[int],
        end: int,
        costs: _Costs,
        penalties: dict[int, float],
        *,
        greed: float = 1.0,
    ) -> tuple[list[int], list[str]] | None:
        """A* over segments. With greed 1 the heuristic is admissible: no route can cost less per km than per_km.

        greed > 1 trades optimality for speed (weighted A*): the route found
        costs at most greed times the best.
        """
        g = self.graph
        goal = g.point(end)
        profile = costs.profile
        per_km = greed * (
            profile.distance_weight
            + profile.fuel_weight * MIN_L_PER_KM
            + (profile.time_weight - profile.scenic_weight) * 60.0 / g.max_speed_kmh
        )

        best: dict[int, float] = {}
        parent: dict[int, tuple[int, Step | None]] = {}
        heap: list[tuple[float, float, int]] = []
        for edge in departures:
            cost = costs.edges[edge] * penalties.get(edge, 1.0)
            best[edge] = cost
            parent[edge] = (-1, None)
            heapq.heappush(heap, (cost + per_km * haversine_m(g.point(g.edge_to[edge]), goal) / 1000, cost, edge))

        while heap:
            _, cost, edge = heapq.heappop(heap)
            if cost > best[edge]:
                continue  # stale heap entry
            if g.edge_to[edge] == end:
                return self._unwind(edge, parent)
            for step in [*self.steps(edge), *self._arrivals(edge, end)]:
                new = cost + self._step_cost(edge, step, costs, penalties)
                last = step.edges[-1]
                if new < best.get(last, math.inf):
                    best[last] = new
                    parent[last] = (edge, step)
                    heapq.heappush(heap, (new + per_km * haversine_m(g.point(g.edge_to[last]), goal) / 1000, new, last))
        return None

    def _arrivals(self, edge: int, end: int) -> list[Step]:
        """A short connector that ends at the destination: a way to arrive, not part of a turn."""
        g = self.graph
        return [
            Step((nxt,), (maneuver,), ((g.edge_to[edge], maneuver, crossing, busy),))
            for nxt, maneuver, crossing, busy in self.moves(edge)
            if self._connector[nxt] and g.edge_to[nxt] == end
        ]

    def _step_cost(self, edge: int, step: Step, costs: _Costs, penalties: dict[int, float]) -> float:
        cost, previous = 0.0, edge
        for nxt in step.edges:
            cost += costs.edges[nxt] * penalties.get(nxt, 1.0) + self._cut_through_cost(previous, nxt)
            entered = costs.lane_closures.get(nxt)
            if entered:
                # Driving into a lane closure means a forced merge: as risky as a busy turn, once per closure.
                newly = entered - costs.lane_closures.get(previous, frozenset())
                cost += costs.profile.lane_closure_weight * len(newly)
            previous = nxt
        for node, maneuver, crossing, busy in step.turns:
            cost += self._turn_cost(costs.table, costs.profile, node, maneuver, crossing, busy)
        return cost

    def _edge_costs(self, profile: ScoringProfile) -> list[float]:
        g = self.graph
        costs = []
        for edge in range(g.edge_count):
            minutes = g.time_s[edge] / 60
            fuel = cruise_liters(g.length_m[edge], g.speed_kmh[edge]) + g.signals[edge] * SIGNAL_L
            cost = (
                profile.time_weight * minutes
                + profile.distance_weight * g.length_m[edge] / 1000
                + profile.fuel_weight * fuel
            )
            if g.speed_kmh[edge] >= HIGHWAY_SPEED_KMH:
                cost += profile.highway_weight * minutes
            elif g.winding[edge]:
                cost -= profile.scenic_weight * minutes  # never negative: scenic_weight <= time_weight
            costs.append(cost)
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
                cost += profile.busy_crossing_weight
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
        points: list[Point] = []
        for edge in edges:
            segment = self.graph.edge_points(edge)
            points.extend(segment if not points else segment[1:])
        return points

    @staticmethod
    def _unwind(edge: int, parent: dict[int, tuple[int, Step | None]]) -> tuple[list[int], list[str]]:
        edges: list[int] = []
        maneuvers: list[str] = []
        while True:
            previous, step = parent[edge]
            if step is None:  # a departure
                edges.append(edge)
                maneuvers.append("DEPART")
                return edges[::-1], maneuvers[::-1]
            edges.extend(reversed(step.edges))
            maneuvers.extend(reversed(step.maneuvers))
            edge = previous
