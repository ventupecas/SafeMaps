"""Data models shared by the routing engine and the HTTP API.

Nothing here knows Google's wire format. The Routes client and the feature
extractor translate raw JSON into these types, so the scoring math works on
clean, validated values.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class LatLng(BaseModel):
    """A WGS84 coordinate."""

    model_config = ConfigDict(frozen=True)

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)

    @classmethod
    def parse(cls, text: str) -> LatLng:
        """Parse a "lat,lng" string such as "37.7955,-122.3937"."""
        try:
            lat, lng = (float(part) for part in text.split(","))
        except ValueError as exc:
            raise ValueError(f"Expected 'lat,lng', got {text!r}") from exc
        return cls(lat=lat, lng=lng)

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lng)


class ScoringProfile(BaseModel):
    """A driver's preferences, expressed as weights on route features.

    Every cost term is in *perceived minutes*, so weights read naturally:
    highway_weight=0.5 means a minute at highway speed feels like 1.5 minutes
    to this driver. The eligible route with the lowest total wins.

    By default, turns across oncoming traffic (lefts where traffic keeps right,
    rights where it keeps left) cost extra at busy and crash-prone junctions,
    so routes using near-side turns win when times are close.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "custom"
    time_weight: float = Field(1.0, ge=0, description="Perceived minutes per minute of trip time.")
    distance_weight: float = Field(0.0, ge=0, description="Perceived minutes per km driven (fuel and wear).")
    highway_weight: float = Field(0.0, ge=0, description="Extra perceived minutes per minute at highway speed.")
    traffic_weight: float = Field(
        0.0,
        ge=0,
        description="Extra perceived minutes per minute of congestion delay. Needs traffic-aware routing.",
    )
    maneuver_weight: float = Field(0.0, ge=0, description="Perceived minutes per turn, merge, fork or roundabout.")
    scenic_weight: float = Field(0.0, ge=0, description="Perceived minutes refunded per minute on winding roads.")
    busy_crossing_weight: float = Field(
        0.5,
        ge=0,
        description="Perceived minutes per turn across oncoming traffic at a busy junction.",
    )
    hotspot_crossing_weight: float = Field(
        2.0,
        ge=0,
        description="Extra perceived minutes per turn across oncoming traffic at a crash-prone junction.",
    )
    max_detour_ratio: float = Field(
        1.6,
        ge=1.0,
        description="Candidates slower than this multiple of the fastest one are not eligible to win.",
    )

    @model_validator(mode="after")
    def _scenic_refund_within_time_cost(self) -> ScoringProfile:
        # A refund larger than the time cost would make a winding minute worth
        # less than nothing, so the longest eligible detour would always win.
        if self.scenic_weight > self.time_weight:
            raise ValueError("scenic_weight must not exceed time_weight")
        return self


class CrossingTurn(BaseModel):
    """A turn across oncoming traffic: a left where traffic keeps right, a right where it keeps left."""

    model_config = ConfigDict(frozen=True)

    location: LatLng | None = Field(description="Where the turn is made; None if Google sent no geometry.")
    maneuver: str
    road_speed_kmh: float = Field(description="No-traffic speed of the faster road at the junction.")
    nearby_crashes: float = Field(description="Recorded crashes near the junction; 0 without crash data.")
    busy: bool = Field(description="At least one road at the junction is arterial class or faster.")
    hotspot: bool = Field(description="Enough recorded crashes nearby to call the junction crash-prone.")


class RouteFeatures(BaseModel):
    """Measurable, profile-independent properties of one route."""

    model_config = ConfigDict(frozen=True)

    distance_km: float
    duration_min: float = Field(description="Trip time; includes traffic when traffic-aware routing is on.")
    static_duration_min: float = Field(description="Google's trip time without live traffic.")
    highway_min: float
    highway_km: float
    traffic_delay_min: float
    maneuver_count: int
    winding_min: float
    crossing_turns: tuple[CrossingTurn, ...] = ()

    @computed_field
    @property
    def busy_crossing_turns(self) -> int:
        return sum(turn.busy for turn in self.crossing_turns)

    @computed_field
    @property
    def hotspot_crossing_turns(self) -> int:
        return sum(turn.hotspot for turn in self.crossing_turns)


class ScoreBreakdown(BaseModel):
    """Each term of the personalized cost, in perceived minutes."""

    model_config = ConfigDict(frozen=True)

    time: float
    distance: float
    highway: float
    traffic: float
    maneuvers: float
    busy_crossings: float
    hotspot_crossings: float
    scenic: float = Field(description="Zero or negative: a refund for winding roads.")
    total: float


class CandidateRoute(BaseModel):
    """One possible path between origin and destination."""

    model_config = ConfigDict(frozen=True)

    id: str
    source: str = Field(description="How it was generated: google, avoid-highways or via-offset.")
    description: str = Field("", description="Google's short summary, usually the main road name.")
    encoded_polyline: str
    is_google_default: bool = Field(False, description="True for the route Google itself would choose.")
    features: RouteFeatures


class ScoredRoute(CandidateRoute):
    """A candidate with its personalized cost under one profile."""

    breakdown: ScoreBreakdown
    eligible: bool = Field(description="False when the route exceeds the profile's max_detour_ratio.")


class RoutePlan(BaseModel):
    """The engine's answer: the winning route plus every candidate, best first."""

    model_config = ConfigDict(frozen=True)

    profile: ScoringProfile
    winner: ScoredRoute
    candidates: list[ScoredRoute]

    @computed_field
    @property
    def overrides_google(self) -> bool:
        """True when the personalized pick differs from Google's default route."""
        return not self.winner.is_google_default
