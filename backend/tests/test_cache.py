from datetime import UTC, datetime, timedelta

import pytest

from app.cache import CacheMiss, CacheMode, ResponseCache

REQUEST = {"endpoint": "e", "body": {"a": 1}}
RESPONSE = {"routes": [{"distanceMeters": 1}]}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class CountingFetch:
    def __init__(self, response=RESPONSE) -> None:
        self.calls = 0
        self.response = response

    def __call__(self):
        self.calls += 1
        return self.response


def test_key_ignores_dict_order_but_not_values():
    assert ResponseCache.key_for({"a": 1, "b": 2}) == ResponseCache.key_for({"b": 2, "a": 1})
    assert ResponseCache.key_for({"a": 1}) != ResponseCache.key_for({"a": 2})


def test_record_mode_fetches_once_then_serves_from_disk(tmp_path):
    fetch = CountingFetch()
    cache = ResponseCache(tmp_path, CacheMode.RECORD)
    assert cache.get_or_fetch(REQUEST, fetch) == RESPONSE
    assert cache.get_or_fetch(REQUEST, fetch) == RESPONSE
    assert fetch.calls == 1
    assert (cache.hits, cache.misses) == (1, 1)

    # A fresh cache on the same directory, as in a new process, still hits.
    fresh = ResponseCache(tmp_path, CacheMode.RECORD)
    assert fresh.get_or_fetch(REQUEST, fetch) == RESPONSE
    assert fetch.calls == 1


def test_replay_mode_never_fetches(tmp_path):
    fetch = CountingFetch()
    with pytest.raises(CacheMiss):
        ResponseCache(tmp_path, CacheMode.REPLAY).get_or_fetch(REQUEST, fetch)
    assert fetch.calls == 0

    ResponseCache(tmp_path, CacheMode.RECORD).get_or_fetch(REQUEST, fetch)
    assert ResponseCache(tmp_path, CacheMode.REPLAY).get_or_fetch(REQUEST, fetch) == RESPONSE
    assert fetch.calls == 1


def test_off_mode_always_fetches_and_writes_nothing(tmp_path):
    fetch = CountingFetch()
    cache = ResponseCache(tmp_path, CacheMode.OFF)
    cache.get_or_fetch(REQUEST, fetch)
    cache.get_or_fetch(REQUEST, fetch)
    assert fetch.calls == 2
    assert list(tmp_path.iterdir()) == []


def test_entries_expire(tmp_path):
    clock = Clock()
    fetch = CountingFetch()
    ResponseCache(tmp_path, CacheMode.RECORD, max_age=timedelta(days=30), clock=clock).get_or_fetch(REQUEST, fetch)

    clock.now += timedelta(days=31)
    with pytest.raises(CacheMiss):
        ResponseCache(tmp_path, CacheMode.REPLAY, clock=clock).get_or_fetch(REQUEST, fetch)

    ResponseCache(tmp_path, CacheMode.RECORD, clock=clock).get_or_fetch(REQUEST, fetch)
    assert fetch.calls == 2  # expired entry was re-recorded


def test_live_data_is_refetched_in_record_mode_but_replayed_as_is(tmp_path):
    clock = Clock()
    fetch = CountingFetch()
    five_minutes = timedelta(minutes=5)
    ResponseCache(tmp_path, CacheMode.RECORD, clock=clock).get_or_fetch(REQUEST, fetch, fresh_for=five_minutes)

    clock.now += timedelta(minutes=2)
    ResponseCache(tmp_path, CacheMode.RECORD, clock=clock).get_or_fetch(REQUEST, fetch, fresh_for=five_minutes)
    assert fetch.calls == 1  # still fresh

    clock.now += timedelta(minutes=10)
    assert ResponseCache(tmp_path, CacheMode.REPLAY, clock=clock).get_or_fetch(REQUEST, fetch, fresh_for=five_minutes)
    assert fetch.calls == 1  # replay serves the recording
    ResponseCache(tmp_path, CacheMode.RECORD, clock=clock).get_or_fetch(REQUEST, fetch, fresh_for=five_minutes)
    assert fetch.calls == 2  # record mode wants live data


def test_corrupt_entry_is_treated_as_a_miss(tmp_path):
    (tmp_path / f"{ResponseCache.key_for(REQUEST)}.json").write_text("{not json", encoding="utf-8")
    fetch = CountingFetch()
    cache = ResponseCache(tmp_path, CacheMode.RECORD)
    assert cache.get_or_fetch(REQUEST, fetch) == RESPONSE
    assert cache.get_or_fetch(REQUEST, fetch) == RESPONSE
    assert fetch.calls == 1  # the corrupt file was overwritten with a good entry


def test_failed_fetch_is_not_cached(tmp_path):
    def boom():
        raise RuntimeError("network down")

    cache = ResponseCache(tmp_path, CacheMode.RECORD)
    with pytest.raises(RuntimeError):
        cache.get_or_fetch(REQUEST, boom)
    assert list(tmp_path.glob("*.json")) == []
