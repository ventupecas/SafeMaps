"""Road events the router steers around: closures, lane closures and traffic jams.

Four sources. ROUTES_ROAD_EVENT_SOURCES lists them in order of preference; a
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from app.geo import (
    Point,
    angle_between_deg,
    decode_polyline,
    densify,
    destination_point,
    haversine_m,
    initial_bearing_deg,
    path_length_m,
)
from app.models import LatLng
from app.overpass import BBox
from app.usage import DailyUsage

logger = logging.getLogger(__name__)

EventKind = Literal["closed", "lanes", "jam", "slow"]

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
WI511_REFRESH = timedelta(minutes=5)
_WI511_CATEGORIES = {"roadwork": "roadwork", "accidentsAndIncidents": "incident", "closures": "closure"}
_COMPASS = {
    "north": 0.0, "northbound": 0.0, "east": 90.0, "eastbound": 90.0,
    "south": 180.0, "southbound": 180.0, "west": 270.0, "westbound": 270.0,
}  # fmt: skip

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


class EventSourceError(RuntimeError):
    """A road event source couldn't answer."""


def trip_area(origin: LatLng, destination: LatLng, margin_m: float = TRIP_MARGIN_M) -> BBox:
    """The box around a trip, widened by margin_m, where its road events matter."""
    south, north = sorted((origin.lat, destination.lat))
    west, east = sorted((origin.lng, destination.lng))
    middle = (south + north) / 2
    return BBox(
        destination_point((south, west), 180, margin_m)[0],
        destination_point((middle, west), 270, margin_m)[1],
        destination_point((north, east), 0, margin_m)[0],
        destination_point((middle, east), 90, margin_m)[1],
    )


def in_area(events: Iterable[RoadEvent], area: BBox) -> list[RoadEvent]:
    return [event for event in events if any(area.contains(lat, lng) for lat, lng in event.path)]


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

    def events(self, area: BBox) -> list[RoadEvent]:
        age = self._clock() - self._fetched_at
        if self._events is None or age > WZDX_REFRESH.total_seconds():
            try:
                self._events = self._load()
                self._fetched_at = self._clock()
            except EventSourceError:
                if self._events is None or age > WZDX_STALE_OK.total_seconds():
                    raise
                logger.warning("WZDx refresh failed; using the copy from %.0f minutes ago", age / 60)
        return in_area(self._events, area)

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
    Road); other descriptions are searched for an off- or on-ramp.
    """
    text = description.strip().lower()
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
    """Wisconsin DOT's 511 events: closures and lane closures, statewide, refreshed at most every WI511_REFRESH."""

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

    def events(self, area: BBox) -> list[RoadEvent]:
        if self._events is None or self._clock() - self._fetched_at > WI511_REFRESH.total_seconds():
            self._events = self._fetch()
            self._fetched_at = self._clock()
        return in_area(self._events, area)

    def _fetch(self) -> list[RoadEvent]:
        try:
            response = self._http.get(WI511_EVENTS_URL, params={"key": self._key, "format": "json"}, timeout=20.0)
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
        items = data if isinstance(data, list) else data.get("events") or data.get("Events") or []
        now = self._now()
        events = [event for item in items if (event := _wi511_event(item, now)) is not None]
        logger.info("511WI: %d of %d events affect lanes", len(events), len(items))
        return events


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
        category=_WI511_CATEGORIES.get(str(item.get("EventType") or ""), ""),
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


def collect(
    order: Sequence[str],
    area: BBox,
    *,
    wzdx: WzdxFeed | None,
    wi511: Wi511Feed | None,
    apify: ApifyTraffic | None,
    google_ready: bool,
) -> EventReport:
    """Road events for a trip from the first group of sources in order ("a+b" is a group) where any answers."""
    report = EventReport(source=None, events=[])
    for group in order:
        answered: dict[str, list[RoadEvent]] = {}
        for name in group.split("+"):
            try:
                if name == "wzdx":
                    if wzdx is None:
                        raise EventSourceError("no WZDx feed is set up")
                    answered[name] = wzdx.events(area)
                elif name == "wi511":
                    if wi511 is None:
                        raise EventSourceError("WI511_API_KEY is not set")
                    answered[name] = wi511.events(area)
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
            report.source = "+".join(answered)
            report.events = [event for events in answered.values() for event in events]
            return report
    return report


# ---- Which events a route drives through


def count_on_route(points: Sequence[Point], events: Iterable[RoadEvent]) -> tuple[int, int, int]:
    """(closures, lane closures, lane closures with workers present) a route drives through, the way their traffic goes."""
    if len(points) < 2:
        return 0, 0, 0
    # A quick box test first: a trip can have a hundred work zones around it.
    margin = EVENT_MATCH_M / 111_320.0 * 2
    south, north = min(p[0] for p in points) - margin, max(p[0] for p in points) + margin
    west, east = min(p[1] for p in points) - margin * 1.5, max(p[1] for p in points) + margin * 1.5
    relevant = [
        event
        for event in events
        if event.kind in ("closed", "lanes")
        and min(p[0] for p in event.path) <= north
        and max(p[0] for p in event.path) >= south
        and min(p[1] for p in event.path) <= east
        and max(p[1] for p in event.path) >= west
    ]
    if not relevant:
        return 0, 0, 0
    route = _RouteIndex(points)
    closures = lane_closures = with_workers = 0
    for event in relevant:
        if route.runs_along(event):
            if event.kind == "closed":
                closures += 1
            else:
                lane_closures += 1
                with_workers += event.workers
    return closures, lane_closures, with_workers


class _RouteIndex:
    """A route's points with their direction of travel, in a grid for "is the route near here?" lookups."""

    def __init__(self, points: Sequence[Point]) -> None:
        dense = densify(points, EVENT_MATCH_M / 3)
        self.cell_deg = EVENT_MATCH_M / 111_320.0
        self.reach_lng = math.ceil(1 / max(math.cos(math.radians(dense[0][0])), 0.01))
        self.cells: dict[tuple[int, int], list[tuple[Point, float]]] = defaultdict(list)
        for i, point in enumerate(dense):
            a, b = (point, dense[i + 1]) if i + 1 < len(dense) else (dense[i - 1], point)
            self.cells[self._cell(point)].append((point, initial_bearing_deg(a, b)))

    def bearings_near(self, point: Point, within_m: float) -> list[float]:
        """Directions of travel of the route's points within within_m (at most EVENT_MATCH_M) of point."""
        ci, cj = self._cell(point)
        return [
            bearing
            for i in range(ci - 1, ci + 2)
            for j in range(cj - self.reach_lng, cj + self.reach_lng + 1)
            for other, bearing in self.cells.get((i, j), ())
            if haversine_m(point, other) <= within_m
        ]

    def runs_along(self, event: RoadEvent) -> bool:
        if len(event.path) == 1:
            # A source's point may sit a little off the road: the looser match.
            near = self.bearings_near(event.path[0], EVENT_MATCH_M)
            return any(carries(bearing, None, event.direction, event.along) for bearing in near)
        if event.road in RAMP_KINDS and not (
            self.bearings_near(event.path[0], ON_ROUTE_M) and self.bearings_near(event.path[-1], ON_ROUTE_M)
        ):
            return False  # a ramp runs beside its freeway for a while: using it means passing both its ends
        spacing = EVENT_MATCH_M / 2
        samples = densify(event.path, spacing)
        longest = run = misses = 0
        for i, point in enumerate(samples):
            a, b = (point, samples[i + 1]) if i + 1 < len(samples) else (samples[i - 1], point)
            line = initial_bearing_deg(a, b)
            near = self.bearings_near(point, ON_ROUTE_M)
            if any(carries(bearing, line, event.direction, event.along) for bearing in near):
                run += 1 + misses  # a single missed sample (a curve drawn differently) doesn't break the run
                misses = 0
            elif run and not misses:
                misses = 1
            else:
                run = misses = 0
            longest = max(longest, run)
        return longest * spacing >= min(EVENT_ON_ROUTE_M, path_length_m(event.path) / 2)

    def _cell(self, point: Point) -> tuple[int, int]:
        return math.floor(point[0] / self.cell_deg), math.floor(point[1] / self.cell_deg)


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
