"""The engine's own router: personalized shortest paths on the OSM road graph.

Costs are the same perceived minutes the scorer uses, applied per road
segment and per turn instead of per whole route:

* a segment costs its expected no-traffic time and distance, plus the
  highway penalty or minus the winding-road refund, weighted by the profile;
* a turn costs its physical time plus the maneuver weight, and a turn across
  oncoming traffic adds the busy and crash-prone penalties.

The search runs over segments rather than junctions (a state is "arrived at
junction v along segment e"), which is what makes turn costs and turn
restrictions possible. Live traffic isn't in the graph; the pricing step asks
Google for it.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.driving_side import DrivingSide
from app.features import DECISION_MANEUVERS, HIGHWAY_SPEED_KMH, HOTSPOT_RADIUS_M, TurnContext, crosses_traffic
from app.geo import Point, encode_polyline, haversine_m
from app.models import ScoringProfile
from app.roadgraph import RoadGraph

# A crossing turn is at a busy junction when either road is one of these.
BUSY_CLASSES = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary"})
LINK_CLASSES = frozenset({"motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"})
FREEWAY_CLASSES = frozenset({"motorway", "trunk"})

# Alternatives: after each route is found, its segments cost ALTERNATIVE_PENALTY
# times more in the next search. A new route is kept only if it shares at most
# MAX_OVERLAP of its length with each earlier one and its true cost is within
# MAX_COST_RATIO of the best route's.
ALTERNATIVE_PENALTY = 1.5
MAX_OVERLAP = 0.75
MAX_COST_RATIO = 1.35
MAX_SNAP_M = 1_000.0

_ALL_MANEUVERS = DECISION_MANEUVERS | {"STRAIGHT"}

Move = tuple[int, str, bool, bool]  # next segment, maneuver, crosses traffic, busy junction


class OutsideGraphError(LookupError):
    """The trip starts or ends too far from any road in the graph."""


@dataclass(frozen=True)
class RoutedPath:
    edges: tuple[int, ...]
    maneuvers: tuple[str, ...]  # how each segment is entered; the first is DEPART
    cost: float  # perceived minutes, without alternative-route penalties


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

        self._busy = flags(BUSY_CLASSES)
        self._link = flags(LINK_CLASSES)
        self._freeway = flags(FREEWAY_CLASSES)
        self._moves: dict[int, list[Move]] = {}
        self._crashes: dict[int, float] = {}

    def routes(self, origin: Point, destination: Point, profile: ScoringProfile, *, count: int = 3) -> list[RoutedPath]:
        """Up to count distinct routes for this profile, best first."""
        g = self.graph
        start = g.nearest_node(origin, MAX_SNAP_M)
        end = g.nearest_node(destination, MAX_SNAP_M)
        if start is None or end is None:
            which = "start" if start is None else "end"
            raise OutsideGraphError(f"trip {which} is more than {MAX_SNAP_M:.0f} m from the {g.area} road graph")
        if start == end:
            return []

        edge_costs = self._edge_costs(profile)
        table = self._turn_table(profile)
        penalties: dict[int, float] = {}
        found: list[RoutedPath] = []
        for _ in range(count * 2):
            result = self._search(start, end, profile, edge_costs, table, penalties)
            if result is None:
                break
            edges, maneuvers = result
            cost = self._path_cost(edges, profile, edge_costs, table)
            if found and cost > found[0].cost * MAX_COST_RATIO:
                break
            if all(self._overlap(edges, other.edges) <= MAX_OVERLAP for other in found):
                found.append(RoutedPath(tuple(edges), tuple(maneuvers), cost))
                if len(found) == count:
                    break
            for edge in edges:
                penalties[edge] = penalties.get(edge, 1.0) * ALTERNATIVE_PENALTY
        return found

    def moves(self, edge: int) -> list[Move]:
        """Legal next segments from the end of edge, with how each turn is classified."""
        moves = self._moves.get(edge)
        if moves is None:
            g = self.graph
            outs = g.out_edges[g.edge_to[edge]]
            banned = g.banned.get(edge, frozenset())
            moves = []
            for nxt in outs:
                if nxt in banned:
                    continue
                if g.edge_to[nxt] == g.edge_from[edge] and len(outs) > 1:
                    continue  # U-turns only at dead ends
                maneuver = self.maneuver(edge, nxt)
                # Between two one-way roads there is no oncoming stream to cross.
                crossing = crosses_traffic(maneuver, g.speed_kmh[edge], self.context.driving_side) and not (
                    g.oneway[edge] and g.oneway[nxt]
                )
                moves.append((nxt, maneuver, crossing, self._busy[edge] or self._busy[nxt]))
            self._moves[edge] = moves
        return moves

    def maneuver(self, edge: int, nxt: int) -> str:
        """Classify the move from edge onto nxt using the Routes API's maneuver names."""
        g = self.graph
        uturn = "UTURN_LEFT" if self.context.driving_side is DrivingSide.RIGHT else "UTURN_RIGHT"
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

    def _search(
        self,
        start: int,
        end: int,
        profile: ScoringProfile,
        edge_costs: list[float],
        table: dict[tuple[str, bool], float],
        penalties: dict[int, float],
    ) -> tuple[list[int], list[str]] | None:
        """A* over segments. The heuristic is admissible: no route can cost less per km than per_km."""
        g = self.graph
        goal = g.point(end)
        per_km = profile.distance_weight + (profile.time_weight - profile.scenic_weight) * 60.0 / g.max_speed_kmh

        best: dict[int, float] = {}
        parent: dict[int, tuple[int, str]] = {}
        heap: list[tuple[float, float, int]] = []
        for edge in g.out_edges[start]:
            cost = edge_costs[edge] * penalties.get(edge, 1.0)
            best[edge] = cost
            parent[edge] = (-1, "DEPART")
            heapq.heappush(heap, (cost + per_km * haversine_m(g.point(g.edge_to[edge]), goal) / 1000, cost, edge))

        while heap:
            _, cost, edge = heapq.heappop(heap)
            if cost > best[edge]:
                continue  # stale heap entry
            node = g.edge_to[edge]
            if node == end:
                return self._unwind(edge, parent)
            for nxt, maneuver, crossing, busy in self.moves(edge):
                new = (
                    cost
                    + edge_costs[nxt] * penalties.get(nxt, 1.0)
                    + self._turn_cost(table, profile, node, maneuver, crossing, busy)
                )
                if new < best.get(nxt, math.inf):
                    best[nxt] = new
                    parent[nxt] = (edge, maneuver)
                    heapq.heappush(heap, (new + per_km * haversine_m(g.point(g.edge_to[nxt]), goal) / 1000, new, nxt))
        return None

    def _edge_costs(self, profile: ScoringProfile) -> list[float]:
        g = self.graph
        costs = []
        for edge in range(g.edge_count):
            minutes = g.time_s[edge] / 60
            cost = profile.time_weight * minutes + profile.distance_weight * g.length_m[edge] / 1000
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
            + (profile.maneuver_weight if maneuver in DECISION_MANEUVERS else 0.0)
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

    def _path_cost(
        self, edges: list[int], profile: ScoringProfile, edge_costs: list[float], table: dict[tuple[str, bool], float]
    ) -> float:
        cost = edge_costs[edges[0]]
        for edge, nxt in zip(edges, edges[1:]):
            _, maneuver, crossing, busy = next(move for move in self.moves(edge) if move[0] == nxt)
            cost += edge_costs[nxt] + self._turn_cost(table, profile, self.graph.edge_to[edge], maneuver, crossing, busy)
        return cost

    def _overlap(self, edges: Iterable[int], other: Iterable[int]) -> float:
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
    def _unwind(edge: int, parent: dict[int, tuple[int, str]]) -> tuple[list[int], list[str]]:
        edges, maneuvers = [], []
        while edge != -1:
            previous, maneuver = parent[edge]
            edges.append(edge)
            maneuvers.append(maneuver)
            edge = previous
        return edges[::-1], maneuvers[::-1]
