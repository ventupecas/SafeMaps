"""Where the sun is, and when it's in a driver's eyes.

Driving toward a low sun is a known crash factor: around sunrise and sunset,
on roads running toward it, the glare hides signals, brake lights and people
crossing. A route's sun glare is the minutes it drives within GLARE_ANGLE_DEG
of the sun's direction while the sun is up but lower than GLARE_MAX_ELEVATION_DEG.

The sun's position comes from NOAA's general solar position formulas (good to
about a degree, plenty here). Clouds aren't known, so a cloudy evening still
counts: a cautious simplification.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from app.geo import angle_between_deg

GLARE_MAX_ELEVATION_DEG = 25.0  # above this, a car's visor blocks it
GLARE_ANGLE_DEG = 25.0  # within this of the direction of travel, it's in the driver's eyes
# After dark (the sun this far below the horizon: past dusk), the gaps in oncoming traffic are judged
# by headlights alone, so a turn across traffic counts DARK_TURN_FACTOR as much. Driving is deadlier
# per mile at night; the factor is a judgment call, less than ice's (app.weather).
DARK_ELEVATION_DEG = -6.0
DARK_TURN_FACTOR = 1.25

Sun = tuple[float, float]  # (azimuth, elevation) in degrees: azimuth clockwise from north


def sun_position(moment: datetime, lat: float, lng: float) -> Sun:
    """The sun's azimuth and elevation at moment (any time zone; a naive one is local time) over lat, lng."""
    utc = moment.astimezone(timezone.utc)
    hours = utc.hour + utc.minute / 60 + utc.second / 3600
    # The fractional year, in radians.
    gamma = 2 * math.pi / 365 * (utc.timetuple().tm_yday - 1 + (hours - 12) / 24)
    equation_of_time_min = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    solar_minutes = hours * 60 + equation_of_time_min + 4 * lng
    hour_angle = math.radians(solar_minutes / 4 - 180)
    latitude = math.radians(lat)
    cos_zenith = math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)
    elevation = 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    azimuth = math.degrees(
        math.atan2(math.sin(hour_angle), math.cos(hour_angle) * math.sin(latitude) - math.tan(declination) * math.cos(latitude))
    )
    return (azimuth + 180) % 360, elevation


def low_sun(sun: Sun | None) -> bool:
    """Whether the sun is up and low enough to dazzle a driver heading toward it."""
    return sun is not None and 0 < sun[1] < GLARE_MAX_ELEVATION_DEG


def dark(sun: Sun | None) -> bool:
    """Whether it's dark: the sun more than DARK_ELEVATION_DEG below the horizon (after dusk, before dawn)."""
    return sun is not None and sun[1] < DARK_ELEVATION_DEG


def glare(bearing: float, sun: Sun | None) -> bool:
    """Whether driving along bearing (degrees clockwise from north) heads into a low sun."""
    return low_sun(sun) and angle_between_deg(bearing, sun[0]) <= GLARE_ANGLE_DEG
