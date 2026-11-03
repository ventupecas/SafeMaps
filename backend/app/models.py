"""Data models shared by the routing engine and the HTTP API.

Nothing here knows Google's wire format. The Routes client and the feature
extractor translate raw JSON into these types, so the scoring math works on
clean, validated values.
"""

from __future__ import annotations

from typing import Literal

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

    The defaults put the driver's safety and fuel ahead of minutes saved. A
    turn across oncoming traffic (a left where traffic keeps right, a right
    where it keeps left) at a busy junction feels like 3 extra minutes; at a
    crash-prone one, 6 more. Every liter of fuel feels like 8 minutes. So a
    route a few minutes slower wins when it's safer or uses less fuel, and
    max_detour_ratio stops that from turning into a long detour.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "custom"
    time_weight: float = Field(1.0, ge=0, description="Perceived minutes per minute of trip time.")
    distance_weight: float = Field(0.0, ge=0, description="Perceived minutes per km driven (wear).")
    fuel_weight: float = Field(8.0, ge=0, description="Perceived minutes per liter of fuel the route burns.")
    highway_weight: float = Field(0.0, ge=0, description="Extra perceived minutes per minute at highway speed.")
    traffic_weight: float = Field(
        0.0,
        ge=0,
        description="Extra perceived minutes per minute of congestion delay. Needs traffic-aware routing.",
    )
    maneuver_weight: float = Field(0.0, ge=0, description="Perceived minutes per turn, merge, fork or roundabout.")
    scenic_weight: float = Field(0.0, ge=0, description="Perceived minutes refunded per minute on winding roads.")
    busy_crossing_weight: float = Field(
        3.0,
        ge=0,
        description="Perceived minutes per turn across oncoming traffic at a busy junction.",
    )
    hotspot_crossing_weight: float = Field(
        6.0,
        ge=0,
        description="Extra perceived minutes per turn across oncoming traffic at a crash-prone junction.",
    )
    lane_closure_weight: float = Field(
        3.0,
        ge=0,
        description="Perceived minutes per lane closure (a work zone or crash with a forced merge) driven through.",
    )
    school_zone_weight: float = Field(
        3.0,
        ge=0,
        description="Perceived minutes per school zone driven through during school hours.",
    )
    rail_crossing_weight: float = Field(
        1.0,
        ge=0,
        description="Perceived minutes per railroad crossing at grade: rare but severe crashes, and trains block them.",
    )
    weather_hazard_weight: float = Field(
        3.0,
        ge=0,
        description="Perceived minutes per stretch with a reported weather hazard (fog, ice, flooding) driven through.",
    )
    sun_glare_weight: float = Field(
        1.0,
        ge=0,
        description="Perceived minutes per minute driven toward a low sun (see app.sun).",
    )
    max_detour_ratio: float = Field(
        1.6,
        ge=1.0,
        description="Candidates slower than this multiple of the fastest one are not eligible to win.",
    )
    max_extra_min: float | None = Field(
        None,
        ge=0,
        description="Nor are candidates more than this many minutes slower than the fastest: the most time the "
        "driver will give up for a safer route. None: only max_detour_ratio limits them.",
    )
    ranking: Literal["weighted", "priority"] = Field(
        "weighted",
        description=(
            "weighted: the lowest total perceived cost wins. "
            "priority: the safest route wins; among equally safe ones the least fuel; then the fastest."
        ),
    )
    fuel_tolerance: float = Field(
        0.03,
        ge=0,
        le=0.5,
        description="In priority ranking, fuel within this share of the lowest counts as equally efficient.",
    )

    @model_validator(mode="after")
    def _scenic_refund_within_time_cost(self) -> ScoringProfile:
        # A refund larger than the time cost would make a winding minute worth
        # less than nothing, so the longest eligible detour would always win.
        if self.scenic_weight > self.time_weight:
            raise ValueError("scenic_weight must not exceed time_weight")
        return self


# A turn across traffic at a busy junction with a traffic signal counts this share of one without:
# it's made on a green (often on its own arrow), not by picking a gap in oncoming cars. A judgment
# call, like the profiles' weights; crash-prone junctions count in full either way.
SIGNALED_CROSSING_SHARE = 0.5


class CrossingTurn(BaseModel):
    """A turn across oncoming traffic: a left where traffic keeps right, a right where it keeps left."""

    model_config = ConfigDict(frozen=True)

    location: LatLng | None = Field(description="Where the turn is made; None if Google sent no geometry.")
    maneuver: str
    road_speed_kmh: float = Field(description="No-traffic speed of the faster road at the junction.")
    nearby_crashes: float = Field(description="Recorded crashes near the junction; 0 without crash data.")
    busy: bool = Field(description="At least one road at the junction is arterial class or faster.")
    hotspot: bool = Field(description="Enough recorded crashes nearby to call the junction crash-prone.")
    signal: bool = Field(False, description="A traffic signal controls the junction (on the road map).")


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
    fuel_l: float = Field(0.0, description="Rough fuel estimate in liters (see app.fuel).")
    signal_count: int = Field(0, description="Traffic signals passed; counted only when the road graph is loaded.")
    crossing_turns: tuple[CrossingTurn, ...] = ()
    closures: int = Field(0, description="Closed roads the route drives through, per the trip's road event source.")
    lane_closures: int = Field(0, description="Lane closures the route drives through, per the road event source.")
    lane_closures_with_workers: int = Field(0, description="Of those, how many have workers on site now.")
    school_zones: int = Field(0, description="Schools the route passes within their school zone, during school hours.")
    rail_crossings: int = Field(0, description="Railroad crossings at grade the route drives over.")
    rail_crossing_points: tuple[LatLng, ...] = Field((), description="Where those railroad crossings are.")
    school_points: tuple[LatLng, ...] = Field((), description="The schools whose zones count (school_zones).")
    weather_hazards: int = Field(0, description="Stretches with a reported weather hazard (fog, ice, flooding) driven through.")
    sun_glare_min: float = Field(0.0, description="Minutes driven toward a low sun, at the trip's departure time.")

    @computed_field
    @property
    def busy_crossing_turns(self) -> int:
        return sum(turn.busy for turn in self.crossing_turns)

    @computed_field
    @property
    def hotspot_crossing_turns(self) -> int:
        return sum(turn.hotspot for turn in self.crossing_turns)

    @computed_field
    @property
    def signaled_busy_crossing_turns(self) -> int:
        """Busy crossing turns at a traffic signal: they count SIGNALED_CROSSING_SHARE of one."""
        return sum(turn.busy and turn.signal for turn in self.crossing_turns)


class ScoreBreakdown(BaseModel):
    """Each term of the personalized cost, in perceived minutes."""

    model_config = ConfigDict(frozen=True)

    time: float
    distance: float
    fuel: float
    highway: float
    traffic: float
    maneuvers: float
    busy_crossings: float
    hotspot_crossings: float
    lane_closures: float = 0.0
    school_zones: float = 0.0
    rail_crossings: float = 0.0
    weather_hazards: float = 0.0
    sun_glare: float = 0.0
    scenic: float = Field(description="Zero or negative: a refund for winding roads.")
    total: float


class Maneuver(BaseModel):
    """One turn-by-turn instruction: what to do, and where."""

    model_config = ConfigDict(frozen=True)

    maneuver: str = Field(description="The Routes API maneuver name, e.g. TURN_RIGHT.")
    instruction: str = Field(description='What to say, e.g. "Turn right onto West Wisconsin Avenue".')
    location: LatLng


class CandidateRoute(BaseModel):
    """One possible path between origin and destination."""

    model_config = ConfigDict(frozen=True)

    id: str
    source: str = Field(description="How it was generated: google, avoid-highways, via-offset or own (our router).")
    description: str = Field("", description="Short summary, usually the main road name.")
    via: str = Field(
        "",
        description='The road the route is known by, from our road map ("W Wisconsin Ave"): its longest one that no '
        "better-ranked route already goes by. Empty without a road map.",
    )
    encoded_polyline: str
    is_google_default: bool = Field(False, description="True for the route Google itself would choose.")
    is_standard: bool = Field(
        False,
        description="The baseline the recommendation is compared with: Google's own choice, or with the "
        "OpenStreetMap map (no Google), our router's fastest route.",
    )
    live_traffic: bool = Field(False, description="True when the travel time includes Google's live traffic.")
    design_match: float | None = Field(
        None,
        description="For our router's routes priced by Google: share of Google's route that follows our design.",
    )
    features: RouteFeatures
    maneuvers: tuple[Maneuver, ...] = Field((), description="Turn-by-turn instructions, in order.")


class ScoredRoute(CandidateRoute):
    """A candidate with its personalized cost under one profile."""

    breakdown: ScoreBreakdown
    eligible: bool = Field(description="False when the route exceeds the profile's max_detour_ratio.")
    reason: str | None = Field(None, description="Why it ranks where it does (priority ranking).")
    safety_score: int = Field(
        100,
        ge=0,
        le=100,
        description="0-100, higher is safer: the route's risks (risky turns, closures, lane closures, school zones, "
        "railroad crossings, weather hazards, sun glare) weighed the same way whatever the profile.",
    )
    safety_risks: dict[str, float] = Field(
        default_factory=dict,
        description="The risk points behind safety_score, by kind (busy_crossings, hotspot_crossings, closures, "
        "lane_closures, school_zones, rail_crossings, weather_hazards, sun_glare): only the kinds the route has. "
        "The score is 100 / (1 + their sum / 10).",
    )


class RoutePlan(BaseModel):
    """The engine's answer: the winning route plus every candidate, best first."""

    model_config = ConfigDict(frozen=True)

    profile: ScoringProfile
    winner: ScoredRoute
    candidates: list[ScoredRoute]
    hidden: dict[str, str] = Field(
        default_factory=dict,
        description="Routes left out because they mostly follow a better one: hidden id -> that route's id.",
    )

    @computed_field
    @property
    def overrides_google(self) -> bool:
        """True when the personalized pick differs from Google's default route."""
        return not self.winner.is_google_default
