"""Approximate fuel use of a typical gasoline car, for comparing routes.

Consumption per 100 km follows the usual U-shape against speed: high when
crawling, lowest around 60 km/h, and rising again with air drag at highway
speeds. Each turn costs the fuel to slow down and speed up again, and time
stuck in traffic costs stop-and-go fuel on top of the no-traffic estimate.

The numbers are rough values for a midsize sedan, roughly 22 mpg in town and
33 mpg on the highway. They only need to rank routes sensibly, not predict a
fuel bill. Tune them for the demo car.
"""

from __future__ import annotations

BASE_L_PER_100KM = 4.0  # engine and rolling losses at any speed
LOW_SPEED_L_PER_100KM = 120.0  # divided by speed: idling and low gears dominate when slow
DRAG_L_PER_100KM = 0.00025  # times speed squared: air resistance
TURN_L = 0.015  # slowing for a turn and speeding up again
SIGNAL_L = 0.012  # a traffic signal on average: a full stop about half the time, a slow-down otherwise
CONGESTION_L_PER_HOUR = 1.0  # stop-and-go, on top of the no-traffic estimate
_MIN_SPEED_KMH = 5.0


def cruise_l_per_100km(speed_kmh: float) -> float:
    v = max(speed_kmh, _MIN_SPEED_KMH)
    return BASE_L_PER_100KM + LOW_SPEED_L_PER_100KM / v + DRAG_L_PER_100KM * v * v


def cruise_liters(distance_m: float, speed_kmh: float) -> float:
    """Fuel to cover distance_m at a steady average of speed_kmh."""
    return distance_m / 1000 * cruise_l_per_100km(speed_kmh) / 100


# The lowest consumption at any speed, where the low-speed and drag terms balance.
# The router's A* uses it as a lower bound on fuel per km.
_MOST_EFFICIENT_KMH = (LOW_SPEED_L_PER_100KM / (2 * DRAG_L_PER_100KM)) ** (1 / 3)
MIN_L_PER_KM = cruise_l_per_100km(_MOST_EFFICIENT_KMH) / 100
