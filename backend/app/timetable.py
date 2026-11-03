"""Bus trips from Milwaukee County Transit System's schedule (GTFS): which buses get a rider there.

The schedule is the same feed app.transit draws bus routes from, here with every trip's times: about a
million stop times, kept as arrays per route pattern (one route's run through the same stops). A trip
is found with RAPTOR (Delling, Pajor and Werneck, 2012): round by round, every stop reachable with one
bus, then with a transfer, then with two, each at its earliest time. A rider walks to a stop within
ACCESS_M, between stops within TRANSFER_M to transfer, and from a stop within ACCESS_M; walking times
here are estimates (straight line, WALK_DETOUR longer), and app.transit_plan replans the walks on the
walking map for the trip shown.

Services run on the dates the feed lists (calendar_dates.txt, and calendar.txt if it has one). A trip
after midnight belongs to the day before (its times run past 24:00), so a search early in the morning
also looks at the day before's late trips.
"""

from __future__ import annotations

import bisect
import csv
import io
import logging
import math
import time
import zipfile
from array import array
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

import httpx

from app.geo import Point, haversine_m

logger = logging.getLogger(__name__)

ACCESS_M = 800.0  # the farthest a rider walks to or from a stop ...
FAR_ACCESS_M = 1600.0  # ... unless there's no bus within that: then up to this
TRANSFER_M = 250.0  # the farthest between two stops to transfer
WALK_MPS = 4.8 / 3.6  # walking speed, meters a second
WALK_DETOUR = 1.3  # streets are this much longer than the straight line, about
BOARD_SLACK_S = 60  # at the stop a minute before the bus
MAX_RIDES = 3  # at most two transfers
REFRESH_S = 7 * 24 * 3600
_CELL_DEG = 0.01  # about 1 km: the stop index's cells
INF = 10**9


def walk_seconds(meters: float) -> int:
    return round(meters * WALK_DETOUR / WALK_MPS)


@dataclass
class Pattern:
    """One route's run through one sequence of stops; its trips sorted by their first departure."""

    route: int
    stops: list[int]
    headsign: str
    trips: list[str] = field(default_factory=list)
    services: list[int] = field(default_factory=list)
    arr: list[array] = field(default_factory=list)  # per position along the stops: each trip's arrival, seconds
    dep: list[array] = field(default_factory=list)  # ... and departure


@dataclass
class Ride:
    pattern: Pattern
    trip: int  # index into pattern.trips
    board: int  # position along pattern.stops
    alight: int


@dataclass
class Journey:
    """A way there: walk to a stop, rides (with a walk between them where the stops differ), walk on."""

    start_walk_m: float
    first_stop: int
    rides: list[Ride]
    transfers: list[tuple[int, int, float]]  # (from stop, to stop, meters) walked between rides
    last_stop: int
    end_walk_m: float
    arrive_s: int  # arrival at the destination, seconds after the service day's midnight
    service_day: date

    @property
    def board_s(self) -> int:
        first = self.rides[0]
        return first.pattern.dep[first.board][first.trip]


class Timetable:
    def __init__(self) -> None:
        self.stop_ids: list[str] = []
        self.stop_names: list[str] = []
        self.stop_points: list[Point] = []
        self.routes: list[tuple[str, str, str]] = []  # (short name, long name, color)
        self.patterns: list[Pattern] = []
        self.stop_patterns: list[list[tuple[Pattern, int]]] = []
        self.transfers: list[list[tuple[int, float]]] = []
        self.service_names: list[str] = []
        self.dates: dict[date, set[int]] = defaultdict(set)
        self._cells: dict[tuple[int, int], list[int]] = defaultdict(list)

    # ---- Reading the feed

    @classmethod
    def from_gtfs(cls, data: bytes) -> Timetable:
        table = cls()
        with zipfile.ZipFile(io.BytesIO(data)) as feed:
            names = set(feed.namelist())
            stop_index: dict[str, int] = {}
            for row in _rows(feed, "stops.txt"):
                if row.get("location_type", "0") not in ("", "0"):
                    continue  # stations and entrances: riders board at stops
                try:
                    point = (float(row["stop_lat"]), float(row["stop_lon"]))
                except (KeyError, ValueError):
                    continue
                stop_index[row["stop_id"]] = len(table.stop_ids)
                table.stop_ids.append(row["stop_id"])
                table.stop_names.append(_title(row.get("stop_name", "")))
                table.stop_points.append(point)
            route_index: dict[str, int] = {}
            for row in _rows(feed, "routes.txt"):
                color = (row.get("route_color") or "").strip().lstrip("#")
                route_index[row["route_id"]] = len(table.routes)
                table.routes.append(
                    (
                        (row.get("route_short_name") or "").strip() or row["route_id"],
                        " ".join((row.get("route_long_name") or "").split()),
                        f"#{color}" if len(color) == 6 and color.upper() != "FFFFFF" else "#1a73e8",
                    )
                )
            service_index: dict[str, int] = {}

            def service(name: str) -> int:
                if name not in service_index:
                    service_index[name] = len(table.service_names)
                    table.service_names.append(name)
                return service_index[name]

            trips: dict[str, tuple[int, int, str]] = {}
            for row in _rows(feed, "trips.txt"):
                if row["route_id"] in route_index:
                    trips[row["trip_id"]] = (route_index[row["route_id"]], service(row["service_id"]), _title(row.get("trip_headsign", "")))
            if "calendar.txt" in names:
                days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
                for row in _rows(feed, "calendar.txt"):
                    start, end = _date(row["start_date"]), _date(row["end_date"])
                    weekdays = {i for i, day in enumerate(days) if row.get(day) == "1"}
                    day = start
                    while day <= end:
                        if day.weekday() in weekdays:
                            table.dates[day].add(service(row["service_id"]))
                        day += timedelta(days=1)
            if "calendar_dates.txt" in names:
                for row in _rows(feed, "calendar_dates.txt"):
                    day, number = _date(row["date"]), service(row["service_id"])
                    if row.get("exception_type") == "1":
                        table.dates[day].add(number)
                    elif row.get("exception_type") == "2":
                        table.dates[day].discard(number)

            # Each trip's stops and times, in order; then trips with the same route and stops make a pattern.
            visits: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)
            for row in _rows(feed, "stop_times.txt"):
                stop = stop_index.get(row["stop_id"])
                if stop is None or row["trip_id"] not in trips:
                    continue
                arrive = _seconds(row.get("arrival_time") or row.get("departure_time") or "")
                leave = _seconds(row.get("departure_time") or row.get("arrival_time") or "")
                if arrive is None or leave is None:
                    continue
                visits[row["trip_id"]].append((int(row["stop_sequence"]), stop, arrive, leave))
        grouped: dict[tuple[int, tuple[int, ...]], list[tuple[int, str, list[tuple[int, int, int, int]]]]] = defaultdict(list)
        for trip_id, stops in visits.items():
            stops.sort()
            route, _, _ = trips[trip_id]
            grouped[(route, tuple(s[1] for s in stops))].append((stops[0][3], trip_id, stops))
        for (route, stops), runs in grouped.items():
            if len(stops) < 2:
                continue
            runs.sort()
            pattern = Pattern(route=route, stops=list(stops), headsign=trips[runs[0][1]][2])
            pattern.arr = [array("i", (run[2][p][2] for run in runs)) for p in range(len(stops))]
            pattern.dep = [array("i", (run[2][p][3] for run in runs)) for p in range(len(stops))]
            pattern.trips = [run[1] for run in runs]
            pattern.services = [trips[run[1]][1] for run in runs]
            table.patterns.append(pattern)
        table._index()
        return table

    def _index(self) -> None:
        self.stop_patterns = [[] for _ in self.stop_ids]
        for pattern in self.patterns:
            for position, stop in enumerate(pattern.stops):
                self.stop_patterns[stop].append((pattern, position))
        for stop, point in enumerate(self.stop_points):
            self._cells[_cell(point)].append(stop)
        self.transfers = [[(other, meters) for other, meters in self.stops_near(point, TRANSFER_M) if other != stop] for stop, point in enumerate(self.stop_points)]

    def stops_near(self, point: Point, within_m: float) -> list[tuple[int, float]]:
        ci, cj = _cell(point)
        reach = math.ceil(within_m / 700) + 1
        found = []
        for i in range(ci - reach, ci + reach + 1):
            for j in range(cj - reach, cj + reach + 1):
                for stop in self._cells.get((i, j), ()):
                    meters = haversine_m(point, self.stop_points[stop])
                    if meters <= within_m:
                        found.append((stop, meters))
        return sorted(found, key=lambda item: item[1])

    # ---- Searching

    def journeys(self, origin: Point, destination: Point, leave: datetime) -> list[Journey]:
        """The fastest way there with one bus, with two, and with three, as far as each is faster than
        with fewer; looking at the service day of leave, and early in the morning the day before's
        trips running past midnight too."""
        found: list[Journey] = []
        for reach in (ACCESS_M, FAR_ACCESS_M):  # a farther walk only where no bus is near
            found = self._raptor(origin, destination, leave.date(), _seconds_of(leave), reach)
            if leave.hour < 4:
                found += self._raptor(origin, destination, leave.date() - timedelta(days=1), _seconds_of(leave) + 86400, reach)
            if found:
                break
        found.sort(key=lambda j: (len(j.rides), j.arrive_s - (86400 if j.service_day < leave.date() else 0)))
        best: list[Journey] = []
        for journey in found:
            arrive = journey.arrive_s - (86400 if journey.service_day < leave.date() else 0)
            if all(arrive < other.arrive_s - (86400 if other.service_day < leave.date() else 0) for other in best):
                best.append(journey)
        return best

    def _raptor(self, origin: Point, destination: Point, day: date, start_s: int, reach_m: float = ACCESS_M) -> list[Journey]:
        active = self.dates.get(day, set())
        if not active:
            return []
        access = self.stops_near(origin, reach_m)
        egress = dict(self.stops_near(destination, reach_m))
        if not access or not egress:
            return []
        best = [INF] * len(self.stop_ids)
        labels: list[dict[int, int]] = [{}]
        parents: list[dict[int, tuple]] = [{}]
        for stop, meters in access:
            arrive = start_s + walk_seconds(meters)
            if arrive < best[stop]:
                best[stop] = arrive
                labels[0][stop] = arrive
                parents[0][stop] = ("access", meters)
        marked = set(labels[0])
        found: list[Journey] = []
        target = INF
        for k in range(1, MAX_RIDES + 1):
            labels.append({})
            parents.append({})
            queue: dict[int, tuple[Pattern, int]] = {}
            for stop in marked:
                for pattern, position in self.stop_patterns[stop]:
                    known = queue.get(id(pattern))
                    if known is None or position < known[1]:
                        queue[id(pattern)] = (pattern, position)
            improved: set[int] = set()
            for pattern, start in queue.values():
                trip = -1
                board = -1
                for position in range(start, len(pattern.stops)):
                    stop = pattern.stops[position]
                    if trip >= 0:
                        arrive = pattern.arr[position][trip]
                        if arrive < min(best[stop], target):
                            best[stop] = arrive
                            labels[k][stop] = arrive
                            parents[k][stop] = ("ride", Ride(pattern, trip, board, position))
                            improved.add(stop)
                    ready = labels[k - 1].get(stop)
                    if ready is not None and (trip < 0 or ready + BOARD_SLACK_S <= pattern.dep[position][trip]):
                        catch = self._earliest(pattern, position, ready + BOARD_SLACK_S, active)
                        if catch >= 0 and (trip < 0 or catch < trip):
                            trip, board = catch, position
            # Walk to a nearby stop to transfer, after this round's rides.
            for stop in list(improved):
                for other, meters in self.transfers[stop]:
                    arrive = labels[k][stop] + walk_seconds(meters)
                    if arrive < min(best[other], target):
                        best[other] = arrive
                        labels[k][other] = arrive
                        parents[k][other] = ("transfer", stop, meters)
                        improved.add(other)
            marked = improved
            # The best arrival at the destination with k rides.
            arrival = min(((labels[k][stop] + walk_seconds(meters), stop) for stop, meters in egress.items() if stop in labels[k]), default=None)
            if arrival and arrival[0] < target:
                target = arrival[0]
                found.append(self._unwind(k, arrival[1], egress[arrival[1]], arrival[0], parents, day))
            if not marked:
                break
        return found

    @staticmethod
    def _earliest(pattern: Pattern, position: int, after_s: int, active: set[int]) -> int:
        """The first trip of the pattern running today that leaves this stop at after_s or later; -1 if none."""
        departures = pattern.dep[position]
        trip = bisect.bisect_left(departures, after_s)
        while trip < len(departures):
            if pattern.services[trip] in active:
                return trip
            trip += 1
        return -1

    def _unwind(self, k: int, last_stop: int, end_walk_m: float, arrive_s: int, parents: list[dict[int, tuple]], day: date) -> Journey:
        rides: list[Ride] = []
        transfers: list[tuple[int, int, float]] = []
        stop = last_stop
        while True:
            parent = parents[k][stop]
            if parent[0] == "transfer":
                transfers.append((parent[1], stop, parent[2]))
                stop = parent[1]
                continue
            if parent[0] == "access":
                return Journey(parent[1], stop, rides[::-1], transfers[::-1], last_stop, end_walk_m, arrive_s, day)
            ride: Ride = parent[1]
            rides.append(ride)
            stop = ride.pattern.stops[ride.board]
            k -= 1


class TimetableSource:
    """The schedule, fetched at most once a REFRESH_S (kept on disk) and read in the background."""

    def __init__(self, http: httpx.Client, url: str, saved: Path, *, clock=time.time) -> None:
        self._http, self._url, self._saved, self._clock = http, url, saved, clock
        self._table: Timetable | None = None
        self._lock = Lock()
        self.error: str | None = None

    @property
    def table(self) -> Timetable | None:
        return self._table

    def load(self) -> Timetable | None:
        with self._lock:
            if self._table is not None:
                return self._table
            try:
                fresh = self._saved.exists() and self._clock() - self._saved.stat().st_mtime < REFRESH_S
                if not fresh:
                    response = self._http.get(self._url, timeout=120.0, follow_redirects=True)
                    response.raise_for_status()
                    self._saved.parent.mkdir(parents=True, exist_ok=True)
                    self._saved.write_bytes(response.content)
                started = time.perf_counter()
                self._table = Timetable.from_gtfs(self._saved.read_bytes())
                logger.info(
                    "Bus schedule: %d stops, %d route patterns read in %.0f s",
                    len(self._table.stop_ids),
                    len(self._table.patterns),
                    time.perf_counter() - started,
                )
                self.error = None
            except (httpx.HTTPError, OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
                self.error = type(exc).__name__
                logger.warning("Bus schedule unavailable: %s", exc)
            return self._table


def _rows(feed: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with feed.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def _seconds(text: str) -> int | None:
    parts = text.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _seconds_of(moment: datetime) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def _date(text: str) -> date:
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _cell(point: Point) -> tuple[int, int]:
    return (math.floor(point[0] / _CELL_DEG), math.floor(point[1] / _CELL_DEG))


def _title(text: str) -> str:
    """MCTS writes names in capitals: "GREENFIELD & S101" reads "Greenfield & S101"."""
    text = " ".join(text.split())
    return " ".join(word if any(c.isdigit() for c in word) and len(word) <= 4 else word.capitalize() for word in text.split(" ")) if text.isupper() else text
