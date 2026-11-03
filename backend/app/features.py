"""Turn a raw Routes API route into measurable, profile-independent features.

The heuristics are deliberately simple enough to explain on a demo stage:

* Highway time: steps whose no-traffic average speed is at highway level.
  Using staticDuration means a jammed freeway still counts as a freeway.
* Winding time: non-highway steps whose geometry turns a lot per km. This is
  a geometric stand-in for "scenic": coastal roads, hills, parkways.
* Maneuvers: turns, merges, forks and roundabouts the driver has to make.
* Crossing turns: turns across oncoming traffic (lefts where traffic keeps
  right, rights where it keeps left), flagged when the junction is busy or a
  known crash hotspot. Google's free tier has no per-junction traffic volume,
  so "busy" means at least one road at the junction is arterial class or
  faster, judged by its no-traffic speed. One at a traffic signal (on the road
  graph) counts SIGNALED_CROSSING_SHARE of one without, unless it's crash-prone.
* Traffic delay: duration minus staticDuration. Only non-zero when the
  request was traffic-aware.
* Fuel: each step's distance at its average speed, plus turns, traffic
  signals passed (counted on the road graph, when loaded) and traffic delay,
  through the rough model in app.fuel.
* Closures and lane closures: the trip's road events (app.road_events) the
  route drives through, the way their traffic goes.
* Railroad crossings at grade, and school zones during school hours: counted
  on the road graph along the route's street stretches.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.driving_side import DrivingSide
from app.fuel import CONGESTION_L_PER_HOUR, SIGNAL_L, TURN_L, cruise_liters
from app.geo import (
    Point,
    decode_polyline,
    haversine_m,
    initial_bearing_deg,
    path_length_m,
    point_along,
    total_heading_change_deg,
)
from app.hotspots import CrashHotspots
from app.models import CrossingTurn, LatLng, Maneuver, RouteFeatures
from app.road_events import RoadEvent, count_on_route, weather_on_route
from app.sun import Sun, glare, low_sun

# Calibration knobs. Tune them against routes you have recorded in the cache.
HIGHWAY_SPEED_KMH = 80.0
WINDING_DEG_PER_KM = 120.0
MIN_WINDING_STEP_M = 200.0
# A junction is busy when either road reaches this no-traffic speed (about
# 30 mph): arterial class or faster. Slower roads are mostly residential.
BUSY_ROAD_SPEED_KMH = 50.0
# Recorded crashes within this distance of a turn count toward that junction.
HOTSPOT_RADIUS_M = 35.0

# Routes API maneuvers that ask the driver to make a decision.
# STRAIGHT, DEPART, NAME_CHANGE and ferries are deliberately left out.
DECISION_MANEUVERS = frozenset(
    {
        "TURN_SLIGHT_LEFT",
        "TURN_SHARP_LEFT",
        "UTURN_LEFT",
        "TURN_LEFT",
        "TURN_SLIGHT_RIGHT",
        "TURN_SHARP_RIGHT",
        "UTURN_RIGHT",
        "TURN_RIGHT",
        "RAMP_LEFT",
        "RAMP_RIGHT",
        "MERGE",
        "FORK_LEFT",
        "FORK_RIGHT",
        "ROUNDABOUT_LEFT",
        "ROUNDABOUT_RIGHT",
    }
)

# Turns that cut across oncoming traffic, by the side traffic keeps to.
# A U-turn crosses every oncoming lane whichever way it swings. Roundabouts,
# forks and merges are left out: none of them crosses an oncoming stream.
CROSSING_MANEUVERS = {
    DrivingSide.RIGHT: frozenset({"TURN_SLIGHT_LEFT", "TURN_LEFT", "TURN_SHARP_LEFT", "UTURN_LEFT", "UTURN_RIGHT"}),
    DrivingSide.LEFT: frozenset({"TURN_SLIGHT_RIGHT", "TURN_RIGHT", "TURN_SHARP_RIGHT", "UTURN_RIGHT", "UTURN_LEFT"}),
}
# A far-side ramp crosses traffic when taken from a surface street (turning
# onto an on-ramp), but not when it exits a divided highway.
CROSSING_RAMPS = {DrivingSide.RIGHT: "RAMP_LEFT", DrivingSide.LEFT: "RAMP_RIGHT"}


# Junction kinds where no turn crosses an oncoming stream.
NO_ONCOMING_KINDS = frozenset({"interchange", "one-way"})


class JunctionLookup(Protocol):
    """Knows what kind of junction is at a point; the OSM road graph implements it."""

    def junction_kind(self, point: Point) -> str | None:
        """'interchange', 'one-way', 'busy' or 'quiet'; None when no junction is close enough."""
        ...

    def signals_along(self, stretches: Iterable[Sequence[Point]]) -> int:
        """Distinct traffic signals the given stretches of road pass."""
        ...

    def road_name_at(self, point: Point, heading: float) -> str:
        """The name of the road at point running along heading; "" if unknown."""
        ...

    def signal_at(self, point: Point) -> bool:
        """Whether a traffic signal controls the junction at point."""
        ...


@dataclass(frozen=True)
class TurnContext:
    """Trip-level facts for judging turns; the same for every candidate and profile."""

    driving_side: DrivingSide = DrivingSide.RIGHT
    hotspots: CrashHotspots | None = None
    # Crashes near a junction needed to call it crash-prone. The right value
    # depends on how many years the crash dataset covers.
    min_hotspot_crashes: float = 3.0
    # With a road graph, junctions are judged by the roads that meet there:
    # interchanges have no oncoming traffic, and "busy" means an arterial or
    # bigger road meets there, the same rule the router uses. Without one,
    # the road speed proxy applies.
    junctions: JunctionLookup | None = None
    # Closures, lane closures and jams on the roads now (see app.road_events).
    events: tuple[RoadEvent, ...] = ()
    # School zones count only while schools are in session (see engine.school_in_session).
    school_hours: bool = False
    # The sun's (azimuth, elevation) at departure, for sun glare (see app.sun); None: not known.
    sun: Sun | None = None

    def __post_init__(self) -> None:
        if self.min_hotspot_crashes <= 0:
            raise ValueError("min_hotspot_crashes must be positive")


def parse_duration_s(value: str | None) -> float:
    """Parse a protobuf Duration string such as "1234s" or "12.5s" into seconds."""
    if not value:
        return 0.0
    if not value.endswith("s"):
        raise ValueError(f"Unexpected duration format: {value!r}")
    return float(value[:-1])


def iter_steps(route: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for leg in route.get("legs", []):
        yield from leg.get("steps", [])


def is_winding(points: Sequence[Point]) -> bool:
    """True when a stretch of road turns more than WINDING_DEG_PER_KM on average."""
    length_m = path_length_m(points)
    if length_m < MIN_WINDING_STEP_M:
        return False
    return total_heading_change_deg(points) / (length_m / 1000) >= WINDING_DEG_PER_KM


def crosses_traffic(maneuver: str, speed_in_kmh: float | None, side: DrivingSide) -> bool:
    """Whether a maneuver cuts across oncoming traffic, given the speed of the road it leaves."""
    if maneuver in CROSSING_MANEUVERS[side]:
        return True
    # Unknown incoming speed is read as a surface street: the cautious choice.
    return maneuver == CROSSING_RAMPS[side] and (speed_in_kmh or 0.0) < HIGHWAY_SPEED_KMH


def extract_features(route: Mapping[str, Any], context: TurnContext = TurnContext()) -> RouteFeatures:
    duration_s = parse_duration_s(route.get("duration"))
    # Fall back to duration so a missing baseline reads as zero delay, not negative.
    static_s = parse_duration_s(route.get("staticDuration")) or duration_s

    highway_s = highway_m = winding_s = glare_s = 0.0
    cruise_l = covered_m = 0.0
    maneuvers = 0
    crossing_turns: list[CrossingTurn] = []
    street_stretches: list[list[Point]] = []  # below freeway speed: where signals can be
    speed_in: float | None = None  # no-traffic speed of the road the driver is turning off
    for step in iter_steps(route):
        maneuver = step.get("navigationInstruction", {}).get("maneuver", "")
        if maneuver in DECISION_MANEUVERS:
            maneuvers += 1

        points = decode_polyline(step.get("polyline", {}).get("encodedPolyline", ""))
        step_m = step.get("distanceMeters", 0)  # the API may omit it on some steps
        step_s = parse_duration_s(step.get("staticDuration"))
        speed = (step_m / step_s) * 3.6 if step_m > 0 and step_s > 0 else None

        # A step's maneuver happens where it starts: between the previous road and this one.
        if crosses_traffic(maneuver, speed_in, context.driving_side):
            turn = _crossing_turn(maneuver, points, speed_in, speed, context)
            if turn is not None:
                crossing_turns.append(turn)

        if speed is not None:
            cruise_l += cruise_liters(step_m, speed)
            covered_m += step_m
        if low_sun(context.sun) and step_s > 0:
            glare_s += step_s * _share_into_sun(points, context.sun)
        if speed is not None and speed >= HIGHWAY_SPEED_KMH:
            highway_s += step_s
            highway_m += step_m
        else:
            street_stretches.append(points)
            if speed is not None and is_winding(points):
                winding_s += step_s
        speed_in = speed

    distance_m = route.get("distanceMeters", 0)
    if covered_m == 0 and distance_m > 0 and static_s > 0:
        cruise_l = cruise_liters(distance_m, distance_m / static_s * 3.6)  # no steps: use the route average
    delay_s = max(0.0, duration_s - static_s)
    # Google's steps don't list traffic signals, railroad crossings or schools;
    # the road graph knows where they are. Freeway stretches pass over them.
    def along(method: str) -> int:
        counter = getattr(context.junctions, method, None)
        return counter(street_stretches) if counter is not None else 0

    def spots(method: str) -> tuple[LatLng, ...]:
        finder = getattr(context.junctions, method, None)
        return tuple(LatLng(lat=lat, lng=lng) for lat, lng in finder(street_stretches)) if finder is not None else ()

    signals = along("signals_along")
    rail_points = spots("rail_crossings_at")
    school_points = spots("schools_at") if context.school_hours else ()
    fuel_l = cruise_l + maneuvers * TURN_L + signals * SIGNAL_L + delay_s / 3600 * CONGESTION_L_PER_HOUR
    route_points = decode_polyline(route.get("polyline", {}).get("encodedPolyline", "")) if context.events else []
    closures, lane_closures, with_workers = count_on_route(route_points, context.events) if context.events else (0, 0, 0)
    weather_hazards = weather_on_route(route_points, context.events) if context.events else 0

    return RouteFeatures(
        distance_km=distance_m / 1000,
        duration_min=duration_s / 60,
        static_duration_min=static_s / 60,
        highway_min=highway_s / 60,
        highway_km=highway_m / 1000,
        traffic_delay_min=delay_s / 60,
        maneuver_count=maneuvers,
        winding_min=winding_s / 60,
        fuel_l=fuel_l,
        signal_count=signals,
        crossing_turns=tuple(crossing_turns),
        closures=closures,
        lane_closures=lane_closures,
        lane_closures_with_workers=with_workers,
        school_zones=len(school_points),
        rail_crossings=len(rail_points),
        rail_crossing_points=rail_points,
        school_points=school_points,
        weather_hazards=weather_hazards,
        sun_glare_min=glare_s / 60,
    )


def _share_into_sun(points: Sequence[Point], sun: Sun | None) -> float:
    """The share of a stretch's length driven toward a low sun."""
    total = into = 0.0
    for a, b in zip(points, points[1:]):
        length = haversine_m(a, b)
        total += length
        if length > 1 and glare(initial_bearing_deg(a, b), sun):
            into += length
    return into / total if total else 0.0


# Turn-by-turn wording per maneuver; "onto <road>" follows when the road has a name.
_SAY = {
    "TURN_LEFT": "Turn left",
    "TURN_RIGHT": "Turn right",
    "TURN_SLIGHT_LEFT": "Bear left",
    "TURN_SLIGHT_RIGHT": "Bear right",
    "TURN_SHARP_LEFT": "Turn sharp left",
    "TURN_SHARP_RIGHT": "Turn sharp right",
    "UTURN_LEFT": "Make a U-turn",
    "UTURN_RIGHT": "Make a U-turn",
    "RAMP_LEFT": "Take the ramp on the left",
    "RAMP_RIGHT": "Take the ramp on the right",
    "MERGE": "Merge",
    "FORK_LEFT": "Keep left",
    "FORK_RIGHT": "Keep right",
    "ROUNDABOUT_LEFT": "Enter the roundabout",
    "ROUNDABOUT_RIGHT": "Enter the roundabout",
    "NAME_CHANGE": "Continue",
}


def directions(route: Mapping[str, Any], context: TurnContext = TurnContext()) -> tuple[Maneuver, ...]:
    """Turn-by-turn instructions: one per maneuver, where it's made.

    Google's steps here carry no road names, so with the road graph each
    instruction names the road the maneuver leads onto.
    """
    road_name_at = getattr(context.junctions, "road_name_at", None)
    result = []
    for step in iter_steps(route):
        maneuver = step.get("navigationInstruction", {}).get("maneuver", "")
        say = _SAY.get(maneuver)
        points = decode_polyline(step.get("polyline", {}).get("encodedPolyline", ""))
        if say is None or len(points) < 2:
            continue
        road = ""
        if road_name_at is not None and not maneuver.startswith("UTURN"):
            ahead, bearing = point_along(points, min(30.0, path_length_m(points) / 2))
            road = road_name_at(ahead, bearing)
        start = points[0]
        result.append(
            Maneuver(
                maneuver=maneuver,
                instruction=f"{say} onto {road}" if road else say,
                location=LatLng(lat=start[0], lng=start[1]),
            )
        )
    return tuple(result)


def _crossing_turn(
    maneuver: str,
    points: Sequence[Point],
    speed_in: float | None,
    speed_out: float | None,
    context: TurnContext,
) -> CrossingTurn | None:
    """The turn's details, or None if the road graph shows there's no oncoming traffic there."""
    # Crossing is risky if either stream is fast: the road being left (its
    # oncoming lanes) or the road being joined (its near lanes).
    road_speed = max(speed_in or 0.0, speed_out or 0.0)
    junction = points[0] if points else None
    kind = context.junctions.junction_kind(junction) if context.junctions is not None and junction else None
    if kind is None and context.junctions is not None and min(speed_in or 0.0, speed_out or 0.0) >= HIGHWAY_SPEED_KMH:
        # Freeway speed on both sides and no junction on the map nearby: a split
        # inside an interchange (where Google's point can sit far from OSM's
        # node), not an at-grade turn, which would have matched a junction.
        kind = "interchange"
    if kind in NO_ONCOMING_KINDS:
        return None  # a freeway split or merge, or only one-way roads: no oncoming traffic to cross
    busy = kind == "busy" if kind is not None else road_speed >= BUSY_ROAD_SPEED_KMH
    crashes = (
        context.hotspots.crashes_near(junction, HOTSPOT_RADIUS_M)
        if context.hotspots is not None and junction is not None
        else 0.0
    )
    signal_at = getattr(context.junctions, "signal_at", None)
    return CrossingTurn(
        location=LatLng(lat=junction[0], lng=junction[1]) if junction else None,
        maneuver=maneuver,
        road_speed_kmh=round(road_speed, 1),
        nearby_crashes=crashes,
        busy=busy,
        hotspot=crashes >= context.min_hotspot_crashes,
        signal=bool(signal_at(junction)) if signal_at is not None and junction is not None else False,
    )
