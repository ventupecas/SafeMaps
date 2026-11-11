"""Weather alerts from the National Weather Service, and what they change.

api.weather.gov is free and needs no key, only a User-Agent. A trip asks for
the alerts in force where it starts (cached for ALERTS_REFRESH per spot).

Winter weather (snow, ice, freezing rain) makes stopping distances longer, so
turning across oncoming traffic gets riskier: in those alerts the risky turn
weights go up by SLIPPERY_TURN_FACTOR. Other alerts (dense fog, flooding,
high wind) are shown to the driver; the hazards TomTom reports on particular
roads count for each route (app.road_events).

The same service's hourly forecast says what's likely in the hour a trip
leaves (WeatherForecast): snow or ice counts as an alert for it does, and rain,
which lengthens stopping distances less, by WET_TURN_FACTOR.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from app.models import LatLng, ScoringProfile

logger = logging.getLogger(__name__)

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
NWS_POINTS_URL = "https://api.weather.gov/points"
USER_AGENT = "SafeRoutePlanner/1.0 (route planner prototype)"  # api.weather.gov refuses requests without one
ALERTS_REFRESH = timedelta(minutes=10)
FORECAST_REFRESH = timedelta(minutes=30)
SLIPPERY_WORDS = ("winter", "snow", "ice", "freezing", "sleet", "blizzard")
SLIPPERY_TURN_FACTOR = 1.5
WET_WORDS = ("rain", "showers", "thunderstorm", "drizzle")
WET_TURN_FACTOR = 1.2
SNOW_FORECAST_WORDS = (*SLIPPERY_WORDS, "wintry", "flurries")
LIKELY_PERCENT = 50  # a forecast counts from this chance of rain or snow


@dataclass(frozen=True)
class WeatherAlert:
    event: str  # e.g. "Winter Weather Advisory"
    headline: str
    ends: str  # ISO time it ends (or expires), "" if not given

    @property
    def slippery(self) -> bool:
        return any(word in self.event.lower() for word in SLIPPERY_WORDS)

    @property
    def wet(self) -> bool:
        return not self.slippery and any(word in self.event.lower() for word in WET_WORDS)


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


class WeatherForecast:
    """Rain or snow likely at a spot in the hour a trip leaves, from the National Weather Service's hourly
    forecast, as an alert of its own ("Snow likely when you leave"). Failures read as nothing forecast.

    The service is asked which forecast grid a spot is in (once per spot), then for that grid's hours
    (kept for FORECAST_REFRESH)."""

    def __init__(self, http: httpx.Client, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._http = http
        self._clock = clock
        self._hourly_urls: dict[tuple[float, float], str] = {}
        self._hours: dict[tuple[float, float], tuple[float, list[tuple[datetime, datetime, str, int]]]] = {}

    def at(self, where: LatLng, leaving: datetime) -> list[WeatherAlert]:
        """leaving: local time, as the server's clock has it."""
        key = (round(where.lat, 1), round(where.lng, 1))  # about 10 km, as the alerts are asked for
        cached = self._hours.get(key)
        if not cached or self._clock() - cached[0] > FORECAST_REFRESH.total_seconds():
            cached = (self._clock(), self._fetch(key))
            self._hours[key] = cached
        moment = leaving.astimezone()
        for start, end, words, percent in cached[1]:
            if start <= moment < end:
                kind = "Snow" if any(word in words.lower() for word in SNOW_FORECAST_WORDS) else "Rain" if any(word in words.lower() for word in WET_WORDS) else None
                if kind is None or percent < LIKELY_PERCENT:
                    return []
                return [WeatherAlert(event=f"{kind} likely when you leave", headline=f"{words}, {percent}% chance", ends="")]
        return []

    def _fetch(self, point: tuple[float, float]) -> list[tuple[datetime, datetime, str, int]]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
        try:
            url = self._hourly_urls.get(point)
            if url is None:
                response = self._http.get(f"{NWS_POINTS_URL}/{point[0]},{point[1]}", headers=headers, timeout=6.0, follow_redirects=True)
                response.raise_for_status()
                url = response.json()["properties"]["forecastHourly"]
                self._hourly_urls[point] = url
            response = self._http.get(url, headers=headers, timeout=6.0)
            response.raise_for_status()
            hours = []
            for period in response.json()["properties"]["periods"]:
                chance = (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
                hours.append(
                    (datetime.fromisoformat(period["startTime"]), datetime.fromisoformat(period["endTime"]), str(period.get("shortForecast") or ""), int(chance))
                )
            return hours
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Weather forecast unavailable: %s", type(exc).__name__)
            return []


def for_the_weather(profile: ScoringProfile, alerts: Sequence[WeatherAlert]) -> ScoringProfile:
    """The profile, warier of turning across traffic when the roads are slippery, and a little warier when they're wet."""
    factor = SLIPPERY_TURN_FACTOR if any(alert.slippery for alert in alerts) else WET_TURN_FACTOR if any(alert.wet for alert in alerts) else 1.0
    if factor == 1.0:
        return profile
    return profile.model_copy(
        update={
            "busy_crossing_weight": profile.busy_crossing_weight * factor,
            "hotspot_crossing_weight": profile.hotspot_crossing_weight * factor,
        }
    )
