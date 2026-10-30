"""HTTP API for the routing engine.

Run from backend/:

    uvicorn app.main:app --reload --port 3143

Then open http://127.0.0.1:3143 for the map, or http://127.0.0.1:3143/docs to
try the API directly.

    GET  /health    what's loaded: road graph, crash data, cache mode, today's Google usage
    GET  /profiles  the preset driver profiles and their weights
    POST /route     rank routes for one trip; the map draws plan.winner.encoded_polyline
    GET  /          the web map (frontend/), when that folder exists

The frontend never calls Google's Routes API itself. Routing, scoring and
every Google call happen here, behind the server-side API key. So do road
events (closures, lane closures, jams): see app.road_events.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
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
    plan_from_candidates,
)
from app.features import TurnContext
from app.geo import encode_polyline
from app.hotspots import CrashHotspots
from app.models import LatLng, RoutePlan
from app.overpass import ATTRIBUTION
from app.profiles import DEFAULT_PROFILE, PRESETS, get_profile
from app.road_events import ApifyTraffic, RoadEvent, Wi511Feed, WzdxFeed, trip_area
from app.road_events import collect as collect_road_events
from app.roadgraph import RoadGraph
from app.router import Router
from app.routes_client import DailyLimitReached, RoutesApiError, RoutesClient, daily_usage
from app.usage import DailyUsage

logger = logging.getLogger(__name__)

# Pages served from this machine (any port) may call the API. Add others with ROUTES_CORS_ORIGINS.
LOCALHOST_ORIGINS = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
MAX_EVENTS_SHOWN = 200


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
    road_events: Literal["auto", "wzdx", "wi511", "google", "apify", "off"] = Field(
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
    kind: Literal["closed", "lanes"]
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
    apify: ApifyTraffic | None = None
    _routers: dict[DrivingSide, Router] = field(default_factory=dict)
    _snapped: dict[RoadEvent, list[RoadEvent]] = field(default_factory=dict)
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
            known = self._snapped.get(event)
            if known is None:
                known = self.graph.snap_events([event])
                with self._lock:
                    if len(self._snapped) > 20_000:
                        self._snapped.clear()
                    self._snapped[event] = known
            snapped += known
        return snapped

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


def create_app(settings: Settings | None = None, *, http: httpx.Client | None = None) -> FastAPI:
    """Build the API. Tests pass their own settings and a mock http client."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http or httpx.Client(timeout=10.0)
        engine = Engine(
            settings=settings,
            graph=load_graph(settings),
            hotspots=load_hotspots(settings),
            http=client,
            usage=daily_usage(settings),
            wzdx=WzdxFeed(client, file=settings.wzdx_file)
            if settings.wzdx_file
            else WzdxFeed(client, url=settings.wzdx_url),
            wi511=Wi511Feed(settings.wi511_api_key, client) if settings.wi511_api_key else None,
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
        logger.info(
            "Ready: road graph %s, crash data %s, cache %s, road events from %s",
            engine.graph.area if engine.graph else "none",
            "loaded" if engine.hotspots else "none",
            settings.cache_mode,
            ", ".join(settings.road_event_sources) or "nowhere",
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
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        engine: Engine = request.app.state.engine
        graph = engine.graph
        return {
            "status": "ok",
            "road_graph": {"area": graph.area, "junctions": graph.node_count} if graph else None,
            "crash_data": engine.hotspots is not None,
            "cache_mode": settings.cache_mode,
            "google_api_key_set": bool(settings.google_maps_api_key),
            "google_requests_today": engine.usage.used_today(),
            "daily_limit": engine.usage.limit,
            "road_events": {
                "order": list(settings.road_event_sources),
                "wzdx": str(settings.wzdx_file.name) if settings.wzdx_file else settings.wzdx_url,
                "wi511_key_set": engine.wi511 is not None,
                "apify_enabled": engine.apify is not None,
                "apify_tiles_today": engine.apify.usage.used_today() if engine.apify else 0,
            },
        }

    @app.get("/profiles")
    def profiles() -> dict[str, Any]:
        return {"default": DEFAULT_PROFILE, "profiles": [profile.model_dump() for profile in PRESETS.values()]}

    @app.post("/route", response_model=TripResponse)
    def route(trip: TripRequest, request: Request) -> TripResponse:
        engine: Engine = request.app.state.engine
        try:
            profile = get_profile(trip.profile, trip.weights)
            context = make_turn_context(settings, engine.hotspots, trip.country_code, engine.graph)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        use_router = trip.use_router and engine.graph is not None
        heading = None if trip.heading is None else round(trip.heading) % 360
        notes: list[str] = []
        if trip.use_router and engine.graph is None:
            notes.append("Own router unavailable: no road graph is loaded.")

        order = {"auto": settings.road_event_sources, "off": ()}.get(trip.road_events, (trip.road_events,))
        area = trip_area(trip.origin, trip.destination)
        report = collect_road_events(
            order,
            area,
            wzdx=engine.wzdx,
            wi511=engine.wi511,
            apify=engine.apify,
            google_ready=use_router and trip.live_traffic and bool(settings.google_maps_api_key),
        )
        notes += report.notes
        # Redrawn along the roads they lie on, for routing, scoring and the map alike.
        events = engine.snap(report.events)
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
        return TripResponse(
            plan=plan_from_candidates(candidates, profile),
            notes=notes,
            driving_side=context.driving_side,
            own_router_used=own_used,
            attribution=[f"Route design: map data {ATTRIBUTION}"] if own_used else [],
            google=google,
            road_events_source=report.source,
            road_events_skipped=report.skipped,
            road_events=[
                RoadEventOut(
                    kind=event.kind, source=event.source, description=event.description, path=encode_polyline(event.path)
                )
                for event in events
                if event.kind in ("closed", "lanes")
            ][:MAX_EVENTS_SHOWN],
        )

    # Mounted last, so the API routes above take precedence over file names.
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


app = create_app()
