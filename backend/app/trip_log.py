"""The trips visitors plan, counted for the expo's stats screen (GET /expo-stats).

Each trip is one JSON line in a file: what the recommended route avoided next
to the standard route (risky turns, railroad crossings, school zones, closed
roads) and how much safer it scored. As with visitors' answers (app.feedback),
no places are kept.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.models import RoutePlan, ScoredRoute

# What a recommended route can avoid, by the name the stats use and how to count it on a route.
AVOIDED = {
    "risky_turns": lambda route: route.features.busy_crossing_turns + route.features.hotspot_crossing_turns,
    "rail_crossings": lambda route: route.features.rail_crossings,
    "school_zones": lambda route: route.features.school_zones,
    "closures": lambda route: route.features.closures,
}


class TripLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(self, plan: RoutePlan, profile: str) -> None:
        winner = plan.winner
        standard = next((route for route in plan.candidates if route.is_standard), None)
        line = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "profile": profile,
            "different": standard is not None and standard.id != winner.id,
            "safety_gain": winner.safety_score - standard.safety_score if standard else 0,
            **{f"{name}_avoided": _avoided(count, winner, standard) for name, count in AVOIDED.items()},
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")

    def summary(self, day: date | None = None, since: datetime | None = None) -> dict[str, Any]:
        """Trips planned (on day, or ever; from since), how many got a safer route than the standard one, and what they avoided."""
        trips = [trip for trip in self._read() if _counts(trip, day, since)]
        gains = [trip["safety_gain"] for trip in trips if trip.get("different") and isinstance(trip.get("safety_gain"), (int, float))]
        return {
            "trips": len(trips),
            "safer_than_standard": sum(1 for trip in trips if trip.get("different")),
            "average_safety_gain": round(sum(gains) / len(gains), 1) if gains else None,
            **{f"{name}_avoided": sum(int(trip.get(f"{name}_avoided") or 0) for trip in trips) for name in AVOIDED},
        }

    def _read(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        trips = []
        for line in lines:
            try:
                trip = json.loads(line)
            except ValueError:
                continue  # a line cut short by a crash mid-write
            if isinstance(trip, dict):
                trips.append(trip)
        return trips


def _counts(line: dict[str, Any], day: date | None, since: datetime | None) -> bool:
    """Whether a logged line (with its "at" time) falls on day and from since; either may be None."""
    at = str(line.get("at", ""))
    return (day is None or at.startswith(day.isoformat())) and (since is None or at >= since.isoformat())


def _avoided(count: Any, winner: ScoredRoute, standard: ScoredRoute | None) -> int:
    """How many fewer the recommended route has than the standard one (none when it has more)."""
    return max(0, count(standard) - count(winner)) if standard is not None else 0
