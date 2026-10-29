"""Runtime settings, read from environment variables and backend/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from app.cache import CacheMode

BACKEND_DIR = Path(__file__).resolve().parent.parent


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

        return cls(
            google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY") or None,
            cache_mode=cache_mode,
            cache_dir=_backend_path(os.getenv("ROUTES_CACHE_DIR") or ".cache/routes"),
            cache_max_age=timedelta(days=float(os.getenv("ROUTES_CACHE_MAX_AGE_DAYS") or 30)),
            traffic_aware=_env_flag("ROUTES_TRAFFIC_AWARE"),
            default_country=os.getenv("ROUTES_DEFAULT_COUNTRY") or None,
            crash_data_path=_backend_path(crash_data) if crash_data else None,
            crash_min_count=crash_min_count,
        )


def _backend_path(value: str) -> Path:
    """Resolve relative paths against backend/, so they work from any working directory."""
    path = Path(value)
    return path if path.is_absolute() else BACKEND_DIR / path


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}
