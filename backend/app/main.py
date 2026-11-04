"""HTTP API for the routing engine.

Run from backend/:

    uvicorn app.main:app --reload --port 3143

Then open http://127.0.0.1:3143 for the map, or http://127.0.0.1:3143/docs to
try the API directly.

    GET  /health      what's loaded: road graph, crash data, cache mode, today's Google usage
    GET  /profiles    the preset driver profiles and their weights
    GET  /app-config  which map the page shows (ROUTES_MAP), and whether it has a traffic layer
    POST /route       rank routes for one trip; the map draws plan.winner.encoded_polyline
    GET  /traffic-tiles/{z}/{x}/{y}.png   TomTom's traffic map, for the OpenStreetMap page
    GET  /bus-routes  the county's bus routes, for the OpenStreetMap page's Transit layer
    POST /bike-route  bike routes for one trip, lowest stress first (app.bike; needs the bike map)
    POST /walk-route  walking routes for one trip, the same way (needs the walking map)
    GET  /bike-stress the bike map's traffic stress in a box, for the page's Bike stress layer
    GET  /expo-stats  today's trips and answers, for the expo's stats screen (stats.html)
    POST /guess       a visitor's guess in the "Which route is safer?" game (game.html)
    GET  /expo-control, POST /expo-control   the expo's control page (control.html): pause demo mode, restart the stats
    GET  /            the web map (frontend/), when that folder exists

The frontend never calls Google's Routes API itself. Routing, scoring and
every Google call happen here, behind the server-side API key. So do road
events (closures, lane closures, jams): see app.road_events. TomTom's traffic
tiles come through here too, so its key stays on the server.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import pairwise
from threading import Lock, Thread
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.cache import CacheMiss
from app.candidates import CandidateOptions
from app.config import BACKEND_DIR, Settings
from app.driving_side import DrivingSide
from app.engine import (
    NoRouteFoundError,
    build_candidates,
    build_router_candidates,
    combine_legs,
    load_graph,
    load_hotspots,
    make_turn_context,
    name_routes,
    plan_from_candidates,
    rejudge,
)
from app.features import TurnContext
from app.feedback import FeedbackLog
from app.geo import decode_polyline, encode_polyline, haversine_m
from app.hotspots import CrashHotspots
from app.models import LatLng, RoutePlan, ScoringProfile
from app.overpass import ATTRIBUTION
from app.profiles import DEFAULT_PROFILE, PRESETS, get_profile
from app.road_events import (
    ApifyTraffic,
    EventReport,
    RoadEvent,
    TomTomFeed,
    Wi511Feed,
    WzdxFeed,
    near_routes,
    trip_area,
    without_repeats,
)
from app.road_events import collect as collect_road_events
from app.roadgraph import RoadGraph
from app.router import Router
from app.routes_client import DailyLimitReached, RoutesApiError, RoutesClient, daily_usage
from app.sun import dark, low_sun, sun_position
from app.traffic_tiles import TILE_REFRESH, TileError, TrafficTiles
from app.transit import BusRoutes
from app import bike, transit_plan
from app.timetable import TimetableSource
from app.expo_control import ExpoControl
from app.trip_log import TripLog
from app.usage import DailyUsage
from app.weather import WeatherAlert, WeatherAlerts, for_the_weather

logger = logging.getLogger(__name__)

# Pages served from this machine (any port) may call the API. Add others with ROUTES_CORS_ORIGINS.
LOCALHOST_ORIGINS = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
MAX_EVENTS_SHOWN = 200
MAX_STOPS = 3
LEAVE_OFFSETS_MIN = (0, 15, 30, 60)  # the departures "Best time to leave" compares
# Closures are drawn when they're about this close to a route shown: the rest of
# the trip's area (5 km around it) would bury the routes under a city's closures.
SHOWN_NEAR_ROUTES_M = 300.0


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: LatLng
    destination: LatLng
    stops: list[LatLng] = Field(
        default_factory=list,
        max_length=MAX_STOPS,
        description="Places to stop at on the way, in order. The trip is planned leg by leg, and the plan joins "
        "each leg's recommended route (and each leg's standard route).",
    )
    profile: str = Field(DEFAULT_PROFILE, description="A preset name from GET /profiles.")
    departure_offset_min: int = Field(
        0,
        ge=0,
        le=24 * 60,
        description="Leave this many minutes from now. School zones and sun glare are judged at that time; "
        "traffic and closures are today's as they are now.",
    )
    weights: dict[str, float] = Field(
        default_factory=dict, description='Overrides for the preset\'s weights, e.g. {"busy_crossing_weight": 4}.'
    )
    country_code: str | None = Field(
        None,
        description="Two-letter country of the trip (from the Places selection). Decides which turns cross traffic.",
    )
    max_extra_min: float | None = Field(
        None,
        ge=0,
        le=120,
        description="The most extra minutes over the fastest route the driver accepts for a safer one. "
        "None: the profile's own limit (max_detour_ratio times the fastest).",
    )
    use_router: bool = Field(True, description="Let our own router design routes too, when a road graph is loaded.")
    counted: bool = Field(
        True,
        description="Count the trip in the expo's stats (GET /expo-stats). False for sample trips, a trip planned "
        "again after a change on the map, and new routes while driving.",
    )
    live_traffic: bool = Field(
        True, description="Use Google's live traffic. False: no-traffic estimates and fewer, cheaper Google calls."
    )
    heading: float | None = Field(
        None,
        ge=0,
        le=360,
        description="Direction the car is driving at the origin, degrees clockwise from north (when rerouting a "
        "moving car). Routes then start along its road instead of turning around.",
    )
    road_events: Literal["auto", "wzdx", "wi511", "tomtom", "google", "apify", "off"] = Field(
        "auto",
        description="Where closures, lane closures and jams come from. auto: the server's ROUTES_ROAD_EVENT_SOURCES "
        "order, the first that's set up and answers. A named source is used alone.",
    )


class GoogleUsage(BaseModel):
    live_requests: int = Field(description="Requests this trip sent to Google.")
    served_from_cache: int
    used_today: int
    daily_limit: int | None


class WeatherAlertOut(BaseModel):
    event: str = Field(description='e.g. "Winter Weather Advisory".')
    headline: str
    ends: str = Field(description="When it ends, ISO 8601; empty if not given.")
    slippery: bool = Field(description="Snow or ice: turning across traffic counts as riskier while it lasts.")


class RoadEventOut(BaseModel):
    kind: Literal["closed", "lanes", "slow", "jam", "hazard"] = Field(
        description="slow and jam: measured traffic (511 travel times, TomTom's jams). hazard: fog, ice, flooding."
    )
    source: str
    description: str
    path: str = Field(description="Encoded polyline; a single point when the source gave no stretch.")


class TripResponse(BaseModel):
    plan: RoutePlan
    notes: list[str] = Field(description="Anything the viewer should know, e.g. a route that couldn't be priced.")
    driving_side: DrivingSide
    own_router_used: bool
    attribution: list[str] = Field(description="Credits the map must show alongside these routes.")
    google: GoogleUsage
    road_events_source: str | None = Field(description='The road event sources used, joined with "+"; or None.')
    road_events_skipped: list[str] = Field(description="Sources tried first that weren't set up or didn't answer.")
    road_events: list[RoadEventOut] = Field(description="Closures and lane closures around the trip, to draw.")
    school_hours: bool = Field(description="School zones count on this trip: it's a school-day drop-off or pickup time.")
    low_sun: bool = Field(False, description="The sun is low at departure: routes heading into it count sun glare.")
    after_dark: bool = Field(False, description="It's dark at departure: turns across traffic count more.")
    leaving_at: str = Field("", description="The departure time the trip was judged at, local, ISO 8601.")
    weather_alerts: list[WeatherAlertOut] = Field(
        default_factory=list, description="National Weather Service alerts in force where the trip starts."
    )


class LeaveTime(BaseModel):
    offset_min: int
    leaving_at: str = Field(description="Local, ISO 8601.")
    safety_score: int
    duration_min: float
    sun_glare_min: float
    school_zones: int
    after_dark: bool = False


class LeaveTimesResponse(BaseModel):
    times: list[LeaveTime] = Field(description="The recommended route's numbers for each departure time, soonest first.")
    best_offset_min: int = Field(description="The safest departure; the soonest among equally safe ones.")


class ControlChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demo_paused: bool | None = Field(None, description="Pause (or resume) demo mode on the screens running it.")
    reset_stats: bool = Field(False, description="Count the stats from now: earlier trips and answers are left out.")


BIKE_STRESS_MAX_SEGMENTS = 6000
MAX_WALK_KM = 8.0  # about 5 miles in a straight line: past that, few walk it, and the search takes most of a minute


class BikeTripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: LatLng
    destination: LatLng
    stops: list[LatLng] = Field(default_factory=list, max_length=MAX_STOPS, description="Places to stop at on the way, in order.")
    profile: str = Field(
        bike.DEFAULT_PROFILE,
        description="Bikes: everyday, family, confident or ebike (app.bike.PROFILES). Walking: everyday, careful or stepfree.",
    )
    departure_offset_min: int = Field(0, ge=0, le=24 * 60)


class TransitTripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: LatLng
    destination: LatLng
    departure_offset_min: int = Field(0, ge=0, le=24 * 60)
    leave_at: datetime | None = Field(None, description="Leave then (local time) instead: within the coming week.")


class Guess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    right: bool = Field(description="The visitor picked the route with the higher safety score.")
    picked_score: int = Field(ge=0, le=100)
    other_score: int = Field(ge=0, le=100)


def guess_summary(guesses: Sequence[dict[str, Any]]) -> dict[str, Any]:
    right = sum(1 for guess in guesses if guess.get("right") is True)
    return {"total": len(guesses), "right": right, "right_share": round(right / len(guesses), 3) if guesses else None}


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: Literal["yes", "no"] = Field(description="Would the visitor take the recommended route?")
    safety_gain: int = Field(0, ge=-100, le=100, description="Recommended route's safety score minus the standard's.")
    extra_min: float = Field(0.0, ge=-600, le=600, description="Its minutes over the standard route's.")
    profile: str = Field("", max_length=60)


@dataclass
class PreparedTrip:
    """A trip's request with what doesn't depend on its route: profile, context, road events, weather."""

    trip: TripRequest
    profile: ScoringProfile
    context: TurnContext  # with the road events, at the departure time
    report: EventReport
    events: list[RoadEvent]
    alerts: list[WeatherAlert]
    departure: datetime
    use_router: bool
    notes: list[str]


@dataclass
class Engine:
    """Everything loaded once at startup and shared by all requests."""

    settings: Settings
    graph: RoadGraph | None
    hotspots: CrashHotspots | None
    http: httpx.Client
    usage: DailyUsage
    wzdx: WzdxFeed | None = None
    wi511: Wi511Feed | None = None
    tomtom: TomTomFeed | None = None
    apify: ApifyTraffic | None = None
    tiles: TrafficTiles | None = None  # TomTom's traffic map, on the OpenStreetMap page only
    weather: WeatherAlerts | None = None  # the National Weather Service's alerts (ROUTES_WEATHER_ALERTS)
    feedback: FeedbackLog | None = None  # visitors' "Would you take this route?" answers
    buses: BusRoutes | None = None  # bus routes for the OpenStreetMap map's Transit layer
    trips: TripLog | None = None  # the trips visitors plan, for the expo's stats (no places)
    control: ExpoControl | None = None  # what the expo's control page set
    guesses: FeedbackLog | None = None  # "Which route is safer?" guesses, right or wrong (no places)
    bike_graph: RoadGraph | None = None  # the bike map, when built (app.bike)
    walk_graph: RoadGraph | None = None  # the walking map, when built
    walk_hotspots: CrashHotspots | None = None  # fatal crashes where a pedestrian died
    _walk_router: bike.BikeRouter | None = None
    bike_hotspots: CrashHotspots | None = None  # fatal crashes where a cyclist died
    schedule: TimetableSource | None = None  # the buses' timetable, for transit trips (app.timetable)
    _bike_router: bike.BikeRouter | None = None
    warmed_up: bool = False  # the road events across the map are matched to it (warm_up)
    _routers: dict[DrivingSide, Router] = field(default_factory=dict)
    _snapped: dict[tuple[Any, ...], list[RoadEvent]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def snap(self, events: Sequence[RoadEvent]) -> list[RoadEvent]:
        """Events redrawn along the roads they lie on (see RoadGraph.snap_events), remembered across trips.

        The same work zones come back trip after trip, and matching one to the
        map takes a search, so each is matched once.
        """
        if self.graph is None:
            return list(events)
        snapped: list[RoadEvent] = []
        for event in events:
            # Remembered by place: a refreshed reading of the same stretch (new travel time) reuses the match.
            key = event.geometry_key()
            known = self._snapped.get(key)
            if known is None:
                known = self.graph.snap_events([event])
                if (event.category == "traffic" or event.source == "tomtom") and all(s.path == event.path for s in known):
                    # No road on our map matched. A traffic reading as a straight line would only
                    # add false delays, and TomTom reports alleys and parking lots our map leaves out.
                    known = []
                with self._lock:
                    if len(self._snapped) > 20_000:
                        self._snapped.clear()
                    self._snapped[key] = known
            snapped += [replace(event, path=s.path, direction=s.direction, along=s.along) for s in known]
        return snapped

    def warm_up(self) -> None:
        """Fetch the road events across the whole road map and match them to it, so trips don't wait for it.

        Matching takes a search per event, about 30 s for the metro's work zones
        and TomTom's closures, so the server does it once at startup, in the
        background. The paid sources (apify) and Google's are left out.
        """
        if self.graph is None or self.graph.bbox is None:
            return
        started = time.perf_counter()
        try:
            report = collect_road_events(
                sources_for_map(self.settings.road_event_sources, self.settings.map),
                self.graph.bbox,
                wzdx=self.wzdx,
                wi511=self.wi511,
                tomtom=self.tomtom,
                apify=None,
                google_ready=False,
            )
            snapped = self.snap(report.events)
            router = self.router(make_turn_context(self.settings, self.hotspots, None, self.graph))
            for event in snapped:
                router.event_edges(event)  # the router's own lookup, remembered too
            self._practice_drive(router)
            # The bike and walking maps too, so their first trips don't wait a minute.
            for graph, routed in ((self.bike_graph, self.bike_router), (self.walk_graph, self.walk_router)):
                if graph is not None:
                    active = routed()
                    for event in snapped:
                        active.event_edges(event)
                    self._practice_trip(active, graph)
            logger.info(
                "Ready for trips: %d closures, lane closures and jams from %s matched to the map in %.0f s",
                len(report.events),
                report.source or "no source",
                time.perf_counter() - started,
            )
            self.warmed_up = True
        except Exception:  # a background nicety: trips still match what they need
            logger.exception("Matching road events ahead of trips failed")

    def _practice_drive(self, router: Router) -> None:
        """A short drive in the middle of the map, as _practice_trip: for the default profile and the
        fastest, which is the standard route on the OpenStreetMap map."""
        bbox = self.graph.bbox
        lat, lng = (bbox.south + bbox.north) / 2, (bbox.west + bbox.east) / 2
        for profile in (PRESETS[DEFAULT_PROFILE], PRESETS["fastest"]):
            try:
                router.routes((lat, lng), (lat + 0.01, lng + 0.01), profile, count=1)
            except LookupError:
                pass

    @staticmethod
    def _practice_trip(router: bike.BikeRouter, graph: RoadGraph) -> None:
        """A short trip in the middle of the map, for what's built on first use (the everyday traveler's
        segment costs by today's light, the map's indexes), so no visitor's first trip waits for it."""
        if graph.bbox is None:
            return
        lat, lng = (graph.bbox.south + graph.bbox.north) / 2, (graph.bbox.west + graph.bbox.east) / 2
        profile = (bike.PROFILES if graph.mode == "bike" else bike.WALK_PROFILES)[bike.DEFAULT_PROFILE]
        sun = sun_position(datetime.now(), lat, lng)
        try:
            bike.plan(router, LatLng(lat=lat, lng=lng), LatLng(lat=lat + 0.01, lng=lng + 0.01), profile, sun=sun)
        except (LookupError, ValueError):
            pass  # no road right there: the trips still build what they need

    def routes_client(self) -> RoutesClient:
        # One client per request so its counters describe that request; the
        # connection pool and the daily counter are shared.
        return RoutesClient.from_settings(self.settings, http=self.http, usage=self.usage)

    def bike_router(self) -> bike.BikeRouter:
        with self._lock:
            if self._bike_router is None:
                assert self.bike_graph is not None
                context = TurnContext(hotspots=self.bike_hotspots, min_hotspot_crashes=bike.MIN_CRASHES)
                self._bike_router = bike.BikeRouter(self.bike_graph, context)
            return self._bike_router

    def walk_router(self) -> bike.BikeRouter:
        with self._lock:
            if self._walk_router is None:
                assert self.walk_graph is not None
                context = TurnContext(hotspots=self.walk_hotspots, min_hotspot_crashes=bike.MIN_CRASHES)
                self._walk_router = bike.BikeRouter(self.walk_graph, context)
            return self._walk_router

    def router(self, context: TurnContext) -> Router:
        # A router remembers its turn analysis, so keep one per driving side.
        with self._lock:
            router = self._routers.get(context.driving_side)
            if router is None:
                assert self.graph is not None
                router = self._routers[context.driving_side] = Router(self.graph, context)
            return router


def sources_for_map(order: Sequence[str], map_kind: str) -> tuple[str, ...]:
    """The road event sources in order, less those whose data the map may not show.

    Each map takes traffic from its own provider: Google's data (google,
    apify) stays on Google's map, and TomTom's on the OpenStreetMap one.
    """
    elsewhere = ("tomtom",) if map_kind == "google" else ("google", "apify")
    return tuple(g for g in ("+".join(s for s in group.split("+") if s not in elsewhere) for group in order) if g)


def shown_events(events: Sequence[RoadEvent], plan: RoutePlan) -> list[RoadEventOut]:
    """What the map draws: closures and lane closures near the routes, once each, and the measured traffic.

    A closure of both directions of a two-way street is matched as one stretch
    per direction, each the other reversed: drawn once.
    """
    routes = [decode_polyline(route.encoded_polyline) for route in plan.candidates]
    closures = near_routes(
        [event for event in events if event.kind in ("closed", "lanes", "hazard")], routes, SHOWN_NEAR_ROUTES_M
    )
    traffic = [event for event in events if event.category == "traffic"]
    shown: list[RoadEventOut] = []
    drawn: set[tuple[str, tuple[Any, ...]]] = set()
    for event in closures + traffic:
        if (event.kind, event.path[::-1]) in drawn:
            continue
        drawn.add((event.kind, event.path))
        shown.append(
            RoadEventOut(kind=event.kind, source=event.source, description=event.description, path=encode_polyline(event.path))
        )
    return shown[:MAX_EVENTS_SHOWN]


def show_app_messages() -> None:
    """Print this app's own messages (ready, road events matched, sources failing) in the server's window.

    Uvicorn prints only its own. This adds the app's, and only those: httpx
    logs each request's address, and some carry an API key.
    """
    app_logger = logging.getLogger("app")
    if app_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))  # lined up with uvicorn's
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)


def create_app(settings: Settings | None = None, *, http: httpx.Client | None = None) -> FastAPI:
    """Build the API. Tests pass their own settings and a mock http client."""
    show_app_messages()
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http or httpx.Client(timeout=10.0)
        graph = load_graph(settings)
        tomtom_key = settings.tomtom_api_key
        engine = Engine(
            settings=settings,
            graph=graph,
            hotspots=load_hotspots(settings),
            http=client,
            usage=daily_usage(settings),
            wzdx=WzdxFeed(client, file=settings.wzdx_file)
            if settings.wzdx_file
            else WzdxFeed(client, url=settings.wzdx_url),
            wi511=Wi511Feed(settings.wi511_api_key, client) if settings.wi511_api_key else None,
            # One request covers the road map's whole area, shared by every trip.
            tomtom=TomTomFeed(
                tomtom_key,
                client,
                DailyUsage(settings.cache_dir / "tomtom-requests.json", settings.tomtom_daily_requests),
                box=graph.bbox if graph else None,
                saved=settings.cache_dir / "tomtom-incidents.json",
            )
            if tomtom_key
            else None,
            tiles=TrafficTiles(tomtom_key, client, DailyUsage(settings.cache_dir / "tomtom-tiles.json", settings.tomtom_daily_tiles))
            if tomtom_key and settings.map == "osm"
            else None,
            # The paid scraper only runs when it's both listed and given a token.
            apify=ApifyTraffic(
                settings.apify_token,
                client,
                DailyUsage(settings.cache_dir / "apify-tiles.json", settings.apify_daily_tiles),
            )
            if settings.apify_token and "apify" in settings.road_event_sources
            else None,
            weather=WeatherAlerts(client) if settings.weather_alerts else None,
            feedback=FeedbackLog(settings.cache_dir / "feedback.jsonl"),
            trips=TripLog(settings.cache_dir / "trips.jsonl"),
            control=ExpoControl(settings.cache_dir / "expo-control.json"),
            guesses=FeedbackLog(settings.cache_dir / "guesses.jsonl"),
            bike_graph=RoadGraph.load(settings.bike_graph_path)
            if settings.bike_graph_path and settings.bike_graph_path.exists()
            else None,
            bike_hotspots=CrashHotspots.from_csv(settings.bike_crash_data_path) if settings.bike_crash_data_path else None,
            walk_graph=RoadGraph.load(settings.walk_graph_path)
            if settings.walk_graph_path and settings.walk_graph_path.exists()
            else None,
            walk_hotspots=CrashHotspots.from_csv(settings.walk_crash_data_path) if settings.walk_crash_data_path else None,
            buses=BusRoutes(client, settings.bus_routes_url, settings.cache_dir / "bus-routes.json")
            if settings.bus_routes_url and settings.map == "osm"
            else None,
            schedule=TimetableSource(client, settings.bus_routes_url, settings.cache_dir / "transit-feed.zip")
            if settings.bus_routes_url and settings.transit
            else None,
        )
        app.state.engine = engine
        Thread(target=engine.warm_up, name="road-events-warm-up", daemon=True).start()
        if engine.buses:  # fetched ahead, so the Transit layer shows at once
            Thread(target=engine.buses.routes, name="bus-routes", daemon=True).start()
        if engine.schedule:  # read in the background: transit trips wait for it, nothing else does
            Thread(target=engine.schedule.load, name="bus-schedule", daemon=True).start()
        logger.info(
            "Started: %s map, road graph %s, crash data %s, cache %s, road events from %s%s",
            "OpenStreetMap" if settings.map == "osm" else "Google",
            engine.graph.area if engine.graph else "none",
            "loaded" if engine.hotspots else "none",
            settings.cache_mode,
            ", ".join(sources_for_map(settings.road_event_sources, settings.map)) or "nowhere",
            "; matching them to the map before trips (about a minute)" if graph and graph.bbox else "",
        )
        yield
        if http is None:
            engine.http.close()

    app = FastAPI(
        title="Personalized Routing Engine",
        version="0.1.0",
        description="Ranks Google's routes and our own router's designs for a driver profile.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_origin_regex=LOCALHOST_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Access-Code"],
    )

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        engine: Engine = request.app.state.engine
        graph = engine.graph
        return {
            "status": "ok",
            "road_graph": {"area": graph.area, "junctions": graph.node_count} if graph else None,
            "crash_data": engine.hotspots is not None,
            "hazards": {
                "crash_locations": engine.hotspots.locations if engine.hotspots else 0,
                "rail_crossings": len(graph.rail_crossing_points) if graph else 0,
                "schools": len(graph.school_points) if graph else 0,
                "school_hours_now": make_turn_context(settings, None).school_hours,
            },
            "cache_mode": settings.cache_mode,
            "google_api_key_set": bool(settings.google_maps_api_key),
            "google_requests_today": engine.usage.used_today(),
            "daily_limit": engine.usage.limit,
            "road_events": {
                "order": list(settings.road_event_sources),
                "matched_ahead": engine.warmed_up,  # false for about 3 minutes after startup
                "wzdx": str(settings.wzdx_file.name) if settings.wzdx_file else settings.wzdx_url,
                "wi511_key_set": engine.wi511 is not None,
                "tomtom_key_set": engine.tomtom is not None,
                "tomtom_requests_today": engine.tomtom.usage.used_today() if engine.tomtom else 0,
                "tomtom_tiles_today": engine.tiles.usage.used_today() if engine.tiles else 0,
                "tomtom_daily_requests": settings.tomtom_daily_requests,
                "tomtom_daily_tiles": settings.tomtom_daily_tiles,
                "apify_enabled": engine.apify is not None,
                "apify_tiles_today": engine.apify.usage.used_today() if engine.apify else 0,
            },
            "weather_alerts": engine.weather is not None,
            "bus_routes": engine.buses is not None,
            "bus_routes_ready": bool(engine.buses and engine.buses.routes_now()),
            "transit": {
                "ready": engine.schedule.table is not None,
                "stops": len(engine.schedule.table.stop_ids) if engine.schedule.table else 0,
                "error": engine.schedule.error,
            }
            if engine.schedule
            else None,
            "walk_map": {
                "segments": engine.walk_graph.edge_count,
                "pedestrian_crash_locations": engine.walk_hotspots.locations if engine.walk_hotspots else 0,
            }
            if engine.walk_graph
            else None,
            "bike_map": {
                "segments": engine.bike_graph.edge_count,
                "trails": sum(1 for e in range(engine.bike_graph.edge_count) if engine.bike_graph.class_name(e) in ("cycleway", "path")),
                "cyclist_crash_locations": engine.bike_hotspots.locations if engine.bike_hotspots else 0,
            }
            if engine.bike_graph
            else None,
            "feedback": {key: engine.feedback.summary()[key] for key in ("yes", "no")} if engine.feedback else None,
        }

    @app.get("/profiles")
    def profiles() -> dict[str, Any]:
        return {"default": DEFAULT_PROFILE, "profiles": [profile.model_dump() for profile in PRESETS.values()]}

    @app.get("/app-config")
    def app_config(request: Request) -> dict[str, Any]:
        """What the web map needs before it loads: which map to show (ROUTES_MAP), whether
        /traffic-tiles serves a traffic layer for it and /bus-routes its buses, and the price
        of gas (ROUTES_FUEL_PRICE)."""
        return {
            "map": settings.map,
            "traffic_tiles": request.app.state.engine.tiles is not None,
            "bus_routes": request.app.state.engine.buses is not None,
            "bike": request.app.state.engine.bike_graph is not None,
            "walk": request.app.state.engine.walk_graph is not None,
            "transit": request.app.state.engine.schedule is not None,
            "fuel_price": settings.fuel_price,
        }

    @app.get("/bus-routes")
    def bus_routes(request: Request, response: Response) -> dict[str, Any]:
        """Each bus route's line, in its own color, for the OpenStreetMap map's Transit layer (Google's map has its own)."""
        engine: Engine = request.app.state.engine
        if engine.buses is None:
            raise HTTPException(status_code=404, detail="No bus routes: they're for ROUTES_MAP=osm, with ROUTES_BUS_ROUTES on.")
        routes = engine.buses.routes()
        response.headers["Cache-Control"] = "public, max-age=3600"
        return {
            "routes": [
                {"name": route.name, "long_name": route.long_name, "color": route.color, "paths": list(route.paths)}
                for route in routes
            ],
            "attribution": engine.buses.attribution if routes else None,
        }

    @app.get("/traffic-tiles/{z}/{x}/{y}.png", include_in_schema=False)
    def traffic_tile(z: int, x: int, y: int, request: Request, code: str | None = None) -> Response:
        """One tile of TomTom's traffic map. The page's map layer can't send headers, so the access code comes as ?code=."""
        engine: Engine = request.app.state.engine
        if engine.tiles is None:
            raise HTTPException(status_code=404, detail="No traffic map: it needs ROUTES_MAP=osm and TOMTOM_API_KEY.")
        if settings.access_code and not hmac.compare_digest(code or "", settings.access_code):
            raise HTTPException(status_code=401, detail="This demo needs its access code.")
        try:
            png = engine.tiles.tile(z, x, y)
        except TileError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        # Browsers keep it as long as the server does, so panning back doesn't fetch it again.
        return Response(png, media_type="image/png", headers={"Cache-Control": f"max-age={int(TILE_REFRESH.total_seconds())}"})

    def from_this_computer(request: Request) -> bool:
        """A request from this computer's own browser. The tunnel connects from here too, but adds
        the visitor's address in a header (Cloudflare's, or the usual forwarding ones)."""
        local = request.client is not None and request.client.host in ("127.0.0.1", "::1", "localhost")
        forwarded = any(name in request.headers for name in ("cf-connecting-ip", "x-forwarded-for", "forwarded"))
        return local and not forwarded

    def check_access(code: str | None) -> None:
        # A demo shared over a public link: only people given the code can spend its Google requests.
        if settings.access_code and not hmac.compare_digest(code or "", settings.access_code):
            raise HTTPException(status_code=401, detail="This demo needs its access code.")

    def prepare(engine: Engine, trip: TripRequest) -> PreparedTrip:
        """Everything about a trip that doesn't depend on the route: profile, weather, time, road events."""
        departure = datetime.now() + timedelta(minutes=trip.departure_offset_min)
        alerts = engine.weather.at(trip.origin) if engine.weather else []
        try:
            limit = {} if trip.max_extra_min is None else {"max_extra_min": trip.max_extra_min}
            profile = for_the_weather(get_profile(trip.profile, {**trip.weights, **limit}), alerts)
            context = make_turn_context(
                settings, engine.hotspots, trip.country_code, engine.graph, now=departure, where=trip.origin
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        # The OpenStreetMap map never asks Google: Google's terms don't allow its
        # results on another map. Our router designs every route, the fastest
        # one standing in for Google's, and live traffic comes from 511 and TomTom.
        if settings.map != "google":
            if engine.graph is None:
                raise HTTPException(status_code=503, detail="The OpenStreetMap map needs the road map: build it first.")
            trip = trip.model_copy(update={"use_router": True, "live_traffic": False})

        use_router = trip.use_router and engine.graph is not None
        notes: list[str] = []
        if trip.use_router and engine.graph is None:
            notes.append("Own router unavailable: no road graph is loaded.")
        order = {"auto": settings.road_event_sources, "off": ()}.get(trip.road_events, (trip.road_events,))
        report = collect_road_events(
            sources_for_map(order, settings.map),
            trip_area(trip.origin, *trip.stops, trip.destination),
            wzdx=engine.wzdx,
            wi511=engine.wi511,
            tomtom=engine.tomtom,
            apify=engine.apify,
            google_ready=use_router and trip.live_traffic and bool(settings.google_maps_api_key),
        )
        notes += report.notes
        # Redrawn along the roads they lie on, for routing, scoring and the map alike,
        # less TomTom's reports of closures an official feed already gives.
        events = without_repeats(engine.snap(report.events))
        return PreparedTrip(
            trip=trip,
            profile=profile,
            context=replace(context, events=tuple(events)),
            report=report,
            events=events,
            alerts=alerts,
            departure=departure,
            use_router=use_router,
            notes=notes,
        )

    def legs(trip: TripRequest) -> list[tuple[LatLng, LatLng, int | None]]:
        """The trip's legs through its stops: (start, end, the car's heading at the start, if moving)."""
        places = [trip.origin, *trip.stops, trip.destination]
        heading = None if trip.heading is None else round(trip.heading) % 360
        return [(start, end, heading if number == 0 else None) for number, (start, end) in enumerate(zip(places, places[1:]))]

    def plan_leg(
        engine: Engine, client: RoutesClient, prepared: PreparedTrip, origin: LatLng, destination: LatLng, heading: int | None
    ) -> RoutePlan:
        """One leg's ranked routes."""
        trip, context = prepared.trip, prepared.context
        try:
            if prepared.use_router:
                pool = build_router_candidates(
                    client,
                    engine.router(context),
                    origin,
                    destination,
                    [prepared.profile],
                    price=trip.live_traffic,
                    heading=heading,
                    context=context,
                    traffic_on_polyline=prepared.report.google,
                    baseline=None if settings.map == "google" else PRESETS["fastest"],
                )
                candidates = pool.candidates
                prepared.notes.extend(pool.notes)
            else:
                options = CandidateOptions(traffic_aware=trip.live_traffic, origin_heading=heading)
                candidates = build_candidates(client, origin, destination, options, context)
        except NoRouteFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except CacheMiss as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except DailyLimitReached as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        except RoutesApiError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return name_routes(plan_from_candidates(candidates, prepared.profile), engine.graph)

    def plan_trip(engine: Engine, client: RoutesClient, prepared: PreparedTrip) -> RoutePlan:
        """The whole trip: leg by leg through its stops, joined into one plan."""
        plans = [plan_leg(engine, client, prepared, *leg) for leg in legs(prepared.trip)]
        return plans[0] if len(plans) == 1 else combine_legs(plans, prepared.trip.stops)

    @app.post("/route", response_model=TripResponse)
    def route(
        trip: TripRequest, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> TripResponse:
        engine: Engine = request.app.state.engine
        check_access(x_access_code)
        prepared = prepare(engine, trip)
        with engine.routes_client() as client:
            plan = plan_trip(engine, client, prepared)
            google = GoogleUsage(
                live_requests=client.live_requests,
                served_from_cache=client.cache.hits,
                used_today=engine.usage.used_today(),
                daily_limit=engine.usage.limit,
            )

        if trip.counted and engine.trips:
            engine.trips.add(plan, trip.profile)
        context = prepared.context
        own_used = any(candidate.source == "own" for candidate in plan.candidates)
        return TripResponse(
            plan=plan,
            notes=prepared.notes,
            driving_side=context.driving_side,
            own_router_used=own_used,
            attribution=[f"Route design: map data {ATTRIBUTION}"] if own_used else [],
            google=google,
            school_hours=context.school_hours,
            low_sun=low_sun(context.sun),
            after_dark=dark(context.sun),
            leaving_at=prepared.departure.isoformat(timespec="minutes"),
            weather_alerts=[
                WeatherAlertOut(event=a.event, headline=a.headline, ends=a.ends, slippery=a.slippery) for a in prepared.alerts
            ],
            road_events_source=prepared.report.source,
            road_events_skipped=prepared.report.skipped,
            road_events=shown_events(prepared.events, plan),
        )

    @app.post("/leave-times", response_model=LeaveTimesResponse)
    def leave_times(
        trip: TripRequest, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> LeaveTimesResponse:
        """The recommended route if leaving now, in 15 or 30 minutes, or in an hour.

        School zones and sun glare depend on the time; traffic and closures are
        as they are now. Our router designs each leg's routes once, as on the
        OpenStreetMap map (no Google), and they're judged again at each time.
        """
        engine: Engine = request.app.state.engine
        check_access(x_access_code)
        if engine.graph is None:
            raise HTTPException(status_code=503, detail="Comparing departure times needs the road map: build it first.")
        prepared = prepare(engine, trip.model_copy(update={"departure_offset_min": 0}))
        pools = []
        for start, end, heading in legs(trip):
            try:
                pools.append(
                    build_router_candidates(
                        None,
                        engine.router(prepared.context),
                        start,
                        end,
                        [prepared.profile],
                        price=False,
                        heading=heading,
                        context=prepared.context,
                        baseline=PRESETS["fastest"],
                    )
                )
            except NoRouteFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
        times = []
        for offset in LEAVE_OFFSETS_MIN:
            moment = prepared.departure + timedelta(minutes=offset)
            then = make_turn_context(settings, engine.hotspots, trip.country_code, engine.graph, now=moment, where=trip.origin)
            context = replace(prepared.context, school_hours=then.school_hours, sun=then.sun)
            plans = [plan_from_candidates(rejudge(pool, context), prepared.profile) for pool in pools]
            winner = (plans[0] if len(plans) == 1 else combine_legs(plans, trip.stops)).winner
            times.append(
                LeaveTime(
                    offset_min=offset,
                    leaving_at=moment.isoformat(timespec="minutes"),
                    safety_score=winner.safety_score,
                    duration_min=round(winner.features.duration_min, 1),
                    sun_glare_min=round(winner.features.sun_glare_min, 1),
                    school_zones=winner.features.school_zones,
                    after_dark=winner.features.after_dark,
                )
            )
        best = max(times, key=lambda t: (t.safety_score, -t.offset_min))
        return LeaveTimesResponse(times=times, best_offset_min=best.offset_min)

    @app.post("/feedback", status_code=204)
    def feedback(
        answer: Feedback, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> Response:
        """A visitor's answer to "Would you take this route?" (no places are kept)."""
        check_access(x_access_code)
        request.app.state.engine.feedback.add(answer.model_dump())
        return Response(status_code=204)

    @app.get("/expo-stats")
    def expo_stats(request: Request) -> dict[str, Any]:
        """Today's trips (what their recommended routes avoided) and visitors' answers, for stats.html. No places."""
        engine: Engine = request.app.state.engine
        today = datetime.now().date()
        since = engine.control.stats_since()  # set from the control page, to leave a rehearsal out
        return {
            "day": today.isoformat(),
            "since": since.isoformat(timespec="minutes") if since else None,
            "trips": engine.trips.summary(today, since),
            "answers": engine.feedback.summary(today, since),
            "guesses": guess_summary(engine.guesses.answers(today, since)),
            "trips_ever": engine.trips.summary()["trips"],
        }

    @app.get("/expo-control")
    def expo_control(request: Request) -> dict[str, Any]:
        """What the control page set: whether demo mode is paused, and since when the stats count."""
        return request.app.state.engine.control.state()

    @app.post("/expo-control")
    def set_expo_control(
        change: ControlChange, request: Request, x_admin_code: str | None = Header(None, include_in_schema=False)
    ) -> dict[str, Any]:
        """Pause or resume demo mode on the booth screens, or restart the stats. Needs ROUTES_ADMIN_CODE,
        or without one, a request from this computer itself (not through the tunnel)."""
        if settings.admin_code:
            if not hmac.compare_digest(x_admin_code or "", settings.admin_code):
                raise HTTPException(status_code=401, detail="The control page needs the admin code.")
        elif not from_this_computer(request):
            raise HTTPException(status_code=403, detail="Without ROUTES_ADMIN_CODE, only this computer can use the control page.")
        return request.app.state.engine.control.update(demo_paused=change.demo_paused, reset_stats=change.reset_stats)

    @app.post("/bike-route")
    def bike_route(
        trip: BikeTripRequest, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> dict[str, Any]:
        """Bike routes for one trip: the lowest-stress route within the rider's detour limit, the shortest
        route to compare with, and what each has (minutes per stress level, stressful crossings...)."""
        return active_route("bike", trip, request.app.state.engine, x_access_code)

    @app.post("/walk-route")
    def walk_route(
        trip: BikeTripRequest, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> dict[str, Any]:
        """Walking routes for one trip, the same way: the calmest route (sidewalks and paths, signals to cross
        busy roads) within the walker's detour limit, beside the shortest."""
        return active_route("walk", trip, request.app.state.engine, x_access_code)

    @app.post("/transit-route")
    def transit_route(
        trip: TransitTripRequest, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> dict[str, Any]:
        """Bus trips by the schedule: walk, ride, maybe transfer, walk; the safest of those arriving near the
        earliest first. The walks are planned on the walking map when there is one."""
        check_access(x_access_code)
        engine: Engine = request.app.state.engine
        if engine.schedule is None:
            raise HTTPException(status_code=503, detail="Transit trips need a bus schedule (ROUTES_BUS_ROUTES).")
        table = engine.schedule.table
        if table is None:
            detail = "The bus schedule couldn't be loaded." if engine.schedule.error else "The bus schedule is still loading: try again in a minute."
            raise HTTPException(status_code=503, detail=detail)
        departure = trip.leave_at.replace(tzinfo=None) if trip.leave_at else datetime.now() + timedelta(minutes=trip.departure_offset_min)
        walker = engine.walk_router() if engine.walk_graph is not None else None
        try:
            result = transit_plan.plan(
                table, walker, trip.origin, trip.destination, departure, lambda moment, point: sun_position(moment, *point)
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {
            "plan": result,
            "attribution": ["Bus schedule: Milwaukee County Transit System", f"Walking routes: map data {ATTRIBUTION}"],
            "leaving_at": departure.isoformat(timespec="minutes"),
            "after_dark": dark(sun_position(departure, trip.origin.lat, trip.origin.lng)),
            "scheduled": True,  # times are the timetable's, not live
        }

    def active_route(mode: str, trip: BikeTripRequest, engine: Engine, x_access_code: str | None) -> dict[str, Any]:
        check_access(x_access_code)
        graph = engine.bike_graph if mode == "bike" else engine.walk_graph
        if graph is None:
            raise HTTPException(status_code=503, detail=f"{mode.title()} routes need the {mode} map: python -m app.build_graph <area> --{mode}")
        profiles = bike.PROFILES if mode == "bike" else bike.WALK_PROFILES
        profile = profiles.get(trip.profile)
        if profile is None:
            raise HTTPException(status_code=422, detail=f"Unknown {mode} profile {trip.profile!r}; choose from {', '.join(profiles)}.")
        places = [trip.origin, *trip.stops, trip.destination]
        if mode == "walk" and sum(haversine_m(a.as_tuple(), b.as_tuple()) for a, b in pairwise(places)) > MAX_WALK_KM * 1000:
            raise HTTPException(status_code=422, detail="This walk is over 5 miles. Try Transit or Bike for a trip this long.")
        departure = datetime.now() + timedelta(minutes=trip.departure_offset_min)
        report = collect_road_events(
            sources_for_map(settings.road_event_sources, settings.map),
            trip_area(trip.origin, *trip.stops, trip.destination),
            wzdx=engine.wzdx,
            wi511=engine.wi511,
            tomtom=engine.tomtom,
            apify=None,
            google_ready=False,
        )
        events = without_repeats(engine.snap(report.events))
        sun = make_turn_context(settings, None, None, None, now=departure, where=trip.origin).sun
        try:
            router = engine.bike_router() if mode == "bike" else engine.walk_router()
            result = bike.plan_with_stops(router, places, profile, events=events, sun=sun)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {
            "plan": result,
            "attribution": [f"{'Bike' if mode == 'bike' else 'Walking'} routes: map data {ATTRIBUTION}"],
            "leaving_at": departure.isoformat(timespec="minutes"),
            "after_dark": dark(sun),
            # Trails and sidewalks may not be cleared of snow and ice from December to March.
            "winter": departure.month in (12, 1, 2, 3),
            "road_events_source": report.source,
        }

    @app.get("/bike-stress")
    def bike_stress(request: Request, south: float, west: float, north: float, east: float) -> dict[str, Any]:
        """The bike map's segments in a box, by traffic stress, as encoded lines: for the Bike stress
        layer. At most BIKE_STRESS_MAX_SEGMENTS: the page asks only when zoomed in."""
        engine: Engine = request.app.state.engine
        if engine.bike_graph is None:
            raise HTTPException(status_code=404, detail="No bike map.")
        graph = engine.bike_graph
        lines: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
        seen: set[tuple[int, int]] = set()
        for edge in graph.edges_in_box(south, west, north, east):
            pair = tuple(sorted((graph.edge_from[edge], graph.edge_to[edge])))
            if pair in seen:
                continue  # the other direction of a two-way street: one line
            seen.add(pair)
            lines[max(1, graph.stress[edge])].append(graph.geometry[edge])
            if len(seen) >= BIKE_STRESS_MAX_SEGMENTS:
                break
        return {"lines": lines, "complete": len(seen) < BIKE_STRESS_MAX_SEGMENTS}

    @app.post("/guess", status_code=204)
    def guess(
        answer: Guess, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> Response:
        """A visitor's guess in "Which route is safer?": right or not, and the two scores (no places)."""
        check_access(x_access_code)
        request.app.state.engine.guesses.add(answer.model_dump())
        return Response(status_code=204)

    @app.get("/feedback-summary")
    def feedback_summary(request: Request) -> dict[str, Any]:
        """How visitors answered "Would you take this route?"."""
        return request.app.state.engine.feedback.summary()

    # Mounted last, so the API routes above take precedence over file names.
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


app = create_app()
