"""Thin client for the Google Routes API (computeRoutes), with response caching.

It calls the REST endpoint directly with httpx. The `googlemaps` SDK only
wraps the legacy Directions API, which new Cloud projects can no longer
enable. Responses come back as raw JSON dicts; interpreting them is the
feature extractor's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from app.cache import ResponseCache
from app.config import Settings
from app.models import LatLng

ROUTES_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"

# The field mask is mandatory. Ask only for what the engine reads: smaller
# responses are faster to fetch and to cache.
FIELD_MASK = ",".join(
    (
        "routes.distanceMeters",
        "routes.duration",
        "routes.staticDuration",
        "routes.description",
        "routes.routeLabels",
        "routes.polyline.encodedPolyline",
        "routes.legs.steps.distanceMeters",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.polyline.encodedPolyline",
        "routes.legs.steps.navigationInstruction.maneuver",
    )
)


@dataclass(frozen=True)
class RouteRequest:
    """What to ask Google for; to_body() turns it into a computeRoutes request body."""

    origin: LatLng
    destination: LatLng
    via: tuple[LatLng, ...] = ()
    # Direction of travel at each via point, degrees clockwise from north. It
    # tells Google which way to pass the point on a two-way road.
    via_headings: tuple[int, ...] = ()
    avoid_highways: bool = False
    # TRAFFIC_AWARE moves the request from the Essentials SKU to the Pro SKU,
    # which has a smaller free allowance. Headings are a Pro feature too.
    traffic_aware: bool = False

    def __post_init__(self) -> None:
        if self.via_headings and len(self.via_headings) != len(self.via):
            raise ValueError("via_headings must have one heading per via point")

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "origin": _waypoint(self.origin),
            "destination": _waypoint(self.destination),
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE" if self.traffic_aware else "TRAFFIC_UNAWARE",
            # Google returns no alternatives for requests with intermediates.
            "computeAlternativeRoutes": not self.via,
            "polylineQuality": "HIGH_QUALITY",
            "units": "METRIC",
        }
        if self.via:
            headings = self.via_headings or (None,) * len(self.via)
            body["intermediates"] = [
                _waypoint(point, via=True, heading=heading) for point, heading in zip(self.via, headings, strict=True)
            ]
        if self.avoid_highways:
            body["routeModifiers"] = {"avoidHighways": True}
        return body


def _waypoint(point: LatLng, *, via: bool = False, heading: int | None = None) -> dict[str, Any]:
    # Rounding to 6 decimals (~0.1 m) keeps cache keys stable when the same
    # point is reached through slightly different floating-point math.
    waypoint: dict[str, Any] = {
        "location": {"latLng": {"latitude": round(point.lat, 6), "longitude": round(point.lng, 6)}}
    }
    if heading is not None:
        waypoint["location"]["heading"] = heading
    if via:
        # Pass through without stopping; doesn't split the route into legs.
        waypoint["via"] = True
    return waypoint


class RoutesApiError(RuntimeError):
    """The Routes API call failed or could not be made."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RoutesClient:
    def __init__(
        self,
        api_key: str | None,
        cache: ResponseCache,
        *,
        http: httpx.Client | None = None,
        timeout_s: float = 10.0,
        traffic_fresh_for: timedelta = timedelta(minutes=5),
    ) -> None:
        self._api_key = api_key
        self.cache = cache
        # Live traffic goes stale fast, so record mode re-fetches traffic-aware
        # answers older than this. Replay mode still serves them.
        self.traffic_fresh_for = traffic_fresh_for
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout_s)
        # Requests that actually went to Google, i.e. the potentially billable ones.
        self.live_requests = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> RoutesClient:
        cache = ResponseCache(settings.cache_dir, settings.cache_mode, max_age=settings.cache_max_age)
        return cls(settings.google_maps_api_key, cache, traffic_fresh_for=settings.traffic_fresh_for)

    def compute_routes(self, request: RouteRequest) -> list[dict[str, Any]]:
        """Return the raw `routes` array for a request; empty when Google found no route."""
        body = request.to_body()
        # Everything that changes Google's answer is part of the cache key. The API key is not.
        cache_request = {"endpoint": ROUTES_ENDPOINT, "fieldMask": FIELD_MASK, "body": body}
        fresh_for = self.traffic_fresh_for if request.traffic_aware else None
        response = self.cache.get_or_fetch(cache_request, lambda: self._post(body), fresh_for=fresh_for)
        # When no route exists Google may omit `routes` entirely.
        return response.get("routes", [])

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise RoutesApiError(
                "GOOGLE_MAPS_API_KEY is not set, so the live Routes API can't be called. "
                "Set it in backend/.env, or use ROUTES_CACHE_MODE=replay with recorded responses."
            )
        self.live_requests += 1
        try:
            response = self._http.post(
                ROUTES_ENDPOINT,
                json=body,
                headers={"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": FIELD_MASK},
            )
        except httpx.HTTPError as exc:
            raise RoutesApiError(f"Could not reach the Routes API: {exc}") from exc
        if response.is_error:
            raise RoutesApiError(
                f"Routes API returned {response.status_code}: {_google_error_message(response)}",
                response.status_code,
            )
        return response.json()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> RoutesClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _google_error_message(response: httpx.Response) -> str:
    # Google errors look like {"error": {"code": 403, "message": "...", "status": "PERMISSION_DENIED"}}.
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:500] or response.reason_phrase
