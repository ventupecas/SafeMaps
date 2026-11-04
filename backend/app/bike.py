"""Bike and walking routes: the lowest-stress route a rider (or walker) is comfortable on, within a detour they accept.

The same router and plans serve the walking map (build_graph(mode="walk")): there, stress is walking
along a street (a sidewalk or a path is calm, a busy road without one is not), crossings are judged by
the traffic crossed (RoadGraph.cross), and the profiles are walkers' (WALK_PROFILES).

See BIKE_MODE.md for the reasoning. In short:

* The bike map (build_graph(mode="bike")) gives every segment a Level of Traffic Stress, 1 (anyone:
  trails, protected lanes, quiet streets) to 4 (only the most confident: fast, multi-lane traffic).
* The bike router searches it with each segment costing its riding time times a stress weight for
  its level, plus crossings: going straight across, or turning left across, a stress 3 or 4 road
  where no signal controls it. Bike crash spots, railroad tracks and closures cost too.
* Each route gets a safety score (100 with no risks, halved every 10 risk points), from its minutes
  at stress 3 and 4, its stressful crossings, crash spots, tracks and closures.
* The standard route to compare with is the shortest one (stress ignored). The recommended route is
  the safest within the rider's detour limit; ties go to the faster.

Riders pick a profile: everyday (most adults: "interested but concerned"), family (the quietest
streets, long detours accepted), confident (busier streets are fine) or e-bike (everyday, faster).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np

import app.search_kernel as kernel
from app.features import TurnContext, directions
from app.geo import encode_polyline
from app.kept import Kept
from app.models import LatLng, ScoringProfile
from app.road_events import RoadEvent
from app.roadgraph import BIKE_SPEED_KMH, WALK_SPEED_KMH, RoadGraph
from app.router import RoutedPath, Router, _Costs
from app.sun import DARK_TURN_FACTOR, Sun, dark


@dataclass(frozen=True)
class BikeProfile:
    name: str
    speed_kmh: float  # the rider's average
    stress_weights: tuple[float, float, float, float]  # a minute at stress 1..4 feels like this many
    crossing_minutes: tuple[float, float]  # crossing a stress 3 / 4 road without a signal feels like this many
    crash_minutes: float  # passing a spot where a cyclist was killed
    rail_minutes: float  # crossing railroad tracks (wheels catch at shallow angles)
    unlit_weight: float  # after dark, a minute on an unlit stretch feels like this many more
    max_detour_ratio: float  # at most this many times the shortest route's time ...
    max_extra_min: float  # ... and at most this many minutes more
    avoid_steps: bool = False  # walking with a stroller or a wheelchair


PROFILES = {
    "everyday": BikeProfile("everyday", 16.0, (1.0, 1.4, 3.0, 8.0), (3.0, 8.0), 5.0, 1.0, 0.5, 1.5, 15.0),
    "family": BikeProfile("family", 13.0, (1.0, 2.0, 8.0, 40.0), (8.0, 25.0), 10.0, 2.0, 1.0, 2.0, 25.0),
    "confident": BikeProfile("confident", 20.0, (1.0, 1.15, 1.6, 3.0), (1.0, 3.0), 3.0, 0.5, 0.3, 1.3, 8.0),
    "ebike": BikeProfile("ebike", 22.0, (1.0, 1.4, 3.0, 8.0), (3.0, 8.0), 5.0, 1.0, 0.5, 1.5, 15.0),
}
DEFAULT_PROFILE = "everyday"
# Walkers: most people; careful (children, older walkers: quiet streets, signals to cross); step-free
# (strollers, wheelchairs: no steps).
WALK_PROFILES = {
    "everyday": BikeProfile("everyday", 4.8, (1.0, 1.5, 3.0, 6.0), (2.0, 5.0), 5.0, 0.5, 0.5, 1.4, 10.0),
    "careful": BikeProfile("careful", 3.6, (1.0, 2.0, 6.0, 15.0), (5.0, 15.0), 10.0, 1.0, 1.0, 1.8, 15.0),
    "stepfree": BikeProfile("stepfree", 4.0, (1.0, 1.5, 3.0, 6.0), (2.0, 5.0), 5.0, 0.5, 0.5, 1.6, 12.0, avoid_steps=True),
}
MAP_SPEED_KMH = {"bike": BIKE_SPEED_KMH, "walk": WALK_SPEED_KMH}  # the speed each map's times are at
# The standard route: the shortest ride, stress ignored.
SHORTEST = BikeProfile("shortest", 16.0, (1.0, 1.0, 1.0, 1.0), (0.0, 0.0), 0.0, 0.0, 0.0, math.inf, math.inf)

# The safety score's risk points, the same for every rider so scores compare across routes and trips.
RISK_POINTS = {
    "stress_3_minutes": 0.5,  # per minute on a stress 3 street
    "stress_4_minutes": 1.5,  # per minute on a stress 4 street
    "busy_crossings": 2.0,  # per crossing of a stress 3 road without a signal; twice that for stress 4
    "crash_spots": 5.0,
    "rail_crossings": 1.0,
    "unlit_minutes": 0.3,  # per minute on an unlit stretch, after dark
    "lane_closures": 3.0,
    "closures": 30.0,
}
SAFETY_SCALE = 10.0
# A walk's alternatives are also looked for heading straight for the destination, estimating what's left at this
# many times the fastest walk there: routes the search for the cheapest passes over, and now and then a safer one.
DIRECT_WALK_GREED = 1.5
ROUTES_SHOWN = 4  # the standard route and three designs
BASE_COSTS_KEPT = 16  # segment costs kept per router, by traveler and dark: 10 MB each on Milwaukee's walking map
MIN_CRASHES = 1.0  # fatal cyclist crashes (FARS) are rare: one near a junction counts


@dataclass(frozen=True)
class _BikeTrip:
    profile: BikeProfile
    dark: bool


class BikeRouter(Router):
    """The car router's search, costed for a rider (see the module docstring)."""

    _with_crossings = True
    _crash_spot_min = MIN_CRASHES

    def __init__(self, graph: RoadGraph, context: TurnContext = TurnContext()) -> None:
        if graph.mode not in MAP_SPEED_KMH:
            raise ValueError("BikeRouter needs the bike or walking map (python -m app.build_graph <area> --bike or --walk)")
        super().__init__(graph, context)
        self._cut_through = np.zeros_like(self._cut_through)  # quiet service roads and alleys are welcome on a bike
        self._base_costs: Kept[np.ndarray] = Kept(BASE_COSTS_KEPT)
        self._crossings: kernel.Crossings | None = None

    def bike_routes(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: BikeProfile,
        *,
        count: int = 3,
        events: Sequence[RoadEvent] = (),
        sun: Sun | None = None,
        avoid: Sequence[Sequence[tuple[float, float]]] = (),
    ) -> list[RoutedPath]:
        return self.routes(origin, destination, profile, count=count, events=events, sun=sun, avoid=avoid)  # type: ignore[arg-type]

    def _trip_costs(self, profile, events, *, keep, school_hours=False, sun=None) -> _Costs:  # type: ignore[override]
        g = self.graph
        slower = MAP_SPEED_KMH[g.mode] / profile.speed_kmh  # the map's times are at its own speed
        weights = profile.stress_weights
        edges = self._base(profile, dark(sun)).copy()  # a copy: this trip's closures change it
        keep = set(keep)
        for event in events:
            for edge in self.event_edges(event):
                if event.kind == "closed" and edge not in keep:
                    edges[edge] = math.inf
                elif event.kind == "lanes":
                    # A lane closure squeezes the rider into traffic: one stress level worse.
                    level = max(1, g.stress[edge])
                    edges[edge] *= weights[min(4, level + 1) - 1] / weights[level - 1]
                elif event.kind == "hazard":
                    edges[edge] *= 2.0  # fog, ice, flooding: slower and riskier on a bike
        # What a turn takes and how it feels to a rider, whatever else is on the road.
        search = ScoringProfile(time_weight=slower, fuel_weight=0.0, maneuver_weight=0.3, busy_crossing_weight=0.0, hotspot_crossing_weight=0.0)
        return _Costs(
            profile=search,
            edges=edges,
            table=self._turn_table(search),
            zones={},
            zone_costs={},
            key=(profile, dark(sun)),
            extra=_BikeTrip(profile, dark(sun)),
        )

    def _base(self, profile: BikeProfile, night: bool) -> np.ndarray:
        """Each segment's cost for this traveler, before road events: built once (a pass over every
        segment of the map) and kept for the next trips."""
        key = (profile, night)
        base = self._base_costs.get(key)
        if base is None:
            g = self.graph
            slower = MAP_SPEED_KMH[g.mode] / profile.speed_kmh
            weights = np.array(profile.stress_weights)[np.maximum(1, g.column("stress")) - 1]
            if night:
                weights = weights + profile.unlit_weight * (g.column("lit") == 0)
            base = g.column("time_s") / 60 * slower * weights + g.column("rail_crossings") * profile.rail_minutes
            if profile.avoid_steps and "steps" in g.classes:
                base[g.column("road_class") == g.classes.index("steps")] = math.inf
            self._base_costs.put(key, base)
        return base

    def direct_routes(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: BikeProfile,
        *,
        count: int = 3,
        events: Sequence[RoadEvent] = (),
        sun: Sun | None = None,
    ) -> list[RoutedPath]:
        """Alternatives that head straight for the destination, for the traveler to compare with the cheapest ones."""
        g = self.graph
        minutes_per_km = MAP_SPEED_KMH[g.mode] / profile.speed_kmh * 60.0 / float(g.column("speed_kmh").max())
        found = self.routes(origin, destination, profile, count=count, events=events, sun=sun, direct_per_km=DIRECT_WALK_GREED * minutes_per_km)  # type: ignore[arg-type]
        return found[1:]

    def _step_costs(self, costs: _Costs) -> np.ndarray:
        steps = self._steps()
        trip: _BikeTrip = costs.extra
        minutes = trip.profile.crossing_minutes
        night = DARK_TURN_FACTOR if trip.dark else 1.0
        return (
            super()._step_costs(costs)
            + steps.cross_3 * (minutes[0] * night)
            + steps.cross_4 * (minutes[1] * night)
            + steps.crash_spots * trip.profile.crash_minutes
        )

    def _step_cost(self, edge: int, step, costs: _Costs, penalties: dict[int, float]) -> float:
        cost = super()._step_cost(edge, step, costs, penalties)
        trip: _BikeTrip = costs.extra
        previous = edge
        for nxt, maneuver in zip(step.edges, step.maneuvers, strict=True):
            stress = self.crossing_stress(previous, nxt, maneuver)
            if stress >= 3:
                cost += trip.profile.crossing_minutes[stress - 3] * (DARK_TURN_FACTOR if trip.dark else 1.0)
            if self.crashes_at(self.graph.edge_to[previous]) >= MIN_CRASHES:
                cost += trip.profile.crash_minutes
            previous = nxt
        return cost

    def crossing_stress(self, edge: int, nxt: int, maneuver: str) -> int:
        """The stress of the traffic crossed going from edge onto nxt: the worst road met at the junction
        that the rider isn't riding along (going straight across or turning left across it), and the road
        being left when turning left across its own oncoming lanes. 0 where a signal controls the junction,
        or for a right turn, which crosses no traffic."""
        if self._crossings is None:
            self._crossings = self._crossing_columns()
        return int(kernel.crossing_stress(edge, nxt, kernel.MANEUVER_CODES[maneuver], self._crossings))


# ---- A route's features, its safety score, and the plan


def route_features(router: BikeRouter, path: RoutedPath, profile: BikeProfile, events: Sequence[RoadEvent], after_dark: bool) -> dict:
    g = router.graph
    slower = MAP_SPEED_KMH[g.mode] / profile.speed_kmh
    minutes = [0.0, 0.0, 0.0, 0.0]
    unlit = 0.0
    for edge in path.edges:
        minutes[max(1, g.stress[edge]) - 1] += g.time_s[edge] / 60 * slower
        if not g.lit[edge]:
            unlit += g.time_s[edge] / 60 * slower
    crossings = []
    crash_spots = 0
    for i in range(1, len(path.edges)):
        previous, nxt = path.edges[i - 1], path.edges[i]
        node = g.edge_to[previous]
        stress = router.crossing_stress(previous, nxt, path.maneuvers[i])
        if stress >= 3:
            lat, lng = g.point(node)
            crossings.append({"lat": lat, "lng": lng, "stress": stress})
        crash_spots += router.crashes_at(node) >= MIN_CRASHES
    on_route = set(path.edges)
    closed = sum(1 for event in events if event.kind == "closed" and router.event_edges(event) & on_route)
    lanes = sum(1 for event in events if event.kind == "lanes" and router.event_edges(event) & on_route)
    return {
        "duration_min": round(sum(minutes), 2),
        "distance_km": round(sum(g.length_m[edge] for edge in path.edges) / 1000, 2),
        "minutes_by_stress": [round(m, 2) for m in minutes],
        "busy_crossings": len(crossings),
        "rail_crossings": sum(g.rail_crossings[edge] for edge in path.edges),
        "crash_spots": int(crash_spots),
        "lane_closures": lanes,
        "closures": closed,
        "unlit_minutes": round(unlit, 2),
        "after_dark": after_dark,
        "crossings": crossings,
    }


def safety(features: dict) -> tuple[int, dict[str, float]]:
    """The safety score (0-100, higher is safer) and the risk points behind it, by kind."""
    night = DARK_TURN_FACTOR if features["after_dark"] else 1.0
    crossing_points = sum((1 if c["stress"] == 3 else 2) for c in features["crossings"]) * RISK_POINTS["busy_crossings"] * night
    points = {
        "stress_3_minutes": features["minutes_by_stress"][2] * RISK_POINTS["stress_3_minutes"],
        "stress_4_minutes": features["minutes_by_stress"][3] * RISK_POINTS["stress_4_minutes"],
        "busy_crossings": crossing_points,
        "crash_spots": features["crash_spots"] * RISK_POINTS["crash_spots"],
        "rail_crossings": features["rail_crossings"] * RISK_POINTS["rail_crossings"],
        "unlit_minutes": (features.get("unlit_minutes", 0.0) * RISK_POINTS["unlit_minutes"]) if features["after_dark"] else 0.0,
        "lane_closures": features["lane_closures"] * RISK_POINTS["lane_closures"],
        "closures": features["closures"] * RISK_POINTS["closures"],
    }
    risks = {kind: round(value, 2) for kind, value in points.items() if value > 1e-9}
    return round(100 / (1 + sum(points.values()) / SAFETY_SCALE)), risks


def stress_segments(router: BikeRouter, path: RoutedPath) -> list[dict]:
    """The route in stretches of one stress level each, for drawing it colored by stress."""
    g = router.graph
    stretches: list[dict] = []
    run: list[int] = []
    for edge in path.edges:
        if run and g.stress[edge] != g.stress[run[-1]]:
            stretches.append({"stress": g.stress[run[-1]], "encoded_polyline": encode_polyline(router._points(run))})
            run = []
        run.append(edge)
    if run:
        stretches.append({"stress": g.stress[run[-1]], "encoded_polyline": encode_polyline(router._points(run))})
    return stretches


def plan(
    router: BikeRouter,
    origin: LatLng,
    destination: LatLng,
    profile: BikeProfile,
    *,
    events: Sequence[RoadEvent] = (),
    sun: Sun | None = None,
) -> dict:
    """Bike routes for one trip, best first: {"winner", "candidates"}, each candidate a dict the page draws."""
    start, end = origin.as_tuple(), destination.as_tuple()
    # The shortest at the traveler's own speed, so turns weigh against minutes as they do for them.
    shortest = router.bike_routes(start, end, replace(SHORTEST, speed_kmh=profile.speed_kmh), count=1, events=events, sun=sun)
    designs = router.bike_routes(start, end, profile, count=3, events=events, sun=sun)
    after_dark = dark(sun)
    paths: list[tuple[str, RoutedPath]] = []
    seen: set[tuple[int, ...]] = set()
    for route_id, path in [*(("standard", p) for p in shortest), *((f"own-{n + 1}", p) for n, p in enumerate(designs))]:
        if path.edges not in seen:
            seen.add(path.edges)
            paths.append((route_id, path))
    if not paths:
        raise LookupError("No bike route was found between these points.")

    junctions = TurnContext(junctions=router.graph)

    def candidate(route_id: str, path: RoutedPath) -> dict:
        route = router.to_route_json(path)
        features = route_features(router, path, profile, events, after_dark)
        score, risks = safety(features)
        lengths = router.graph.known_road_lengths.get(route["polyline"]["encodedPolyline"], {})
        return {
            "id": route_id,
            "source": "own" if route_id != "standard" else "baseline",
            "is_standard": route_id == "standard",
            "encoded_polyline": route["polyline"]["encodedPolyline"],
            "road_lengths": lengths,
            "features": features,
            "safety_score": score,
            "safety_risks": risks,
            "maneuvers": [m.model_dump() for m in directions(route, junctions)],
            "stress_segments": stress_segments(router, path),
        }

    def rank(c: dict) -> tuple:
        return (not c["eligible"], -c["safety_score"], c["features"]["duration_min"])

    candidates = [candidate(route_id, path) for route_id, path in paths]
    standard = candidates[0] if candidates[0]["is_standard"] else None
    shortest_min = standard["features"]["duration_min"] if standard else min(c["features"]["duration_min"] for c in candidates)
    limit_min = min(shortest_min * profile.max_detour_ratio, shortest_min + profile.max_extra_min)
    for c in candidates:
        c["eligible"] = c["is_standard"] or c["features"]["duration_min"] <= limit_min + 1e-9
    candidates.sort(key=rank)
    if router.graph.mode == "walk":
        # One takes the lead only by beating the best so far, so the recommendation can only get better.
        direct = (path for path in router.direct_routes(start, end, profile, count=3, events=events, sun=sun) if path.edges not in seen)
        for number, path in enumerate(direct, start=1):
            challenger = candidate(f"direct-{number}", path)
            challenger["eligible"] = challenger["features"]["duration_min"] <= limit_min + 1e-9
            if rank(challenger) < rank(candidates[0]):
                if len(candidates) >= ROUTES_SHOWN:
                    del candidates[max(i for i, c in enumerate(candidates) if not c["is_standard"])]
                candidates.insert(0, challenger)
    winner = candidates[0]
    _name_and_explain(candidates, winner, standard, profile, "streets without sidewalks" if router.graph.mode == "walk" else "busy streets")
    return {"profile": profile.name, "winner": winner, "candidates": candidates}


def _name_and_explain(candidates: list[dict], winner: dict, standard: dict | None, profile: BikeProfile, busy_words: str) -> None:
    """Each route's name (its longest road no better route goes by) and the reason for its place."""
    from app.engine import short_road_name

    taken: set[str] = set()
    for candidate in candidates:
        lengths = candidate.pop("road_lengths")
        longest = sorted(lengths, key=lambda name: -lengths[name])
        name = next((n for n in longest if n not in taken), longest[0] if longest else "")
        taken.add(name)
        candidate["via"] = short_road_name(name)
    limit = f"within {profile.max_detour_ratio:g}x the shortest time, at most {profile.max_extra_min:g} min more"
    for candidate in candidates:
        busy = candidate["features"]["minutes_by_stress"][2] + candidate["features"]["minutes_by_stress"][3]
        if candidate is winner:
            if standard is None or candidate is standard:
                candidate["reason"] = "the shortest route is also the lowest-stress one"
            else:
                theirs = standard["features"]["minutes_by_stress"][2] + standard["features"]["minutes_by_stress"][3]
                if round(theirs) > round(busy):
                    candidate["reason"] = f"lowest stress {limit}: {busy:.0f} min on {busy_words} instead of {theirs:.0f}"
                else:
                    candidate["reason"] = f"safest {limit}"
        elif not candidate["eligible"]:
            candidate["reason"] = f"too long a detour: over {profile.max_detour_ratio:g}x the shortest time or {profile.max_extra_min:g} min more"
        elif candidate["safety_score"] < winner["safety_score"]:
            mine = winner["features"]["minutes_by_stress"][2] + winner["features"]["minutes_by_stress"][3]
            if round(busy) > round(mine):
                candidate["reason"] = f"more stress: {busy:.0f} min on {busy_words}"
            else:
                candidate["reason"] = f"less safe: safety score {candidate['safety_score']}"
        else:
            candidate["reason"] = "as low-stress, but slower"


# ---- Trips with stops: leg by leg, joined (as car trips are, engine.combine_legs)


def plan_with_stops(
    router: BikeRouter,
    places: Sequence[LatLng],
    profile: BikeProfile,
    *,
    events: Sequence[RoadEvent] = (),
    sun: Sun | None = None,
) -> dict:
    """A trip through stops (places: start, stops, destination): each leg planned on its own, then the
    recommended route joined from each leg's recommendation and the shortest from each leg's shortest."""
    legs = [plan(router, a, b, profile, events=events, sun=sun) for a, b in zip(places, places[1:])]
    if len(legs) == 1:
        return legs[0]
    winner = _joined([leg["winner"] for leg in legs], places[1:-1], "trip-recommended", standard=False)
    standards = [next((c for c in leg["candidates"] if c["is_standard"]), leg["winner"]) for leg in legs]
    shortest = _joined(standards, places[1:-1], "trip-standard", standard=True)
    winner["reason"] = "the recommended route on every leg"
    shortest["reason"] = "the shortest route on every leg"
    candidates = [winner] if winner["encoded_polyline"] == shortest["encoded_polyline"] else [winner, shortest]
    return {"profile": profile.name, "winner": winner, "candidates": candidates}


def _joined(legs: Sequence[dict], stops: Sequence[LatLng], route_id: str, *, standard: bool) -> dict:
    from app.geo import decode_polyline

    points: list[tuple[float, float]] = []
    maneuvers: list[dict] = []
    for number, leg in enumerate(legs):
        leg_points = decode_polyline(leg["encoded_polyline"])
        points += leg_points if not points else leg_points[1:]
        if number:
            stop = stops[number - 1]
            maneuvers.append({"maneuver": "WAYPOINT", "instruction": f"Arrive at stop {number}", "location": {"lat": stop.lat, "lng": stop.lng}})
        maneuvers += leg["maneuvers"]
    added = ("duration_min", "distance_km", "busy_crossings", "rail_crossings", "crash_spots", "lane_closures", "closures", "unlit_minutes")
    features = {key: round(sum(leg["features"].get(key, 0) for leg in legs), 2) for key in added}
    features["minutes_by_stress"] = [round(sum(leg["features"]["minutes_by_stress"][i] for leg in legs), 2) for i in range(4)]
    features["crossings"] = [c for leg in legs for c in leg["features"]["crossings"]]
    features["after_dark"] = any(leg["features"]["after_dark"] for leg in legs)
    score, risks = safety(features)
    vias = list(dict.fromkeys(leg["via"] for leg in legs if leg["via"]))
    return {
        "id": route_id,
        "source": "baseline" if standard else "own",
        "is_standard": standard,
        "encoded_polyline": encode_polyline(points),
        "via": ", then ".join(vias[:3]),
        "eligible": True,
        "features": features,
        "safety_score": score,
        "safety_risks": risks,
        "maneuvers": maneuvers,
        "stress_segments": [segment for leg in legs for segment in leg["stress_segments"]],
    }
