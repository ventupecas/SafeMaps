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
    load_graph,
    load_hotspots,
    make_turn_context,
    name_routes,
    plan_from_candidates,
)
from app.features import TurnContext
from app.geo import decode_polyline, encode_polyline
from app.hotspots import CrashHotspots
from app.models import LatLng, RoutePlan
from app.overpass import ATTRIBUTION
from app.profiles import DEFAULT_PROFILE, PRESETS, get_profile
from app.road_events import (
    ApifyTraffic,
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
from app.traffic_tiles import TILE_REFRESH, TileError, TrafficTiles
from app.usage import DailyUsage

logger = logging.getLogger(__name__)

# Pages served from this machine (any port) may call the API. Add others with ROUTES_CORS_ORIGINS.
LOCALHOST_ORIGINS = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
MAX_EVENTS_SHOWN = 200
# Closures are drawn when they're about this close to a route shown: the rest of
# the trip's area (5 km around it) would bury the routes under a city's closures.
SHOWN_NEAR_ROUTES_M = 300.0


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: LatLng
    destination: LatLng
    profile: str = Field(DEFAULT_PROFILE, description="A preset name from GET /profiles.")
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


class RoadEventOut(BaseModel):
    kind: Literal["closed", "lanes", "slow", "jam"] = Field(
        description="slow and jam: measured traffic (511 travel times, TomTom's jams)."
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
            logger.info(
                "Ready for trips: %d closures, lane closures and jams from %s matched to the map in %.0f s",
                len(report.events),
                report.source or "no source",
                time.perf_counter() - started,
            )
            self.warmed_up = True
        except Exception:  # a background nicety: trips still match what they need
            logger.exception("Matching road events ahead of trips failed")

    def routes_client(self) -> RoutesClient:
        # One client per request so its counters describe that request; the
        # connection pool and the daily counter are shared.
        return RoutesClient.from_settings(self.settings, http=self.http, usage=self.usage)

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
    closures = near_routes([event for event in events if event.kind in ("closed", "lanes")], routes, SHOWN_NEAR_ROUTES_M)
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
        )
        app.state.engine = engine
        Thread(target=engine.warm_up, name="road-events-warm-up", daemon=True).start()
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
                "matched_ahead": engine.warmed_up,  # false for a minute or so after startup
                "wzdx": str(settings.wzdx_file.name) if settings.wzdx_file else settings.wzdx_url,
                "wi511_key_set": engine.wi511 is not None,
                "tomtom_key_set": engine.tomtom is not None,
                "tomtom_requests_today": engine.tomtom.usage.used_today() if engine.tomtom else 0,
                "tomtom_tiles_today": engine.tiles.usage.used_today() if engine.tiles else 0,
                "apify_enabled": engine.apify is not None,
                "apify_tiles_today": engine.apify.usage.used_today() if engine.apify else 0,
            },
        }

    @app.get("/profiles")
    def profiles() -> dict[str, Any]:
        return {"default": DEFAULT_PROFILE, "profiles": [profile.model_dump() for profile in PRESETS.values()]}

    @app.get("/app-config")
    def app_config(request: Request) -> dict[str, Any]:
        """What the web map needs before it loads: which map to show (ROUTES_MAP), and whether
        /traffic-tiles serves a traffic layer for it."""
        return {"map": settings.map, "traffic_tiles": request.app.state.engine.tiles is not None}

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

    @app.post("/route", response_model=TripResponse)
    def route(
        trip: TripRequest, request: Request, x_access_code: str | None = Header(None, include_in_schema=False)
    ) -> TripResponse:
        engine: Engine = request.app.state.engine
        # A demo shared over a public link: only people given the code can spend its Google requests.
        if settings.access_code and not hmac.compare_digest(x_access_code or "", settings.access_code):
            raise HTTPException(status_code=401, detail="This demo needs its access code.")
        try:
            limit = {} if trip.max_extra_min is None else {"max_extra_min": trip.max_extra_min}
            profile = get_profile(trip.profile, {**trip.weights, **limit})
            context = make_turn_context(settings, engine.hotspots, trip.country_code, engine.graph)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        # The OpenStreetMap map never asks Google: Google's terms don't allow its
        # results on another map. Our router designs every route, the fastest
        # one standing in for Google's, and live traffic comes from 511 and TomTom.
        google = settings.map == "google"
        if not google:
            if engine.graph is None:
                raise HTTPException(status_code=503, detail="The OpenStreetMap map needs the road map: build it first.")
            trip = trip.model_copy(update={"use_router": True, "live_traffic": False})

        use_router = trip.use_router and engine.graph is not None
        heading = None if trip.heading is None else round(trip.heading) % 360
        notes: list[str] = []
        if trip.use_router and engine.graph is None:
            notes.append("Own router unavailable: no road graph is loaded.")

        order = {"auto": settings.road_event_sources, "off": ()}.get(trip.road_events, (trip.road_events,))
        order = sources_for_map(order, settings.map)
        area = trip_area(trip.origin, trip.destination)
        report = collect_road_events(
            order,
            area,
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
        context = replace(context, events=tuple(events))

        with engine.routes_client() as client:
            try:
                if use_router:
                    pool = build_router_candidates(
                        client,
                        engine.router(context),
                        trip.origin,
                        trip.destination,
                        [profile],
                        price=trip.live_traffic,
                        heading=heading,
                        context=context,
                        traffic_on_polyline=report.google,
                        baseline=None if google else PRESETS["fastest"],
                    )
                    candidates = pool.candidates
                    notes += pool.notes
                else:
                    options = CandidateOptions(traffic_aware=trip.live_traffic, origin_heading=heading)
                    candidates = build_candidates(client, trip.origin, trip.destination, options, context)
            except NoRouteFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
            except CacheMiss as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from None
            except DailyLimitReached as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from None
            except RoutesApiError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
            google = GoogleUsage(
                live_requests=client.live_requests,
                served_from_cache=client.cache.hits,
                used_today=engine.usage.used_today(),
                daily_limit=engine.usage.limit,
            )

        own_used = any(candidate.source == "own" for candidate in candidates)
        plan = name_routes(plan_from_candidates(candidates, profile), engine.graph)
        return TripResponse(
            plan=plan,
            notes=notes,
            driving_side=context.driving_side,
            own_router_used=own_used,
            attribution=[f"Route design: map data {ATTRIBUTION}"] if own_used else [],
            google=google,
            school_hours=context.school_hours,
            road_events_source=report.source,
            road_events_skipped=report.skipped,
            road_events=shown_events(events, plan),
        )

    # Mounted last, so the API routes above take precedence over file names.
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


app = create_app()
