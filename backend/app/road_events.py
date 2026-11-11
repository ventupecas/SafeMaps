"""Road events the router steers around: closures, lane closures and traffic jams.

Five sources. ROUTES_ROAD_EVENT_SOURCES lists them in order of preference; a
trip uses the first one that is set up and answers, so the later ones are
backups. "a+b" in that list uses a and b together (when either answers). A
trip can also ask for one by name.

* wzdx    Wisconsin's Work Zone Data Exchange feed (WZDx 4.2, public domain,
          no key): every work zone with its stretch of road, direction, dates
          and which lanes are closed. Work zones only: no crashes or incidents.
          The feed is about 6 MB, so it's fetched at most every WZDX_REFRESH.
          Together with wi511, 511's own roadwork entries are dropped: the
          same work zones, described in less detail.
* wi511   Wisconsin DOT's 511 feed: official closures, lane closures,
          construction and incidents, statewide. Free, with a developer key
          (WI511_API_KEY) that 511WI approves by email. The feed allows 10 calls
          a minute, so it's fetched at most every WI511_REFRESH.
* tomtom  TomTom's traffic incidents (TOMTOM_API_KEY): jams with their delay,
          closures, lane closures and crashes on every road, side streets
          included. One request covers the whole road map's area, made at most
          every TOMTOM_REFRESH and capped per day (the free plan has 2,500 a
          month). The OpenStreetMap map only. Together with wi511, TomTom's
          jams replace 511's travel times, and TomTom closures an official
          feed already reports are dropped (without_repeats).
* google  Google's traffic speeds along its own suggested routes
          (TRAFFIC_ON_POLYLINE on the request already sent for them). No new
          account, but billed at a higher Routes tier than a plain live-traffic
          request. Slowdowns only: it reports no closures. Needs live traffic
          and our router, whose designs then avoid the jams.
* apify   A third-party Apify actor that scrapes Google Maps' traffic tiles:
          slowdowns and fully closed roads. Paid per tile, and scraping Google
          Maps is against Google's terms, which puts this app's Google keys at
          risk. Used only when named in ROUTES_ROAD_EVENT_SOURCES and APIFY_TOKEN
          is set, and capped at ROUTES_APIFY_DAILY_TILES tiles a day.

Each source's data becomes RoadEvents: a kind and a line (or a point) on the
map. The router makes them cost more or blocks them (Router.routes), and the
scorer counts the closures and lane closures each route drives through
(count_on_route).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import lru_cache
from threading import Lock
from typing import Any, Literal

import httpx
import numpy as np

import app.geo_kernel as kernel
from app.geo import (
    columns,
    Point,
    angle_between_deg,
    decode_polyline,
    densify,
    destination_point,
    haversine_m,
)
from app.kept import Kept
from app.models import LatLng
from app.overpass import BBox
from app.usage import DailyUsage

logger = logging.getLogger(__name__)

# hazard: a weather hazard on the road (fog, ice, flooding, high wind), as TomTom reports them.
EventKind = Literal["closed", "lanes", "jam", "slow", "hazard"]

# Matching events to roads on the map: within EVENT_MATCH_M of a point (GPS-
# grade positions from the sources).
EVENT_MATCH_M = 30.0
# A route drives through an event when it runs along at least this much of
# the event's stretch in one go (or half of a shorter one), within ON_ROUTE_M,
# the same way. Tighter than EVENT_MATCH_M: events are snapped to the road
# first, and a frontage road can run 30 m from a freeway. In one go, because a
# ramp merging beside a closure's end runs alongside it for a little while.
EVENT_ON_ROUTE_M = 300.0
ON_ROUTE_M = 15.0
# A lane closure with workers on site is this much riskier than an empty one.
WORKERS_PRESENT_FACTOR = 1.5
KMH_PER_MPH = 1.609344
EVENT_MATCH_DEG = 60.0  # a direction of travel this close counts as the same
PARALLEL_DEG = 45.0  # a road this close to a line, either way, runs along it
TRIP_MARGIN_M = 5_000.0  # events this far outside the trip's box can still matter

WZDX_REFRESH = timedelta(minutes=5)
WZDX_STALE_OK = timedelta(hours=1)  # if a refresh fails, the last copy is used until it's this old
# WZDx vehicle_impact values that take lanes away. "some-lanes-closed" counts only
# when a travel lane is closed, not just the shoulder.
_WZDX_KINDS: dict[str, EventKind] = {
    "all-lanes-closed": "closed",
    "some-lanes-closed": "lanes",
    "flagging": "lanes",
    "alternating-one-way": "lanes",
    "temporary-traffic-signal": "lanes",
}

WI511_EVENTS_URL = "https://511wi.gov/api/v2/get/event"
WI511_TRAVEL_TIMES_URL = "https://511wi.gov/api/v2/get/traveltimes"
WI511_REFRESH = timedelta(minutes=5)
# 511 travel times: a segment this much slower than normal is slow, or jammed.
SLOW_RATIO = 1.25
JAM_RATIO = 2.0
MAX_SLOWDOWN = 6.0  # above this the reading is more likely a detour sign's time than traffic
_WI511_CATEGORIES = {"roadwork": "roadwork", "accidentsAndIncidents": "incident", "closures": "closure"}
_COMPASS = {
    "north": 0.0, "northbound": 0.0, "east": 90.0, "eastbound": 90.0,
    "south": 180.0, "southbound": 180.0, "west": 270.0, "westbound": 270.0,
}  # fmt: skip

TOMTOM_INCIDENTS_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"
TOMTOM_REFRESH = timedelta(minutes=10)  # at most 6 requests an hour, only while trips are planned
TOMTOM_STALE_OK = timedelta(minutes=30)  # if a refresh fails, the last copy is used until it's this old
TOMTOM_MAX_BOX_KM2 = 10_000.0  # the largest box one request may cover; a larger one is split
# A closure listed once per direction: two lines, each one's ends within this of the other's, swapped.
TOMTOM_PAIR_M = 60.0
_TOMTOM_FIELDS = (
    "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,"
    "events{description,code,iconCategory},from,to,length,delay,roadNumbers}}}"
)
# TomTom's incident categories (iconCategory) that matter here.
_TT_ACCIDENT, _TT_JAM, _TT_LANE_CLOSED, _TT_ROAD_CLOSED, _TT_ROADWORKS = 1, 6, 7, 8, 9
# Weather on the road: fog, dangerous conditions, rain, ice, wind, flooding.
_TT_WEATHER = frozenset({2, 3, 4, 5, 10, 11})
# A TomTom closure with this share of its stretch on official ones (WZDx, 511) is one they already report.
REPEAT_SHARE = 0.75
OFFICIAL_SOURCES = frozenset({"wzdx", "wi511"})

APIFY_ACTOR_URL = "https://api.apify.com/v2/acts/romy~google-maps-traffic-scraper/run-sync-get-dataset-items"
APIFY_REFRESH = timedelta(minutes=10)
APIFY_MAX_RADIUS = 4  # (2r+1)^2 tiles: 81 at most per trip
APIFY_TIMEOUT_S = 180.0  # an actor run starts a container: tens of seconds is normal
# The actor's speed levels: 1 free flow (ignored), 2 slow, 3-4 congested, 6 closed.
_APIFY_KINDS: dict[int, EventKind] = {2: "slow", 3: "jam", 4: "jam", 6: "closed"}
_EARTH_CIRCUMFERENCE_M = 40_075_016.686


@dataclass(frozen=True)
class RoadEvent:
    source: str
    kind: EventKind
    path: tuple[Point, ...]  # one point, or a line along the road
    direction: float | None = None  # compass bearing of the traffic affected; None: both ways or unknown
    along: bool = False  # the traffic affected moves the path's own way (the path runs in travel direction)
    description: str = ""
    category: str = ""  # roadwork, incident or closure, when the source says; used to avoid counting one twice
    # Which roads it can be on: "off-ramp" or "on-ramp" (a ramp leaving or
    # joining the freeway), "ramp" (either), "mainline" (not a ramp), or "any".
    # Sources place a ramp closure on the freeway beside the ramp, so without
    # this it would close the freeway.
    road: str = "any"
    ramp_road: str = ""  # words naming the street a closed ramp leads to or comes from, e.g. "hawley"
    ramp_toward: float | None = None  # for a freeway-to-freeway ramp: the compass direction of the freeway it joins
    workers: bool = False  # workers on site now (WZDx worker_presence)
    speed_limit_kmh: float | None = None  # a reduced speed limit through it
    # Measured traffic (511 travel times): how many times slower than normal, and the minutes lost over the stretch.
    slowdown: float | None = None
    delay_min: float = 0.0

    def geometry_key(self) -> tuple[Any, ...]:
        """What decides where an event lies on the map: the same for every refresh of the same stretch."""
        return (self.path, self.direction, self.along, self.road, self.ramp_road, self.ramp_toward)


class EventSourceError(RuntimeError):
    """A road event source couldn't answer."""


def trip_area(*points: LatLng, margin_m: float = TRIP_MARGIN_M) -> BBox:
    """The box around a trip's places (start, stops, destination), widened by margin_m, where its road events matter."""
    south, north = min(p.lat for p in points), max(p.lat for p in points)
    west, east = min(p.lng for p in points), max(p.lng for p in points)
    middle = (south + north) / 2
    return BBox(
        destination_point((south, west), 180, margin_m)[0],
        destination_point((middle, west), 270, margin_m)[1],
        destination_point((north, east), 0, margin_m)[0],
        destination_point((middle, east), 90, margin_m)[1],
    )


def in_area(events: Sequence[RoadEvent], area: BBox) -> list[RoadEvent]:
    """The events with a point in the area. A source's events are a tuple until its next refresh: flattened once."""
    lat, lng, owner = _event_points(events)
    inside = (lat >= area.south) & (lat <= area.north) & (lng >= area.west) & (lng <= area.east)
    return [events[event] for event in np.unique(owner[inside]).tolist()]


_kept_points: Kept[tuple[Sequence[RoadEvent], tuple[np.ndarray, np.ndarray, np.ndarray]]] = Kept(8)


def _event_points(events: Sequence[RoadEvent]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Every point of the events, and which event each belongs to."""
    kept = _kept_points.get(id(events))
    if kept is not None and kept[0] is events:
        return kept[1]
    lat, lng = columns([point for event in events for point in event.path])
    owner = np.repeat(np.arange(len(events)), [len(event.path) for event in events])
    if isinstance(events, tuple):
        _kept_points.put(id(events), (events, (lat, lng, owner)))
    return lat, lng, owner


# ---- WZDx: the Work Zone Data Exchange


class WzdxFeed:
    """Work zones from a WZDx feed (by URL, or a saved copy), refreshed at most every WZDX_REFRESH."""

    name = "wzdx"

    def __init__(
        self,
        http: httpx.Client,
        *,
        url: str | None = None,
        file: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if (url is None) == (file is None):
            raise ValueError("give a WZDx feed url or a saved file, not both")
        self._http = http
        self._url = url
        self._file = file
        self._clock = clock
        self._now = now
        self._events: list[RoadEvent] | None = None
        self._fetched_at = 0.0
        self._refreshing = Lock()

    def events(self, area: BBox, ahead_s: float = 0.0) -> list[RoadEvent]:
        """ahead_s: fetch the feed this many seconds before it's due, so the trips that follow don't have to (Engine.keep_events_fresh)."""
        with self._refreshing:  # of trips planned at once, one fetches it and the rest use its answer
            age = self._clock() - self._fetched_at
            if self._events is None or age > WZDX_REFRESH.total_seconds() - ahead_s:
                try:
                    self._events = tuple(self._load())
                    self._fetched_at = self._clock()
                except EventSourceError:
                    if self._events is None or age > WZDX_STALE_OK.total_seconds():
                        raise
                    logger.warning("WZDx refresh failed; using the copy from %.0f minutes ago", age / 60)
            events = self._events
        return in_area(events, area)

    def _load(self) -> list[RoadEvent]:
        if self._file is not None:
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise EventSourceError(f"couldn't read the WZDx file {self._file.name}: {exc}") from None
        else:
            try:
                response = self._http.get(self._url, timeout=60.0)
            except httpx.HTTPError as exc:
                raise EventSourceError(f"couldn't reach the WZDx feed: {type(exc).__name__}") from None
            if response.is_error:
                raise EventSourceError(f"the WZDx feed answered {response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise EventSourceError("the WZDx feed sent something that isn't JSON") from None
        features = data.get("features") if isinstance(data, Mapping) else None
        if not isinstance(features, list):
            raise EventSourceError("the WZDx feed has no features list")
        now = self._now()
        events = [event for feature in features if (event := _wzdx_event(feature, now)) is not None]
        logger.info("WZDx: %d of %d work zones take lanes now", len(events), len(features))
        return events


def _wzdx_event(feature: Mapping[str, Any], now: datetime) -> RoadEvent | None:
    properties = feature.get("properties") or {}
    core = properties.get("core_details") or {}
    if core.get("event_type") != "work-zone":
        return None  # detours are suggested routes around a work zone, not closures
    impact = properties.get("vehicle_impact")
    kind = _WZDX_KINDS.get(impact)
    if kind is None or (impact == "some-lanes-closed" and _only_shoulders_closed(properties.get("lanes"))):
        return None
    start, end = _iso_time(properties.get("start_date")), _iso_time(properties.get("end_date"))
    if (start and start > now) or (end and end < now):
        return None  # not happening right now
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Point":
        coordinates = [coordinates]
    try:
        # A LineString follows the road; a MultiPoint gives the work zone's ends.
        path = tuple((float(lat), float(lng)) for lng, lat, *_ in coordinates)
    except (TypeError, ValueError):
        return None
    if not path:
        return None
    roads = ", ".join(core.get("road_names") or [])
    description = core.get("description") or roads
    workers = bool((properties.get("worker_presence") or {}).get("are_workers_present"))
    try:
        limit = float(properties["reduced_speed_limit_kph"]) or None
    except (KeyError, TypeError, ValueError):
        limit = None
    details = [f"speed limit {round(limit / KMH_PER_MPH)} mph"] if limit else []
    details += ["workers present"] if workers else []
    return RoadEvent(
        source="wzdx",
        kind=kind,
        path=path,
        direction=_COMPASS.get(str(core.get("direction") or "").lower()),
        description=(description[:260] + (f" ({', '.join(details)})" if details else "")),
        category="roadwork",
        road=_road_kind(description),
        ramp_road=_ramp_road(description),
        ramp_toward=_ramp_toward(description),
        workers=workers,
        speed_limit_kmh=limit,
    )


RAMP_KINDS = frozenset({"ramp", "off-ramp", "on-ramp"})
_RAMP_ROAD = re.compile(
    r"\b(?:off[- ]?ramp|exit ramp)\s+to\s+(?P<to>[^.;,]+)|\b(?:on[- ]?ramp|entrance ramp)\s+from\s+(?P<frm>[^.;,]+)",
    re.IGNORECASE,
)
# Words that don't tell streets apart: directions, road types, and the like.
_ROAD_NOISE = frozenset(
    "n s e w north south east west nb sb eb wb st street rd road av ave avenue blvd boulevard "
    "dr drive pkwy parkway hwy highway ln lane ct court way pl place the".split()
)


def road_words(name: str) -> frozenset[str]:
    """The words in a street name that tell it apart: "S 68TH ST" -> {"68th"}."""
    return frozenset(word for word in re.findall(r"[a-z0-9]+", name.lower()) if word not in _ROAD_NOISE)


_RAMP_TOWARD = re.compile(r"\bto\s+[a-z]+[- ]?\d+\s+(nb|eb|sb|wb)\b", re.IGNORECASE)
_SHORT_COMPASS = {"nb": 0.0, "eb": 90.0, "sb": 180.0, "wb": 270.0}


def _ramp_toward(description: str) -> float | None:
    """Which way the freeway a system ramp joins runs ("... WIS 175 SB TO I-94 WB" -> 270)."""
    match = _RAMP_TOWARD.search(description)
    return _SHORT_COMPASS[match.group(1).lower()] if match else None


def _ramp_road(description: str) -> str:
    """The street a ramp in the description leads to or comes from ("OFF RAMP TO HAWLEY RD" -> "hawley")."""
    match = _RAMP_ROAD.search(description)
    if not match:
        return ""
    return " ".join(sorted(road_words(match.group("to") or match.group("frm"))))


def _road_kind(description: str) -> str:
    """Which roads a description says an event is on ("Ramp Full Closure on I-94 EB at OFF RAMP TO HAWLEY RD").

    WZDx descriptions open with the kind of road (Ramp, Mainline, Local
    Road); 511's put the event type first ("Roadwork - Ramp Full Closure ...").
    Other descriptions are searched for an off- or on-ramp.
    """
    text = re.sub(r"^(roadwork|incident|closures?)\s*-\s*", "", description.strip().lower())
    if text.startswith(("mainline", "local road")):
        return "mainline" if text.startswith("mainline") else "any"
    if text.startswith("system interchange"):
        return "off-ramp"  # a freeway-to-freeway ramp, leaving the road the event is on
    if re.search(r"\boff[- ]?ramp\b|\bexit ramp\b", text):
        return "off-ramp"
    if re.search(r"\bon[- ]?ramp\b|\bentrance ramp\b", text):
        return "on-ramp"
    return "ramp" if text.startswith("ramp") else "any"


def _only_shoulders_closed(lanes: Any) -> bool:
    closed = [lane for lane in lanes or [] if isinstance(lane, Mapping) and lane.get("status") == "closed"]
    return bool(closed) and all(lane.get("type") in ("shoulder", "parking") for lane in closed)


def _iso_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---- Wisconsin 511


class Wi511Feed:
    """Wisconsin DOT's 511: closures and lane closures, plus measured travel times, refreshed at most every WI511_REFRESH.

    Travel times cover the freeways and main highways: each slow or jammed
    segment becomes a "slow" or "jam" event with its measured slowdown, so the
    router has live traffic without Google.
    """

    name = "wi511"

    def __init__(
        self,
        key: str,
        http: httpx.Client,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._key = key
        self._http = http
        self._clock = clock
        self._now = now
        self._events: list[RoadEvent] | None = None
        self._fetched_at = 0.0
        self._refreshing = Lock()

    def events(self, area: BBox, ahead_s: float = 0.0) -> list[RoadEvent]:
        with self._refreshing:  # of trips planned at once, one fetches it and the rest use its answer
            if self._events is None or self._clock() - self._fetched_at > WI511_REFRESH.total_seconds() - ahead_s:
                self._events = tuple(self._fetch())
                self._fetched_at = self._clock()
            events = self._events
        return in_area(events, area)

    def _fetch(self) -> list[RoadEvent]:
        items = self._get(WI511_EVENTS_URL)
        now = self._now()
        events = [event for item in items if (event := _wi511_event(item, now)) is not None]
        logger.info("511WI: %d of %d events affect lanes", len(events), len(items))
        try:
            segments = self._get(WI511_TRAVEL_TIMES_URL)
        except EventSourceError as exc:
            logger.warning("511WI travel times unavailable: %s", exc)  # closures still count
            return events
        traffic = [event for item in segments if (event := _wi511_traffic(item)) is not None]
        logger.info("511WI: %d of %d travel time segments are slow", len(traffic), len(segments))
        return events + traffic

    def _get(self, url: str) -> list[Any]:
        try:
            response = self._http.get(url, params={"key": self._key, "format": "json"}, timeout=20.0)
        except httpx.HTTPError as exc:
            raise EventSourceError(f"couldn't reach 511WI: {type(exc).__name__}") from None  # the URL holds the key
        if response.status_code in (401, 403):
            raise EventSourceError("511WI refused the key; check WI511_API_KEY")
        if response.status_code == 429:
            raise EventSourceError("511WI is throttling requests (10 a minute)")
        if response.is_error:
            raise EventSourceError(f"511WI answered {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise EventSourceError("511WI sent something that isn't JSON") from None
        return data if isinstance(data, list) else data.get("events") or data.get("Events") or []


def _wi511_traffic(item: Mapping[str, Any]) -> RoadEvent | None:
    """A slow or jammed travel time segment as an event along its waypoints; None in free flow."""
    try:
        normal, current = float(item["NormalTime"]), float(item["CurrentTime"])
    except (KeyError, TypeError, ValueError):
        return None
    if normal <= 0 or current < normal * SLOW_RATIO or current - normal < 1:
        return None
    try:
        path = tuple((float(w["Latitude"]), float(w["Longitude"])) for w in item.get("Waypoints") or [])
        if len(path) < 2:
            path = ((float(item["StartLatitude"]), float(item["StartLongitude"])),
                    (float(item["EndLatitude"]), float(item["EndLongitude"])))  # fmt: skip
    except (KeyError, TypeError, ValueError):
        return None
    slowdown = min(current / normal, MAX_SLOWDOWN)
    description = str(item.get("Description") or item.get("RoadwayName") or "").strip()
    return RoadEvent(
        source="wi511",
        kind="jam" if slowdown >= JAM_RATIO else "slow",
        path=path,
        along=True,  # waypoints run from the segment's start to its end
        description=f"{description}: {current:g} min now, usually {normal:g}"[:300],
        category="traffic",
        slowdown=slowdown,
        delay_min=min(current - normal, normal * (MAX_SLOWDOWN - 1)),
    )


def _wi511_event(item: Mapping[str, Any], now: float) -> RoadEvent | None:
    start, end = item.get("StartDate"), item.get("PlannedEndDate")
    if (start and start > now) or (end and end < now):
        return None  # not happening right now
    if item.get("IsFullClosure"):
        kind: EventKind = "closed"
    elif _lanes_blocked(item.get("LanesAffected") or ""):
        kind = "lanes"
    else:
        return None  # e.g. shoulder work: no lane taken
    path = _wi511_path(item)
    if not path:
        return None
    road = item.get("RoadwayName") or ""
    description = item.get("Description") or item.get("LanesAffected") or ""
    return RoadEvent(
        source="wi511",
        kind=kind,
        path=path,
        direction=_COMPASS.get(str(item.get("DirectionOfTravel") or "").strip().lower()),
        description=f"{road}: {description}"[:300] if road else description[:300],
        # Work zone closures are filed under "closures", but their description says "Roadwork - ...".
        category="roadwork"
        if description.strip().lower().startswith("roadwork")
        else _WI511_CATEGORIES.get(str(item.get("EventType") or ""), ""),
        road=_road_kind(description),
        ramp_road=_ramp_road(description),
        ramp_toward=_ramp_toward(description),
    )


def _lanes_blocked(text: str) -> bool:
    text = text.lower()
    return bool(re.search(r"\blanes?\b", text)) and not re.search(r"\bno lanes?\b|\bnone\b", text)


def _wi511_path(item: Mapping[str, Any]) -> tuple[Point, ...]:
    encoded = item.get("EncodedPolyline")
    if isinstance(encoded, str) and encoded:
        try:
            points = decode_polyline(encoded)
            if points:
                return tuple(points)
        except (ValueError, IndexError):
            pass
    try:
        first = (float(item["Latitude"]), float(item["Longitude"]))
    except (KeyError, TypeError, ValueError):
        return ()
    try:
        second = (float(item["LatitudeSecondary"]), float(item["LongitudeSecondary"]))
    except (KeyError, TypeError, ValueError):
        return (first,)
    # A secondary point far away is likely the other end of the stretch; a missing one reads as 0, 0.
    return (first, second) if 20 < haversine_m(first, second) < 50_000 else (first,)


# ---- TomTom: traffic incidents on every road


class TomTomFeed:
    """TomTom's jams, closures and lane closures, refreshed at most every TOMTOM_REFRESH.

    Each request covers a whole box: the road map's area when given (box), so
    every trip shares one answer, or else the trip's own area. Requests are
    counted against usage, a daily cap that keeps a month inside the free plan.
    The last answer is also kept in a file (saved), so a server restarted
    within TOMTOM_REFRESH uses it instead of asking again.
    """

    name = "tomtom"

    def __init__(
        self,
        key: str,
        http: httpx.Client,
        usage: DailyUsage,
        *,
        box: BBox | None = None,
        saved: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._key = key
        self._http = http
        self.usage = usage  # requests made today, against ROUTES_TOMTOM_DAILY_REQUESTS
        self._box = box
        self._saved = saved
        self._clock = clock
        self._now = now
        self._events: list[RoadEvent] | None = None
        self._fetched_box: BBox | None = None
        self._fetched_at = 0.0
        self._refreshing = Lock()

    def events(self, area: BBox, ahead_s: float = 0.0) -> list[RoadEvent]:
        box = self._box or area
        # Of trips planned at once, one asks and the rest use its answer: each request counts against the day's.
        with self._refreshing:
            if self._events is None:
                self._load_saved(box)
            age = self._clock() - self._fetched_at
            if self._events is None or box != self._fetched_box or age > TOMTOM_REFRESH.total_seconds() - ahead_s:
                try:
                    self._events = tuple(self._fetch(box))
                    self._fetched_box, self._fetched_at = box, self._clock()
                except EventSourceError:
                    if self._events is None or box != self._fetched_box or age > TOMTOM_STALE_OK.total_seconds():
                        raise
                    logger.warning("TomTom refresh failed; using the copy from %.0f minutes ago", age / 60)
            events = self._events
        return in_area(events, area)

    def _fetch(self, box: BBox) -> list[RoadEvent]:
        parts = split_box(box, TOMTOM_MAX_BOX_KM2)
        if not self.usage.take(len(parts)):
            raise EventSourceError(f"today's {self.usage.limit} requests are used up (ROUTES_TOMTOM_DAILY_REQUESTS)")
        incidents: dict[str, Mapping[str, Any]] = {}
        for part in parts:
            for incident in self._get(part):
                # An incident crossing two parts comes back in both.
                found = (incident.get("properties") or {}).get("id") or f"#{len(incidents)}"
                incidents.setdefault(str(found), incident)
        events = tomtom_events(incidents.values())
        logger.info("TomTom: %d road events from %d incidents", len(events), len(incidents))
        self._save(box, list(incidents.values()))
        return events

    def _save(self, box: BBox, incidents: list[Mapping[str, Any]]) -> None:
        if self._saved is None:
            return
        try:
            self._saved.parent.mkdir(parents=True, exist_ok=True)
            data = {"fetched": self._now(), "box": [box.south, box.west, box.north, box.east], "incidents": incidents}
            temporary = self._saved.with_suffix(".tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            temporary.replace(self._saved)
        except OSError as exc:
            logger.warning("Couldn't keep TomTom's answer in %s: %s", self._saved.name, exc)

    def _load_saved(self, box: BBox) -> None:
        """The answer a previous run of the server kept, if it's for this box and recent enough."""
        if self._saved is None or not self._saved.exists():
            return
        try:
            data = json.loads(self._saved.read_text(encoding="utf-8"))
            age = self._now() - float(data["fetched"])
            if data["box"] != [box.south, box.west, box.north, box.east] or not 0 <= age <= TOMTOM_REFRESH.total_seconds():
                return
            events = tomtom_events(data["incidents"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring TomTom's kept answer in %s: %s", self._saved.name, exc)
            return
        self._events, self._fetched_box, self._fetched_at = tuple(events), box, self._clock() - age
        logger.info("TomTom: using the answer from %.0f minutes ago, kept across a restart", age / 60)

    def _get(self, part: BBox) -> list[Mapping[str, Any]]:
        params = {
            "key": self._key,
            "bbox": f"{part.west},{part.south},{part.east},{part.north}",
            "fields": _TOMTOM_FIELDS,
            "language": "en-US",
            "timeValidityFilter": "present",
        }
        try:
            response = self._http.get(TOMTOM_INCIDENTS_URL, params=params, timeout=20.0)
        except httpx.HTTPError as exc:
            raise EventSourceError(f"couldn't reach TomTom: {type(exc).__name__}") from None  # the URL holds the key
        if response.status_code in (401, 403):
            raise EventSourceError("TomTom refused the key, or its free allowance is used up; check TOMTOM_API_KEY")
        if response.status_code == 429:
            raise EventSourceError("TomTom is throttling requests")
        if response.is_error:
            raise EventSourceError(f"TomTom answered {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise EventSourceError("TomTom sent something that isn't JSON") from None
        incidents = data.get("incidents") if isinstance(data, Mapping) else None
        if not isinstance(incidents, list):
            raise EventSourceError("TomTom's answer has no incidents list")
        return [incident for incident in incidents if isinstance(incident, Mapping)]


def split_box(box: BBox, max_km2: float) -> list[BBox]:
    """box cut into equal parts of at most max_km2 each."""
    height_km = haversine_m((box.south, box.west), (box.north, box.west)) / 1000
    width_km = max(haversine_m((lat, box.west), (lat, box.east)) for lat in (box.south, box.north)) / 1000
    pieces = max(1, math.ceil(height_km * width_km / max_km2))
    columns = math.ceil(math.sqrt(pieces))
    return box.tiles(math.ceil(pieces / columns), columns)


def tomtom_events(incidents: Iterable[Mapping[str, Any]]) -> list[RoadEvent]:
    """TomTom incidents as road events; a closure listed once per direction becomes one event for both.

    Each incident is one direction of traffic, its line drawn the way that
    traffic goes, so a street closed both ways comes as two lines, each the
    other reversed. One event covers both: one closure on the map, not two.
    """
    parsed = [found for incident in incidents if (found := _tomtom_event(incident)) is not None]
    cell = TOMTOM_PAIR_M * 2 / 111_320.0  # degrees: a point within TOMTOM_PAIR_M is in the same cell or the next

    def cell_of(point: Point) -> tuple[int, int]:
        return math.floor(point[0] / cell), math.floor(point[1] / cell)

    ends: dict[tuple[int, int], list[int]] = defaultdict(list)  # where each closure's line ends -> its index
    for i, (event, _) in enumerate(parsed):
        if event.kind in ("closed", "lanes"):
            ends[cell_of(event.path[-1])].append(i)
    taken: set[int] = set()  # closures already part of a pair

    def other_way(i: int, event: RoadEvent) -> int | None:
        ci, cj = cell_of(event.path[0])
        for j in (j for a in (ci - 1, ci, ci + 1) for b in (cj - 1, cj, cj + 1) for j in ends.get((a, b), ())):
            other = parsed[j][0]
            if (
                j > i
                and j not in taken
                and other.kind == event.kind
                and haversine_m(event.path[0], other.path[-1]) <= TOMTOM_PAIR_M
                and haversine_m(event.path[-1], other.path[0]) <= TOMTOM_PAIR_M
            ):
                return j
        return None

    events: list[RoadEvent] = []
    for i, (event, properties) in enumerate(parsed):
        if i in taken:
            continue
        j = other_way(i, event) if event.kind in ("closed", "lanes") else None
        if j is not None:
            taken.add(j)
            event = replace(event, along=False, description=_tomtom_description(properties, both_ways=True))
        events.append(event)
    return events


def _tomtom_event(incident: Mapping[str, Any]) -> tuple[RoadEvent, Mapping[str, Any]] | None:
    properties = incident.get("properties") or {}
    kind = _tomtom_kind(properties)
    if kind is None:
        return None
    geometry = incident.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Point":
        coordinates = [coordinates]
    try:
        path = tuple((float(lat), float(lng)) for lng, lat, *_ in coordinates)
    except (TypeError, ValueError):
        return None
    if not path:
        return None
    categories = _tomtom_categories(properties)
    if kind in ("jam", "slow"):
        category = "traffic"
    elif kind == "hazard":
        category = "weather"
    elif _TT_ACCIDENT in categories:
        category = "incident"
    else:
        category = "roadwork" if _TT_ROADWORKS in categories else "closure"
    try:
        delay_min = max(0.0, float(properties.get("delay") or 0) / 60)
    except (TypeError, ValueError):
        delay_min = 0.0
    event = RoadEvent(
        source="tomtom",
        kind=kind,
        path=path,
        along=True,  # one direction of traffic, drawn the way it goes
        description=_tomtom_description(properties),
        category=category,
        delay_min=delay_min,
    )
    return event, properties


def _tomtom_details(properties: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """An incident's events: what happened, one or more ("Accident", "Lane closed")."""
    return [detail for detail in properties.get("events") or [] if isinstance(detail, Mapping)]


def _tomtom_categories(properties: Mapping[str, Any]) -> set[Any]:
    return {properties.get("iconCategory")} | {detail.get("iconCategory") for detail in _tomtom_details(properties)}


def _tomtom_kind(properties: Mapping[str, Any]) -> EventKind | None:
    details = _tomtom_details(properties)
    categories = _tomtom_categories(properties)
    if _TT_ROAD_CLOSED in categories:
        return "closed"
    lanes = [detail for detail in details if detail.get("iconCategory") == _TT_LANE_CLOSED]
    if any("shoulder" not in str(detail.get("description") or "").lower() for detail in lanes) or (
        properties.get("iconCategory") == _TT_LANE_CLOSED and not details
    ):
        return "lanes"  # a travel lane, not just the shoulder
    if _TT_ACCIDENT in categories:
        return "lanes"  # a crash scene takes lanes for a while, and invites another crash
    if _TT_JAM in categories:
        return "slow" if properties.get("magnitudeOfDelay") == 1 else "jam"
    if categories & _TT_WEATHER:
        return "hazard"  # fog, ice, flooding: every lane open, but harder to drive
    return None  # roadwork beside the road, a car on the shoulder: no lane taken


def _tomtom_description(properties: Mapping[str, Any], *, both_ways: bool = False) -> str:
    """'I-94 W: Accident, Broken down vehicle (Holt Ave to Becher St), 4 min delay'."""
    what = ", ".join(dict.fromkeys(str(d["description"]) for d in _tomtom_details(properties) if d.get("description")))
    roads = ", ".join(str(road) for road in properties.get("roadNumbers") or [])
    text = f"{roads}: {what or 'Traffic incident'}" if roads else what or "Traffic incident"
    stretch = " to ".join(str(properties[end]) for end in ("from", "to") if properties.get(end))
    if stretch or both_ways:
        text += f" ({', '.join(part for part in (stretch, 'both directions' if both_ways else '') if part)})"
    try:
        delay_s = float(properties.get("delay") or 0)
    except (TypeError, ValueError):
        delay_s = 0.0
    if delay_s >= 60:
        text += f", {round(delay_s / 60)} min delay"
    return text[:300]


# ---- Google: traffic speeds along its own suggested routes


def jams_on_routes(routes: Iterable[Mapping[str, Any]]) -> list[RoadEvent]:
    """Slow and jammed stretches of routes fetched with TRAFFIC_ON_POLYLINE (see RouteRequest)."""
    events = []
    for route in routes:
        points = decode_polyline(route.get("polyline", {}).get("encodedPolyline", ""))
        for interval in route.get("travelAdvisory", {}).get("speedReadingIntervals", []):
            kind: EventKind | None = {"SLOW": "slow", "TRAFFIC_JAM": "jam"}.get(interval.get("speed", ""))
            if kind is None:
                continue
            # Start inclusive, end exclusive, and each interval starts where the last one ended:
            # the stretch runs from its start point to the next interval's start point.
            start = interval.get("startPolylinePointIndex", 0)
            end = interval.get("endPolylinePointIndex", len(points) - 1)
            stretch = tuple(points[start : end + 1])
            if len(stretch) >= 2:
                events.append(RoadEvent(source="google", kind=kind, path=stretch, along=True))
    return events


# ---- Apify: scraped traffic tiles (paid, against Google's terms; opt-in only)


class ApifyTraffic:
    """Traffic tiles around a trip from the romy/google-maps-traffic-scraper Apify actor, cached for APIFY_REFRESH."""

    name = "apify"

    def __init__(
        self,
        token: str,
        http: httpx.Client,
        usage: DailyUsage,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._http = http
        self.usage = usage  # tiles fetched today, against ROUTES_APIFY_DAILY_TILES
        self._clock = clock
        self._cache: dict[tuple[float, float, int, int], tuple[float, list[RoadEvent]]] = {}

    def events(self, area: BBox) -> list[RoadEvent]:
        zoom, radius, center = tiles_for(area)
        key = (round(center[0], 2), round(center[1], 2), zoom, radius)
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] <= APIFY_REFRESH.total_seconds():
            return cached[1]

        tiles = (2 * radius + 1) ** 2
        if not self.usage.take(tiles):
            raise EventSourceError(
                f"{tiles} more tiles would pass today's limit of {self.usage.limit} (ROUTES_APIFY_DAILY_TILES)"
            )
        logger.info("Apify: fetching %d traffic tiles at zoom %d around %.4f,%.4f", tiles, zoom, *center)
        body = {"lat": f"{center[0]:.6f}", "lon": f"{center[1]:.6f}", "zoom": zoom, "radius": radius, "seconds": -1}
        try:
            response = self._http.post(
                APIFY_ACTOR_URL,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=APIFY_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise EventSourceError(f"couldn't reach Apify: {type(exc).__name__}") from None
        if response.status_code == 401:
            raise EventSourceError("Apify refused the token; check APIFY_TOKEN")
        if response.status_code == 402:
            raise EventSourceError("the Apify account is out of credit")
        if response.is_error:
            raise EventSourceError(f"Apify answered {response.status_code}")
        try:
            items = response.json()
        except ValueError:
            raise EventSourceError("Apify sent something that isn't JSON") from None
        events = [_apify_event(feature) for feature in _features(items)]
        events = [event for event in events if event is not None]
        self._cache[key] = (self._clock(), events)
        return events


def tiles_for(area: BBox) -> tuple[int, int, Point]:
    """(zoom, tile radius, center) covering area with the most detail for at most APIFY_MAX_RADIUS."""
    center = ((area.south + area.north) / 2, (area.west + area.east) / 2)
    span_m = max(
        haversine_m((area.south, center[1]), (area.north, center[1])),
        haversine_m((center[0], area.west), (center[0], area.east)),
    )
    for zoom in (15, 14, 13, 12):
        tile_m = _EARTH_CIRCUMFERENCE_M * math.cos(math.radians(center[0])) / 2**zoom
        radius = max(0, math.ceil((span_m / tile_m - 1) / 2))
        if radius <= APIFY_MAX_RADIUS:
            return zoom, radius, center
    return 12, APIFY_MAX_RADIUS, center  # a very long trip: its middle is covered


def _features(data: Any) -> Iterator[Mapping[str, Any]]:
    """GeoJSON features from the actor's dataset items, whatever they're wrapped in."""
    if isinstance(data, list):
        for item in data:
            yield from _features(item)
    elif isinstance(data, Mapping):
        if data.get("type") == "Feature":
            yield data
        elif isinstance(data.get("features"), list):
            yield from _features(data["features"])
        elif "results" in data:
            results = data["results"]
            if isinstance(results, str):
                try:
                    results = json.loads(results)
                except ValueError:
                    return
            yield from _features(results)


def _apify_event(feature: Mapping[str, Any]) -> RoadEvent | None:
    properties = feature.get("properties") or {}
    try:
        kind = _APIFY_KINDS.get(int(properties.get("speed_level")))
    except (TypeError, ValueError):
        return None
    geometry = feature.get("geometry") or {}
    if kind is None or geometry.get("type") != "LineString":
        return None
    try:
        path = tuple((float(lat), float(lng)) for lng, lat, *_ in geometry.get("coordinates", []))
    except (TypeError, ValueError):
        return None
    if not path:
        return None
    # Which way the tile's lines run isn't documented, so both directions count.
    return RoadEvent(source="apify", kind=kind, path=path, description=str(properties.get("speed_label") or ""))


# ---- Choosing a source


@dataclass
class EventReport:
    source: str | None  # the sources used, joined with "+"; None when none could be
    events: list[RoadEvent]
    skipped: list[str] = field(default_factory=list)  # sources tried that weren't set up or didn't answer
    notes: list[str] = field(default_factory=list)  # why they were skipped
    google: bool = False  # ask Google for traffic along its suggestions; those events arrive with them


def _asked(feed: Any, area: BBox, ahead_s: float) -> list[RoadEvent]:
    """A kept feed's events in the area: fetched ahead of time only when that's asked for."""
    return feed.events(area, ahead_s) if ahead_s else feed.events(area)


def collect(
    order: Sequence[str],
    area: BBox,
    *,
    wzdx: WzdxFeed | None,
    wi511: Wi511Feed | None,
    apify: ApifyTraffic | None,
    google_ready: bool,
    tomtom: TomTomFeed | None = None,
    ahead_s: float = 0.0,
) -> EventReport:
    """Road events for a trip from the first group of sources in order ("a+b" is a group) where any answers.

    ahead_s: the feeds kept by the server (WZDx, 511, TomTom) are fetched this many seconds before they're due."""
    report = EventReport(source=None, events=[])
    for group in order:
        answered: dict[str, list[RoadEvent]] = {}
        for name in group.split("+"):
            try:
                if name == "wzdx":
                    if wzdx is None:
                        raise EventSourceError("no WZDx feed is set up")
                    answered[name] = _asked(wzdx, area, ahead_s)
                elif name == "wi511":
                    if wi511 is None:
                        raise EventSourceError("WI511_API_KEY is not set")
                    answered[name] = _asked(wi511, area, ahead_s)
                elif name == "tomtom":
                    if tomtom is None:
                        raise EventSourceError("TOMTOM_API_KEY is not set")
                    answered[name] = _asked(tomtom, area, ahead_s)
                elif name == "google":
                    if not google_ready:
                        raise EventSourceError("it needs live traffic and our router")
                    answered[name] = []
                    report.google = True
                elif name == "apify":
                    if apify is None:
                        raise EventSourceError("APIFY_TOKEN is not set")
                    answered[name] = apify.events(area)
                else:
                    raise EventSourceError("unknown source")
            except EventSourceError as exc:
                report.skipped.append(name)
                report.notes.append(f"Road events: {name} skipped: {exc}.")
        if answered:
            if "wzdx" in answered and "wi511" in answered:
                # The same work zones, in less detail: keep 511's incidents and closures only.
                answered["wi511"] = [event for event in answered["wi511"] if event.category != "roadwork"]
            if "tomtom" in answered and "wi511" in answered:
                # TomTom's jams cover every road, 511's travel times only highways: both would count a jam twice.
                answered["wi511"] = [event for event in answered["wi511"] if event.category != "traffic"]
            report.source = "+".join(answered)
            report.events = [event for events in answered.values() for event in events]
            return report
    return report


# ---- Which events a route drives through


def count_on_route(points: Sequence[Point], events: Iterable[RoadEvent]) -> tuple[int, int, int]:
    """(closures, lane closures, lane closures with workers present) a route drives through, the way their traffic goes."""
    if len(points) < 2:
        return 0, 0, 0
    table = _event_table(events)
    wanted = table.closed | table.lanes
    if not wanted.any():
        return 0, 0, 0
    driven = _route_index(tuple(points)).runs_along_each(table, wanted)
    return (
        int((driven & table.closed).sum()),
        int((driven & table.lanes).sum()),
        int((driven & table.lanes & table.workers).sum()),
    )


def weather_on_route(points: Sequence[Point], events: Iterable[RoadEvent]) -> int:
    """Weather hazards (fog, ice, flooding) a route drives through, the way their traffic goes."""
    if len(points) < 2:
        return 0
    table = _event_table(events)
    if not table.hazard.any():
        return 0
    return int(_route_index(tuple(points)).runs_along_each(table, table.hazard).sum())


def delay_on_route(points: Sequence[Point], events: Iterable[RoadEvent]) -> float:
    """Minutes of measured traffic delay a route picks up: each slow segment's delay, for the share of it driven."""
    if len(points) < 2:
        return 0.0
    table = _event_table(events)
    if not table.delayed.any():
        return 0.0
    shares = _route_index(tuple(points)).share_along_each(table, table.delayed)
    return sum(table.delay_min[event] * shares[event] for event in np.flatnonzero(table.delayed).tolist())


def without_repeats(events: Sequence[RoadEvent]) -> list[RoadEvent]:
    """The events, less TomTom's closures and lane closures that an official feed (WZDx, 511) already reports.

    For events matched to the map (Engine.snap): stretches of road, each in
    its direction of travel. A TomTom one with REPEAT_SHARE of its stretch on
    official closures, or official ones of its own kind, is the same thing
    reported twice. The official one says more (workers on site, the speed
    limit, which ramp), and counting both would make a route look riskier.
    """
    official = [
        event for event in events if event.source in OFFICIAL_SOURCES and event.kind in ("closed", "lanes") and len(event.path) > 1
    ]
    if not official:
        return list(events)
    boxes = _boxes(official)  # once: each of TomTom's events is tested against all of them

    def repeated(event: RoadEvent) -> bool:
        lines = [_route_index(other.path) for other in _near(event.path, official, boxes) if other.kind in ("closed", event.kind)]
        if not lines:
            return False
        samples_lat, samples_lng = kernel.densified(*columns(event.path), EVENT_MATCH_M / 2)
        bearings = kernel.bearings_of(samples_lat, samples_lng)
        covered = np.zeros(samples_lat.size, dtype=np.bool_)
        has_direction = event.direction is not None
        direction = event.direction if has_direction else 0.0
        for other in lines:
            kernel.mark_carried(other.line, samples_lat, samples_lng, bearings, has_direction, direction, event.along, MATCHING, covered)
        return int(covered.sum()) >= REPEAT_SHARE * samples_lat.size

    return [
        event
        for event in events
        if not (event.source == "tomtom" and event.kind in ("closed", "lanes") and len(event.path) > 1 and repeated(event))
    ]


def near_routes(events: Iterable[RoadEvent], routes: Sequence[Sequence[Point]], within_m: float) -> list[RoadEvent]:
    """The events that come within about within_m of a route (up to three times that): the ones worth drawing."""
    if not any(routes):
        return []
    events = list(events)
    cell_lat = within_m / 111_320.0
    cell_lng = cell_lat / max(math.cos(math.radians(next(route for route in routes if route)[0][0])), 0.01)
    route_ends = np.cumsum([0, *(len(route) for route in routes)], dtype=np.int64)
    route_lat, route_lng = columns([point for route in routes for point in route])
    event_ends = np.cumsum([0, *(len(event.path) for event in events)], dtype=np.int64)
    event_lat, event_lng = columns([point for event in events for point in event.path])
    near = np.zeros(len(events), dtype=np.bool_)
    kernel.lines_near_lines(event_ends, event_lat, event_lng, route_ends, route_lat, route_lng, within_m / 2, cell_lat, cell_lng, near)
    return [event for event, is_near in zip(events, near.tolist()) if is_near]


def _boxes(events: Sequence[RoadEvent]) -> np.ndarray:
    """Each event's box, a row of south, west, north, east: for _near."""
    rows = []
    for event in events:
        lat, lng = zip(*event.path)
        rows.append((min(lat), min(lng), max(lat), max(lng)))
    return np.array(rows, dtype=np.float64).reshape(-1, 4)


def _near(points: Sequence[Point], events: Sequence[RoadEvent], boxes: np.ndarray) -> list[RoadEvent]:
    """The events whose box (their row of boxes, from _boxes) overlaps the route's: a quick test first, as a
    trip can have a hundred around it."""
    margin = EVENT_MATCH_M / 111_320.0 * 2
    lat, lng = zip(*points)
    south, north = min(lat) - margin, max(lat) + margin
    west, east = min(lng) - margin * 1.5, max(lng) + margin * 1.5
    overlapping = (boxes[:, 0] <= north) & (boxes[:, 2] >= south) & (boxes[:, 1] <= east) & (boxes[:, 3] >= west)
    return [events[index] for index in np.flatnonzero(overlapping).tolist()]


class _EventTable:
    """Road events as flat columns, for the compiled matching, with which kind each one is."""

    def __init__(self, events: Sequence[RoadEvent]) -> None:
        events = [event for event in events if event.path]  # no source sends one without a place, and it can't be on a route
        ends = np.cumsum([0, *(len(event.path) for event in events)], dtype=np.int64)
        points = np.array([point for event in events for point in event.path], dtype=np.float64).reshape(-1, 2)
        lat, lng = points[:, 0].copy(), points[:, 1].copy()
        starts = ends[:-1]
        count = len(events)
        empty = count == 0
        self.columns = kernel.EventColumns(
            ends,
            lat,
            lng,
            np.minimum.reduceat(lat, starts) if not empty else np.empty(0),
            np.minimum.reduceat(lng, starts) if not empty else np.empty(0),
            np.maximum.reduceat(lat, starts) if not empty else np.empty(0),
            np.maximum.reduceat(lng, starts) if not empty else np.empty(0),
            np.fromiter((event.direction is not None for event in events), dtype=np.bool_, count=count),
            np.fromiter((event.direction or 0.0 for event in events), dtype=np.float64, count=count),
            np.fromiter((event.along for event in events), dtype=np.bool_, count=count),
            np.fromiter((event.road in RAMP_KINDS for event in events), dtype=np.bool_, count=count),
            spacing_m=EVENT_MATCH_M / 2,
        )
        self.count = count
        self.closed = np.fromiter((event.kind == "closed" for event in events), dtype=np.bool_, count=count)
        self.lanes = np.fromiter((event.kind == "lanes" for event in events), dtype=np.bool_, count=count)
        self.hazard = np.fromiter((event.kind == "hazard" for event in events), dtype=np.bool_, count=count)
        self.workers = np.fromiter((bool(event.workers) for event in events), dtype=np.bool_, count=count)
        self.delay_min = [event.delay_min for event in events]
        self.delayed = np.fromiter((event.delay_min > 0 and len(event.path) > 1 for event in events), dtype=np.bool_, count=count)


_event_tables: Kept[tuple[tuple[RoadEvent, ...], _EventTable]] = Kept(16)


def _event_table(events: Iterable[RoadEvent]) -> _EventTable:
    """A trip's events, flattened once: every route of the trip is judged against the same tuple of them."""
    if not isinstance(events, tuple):
        return _EventTable(tuple(events))
    kept = _event_tables.get(id(events))
    if kept is not None and kept[0] is events:
        return kept[1]
    table = _EventTable(events)
    _event_tables.put(id(events), (events, table))
    return table


class _RouteIndex:
    """A route's points with their direction of travel, in a grid for "is the route near here?" lookups."""

    def __init__(self, points: Sequence[Point]) -> None:
        lat, lng = columns(points)
        dense_lat, dense_lng = kernel.densified(lat, lng, EVENT_MATCH_M / 3)
        grid = kernel.make_grid(dense_lat, dense_lng, EVENT_MATCH_M / 111_320.0)
        self.line = kernel.RouteLine(
            grid,
            kernel.bearings_of(dense_lat, dense_lng)[grid.items],
            kernel.reach_lng_at(float(dense_lat[0])),
            float(lat.min()),
            float(lng.min()),
            float(lat.max()),
            float(lng.max()),
            near_m=EVENT_MATCH_M,
            m_per_deg=111_320.0,
        )

    def runs_along_each(self, table: _EventTable, wanted: np.ndarray) -> np.ndarray:
        """For each of the wanted events, whether the route drives through it, the way its traffic goes."""
        driven = np.zeros(table.count, dtype=np.bool_)
        kernel.runs_along_each(self.line, table.columns, wanted, MATCHING, driven)
        return driven

    def share_along_each(self, table: _EventTable, wanted: np.ndarray) -> np.ndarray:
        """For each of the wanted events, the share of its stretch the route drives, the way its traffic goes."""
        shares = np.zeros(table.count)
        kernel.share_along_each(self.line, table.columns, wanted, MATCHING, shares)
        return shares


@lru_cache(maxsize=64)
def _route_index(points: tuple[Point, ...]) -> _RouteIndex:
    """Kept for a while: a route is judged for closures, for hazards and for delays, and event lines come back trip after trip."""
    return _RouteIndex(points)


MATCHING = kernel.Matching(
    EVENT_MATCH_M,
    ON_ROUTE_M,
    EVENT_ON_ROUTE_M,
    EVENT_MATCH_DEG,
    PARALLEL_DEG,
    EVENT_MATCH_M / 111_320.0 * 2,
    EVENT_MATCH_M / 111_320.0 * 2 * 1.5,
)


def carries(travel: float, line: float | None, direction: float | None, along: bool) -> bool:
    """Whether traffic heading along travel, beside an event's line (bearing line there), is the traffic it affects.

    direction is the event's compass direction, or None for both ways; along
    means the line's own direction. Traffic crossing the line isn't affected.
    """
    if along and line is not None:
        return angle_between_deg(travel, line) <= EVENT_MATCH_DEG
    if line is not None and min(angle_between_deg(travel, line), 180 - angle_between_deg(travel, line)) > PARALLEL_DEG:
        return False  # crossing the event's road, not driving along it
    return direction is None or angle_between_deg(travel, direction) <= EVENT_MATCH_DEG
