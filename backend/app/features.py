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
  faster, judged by its no-traffic speed.
* Traffic delay: duration minus staticDuration. Only non-zero when the
  request was traffic-aware.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.driving_side import DrivingSide
from app.geo import Point, decode_polyline, path_length_m, total_heading_change_deg
from app.hotspots import CrashHotspots
from app.models import CrossingTurn, LatLng, RouteFeatures

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


@dataclass(frozen=True)
class TurnContext:
    """Trip-level facts for judging turns; the same for every candidate and profile."""

    driving_side: DrivingSide = DrivingSide.RIGHT
    hotspots: CrashHotspots | None = None
    # Crashes near a junction needed to call it crash-prone. The right value
    # depends on how many years the crash dataset covers.
    min_hotspot_crashes: float = 3.0

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

    highway_s = highway_m = winding_s = 0.0
    maneuvers = 0
    crossing_turns: list[CrossingTurn] = []
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
            crossing_turns.append(_crossing_turn(maneuver, points, speed_in, speed, context))

        if speed is not None and speed >= HIGHWAY_SPEED_KMH:
            highway_s += step_s
            highway_m += step_m
        elif speed is not None and is_winding(points):
            winding_s += step_s
        speed_in = speed

    return RouteFeatures(
        distance_km=route.get("distanceMeters", 0) / 1000,
        duration_min=duration_s / 60,
        static_duration_min=static_s / 60,
        highway_min=highway_s / 60,
        highway_km=highway_m / 1000,
        traffic_delay_min=max(0.0, duration_s - static_s) / 60,
        maneuver_count=maneuvers,
        winding_min=winding_s / 60,
        crossing_turns=tuple(crossing_turns),
    )


def _crossing_turn(
    maneuver: str,
    points: Sequence[Point],
    speed_in: float | None,
    speed_out: float | None,
    context: TurnContext,
) -> CrossingTurn:
    # Crossing is risky if either stream is fast: the road being left (its
    # oncoming lanes) or the road being joined (its near lanes).
    road_speed = max(speed_in or 0.0, speed_out or 0.0)
    junction = points[0] if points else None
    crashes = (
        context.hotspots.crashes_near(junction, HOTSPOT_RADIUS_M)
        if context.hotspots is not None and junction is not None
        else 0.0
    )
    return CrossingTurn(
        location=LatLng(lat=junction[0], lng=junction[1]) if junction else None,
        maneuver=maneuver,
        road_speed_kmh=round(road_speed, 1),
        nearby_crashes=crashes,
        busy=road_speed >= BUSY_ROAD_SPEED_KMH,
        hotspot=crashes >= context.min_hotspot_crashes,
    )
