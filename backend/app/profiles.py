"""Preset driver personas for the demo. Each one is just a ScoringProfile."""

from __future__ import annotations

from collections.abc import Mapping

from app.models import ScoringProfile

DEFAULT_PROFILE = "safe-economy"

PRESETS: dict[str, ScoringProfile] = {
    profile.name: profile
    for profile in (
        # Pure trip time, with no safety or fuel preference: the baseline that
        # should usually agree with Google's default route.
        ScoringProfile(
            name="fastest",
            busy_crossing_weight=0.0,
            hotspot_crossing_weight=0.0,
            lane_closure_weight=0.0,
            school_zone_weight=0.0,
            rail_crossing_weight=0.0,
            fuel_weight=0.0,
        ),
        # The default: safety first, then fuel, then time. The safest route wins;
        # among equally safe ones the least fuel; then the fastest. (The weights,
        # a busy left = 3 min, crash-prone = 6 more, a liter = 8 min, still shape
        # the router's designs and the cost column.)
        ScoringProfile(name=DEFAULT_PROFILE, ranking="priority"),
        # Every minute at highway speed feels like two; extra turns are a small price.
        ScoringProfile(name="highway-averse", highway_weight=1.0, maneuver_weight=0.05),
        # Winding roads give back 60% of their time; highways feel a bit dull.
        ScoringProfile(name="scenic", scenic_weight=0.6, highway_weight=0.3, max_detour_ratio=1.5),
        # Hates stop-and-go and fiddly junctions (each turn costs 15 perceived seconds),
        # and strongly avoids turning across traffic where it's busy or crashes cluster.
        ScoringProfile(
            name="calm",
            traffic_weight=1.5,
            maneuver_weight=0.25,
            busy_crossing_weight=4.0,
            hotspot_crossing_weight=8.0,
            lane_closure_weight=4.0,
            school_zone_weight=4.0,
            rail_crossing_weight=1.5,
        ),
        # Fuel first: a liter feels like 15 minutes and time counts half.
        ScoringProfile(name="eco", time_weight=0.5, fuel_weight=15.0),
    )
}


def get_profile(name: str, overrides: Mapping[str, float] | None = None) -> ScoringProfile:
    """Look up a preset and optionally override some of its weights.

    Raises ValueError for an unknown preset or an invalid override.
    """
    try:
        preset = PRESETS[name]
    except KeyError:
        raise ValueError(f"Unknown profile {name!r}; choose from {', '.join(PRESETS)}") from None
    if not overrides:
        return preset
    # Re-validate rather than model_copy(update=...), which would skip validation.
    return ScoringProfile.model_validate({**preset.model_dump(), **overrides, "name": f"{name} (custom)"})
