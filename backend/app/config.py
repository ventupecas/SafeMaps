"""Runtime settings, read from environment variables and backend/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path

from dotenv import load_dotenv

from app.cache import CacheMode

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROAD_EVENT_SOURCES = ("wzdx", "wi511", "tomtom", "google", "apify")
DEFAULT_ROAD_EVENT_SOURCES = "wzdx+wi511+tomtom,google"
WZDX_FEED_URL = "https://511wi.gov/api/wzdx"
BUS_ROUTES_URL = "https://kamino.mcts.org/gtfs/google_transit.zip"  # Milwaukee County Transit System's GTFS


@dataclass(frozen=True)
class Settings:
    google_maps_api_key: str | None
    cache_mode: CacheMode
    cache_dir: Path
    cache_max_age: timedelta
    traffic_aware: bool
    default_country: str | None
    crash_data_path: Path | None
    crash_min_count: float
    graph_path: Path
    traffic_fresh_for: timedelta
    daily_limit: int  # live Google requests per day; 0 means no cap
    cors_origins: tuple[str, ...] = ()  # browser origins allowed to call the API besides localhost
    # Road events (closures, lane closures, jams): sources in order of preference,
    # the later ones backups; "a+b" uses a and b together. See app.road_events.
    road_event_sources: tuple[str, ...] = tuple(DEFAULT_ROAD_EVENT_SOURCES.split(","))
    wzdx_url: str = WZDX_FEED_URL
    wzdx_file: Path | None = None  # a saved copy of the feed to read instead, e.g. for an offline demo
    wi511_api_key: str | None = None
    # TomTom's traffic, for the OpenStreetMap map: incidents for routing (requests per day) and the
    # traffic map's tiles (per day). The free plan has 2,500 and 200,000 a month.
    tomtom_api_key: str | None = None
    tomtom_daily_requests: int = 80
    tomtom_daily_tiles: int = 6_000
    apify_token: str | None = None
    apify_daily_tiles: int = 300  # traffic tiles per day the paid Apify source may fetch
    # When set, route requests must send it (X-Access-Code): for a demo shared over a public link.
    access_code: str | None = None
    # For the expo's control page (pausing demo mode, restarting the stats). Unset: only this
    # computer itself may use it, not visitors coming through the tunnel.
    admin_code: str | None = None
    # The web map: "google" (Google's map, live traffic) or "osm" (OpenStreetMap, no Google at all;
    # Google's terms don't allow its results on another map, so that mode never asks Google).
    map: str = "google"
    # When school zones count: these local times on weekdays, outside July and August.
    school_hours: tuple[tuple[time, time], ...] = ((time(7, 0), time(9, 0)), (time(14, 0), time(16, 30)))
    # What a gallon of gas costs, in US dollars, for each route's fuel cost on the page.
    fuel_price: float = 3.25
    # Ask the National Weather Service for alerts where each trip starts (free, no key).
    weather_alerts: bool = True
    # The bus routes the OpenStreetMap map's Transit layer draws: a GTFS feed's address. None: rail only.
    bus_routes_url: str | None = BUS_ROUTES_URL

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(BACKEND_DIR / ".env")  # never overrides variables already set

        mode = os.getenv("ROUTES_CACHE_MODE") or CacheMode.RECORD
        try:
            cache_mode = CacheMode(mode.lower())
        except ValueError:
            raise ValueError(
                f"ROUTES_CACHE_MODE must be one of {', '.join(CacheMode)}; got {mode!r}"
            ) from None

        crash_min_count = float(os.getenv("ROUTES_CRASH_MIN_COUNT") or 3)
        if crash_min_count <= 0:
            raise ValueError("ROUTES_CRASH_MIN_COUNT must be positive")
        crash_data = os.getenv("ROUTES_CRASH_DATA")
        daily_limit = int(os.getenv("ROUTES_DAILY_LIMIT") or 150)
        if daily_limit < 0:
            raise ValueError("ROUTES_DAILY_LIMIT must be 0 (no cap) or more")
        sources = tuple(
            "+".join(part.strip() for part in group.split("+") if part.strip())
            for group in (os.getenv("ROUTES_ROAD_EVENT_SOURCES") or DEFAULT_ROAD_EVENT_SOURCES).lower().split(",")
            if group.strip()
        )
        unknown = [part for group in sources for part in group.split("+") if part not in ROAD_EVENT_SOURCES]
        if unknown:
            raise ValueError(f"ROUTES_ROAD_EVENT_SOURCES may only list {', '.join(ROAD_EVENT_SOURCES)}; got {unknown}")
        wzdx_file = os.getenv("ROUTES_WZDX_FILE")
        school_hours = _time_windows(os.getenv("ROUTES_SCHOOL_HOURS") or "07:00-09:00,14:00-16:30")
        map_kind = (os.getenv("ROUTES_MAP") or "google").strip().lower()
        if map_kind not in ("google", "osm"):
            raise ValueError(f"ROUTES_MAP must be google or osm; got {map_kind!r}")
        apify_daily_tiles = int(os.getenv("ROUTES_APIFY_DAILY_TILES") or 300)
        if apify_daily_tiles < 0:
            raise ValueError("ROUTES_APIFY_DAILY_TILES must be 0 or more")
        tomtom_daily_requests = int(os.getenv("ROUTES_TOMTOM_DAILY_REQUESTS") or 80)
        tomtom_daily_tiles = int(os.getenv("ROUTES_TOMTOM_DAILY_TILES") or 6_000)
        if tomtom_daily_requests < 0 or tomtom_daily_tiles < 0:
            raise ValueError("ROUTES_TOMTOM_DAILY_REQUESTS and ROUTES_TOMTOM_DAILY_TILES must be 0 or more")
        fuel_price = float(os.getenv("ROUTES_FUEL_PRICE") or 3.25)
        if fuel_price < 0:
            raise ValueError("ROUTES_FUEL_PRICE must be 0 or more (US dollars a gallon)")

        return cls(
            google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY") or None,
            cache_mode=cache_mode,
            cache_dir=_backend_path(os.getenv("ROUTES_CACHE_DIR") or ".cache/routes"),
            cache_max_age=timedelta(days=float(os.getenv("ROUTES_CACHE_MAX_AGE_DAYS") or 30)),
            traffic_aware=_env_flag("ROUTES_TRAFFIC_AWARE"),
            default_country=os.getenv("ROUTES_DEFAULT_COUNTRY") or None,
            crash_data_path=_backend_path(crash_data) if crash_data else None,
            crash_min_count=crash_min_count,
            graph_path=_backend_path(os.getenv("ROUTES_GRAPH") or "data/graphs/milwaukee-metro.json.gz"),
            traffic_fresh_for=timedelta(minutes=float(os.getenv("ROUTES_TRAFFIC_FRESH_MINUTES") or 5)),
            daily_limit=daily_limit,
            cors_origins=tuple(o.strip() for o in (os.getenv("ROUTES_CORS_ORIGINS") or "").split(",") if o.strip()),
            road_event_sources=sources,
            wzdx_url=os.getenv("ROUTES_WZDX_URL") or WZDX_FEED_URL,
            wzdx_file=_backend_path(wzdx_file) if wzdx_file else None,
            wi511_api_key=os.getenv("WI511_API_KEY") or None,
            tomtom_api_key=os.getenv("TOMTOM_API_KEY") or None,
            tomtom_daily_requests=tomtom_daily_requests,
            tomtom_daily_tiles=tomtom_daily_tiles,
            apify_token=os.getenv("APIFY_TOKEN") or None,
            apify_daily_tiles=apify_daily_tiles,
            access_code=os.getenv("ROUTES_ACCESS_CODE") or None,
            admin_code=os.getenv("ROUTES_ADMIN_CODE") or None,
            map=map_kind,
            school_hours=school_hours,
            fuel_price=fuel_price,
            weather_alerts=(os.getenv("ROUTES_WEATHER_ALERTS") or "on").strip().lower() not in {"0", "false", "no", "off"},
            bus_routes_url=_bus_routes_url(os.getenv("ROUTES_BUS_ROUTES")),
        )


def _time_windows(text: str) -> tuple[tuple[time, time], ...]:
    """'07:00-09:00,14:00-16:30' -> ((07:00, 09:00), (14:00, 16:30)); an empty string means never."""
    windows = []
    for part in text.split(","):
        if not part.strip():
            continue
        try:
            start, end = (time.fromisoformat(value.strip()) for value in part.split("-"))
        except ValueError:
            raise ValueError(f"ROUTES_SCHOOL_HOURS expects windows like 07:00-09:00; got {part.strip()!r}") from None
        windows.append((start, end))
    return tuple(windows)


def _bus_routes_url(value: str | None) -> str | None:
    """ROUTES_BUS_ROUTES: unset or on means MCTS's feed, off means none, or another GTFS feed's http(s) address."""
    value = (value or "").strip()
    if value.lower() in {"", "1", "true", "yes", "on"}:
        return BUS_ROUTES_URL
    if value.lower() in {"0", "false", "no", "off"}:
        return None
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError(f"ROUTES_BUS_ROUTES must be on, off or a GTFS feed's http(s) address; got {value!r}")
    return value


def _backend_path(value: str) -> Path:
    """Resolve relative paths against backend/, so they work from any working directory."""
    path = Path(value)
    return path if path.is_absolute() else BACKEND_DIR / path


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}
