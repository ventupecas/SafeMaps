"""Weather alerts from the National Weather Service, and what they change.

api.weather.gov is free and needs no key, only a User-Agent. A trip asks for
the alerts in force where it starts (cached for ALERTS_REFRESH per spot).

Winter weather (snow, ice, freezing rain) makes stopping distances longer, so
turning across oncoming traffic gets riskier: in those alerts the risky turn
weights go up by SLIPPERY_TURN_FACTOR. Other alerts (dense fog, flooding,
high wind) are shown to the driver; the hazards TomTom reports on particular
roads count for each route (app.road_events).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

import httpx

from app.models import LatLng, ScoringProfile

logger = logging.getLogger(__name__)

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
USER_AGENT = "SafeRoutePlanner/1.0 (route planner prototype)"  # api.weather.gov refuses requests without one
ALERTS_REFRESH = timedelta(minutes=10)
SLIPPERY_WORDS = ("winter", "snow", "ice", "freezing", "sleet", "blizzard")
SLIPPERY_TURN_FACTOR = 1.5


@dataclass(frozen=True)
class WeatherAlert:
    event: str  # e.g. "Winter Weather Advisory"
    headline: str
    ends: str  # ISO time it ends (or expires), "" if not given

    @property
    def slippery(self) -> bool:
        return any(word in self.event.lower() for word in SLIPPERY_WORDS)


class WeatherAlerts:
    """The alerts in force at a spot, from the National Weather Service. Failures read as none."""

    def __init__(self, http: httpx.Client, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._http = http
        self._clock = clock
        self._cache: dict[tuple[float, float], tuple[float, list[WeatherAlert]]] = {}

    def at(self, where: LatLng) -> list[WeatherAlert]:
        key = (round(where.lat, 1), round(where.lng, 1))  # about 10 km: the same county, the same alerts
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] <= ALERTS_REFRESH.total_seconds():
            return cached[1]
        alerts = self._fetch(key)
        self._cache[key] = (self._clock(), alerts)
        return alerts

    def _fetch(self, point: tuple[float, float]) -> list[WeatherAlert]:
        try:
            response = self._http.get(
                NWS_ALERTS_URL,
                params={"point": f"{point[0]},{point[1]}"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
                timeout=10.0,
            )
            response.raise_for_status()
            features = response.json().get("features") or []
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("Weather alerts unavailable: %s", type(exc).__name__)
            return []
        alerts = []
        for feature in features:
            properties = (feature.get("properties") or {}) if isinstance(feature, dict) else {}
            event = str(properties.get("event") or "").strip()
            if event:
                alerts.append(
                    WeatherAlert(
                        event=event,
                        headline=str(properties.get("headline") or event),
                        ends=str(properties.get("ends") or properties.get("expires") or ""),
                    )
                )
        return alerts


def for_the_weather(profile: ScoringProfile, alerts: Sequence[WeatherAlert]) -> ScoringProfile:
    """The profile, warier of turning across traffic when the roads are slippery."""
    if not any(alert.slippery for alert in alerts):
        return profile
    return profile.model_copy(
        update={
            "busy_crossing_weight": profile.busy_crossing_weight * SLIPPERY_TURN_FACTOR,
            "hotspot_crossing_weight": profile.hotspot_crossing_weight * SLIPPERY_TURN_FACTOR,
        }
    )
