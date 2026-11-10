"""A local cap on live Google requests per day.

Google's console doesn't offer a per-day quota for every API, and a budget
alert only sends email. This counter refuses live requests once the day's
limit is used up; cached answers keep working. The count lives in a small
JSON file so it survives restarts, and resets at local midnight.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# How long a count read for show (used_today) stands before the file is read again. Every trip's
# answer carries it, and through Docker on Windows each read takes about a hundredth of a second.
SHOWN_FOR_S = 1.0


class DailyUsage:
    def __init__(self, path: Path, limit: int | None, *, today: Callable[[], date] = date.today) -> None:
        """limit=None means no cap; the requests are still counted."""
        self.path = Path(path)
        self.limit = limit
        self._today = today
        self._lock = threading.Lock()
        self._shown: tuple[str, int, float] | None = None  # the day, its count, and when that was read or written

    def take(self, count: int = 1) -> bool:
        """Count count requests. Returns False, counting nothing, if they don't fit in what's left today."""
        with self._lock:
            used = self._load()
            if self.limit is not None and used + count > self.limit:
                return False
            self._save(used + count)
            return True

    def used_today(self) -> int:
        """Today's count, to show. The cap doesn't go by it: take() reads the file every time, as other
        workers and the command line count in the same file. This is at most SHOWN_FOR_S behind them."""
        with self._lock:
            day = self._today().isoformat()
            if self._shown and self._shown[0] == day and time.monotonic() - self._shown[2] < SHOWN_FOR_S:
                return self._shown[1]
            used = self._load()
            self._shown = (day, used, time.monotonic())
            return used

    def _load(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return int(data["requests"]) if data.get("date") == self._today().isoformat() else 0
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring unreadable usage file %s: %s", self.path, exc)
            return 0

    def _save(self, used: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"date": self._today().isoformat(), "requests": used}, handle)
            os.replace(tmp_name, self.path)
            self._shown = (self._today().isoformat(), used, time.monotonic())
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
