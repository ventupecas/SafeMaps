"""Visitors' answers to "Would you take this route?", kept for the expo's results.

Each answer is one JSON line in a file: the answer, how much safer and slower
the recommended route was than the standard one, and the profile. No places
are kept: a visitor's start or destination may be their home.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any


class FeedbackLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(self, answer: Mapping[str, Any]) -> None:
        line = json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **answer})
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def summary(self, day: date | None = None, since: datetime | None = None) -> dict[str, Any]:
        """How many said yes and no (on day, or ever; from since), and how much safer and slower the routes they judged were."""
        answers = self.answers(day, since)
        yes = [a for a in answers if a.get("choice") == "yes"]
        no = [a for a in answers if a.get("choice") == "no"]

        def average(group: list[dict[str, Any]], key: str) -> float | None:
            values = [float(a[key]) for a in group if isinstance(a.get(key), (int, float))]
            return round(sum(values) / len(values), 1) if values else None

        return {
            "yes": len(yes),
            "no": len(no),
            "total": len(answers),
            "yes_share": round(len(yes) / len(answers), 3) if answers else None,
            "safety_gain_when_yes": average(yes, "safety_gain"),
            "extra_min_when_yes": average(yes, "extra_min"),
            "safety_gain_when_no": average(no, "safety_gain"),
            "extra_min_when_no": average(no, "extra_min"),
        }

    def answers(self, day: date | None = None, since: datetime | None = None) -> list[dict[str, Any]]:
        """The lines kept (on day, or ever; from since)."""
        from app.trip_log import _counts

        return [answer for answer in self._read() if _counts(answer, day, since)]

    def _read(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        answers = []
        for line in lines:
            try:
                answer = json.loads(line)
            except ValueError:
                continue  # a line cut short by a crash mid-write
            if isinstance(answer, dict):
                answers.append(answer)
        return answers
