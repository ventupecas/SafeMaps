"""File-backed cache for Google API responses.

Each distinct request is stored as one JSON file named by a hash of the
request, so you can tweak scoring weights and re-run as often as you like
without spending API quota. Files hold the request next to the response, so
they're easy to inspect.

Modes (ROUTES_CACHE_MODE):
    record  Serve from cache; on a miss call Google and save the answer. Default.
    replay  Serve only from cache. A miss raises CacheMiss and no call is made.
            Use this on the expo floor to guarantee zero API spend.
    off     Always call Google; never read or write the cache.

Google Maps Platform terms limit how long Maps content may be stored, so
entries expire after max_age (30 days by default). Check the current Service
Specific Terms before keeping cached data beyond development.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JsonDict = dict[str, Any]


class CacheMode(StrEnum):
    RECORD = "record"
    REPLAY = "replay"
    OFF = "off"


class CacheMiss(LookupError):
    """Replay mode was asked for a response it has not recorded."""


class ResponseCache:
    def __init__(
        self,
        directory: Path,
        mode: CacheMode = CacheMode.RECORD,
        *,
        max_age: timedelta = timedelta(days=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.directory = Path(directory)
        self.mode = mode
        self.max_age = max_age
        self._clock = clock
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key_for(request: Mapping[str, Any]) -> str:
        """Stable hash of a request. Key order doesn't matter; values do."""
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get_or_fetch(
        self,
        request: Mapping[str, Any],
        fetch: Callable[[], JsonDict],
        *,
        fresh_for: timedelta | None = None,
    ) -> JsonDict:
        """Return the response for request, calling fetch only when the mode allows and the cache can't answer.

        request must describe everything that changes the response (endpoint,
        field mask, body) and must never contain credentials: it is written to
        disk alongside the response.

        fresh_for is for time-sensitive answers such as live traffic: in record
        mode an older entry is fetched again. Replay mode ignores it, since its
        point is to serve what was recorded.
        """
        if self.mode is CacheMode.OFF:
            return fetch()

        key = self.key_for(request)
        cached = self._load(key, fresh_for if self.mode is CacheMode.RECORD else None)
        if cached is not None:
            self.hits += 1
            return cached

        self.misses += 1
        if self.mode is CacheMode.REPLAY:
            raise CacheMiss(
                f"No recorded response for request {key[:12]}. "
                "Record it first with ROUTES_CACHE_MODE=record."
            )

        response = fetch()  # errors propagate and are never cached
        try:
            self._store(key, request, response)
        except OSError as exc:
            # The caller still gets a good answer; we just can't reuse it.
            logger.warning("Could not write cache entry %s: %s", key[:12], exc)
        return response

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _load(self, key: str, fresh_for: timedelta | None = None) -> JsonDict | None:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            age = self._clock() - datetime.fromisoformat(entry["recorded_at"])
            response = entry["response"]
            if not isinstance(response, dict):
                raise TypeError("response is not a JSON object")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring unreadable cache entry %s: %s", path.name, exc)
            return None

        if age > self.max_age:
            logger.info("Cache entry %s expired (age %s)", path.name, age)
            return None
        if fresh_for is not None and age > fresh_for:
            logger.info("Cache entry %s is stale for live data (age %s)", path.name, age)
            return None
        return response

    def _store(self, key: str, request: Mapping[str, Any], response: JsonDict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {"recorded_at": self._clock().isoformat(), "request": request, "response": response}
        # Write to a temp file, then rename, so a crash mid-write never leaves a truncated entry.
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, indent=2)
            os.replace(tmp_name, self._path(key))
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
