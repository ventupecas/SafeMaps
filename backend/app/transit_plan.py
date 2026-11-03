"""Transit trips, explained: the bus options app.timetable finds, with their walks planned on the walking
map, their waits, and a safety score, safest first among those arriving near the earliest.

On transit, the risk is mostly getting to and from the bus: walking legs count as walking routes do
(streets without sidewalks, busy roads crossed without a signal, places where a pedestrian was killed,
unlit stretches after dark: app.bike.safety), plus waiting at a stop after dark (DARK_WAIT_POINTS a
minute) and each transfer (TRANSFER_POINTS: another wait, and a connection to miss). Among the options
arriving within MAX_LATER of the earliest, the safest comes first; ties go to the earlier arrival.

The times are the schedule's, not live: buses can run late.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from app import bike
from app.geo import Point, decode_polyline, encode_polyline, haversine_m
from app.models import LatLng
from app.sun import Sun, dark
from app.timetable import BOARD_SLACK_S, Journey, Timetable, walk_seconds

DARK_WAIT_POINTS = 0.2  # risk points per minute waiting at a stop after dark
TRANSFER_POINTS = 1.0
MAX_LATER = timedelta(minutes=10)  # options arriving this much after the earliest still compete on safety
MAX_LATER_SHARE = 0.25  # ... or this share of the earliest trip's time, if more
SAFETY_SCALE = 10.0


def plan(
    table: Timetable,
    walker: bike.BikeRouter | None,
    origin: LatLng,
    destination: LatLng,
    leave: datetime,
    sun_at: Callable[[datetime, Point], Sun | None],
) -> dict:
    """{"winner", "candidates"}: each candidate a bus option the page draws (see _option)."""
    journeys = table.journeys(origin.as_tuple(), destination.as_tuple(), leave)
    if not journeys:
        raise LookupError("No bus gets there from here at this time: try another time, or walking or biking.")
    walks: dict[tuple, dict] = {}  # options often share a walk: plan each once
    options = [_option(table, walker, walks, origin, destination, journey, sun_at, number) for number, journey in enumerate(journeys, 1)]
    earliest = min(options, key=lambda o: o["arrive_at"])
    first_arrival = datetime.fromisoformat(earliest["arrive_at"])
    window = max(MAX_LATER, timedelta(minutes=earliest["duration_min"] * MAX_LATER_SHARE))
    for option in options:
        option["eligible"] = datetime.fromisoformat(option["arrive_at"]) <= first_arrival + window
    options.sort(key=lambda o: (not o["eligible"], -o["safety_score"], o["arrive_at"]))
    winner = options[0]
    for option in options:
        later = round((datetime.fromisoformat(option["arrive_at"]) - first_arrival).total_seconds() / 60)
        if option is winner:
            option["reason"] = "safest, and arrives earliest" if later <= 0 else f"safest within {round(window.total_seconds() / 60)} min of the earliest arrival"
        elif not option["eligible"]:
            option["reason"] = f"arrives {later} min after the earliest"
        elif option is earliest:
            option["reason"] = "arrives earliest"
        elif option["safety_score"] < winner["safety_score"]:
            option["reason"] = f"less safe: safety score {option['safety_score']}"
        else:
            option["reason"] = "as safe, but arrives later"
    return {"winner": winner, "candidates": options}


def _option(table, walker, walks, origin: LatLng, destination: LatLng, journey: Journey, sun_at, number: int) -> dict:
    midnight = datetime.combine(journey.service_day, datetime.min.time())
    at = lambda seconds: midnight + timedelta(seconds=seconds)  # noqa: E731
    stop_point = lambda stop: table.stop_points[stop]  # noqa: E731
    stop_place = lambda stop, seconds: {  # noqa: E731
        "name": table.stop_names[stop],
        "lat": stop_point(stop)[0],
        "lng": stop_point(stop)[1],
        "time": at(seconds).isoformat(timespec="minutes"),
    }
    legs: list[dict] = []
    risks: dict[str, float] = {}
    waits = 0.0
    dark_waits = 0.0

    def walk(start: Point, end: Point, start_s: int) -> dict:
        sun = sun_at(at(start_s), start)
        key = (start, end, dark(sun))
        if key not in walks:
            walks[key] = _walk_leg(walker, start, end, sun)
        leg = dict(walks[key])
        for kind, points in leg.pop("risks").items():
            risks[kind] = risks.get(kind, 0.0) + points
        return leg

    # Walk to the first stop, leaving so as to be there BOARD_SLACK_S before the bus.
    first = journey.rides[0]
    first_stop = first.pattern.stops[first.board]
    to_stop = walk(origin.as_tuple(), stop_point(first_stop), journey.board_s)
    leave_s = journey.board_s - BOARD_SLACK_S - round(to_stop["minutes"] * 60)
    legs.append(to_stop)
    ready_s = leave_s + round(to_stop["minutes"] * 60)
    for index, ride in enumerate(journey.rides):
        pattern = ride.pattern
        board_s, alight_s = pattern.dep[ride.board][ride.trip], pattern.arr[ride.alight][ride.trip]
        wait = max(0.0, (board_s - ready_s) / 60)
        waits += wait
        if dark(sun_at(at(board_s), stop_point(pattern.stops[ride.board]))):
            dark_waits += wait
        name, long_name, color = table.routes[pattern.route]
        stops = [stop_point(stop) for stop in pattern.stops[ride.board : ride.alight + 1]]
        legs.append(
            {
                "kind": "ride",
                "route": {"name": name, "long_name": long_name, "color": color},
                "headsign": pattern.headsign,
                "board": stop_place(pattern.stops[ride.board], board_s),
                "alight": stop_place(pattern.stops[ride.alight], alight_s),
                "stops": ride.alight - ride.board,
                "minutes": round((alight_s - board_s) / 60, 1),
                "wait_minutes": round(wait, 1),
                "encoded_polyline": encode_polyline(stops),
            }
        )
        ready_s = alight_s
        if index + 1 < len(journey.rides):
            following = journey.rides[index + 1]
            here, there = pattern.stops[ride.alight], following.pattern.stops[following.board]
            if here != there:
                between = walk(stop_point(here), stop_point(there), alight_s)
                between["transfer"] = True
                legs.append(between)
                ready_s = alight_s + round(between["minutes"] * 60)
    last = journey.rides[-1]
    off = walk(stop_point(last.pattern.stops[last.alight]), destination.as_tuple(), ready_s)
    legs.append(off)
    arrive_s = ready_s + round(off["minutes"] * 60)

    transfers = len(journey.rides) - 1
    if dark_waits:
        risks["dark_wait_minutes"] = dark_waits * DARK_WAIT_POINTS
    if transfers:
        risks["transfers"] = transfers * TRANSFER_POINTS
    risks = {kind: round(points, 2) for kind, points in risks.items() if points > 1e-9}
    walk_minutes = sum(leg["minutes"] for leg in legs if leg["kind"] == "walk")
    points = [point for leg in legs for point in decode_polyline(leg["encoded_polyline"])]
    return {
        "id": f"bus-{number}",
        "leave_at": at(leave_s).isoformat(timespec="minutes"),
        "arrive_at": at(arrive_s).isoformat(timespec="minutes"),
        "duration_min": round((arrive_s - leave_s) / 60, 1),
        "walk_min": round(walk_minutes, 1),
        "wait_min": round(waits, 1),
        "transfers": transfers,
        "busy_crossings": sum(leg.get("busy_crossings", 0) for leg in legs),
        "safety_score": round(100 / (1 + sum(risks.values()) / SAFETY_SCALE)),
        "safety_risks": risks,
        "legs": legs,
        "encoded_polyline": encode_polyline(points),
    }


def _walk_leg(walker: bike.BikeRouter | None, start: Point, end: Point, sun: Sun | None) -> dict:
    """A walk planned on the walking map (the calmest within an everyday walker's detour), or, without
    the map, a straight line at walking pace with its risks unknown."""
    profile = bike.WALK_PROFILES["everyday"]
    if walker is not None and haversine_m(start, end) > 20:
        try:
            result = bike.plan(walker, LatLng(lat=start[0], lng=start[1]), LatLng(lat=end[0], lng=end[1]), profile, sun=sun)
        except (LookupError, ValueError):
            result = None
        if result is not None:
            route = result["winner"]
            features = route["features"]
            _, risks = bike.safety(features)
            return {
                "kind": "walk",
                "minutes": features["duration_min"],
                "meters": round(features["distance_km"] * 1000),
                "busy_crossings": features["busy_crossings"],
                "crossings": features["crossings"],
                "minutes_by_stress": features["minutes_by_stress"],
                "encoded_polyline": route["encoded_polyline"],
                "maneuvers": route["maneuvers"],
                "risks": risks,
            }
    meters = haversine_m(start, end)
    return {
        "kind": "walk",
        "minutes": round(walk_seconds(meters) / 60, 1),
        "meters": round(meters),
        "busy_crossings": 0,
        "crossings": [],
        "minutes_by_stress": [round(walk_seconds(meters) / 60, 1), 0, 0, 0],
        "encoded_polyline": encode_polyline([start, end]),
        "maneuvers": [],
        "risks": {},
    }

