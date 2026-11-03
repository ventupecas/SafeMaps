"""What the expo's control page (control.html) sets: kept on disk, so a restart keeps it.

* Demo mode paused: screens running demo mode stop starting sample trips until it's resumed.
  Only screens already in demo mode (the booth screen) are affected; visitors' phones never run it.
* Stats counted since: the stats screen counts trips and answers from this moment, so a
  rehearsal's trips can be left out without deleting anything.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ExpoControl:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state = {"demo_paused": False, "stats_since": None}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self._state.update({key: saved[key] for key in self._state if key in saved})
        except (OSError, ValueError):
            pass  # nothing saved yet: the defaults

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def stats_since(self) -> datetime | None:
        since = self.state()["stats_since"]
        return datetime.fromisoformat(since) if since else None

    def update(self, *, demo_paused: bool | None = None, reset_stats: bool = False) -> dict[str, Any]:
        with self._lock:
            if demo_paused is not None:
                self._state["demo_paused"] = demo_paused
            if reset_stats:
                self._state["stats_since"] = datetime.now().isoformat()  # to the microsecond: a trip that second came before
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._state), encoding="utf-8")
            except OSError as exc:
                logger.warning("Expo control not saved: %s", exc)
            return dict(self._state)
