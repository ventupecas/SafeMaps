"""Bus trips from a GTFS schedule (app.timetable) and how they're explained (app.transit_plan)."""

import io
import os
import zipfile
from datetime import datetime

import httpx
import pytest

from app import transit_plan
from app.geo import Point, destination_point
from app.models import LatLng
from app.timetable import REFRESH_S, Timetable, TimetableSource, _title
from tests.test_transit import START, URL, table

TUESDAY = datetime(2026, 11, 10, 10, 0)
DAYLIGHT = (180.0, 30.0)
NIGHT = (0.0, -20.0)


def clock(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:00"


def feed(start: Point = START) -> bytes:
    """The GreenLine runs east from start, stops S0-S3 a kilometer apart, every 15 minutes from 6:00 to
    23:00 and once at 1:10 after midnight, three minutes from stop to stop. Route 15 runs north from S4,
    100 m north of S2, to S5, 1.5 km on, every 20 minutes from 6:05. Weekdays in November and December
    (not Thanksgiving), and Saturday the 7th."""
    east = [destination_point(start, 90, 1000 * i) for i in range(4)]
    s4 = destination_point(east[2], 0, 100)
    points = [*east, s4, destination_point(s4, 0, 1500)]
    stops = [{"stop_id": f"S{i}", "stop_name": f"STOP {i} & MAIN", "stop_lat": lat, "stop_lon": lng, "location_type": "0"} for i, (lat, lng) in enumerate(points)]
    stops.append({"stop_id": "ST", "stop_name": "STATION", "stop_lat": START[0], "stop_lon": START[1], "location_type": "1"})
    routes = [
        {"route_id": "GRE", "route_short_name": "GRE", "route_long_name": "GreenLine", "route_color": "00A650"},
        {"route_id": "15", "route_short_name": "15", "route_long_name": "Holton - Kinnickinnic", "route_color": ""},
    ]
    trips, times = [], []
    green = [6 * 3600 + 900 * n for n in range(69)] + [25 * 3600 + 600]
    for n, first in enumerate(green):
        for service in ("wk", "sat"):
            trip = f"gre-{service}-{n}"
            trips.append({"route_id": "GRE", "service_id": service, "trip_id": trip, "trip_headsign": "BAY VIEW"})
            times += [{"trip_id": trip, "stop_id": f"S{i}", "stop_sequence": i + 1, "arrival_time": clock(first + 180 * i), "departure_time": clock(first + 180 * i)} for i in range(4)]
    for n in range(50):
        first = 6 * 3600 + 300 + 1200 * n
        trip = f"15-{n}"
        trips.append({"route_id": "15", "service_id": "wk", "trip_id": trip, "trip_headsign": "Holton"})
        times += [{"trip_id": trip, "stop_id": stop, "stop_sequence": i + 1, "arrival_time": clock(first + 240 * i), "departure_time": clock(first + 240 * i)} for i, stop in enumerate(("S4", "S5"))]
    calendar = [{"service_id": "wk", **{day: "1" if day not in ("saturday", "sunday") else "0" for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}, "start_date": "20261101", "end_date": "20261231"}]
    dates = [{"service_id": "sat", "date": "20261107", "exception_type": "1"}, {"service_id": "wk", "date": "20261126", "exception_type": "2"}]
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as gtfs:
        gtfs.writestr("stops.txt", table(stops))
        gtfs.writestr("routes.txt", table(routes))
        gtfs.writestr("trips.txt", table(trips))
        gtfs.writestr("stop_times.txt", table(times))
        gtfs.writestr("calendar.txt", table(calendar))
        gtfs.writestr("calendar_dates.txt", table(dates))
    return data.getvalue()


@pytest.fixture(scope="module")
def schedule() -> Timetable:
    return Timetable.from_gtfs(feed())


def stop(schedule: Timetable, stop_id: str) -> Point:
    return schedule.stop_points[schedule.stop_ids.index(stop_id)]


def near(schedule: Timetable, stop_id: str, bearing: float, meters: float) -> Point:
    return destination_point(stop(schedule, stop_id), bearing, meters)


def test_the_schedule_reads_stops_routes_and_service_days(schedule):
    assert "ST" not in schedule.stop_ids  # a station isn't where riders board
    assert schedule.stop_names[0] == "Stop 0 & Main"
    assert schedule.routes == [("GRE", "GreenLine", "#00A650"), ("15", "Holton - Kinnickinnic", "#1a73e8")]
    assert len(schedule.patterns) == 2 and schedule.patterns[0].headsign == "Bay View"
    assert {schedule.service_names[s] for s in schedule.dates[TUESDAY.date()]} == {"wk"}
    assert {schedule.service_names[s] for s in schedule.dates[datetime(2026, 11, 7).date()]} == {"sat"}
    assert not schedule.dates.get(datetime(2026, 11, 26).date())  # no buses on Thanksgiving
    assert _title("GREENFIELD & S101") == "Greenfield & S101" and _title("Holton") == "Holton"


def test_one_bus_there(schedule):
    (journey,) = schedule.journeys(near(schedule, "S0", 270, 200), near(schedule, "S3", 90, 100), TUESDAY)
    (ride,) = journey.rides
    assert schedule.stop_ids[journey.first_stop] == "S0" and schedule.stop_ids[journey.last_stop] == "S3"
    # About three minutes' walk and a minute at the stop: the 10:00 bus has gone, the 10:15 it is.
    assert journey.board_s == 10 * 3600 + 900
    assert ride.pattern.arr[ride.alight][ride.trip] == 10 * 3600 + 900 + 540


def test_a_transfer_to_another_route(schedule):
    (journey,) = schedule.journeys(near(schedule, "S0", 270, 200), near(schedule, "S5", 0, 50), TUESDAY)
    assert [schedule.routes[ride.pattern.route][0] for ride in journey.rides] == ["GRE", "15"]
    assert [(schedule.stop_ids[a], schedule.stop_ids[b]) for a, b, _ in journey.transfers] == [("S2", "S4")]
    second = journey.rides[1]
    assert second.pattern.dep[second.board][second.trip] == 10 * 3600 + 25 * 60  # 10:21 off the GreenLine, 10:25 on the 15


def test_no_buses_no_trip(schedule):
    origin, destination = near(schedule, "S0", 270, 200), near(schedule, "S3", 90, 100)
    assert schedule.journeys(origin, destination, datetime(2026, 11, 8, 10, 0)) == []  # a Sunday
    assert schedule.journeys(origin, destination, datetime(2026, 11, 26, 10, 0)) == []  # Thanksgiving
    assert schedule.journeys(origin, destination, datetime(2026, 11, 7, 10, 0))  # that Saturday's buses


def test_after_midnight_the_last_bus_of_the_day_before(schedule):
    (journey,) = schedule.journeys(near(schedule, "S0", 270, 200), near(schedule, "S3", 90, 100), datetime(2026, 11, 11, 1, 0))
    assert journey.service_day == TUESDAY.date() and journey.board_s == 25 * 3600 + 600


def test_a_longer_walk_only_where_no_bus_is_near(schedule):
    destination = near(schedule, "S3", 90, 100)
    assert schedule.journeys(near(schedule, "S0", 270, 1200), destination, TUESDAY)
    assert schedule.journeys(near(schedule, "S0", 270, 2000), destination, TUESDAY) == []


def test_a_planned_trip_has_its_walks_rides_and_times(schedule):
    origin, destination = near(schedule, "S0", 270, 200), near(schedule, "S5", 0, 50)
    trip = transit_plan.plan(schedule, None, LatLng(lat=origin[0], lng=origin[1]), LatLng(lat=destination[0], lng=destination[1]), TUESDAY, lambda moment, at: DAYLIGHT)
    winner = trip["winner"]
    assert [leg["kind"] for leg in winner["legs"]] == ["walk", "ride", "walk", "ride", "walk"]
    green = winner["legs"][1]
    assert green["route"] == {"name": "GRE", "long_name": "GreenLine", "color": "#00A650"}
    assert (green["board"]["name"], green["board"]["time"], green["alight"]["name"]) == ("Stop 0 & Main", "2026-11-10T10:15", "Stop 2 & Main")
    assert winner["legs"][2]["transfer"] and winner["transfers"] == 1
    assert winner["leave_at"] == "2026-11-10T10:10" and winner["arrive_at"] == "2026-11-10T10:29"
    assert winner["safety_risks"] == {"transfers": 1.0} and winner["safety_score"] == 91
    assert winner["reason"] == "safest, and arrives earliest"


def test_waiting_in_the_dark_counts(schedule):
    # Riders leave to be at the first stop a minute ahead: the waits are mostly at transfers.
    origin, destination = near(schedule, "S0", 270, 200), near(schedule, "S5", 0, 50)
    args = (schedule, None, LatLng(lat=origin[0], lng=origin[1]), LatLng(lat=destination[0], lng=destination[1]), TUESDAY)
    by_day = transit_plan.plan(*args, lambda moment, at: DAYLIGHT)["winner"]
    by_night = transit_plan.plan(*args, lambda moment, at: NIGHT)["winner"]
    assert "dark_wait_minutes" not in by_day["safety_risks"] and by_day["wait_min"] > 3
    assert by_night["safety_risks"]["dark_wait_minutes"] == pytest.approx(by_night["wait_min"] * transit_plan.DARK_WAIT_POINTS, abs=0.01)
    assert by_night["safety_score"] < by_day["safety_score"]


def test_no_bus_says_so(schedule):
    origin, destination = near(schedule, "S0", 270, 200), near(schedule, "S3", 90, 100)
    with pytest.raises(LookupError, match="No bus"):
        transit_plan.plan(schedule, None, LatLng(lat=origin[0], lng=origin[1]), LatLng(lat=destination[0], lng=destination[1]), datetime(2026, 11, 8, 10), lambda m, a: DAYLIGHT)


def test_the_schedule_is_fetched_weekly_and_kept_on_disk(tmp_path):
    requests = []

    def mcts(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=feed())

    saved = tmp_path / "transit-feed.zip"
    http = httpx.Client(transport=httpx.MockTransport(mcts))
    assert TimetableSource(http, URL, saved).load() is not None and len(requests) == 1
    assert TimetableSource(http, URL, saved).load() is not None and len(requests) == 1  # read back from disk
    os.utime(saved, (saved.stat().st_mtime - REFRESH_S - 60,) * 2)
    assert TimetableSource(http, URL, saved).load() is not None and len(requests) == 2  # a week old: fetched again

    down = TimetableSource(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))), URL, tmp_path / "none.zip")
    assert down.load() is None and down.error == "HTTPStatusError"
