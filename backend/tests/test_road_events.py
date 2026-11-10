"""Road events: the five sources, choosing between them, and which events a route drives through."""

import itertools
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest

from app.geo import destination_point, encode_polyline, haversine_m
from app.overpass import AREAS, BBox
from app.road_events import (
    APIFY_MAX_RADIUS,
    TOMTOM_MAX_BOX_KM2,
    ApifyTraffic,
    EventSourceError,
    RoadEvent,
    TomTomFeed,
    Wi511Feed,
    WzdxFeed,
    collect,
    count_on_route,
    delay_on_route,
    MAX_SLOWDOWN,
    _ramp_road,
    _road_kind,
    jams_on_routes,
    near_routes,
    split_box,
    tiles_for,
    tomtom_events,
    weather_on_route,
    without_repeats,
)
from app.usage import DailyUsage

NOW = 1_800_000_000.0
METRO = AREAS["milwaukee-metro"]
DOWNTOWN = (43.0389, -87.9065)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def wi511_item(**fields) -> dict:
    item = {
        "ID": 1,
        "RoadwayName": "I-94",
        "DirectionOfTravel": "Westbound",
        "Description": "Right lane closed for bridge work",
        "IsFullClosure": False,
        "LanesAffected": "Right lane closed",
        "Latitude": 43.033,
        "Longitude": -87.95,
        "LatitudeSecondary": 43.033,
        "LongitudeSecondary": -87.97,
        "StartDate": NOW - 3600,
        "PlannedEndDate": NOW + 3600,
        "EncodedPolyline": None,
    }
    return {**item, **fields}


def wi511_feed(items, status: int = 200):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=items)

    clock = Clock()
    feed = Wi511Feed("secret-key", httpx.Client(transport=httpx.MockTransport(handler)), clock=clock, now=lambda: NOW)
    return feed, requests, clock


def test_wi511_turns_closures_and_lane_closures_into_events():
    items = [
        wi511_item(ID=1, IsFullClosure=True, LanesAffected="All lanes closed", DirectionOfTravel="Eastbound"),
        wi511_item(ID=2, LatitudeSecondary=None, LongitudeSecondary=None, DirectionOfTravel="Both Directions"),
        wi511_item(ID=3, LanesAffected="Shoulder closed"),  # no lane taken
        wi511_item(ID=4, StartDate=NOW + 3600),  # not started yet
        wi511_item(ID=5, PlannedEndDate=NOW - 60),  # already over
        wi511_item(ID=6, Latitude=43.07, Longitude=-89.40, LatitudeSecondary=43.07, LongitudeSecondary=-89.41),  # Madison
    ]
    feed, requests, _ = wi511_feed(items)
    closed, lanes = feed.events(METRO)

    assert (closed.kind, closed.direction, len(closed.path)) == ("closed", 90.0, 2)
    assert (lanes.kind, lanes.direction, len(lanes.path)) == ("lanes", None, 1)
    assert lanes.description.startswith("I-94: Right lane closed")
    events_request, travel_times_request = requests
    assert events_request.url.path == "/api/v2/get/event" and travel_times_request.url.path == "/api/v2/get/traveltimes"
    assert events_request.url.params["key"] == "secret-key" and events_request.url.params["format"] == "json"


def test_wi511_prefers_the_encoded_stretch():
    line = [DOWNTOWN, destination_point(DOWNTOWN, 0, 500), destination_point(DOWNTOWN, 0, 1000)]
    feed, _, _ = wi511_feed([wi511_item(EncodedPolyline=encode_polyline(line))])
    (event,) = feed.events(METRO)
    assert len(event.path) == 3


def test_wi511_is_fetched_at_most_every_five_minutes():
    feed, requests, clock = wi511_feed([wi511_item()])
    feed.events(METRO)
    clock.now = 200
    feed.events(METRO)
    assert len(requests) == 2  # events and travel times, once
    clock.now = 400
    feed.events(METRO)
    assert len(requests) == 4


def test_wi511_errors_never_show_the_key():
    feed, _, _ = wi511_feed({"error": "nope"}, status=403)
    with pytest.raises(EventSourceError, match="refused the key") as error:
        feed.events(METRO)
    assert "secret-key" not in str(error.value)


def test_google_traffic_intervals_become_jams_along_the_route():
    points = [destination_point(DOWNTOWN, 90, 200 * k) for k in range(5)]
    route = {
        "polyline": {"encodedPolyline": encode_polyline(points)},
        "travelAdvisory": {
            "speedReadingIntervals": [
                {"endPolylinePointIndex": 1, "speed": "NORMAL"},
                {"startPolylinePointIndex": 1, "endPolylinePointIndex": 3, "speed": "TRAFFIC_JAM"},
                {"startPolylinePointIndex": 3, "endPolylinePointIndex": 4, "speed": "SLOW"},
            ]
        },
    }
    jam, slow = jams_on_routes([route])
    assert (jam.kind, len(jam.path), jam.along) == ("jam", 3, True)
    assert (slow.kind, len(slow.path)) == ("slow", 2)


def test_apify_tiles_cover_the_trip_with_the_most_detail_that_fits():
    zoom, radius, _ = tiles_for(BBox(43.03, -87.92, 43.05, -87.90))  # about 2 km
    assert zoom == 15 and radius <= 1
    zoom, radius, _ = tiles_for(METRO)  # about 80 km
    assert radius <= APIFY_MAX_RADIUS and zoom <= 13


def feature(level: int, coordinates) -> dict:
    geometry = {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in coordinates]}
    return {"type": "Feature", "geometry": geometry, "properties": {"speed_level": level, "speed_label": f"level {level}"}}


def apify(payload, *, daily_tiles: int = 500, tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    usage = DailyUsage(tmp_path / "tiles.json", daily_tiles)
    source = ApifyTraffic("apify-token", httpx.Client(transport=httpx.MockTransport(handler)), usage, clock=Clock())
    return source, requests, usage


def test_apify_reads_closures_and_jams_and_counts_its_tiles(tmp_path):
    line = [DOWNTOWN, destination_point(DOWNTOWN, 90, 300)]
    collection = {"type": "FeatureCollection", "features": [feature(6, line), feature(1, line), feature(4, line)]}
    source, requests, usage = apify([collection], tmp_path=tmp_path)
    area = BBox(43.03, -87.92, 43.05, -87.90)

    closed, jam = source.events(area)
    assert (closed.kind, jam.kind) == ("closed", "jam")
    assert closed.path[0] == pytest.approx(DOWNTOWN)  # GeoJSON's [lng, lat] read the right way round
    (sent,) = requests
    assert sent.headers["Authorization"] == "Bearer apify-token" and "token" not in str(sent.url)
    body = json.loads(sent.content)
    assert usage.used_today() == (2 * body["radius"] + 1) ** 2 and body["seconds"] == -1

    source.events(area)
    assert len(requests) == 1  # cached


def test_apify_results_may_arrive_as_a_json_string(tmp_path):
    collection = {"type": "FeatureCollection", "features": [feature(6, [DOWNTOWN, destination_point(DOWNTOWN, 0, 100)])]}
    source, _, _ = apify([{"results": json.dumps(collection)}], tmp_path=tmp_path)
    assert [e.kind for e in source.events(BBox(43.03, -87.92, 43.05, -87.90))] == ["closed"]


def test_apify_stops_at_the_daily_tile_limit(tmp_path):
    source, requests, _ = apify([], daily_tiles=0, tmp_path=tmp_path)
    with pytest.raises(EventSourceError, match="ROUTES_APIFY_DAILY_TILES"):
        source.events(METRO)
    assert requests == []


class FixedSource:
    def __init__(self, events=None, error=None) -> None:
        self.result, self.error = events or [], error

    def events(self, area):
        if self.error:
            raise EventSourceError(self.error)
        return self.result


def test_the_first_source_that_answers_is_used_and_the_rest_are_backups():
    report = collect(["wi511", "google", "apify"], METRO, wzdx=None, wi511=None, apify=None, google_ready=True)
    assert (report.source, report.google, report.skipped) == ("google", True, ["wi511"])

    closure = RoadEvent("apify", "closed", (DOWNTOWN,))
    report = collect(
        ["wi511", "google", "apify"],
        METRO,
        wzdx=None,
        wi511=FixedSource(error="511WI answered 500"),
        apify=FixedSource([closure]),
        google_ready=False,
    )
    assert (report.source, report.events, report.skipped) == ("apify", [closure], ["wi511", "google"])
    assert "511WI answered 500" in report.notes[0]

    report = collect(["wi511"], METRO, wzdx=None, wi511=None, apify=None, google_ready=True)
    assert (report.source, report.events) == (None, [])


def straight_east(length_m: float = 1_000) -> list:
    return [destination_point(DOWNTOWN, 90, d) for d in range(0, int(length_m) + 1, 50)]


def test_a_route_drives_through_a_closure_on_its_road():
    route = straight_east()
    closure = RoadEvent("wi511", "closed", (route[6], route[12]))
    assert count_on_route(route, [closure]) == (1, 0, 0)


def test_a_closure_the_other_way_or_across_the_road_does_not_count():
    route = straight_east()
    westbound = RoadEvent("wi511", "lanes", (route[12], route[6]), direction=270.0)
    middle = route[10]
    cross_street = RoadEvent("wi511", "closed", (destination_point(middle, 0, 150), destination_point(middle, 180, 150)))
    assert count_on_route(route, [westbound, cross_street]) == (0, 0, 0)


def test_a_lane_closure_at_a_spot_on_the_route_counts():
    route = straight_east()
    assert count_on_route(route, [RoadEvent("wi511", "lanes", (route[5],))]) == (0, 1, 0)


# ---- WZDx


def work_zone(impact, *, lanes=(), event_type="work-zone", direction="westbound", start=-3600, end=3600, geometry=None):
    """A WZDx 4.2 feature shaped like Wisconsin's feed, active from NOW + start to NOW + end (seconds)."""
    line = geometry or {"type": "LineString", "coordinates": [[-87.95, 43.033], [-87.96, 43.033], [-87.97, 43.033]]}
    when = lambda offset: datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat()  # noqa: E731
    return {
        "type": "Feature",
        "geometry": line,
        "properties": {
            "core_details": {
                "event_type": event_type,
                "road_names": ["I-94 WB"],
                "direction": direction,
                "description": f"Mainline lanes on I-94 WB: {impact}",
            },
            "start_date": when(start),
            "end_date": when(end),
            "vehicle_impact": impact,
            "lanes": [{"order": n, "type": kind, "status": status} for n, (kind, status) in enumerate(lanes, 1)],
        },
    }


class WzdxServer:
    """MockTransport handler serving a WZDx feed; set status to make it fail."""

    def __init__(self, features) -> None:
        self.features, self.status, self.requests = features, 200, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        feed = {"feed_info": {"version": "4.2"}, "features": self.features, "type": "FeatureCollection"}
        return httpx.Response(self.status, json=feed)


def wzdx_feed(features):
    server, clock = WzdxServer(features), Clock()
    http = httpx.Client(transport=httpx.MockTransport(server))
    feed = WzdxFeed(http, url="https://511wi.gov/api/wzdx", clock=clock, now=lambda: datetime.fromtimestamp(NOW, timezone.utc))
    return feed, server, clock


def test_wzdx_work_zones_become_closures_and_lane_closures():
    two_ends = {"type": "MultiPoint", "coordinates": [[-87.95, 43.04], [-87.97, 43.04]]}
    features = [
        work_zone("all-lanes-closed"),
        work_zone("some-lanes-closed", lanes=[("shoulder", "closed"), ("general", "closed"), ("general", "open")]),
        work_zone("some-lanes-closed", lanes=[("shoulder", "closed"), ("general", "open")]),  # shoulder only
        work_zone("flagging", direction="unknown", geometry=two_ends),
        work_zone(None, event_type="detour"),  # a suggested detour, not a closure
        work_zone("some-lanes-closed", start=3600),  # starts later
    ]
    feed, server, _ = wzdx_feed(features)
    closed, lanes, flagging = feed.events(METRO)
    assert (closed.kind, closed.direction, closed.category, len(closed.path)) == ("closed", 270.0, "roadwork", 3)
    assert closed.path[0] == pytest.approx((43.033, -87.95))  # GeoJSON's [lng, lat] read the right way round
    assert lanes.kind == "lanes" and lanes.description.startswith("Mainline lanes on I-94 WB")
    assert (flagging.kind, flagging.direction, len(flagging.path)) == ("lanes", None, 2)
    assert len(server.requests) == 1


def test_wzdx_keeps_its_last_copy_for_a_while_when_the_feed_fails():
    feed, server, clock = wzdx_feed([work_zone("all-lanes-closed")])
    assert len(feed.events(METRO)) == 1
    server.status, clock.now = 503, 600  # time to refresh, and the feed fails
    assert len(feed.events(METRO)) == 1  # the copy from 10 minutes ago
    clock.now = 4000  # over an hour old: no longer trusted
    with pytest.raises(EventSourceError, match="503"):
        feed.events(METRO)


def test_wzdx_can_read_a_saved_copy_of_the_feed(tmp_path):
    saved = tmp_path / "wzdx.json"
    saved.write_text(json.dumps({"features": [work_zone("all-lanes-closed")]}), encoding="utf-8")
    feed = WzdxFeed(httpx.Client(), file=saved, now=lambda: datetime.fromtimestamp(NOW, timezone.utc))
    assert [event.kind for event in feed.events(METRO)] == ["closed"]


def test_work_zones_and_511_together_without_counting_a_work_zone_twice():
    work = RoadEvent("wzdx", "lanes", (DOWNTOWN,), category="roadwork")
    same_work_from_511 = RoadEvent("wi511", "lanes", (DOWNTOWN,), category="roadwork")
    crash = RoadEvent("wi511", "lanes", (DOWNTOWN,), category="incident")
    report = collect(
        ["wzdx+wi511", "google"],
        METRO,
        wzdx=FixedSource([work]),
        wi511=FixedSource([same_work_from_511, crash]),
        apify=None,
        google_ready=True,
    )
    assert (report.source, report.events, report.google) == ("wzdx+wi511", [work, crash], False)

    # Without a 511 key the group still answers with work zones alone.
    report = collect(["wzdx+wi511", "google"], METRO, wzdx=FixedSource([work]), wi511=None, apify=None, google_ready=True)
    assert (report.source, report.events, report.skipped) == ("wzdx", [work], ["wi511"])


@pytest.mark.parametrize(
    ("description", "road", "ramp_road"),
    [
        ("Ramp Full Closure on I-94 EB at OFF RAMP TO HAWLEY RD. Detour: EB I-94", "off-ramp", "hawley"),
        ("Ramp Full Closure on I-94 EB at ON RAMP FROM S 68TH ST. Detour: EB W.", "on-ramp", "68th"),
        ("Ramp Full Closure on I-894 EB at OFF RAMP TO COUNTY ES. Detour: Use I-41", "off-ramp", "county es"),
        ("Mainline Left Lane Closed on I-94 WB from 35TH ST to 27TH ST", "mainline", ""),
        ("Local Road Full Closure on S. 16th Street NB from Viaduct to ramp", "any", ""),
    ],
)
def test_descriptions_say_which_roads_an_event_is_on(description, road, ramp_road):
    assert (_road_kind(description), _ramp_road(description)) == (road, ramp_road)


def test_wzdx_reads_workers_on_site_and_reduced_speed_limits():
    zone = work_zone("some-lanes-closed", lanes=[("general", "closed"), ("general", "open")])
    zone["properties"]["worker_presence"] = {"are_workers_present": True, "method": "arrow-board-present"}
    zone["properties"]["reduced_speed_limit_kph"] = 88.495
    feed, _, _ = wzdx_feed([zone])
    (event,) = feed.events(METRO)
    assert event.workers and event.speed_limit_kmh == pytest.approx(88.495)
    assert event.description.endswith("(speed limit 55 mph, workers present)")


def test_a_lane_closure_with_workers_counts_once_and_is_flagged():
    route = straight_east()
    crew = RoadEvent("wzdx", "lanes", (route[5],), workers=True)
    assert count_on_route(route, [crew]) == (0, 1, 1)


def test_driving_past_a_closed_ramp_is_not_driving_through_it():
    freeway = straight_east()
    exit_ramp = tuple(destination_point(freeway[6], 110, metres) for metres in range(0, 301, 50))
    closed = RoadEvent("wzdx", "closed", exit_ramp, along=True, road="off-ramp")
    assert count_on_route(freeway, [closed]) == (0, 0, 0)  # stays on the freeway beside it
    taking_the_exit = freeway[:7] + list(exit_ramp[1:])
    assert count_on_route(taking_the_exit, [closed]) == (1, 0, 0)


def test_511_roadwork_closures_are_known_as_roadwork_and_by_their_road():
    item = wi511_item(
        EventType="closures",
        IsFullClosure=True,
        Description="Roadwork - System Interchange Full Closure on WIS 175 SB at WIS 175 SB TO I-94 WB (N-W STADIUM)",
    )
    feed, _, _ = wi511_feed([item, wi511_item(ID=2, EventType="accidentsAndIncidents", Description="Crash blocking right lane")])
    roadwork, crash = feed.events(METRO)
    assert (roadwork.category, roadwork.road, roadwork.ramp_toward) == ("roadwork", "off-ramp", 270.0)
    assert crash.category == "incident"


def travel_time(normal: float, current: float, **fields) -> dict:
    """A 511WI travel time segment heading east along a 2 km stretch of road downtown."""
    waypoints = [destination_point(DOWNTOWN, 90, metres) for metres in (0, 700, 1400, 2000)]
    item = {
        "Id": "STOC-Milwaukee::1",
        "RoadwayName": "I-94 ",
        "Description": "I-94 EB Hawley Rd to 35th St",
        "Distance": 1.2,
        "NormalTime": normal,
        "CurrentTime": current,
        "Delay": current - normal,
        "Waypoints": [{"RoadwayName": "I-94 East", "Direction": "E", "Latitude": lat, "Longitude": lng} for lat, lng in waypoints],
    }
    return {**item, **fields}


def test_wi511_travel_times_become_slowdowns_without_google():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("traveltimes"):
            return httpx.Response(200, json=[travel_time(4, 4), travel_time(4, 6), travel_time(4, 12), travel_time(2, 30)])
        return httpx.Response(200, json=[])

    feed = Wi511Feed("secret-key", httpx.Client(transport=httpx.MockTransport(handler)), clock=Clock(), now=lambda: NOW)
    slow, jam, extreme = feed.events(METRO)  # free flow (4 -> 4 min) is left out
    assert (slow.kind, slow.slowdown, slow.delay_min, slow.along, len(slow.path)) == ("slow", 1.5, 2, True, 4)
    assert (jam.kind, jam.slowdown, jam.category) == ("jam", 3.0, "traffic")
    assert extreme.slowdown == MAX_SLOWDOWN and extreme.delay_min == 2 * (MAX_SLOWDOWN - 1)  # capped
    assert jam.description == "I-94 EB Hawley Rd to 35th St: 12 min now, usually 4"


def test_a_route_picks_up_the_delay_of_the_slow_stretch_it_drives():
    route = straight_east(2_000)
    jam = RoadEvent("wi511", "jam", tuple(route), along=True, slowdown=3.0, delay_min=8.0)
    assert delay_on_route(route, [jam]) == pytest.approx(8.0, abs=0.5)
    half = route[: len(route) // 2 + 1]
    assert delay_on_route(half, [jam]) == pytest.approx(4.0, abs=0.8)
    assert delay_on_route(list(reversed(route)), [jam]) == 0.0  # the other direction flows freely


# ---- TomTom

_incident_ids = itertools.count(1)


def tomtom_incident(category: int, line, *, events=None, magnitude: int = 0, delay=None, **properties) -> dict:
    """A TomTom Incident Details incident along line (one direction of traffic), shaped like Milwaukee's answers."""
    names = {1: "Accident", 2: "Fog", 6: "Stopped traffic", 7: "Lane closed", 8: "Closed", 9: "Roadworks"}
    if events is None:
        events = [{"description": names[category], "code": 0, "iconCategory": category}]
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in line]},
        "properties": {
            "id": f"tt-{next(_incident_ids)}",
            "iconCategory": category,
            "magnitudeOfDelay": magnitude,
            "events": events,
            "from": "W Wells St",
            "to": "W Michigan St",
            "delay": delay,
            "roadNumbers": [],
            **properties,
        },
    }


def detail(description: str, category: int) -> dict:
    return {"description": description, "code": 0, "iconCategory": category}


def test_tomtom_incidents_become_closures_lane_closures_and_jams():
    line = [DOWNTOWN, destination_point(DOWNTOWN, 0, 300)]
    incidents = [
        tomtom_incident(8, line, events=[detail("Closed", 8), detail("Parade", 0)]),
        tomtom_incident(7, line, events=[detail("Lane closed", 7), detail("Hard shoulder closed", 7)], roadNumbers=["WI-60"]),
        tomtom_incident(7, line, events=[detail("Hard shoulder closed", 7)]),  # no travel lane taken
        tomtom_incident(9, line, events=[detail("Roadworks", 9), detail("Traffic pattern changes", 9)]),  # beside the road
        tomtom_incident(1, line, events=[detail("Accident", 1), detail("Broken down vehicle", 14)], roadNumbers=["I-94 W"]),
        tomtom_incident(6, line, magnitude=3, delay=600),
        tomtom_incident(6, line, magnitude=1, delay=45, events=[detail("Slow traffic", 6)]),
        tomtom_incident(2, line),  # fog
    ]
    closed, lanes, crash, jam, slow, fog = tomtom_events(incidents)
    assert (fog.kind, fog.category, fog.description) == ("hazard", "weather", "Fog (W Wells St to W Michigan St)")
    assert (closed.kind, closed.category, closed.along, closed.source) == ("closed", "closure", True, "tomtom")
    assert closed.path[0] == pytest.approx(DOWNTOWN)  # GeoJSON's [lng, lat] read the right way round
    assert closed.description == "Closed, Parade (W Wells St to W Michigan St)"
    assert lanes.kind == "lanes" and lanes.description.startswith("WI-60: Lane closed, Hard shoulder closed")
    assert (crash.kind, crash.category) == ("lanes", "incident")  # a crash scene counts as a lane closure
    assert (jam.kind, jam.category, jam.delay_min, jam.slowdown) == ("jam", "traffic", 10.0, None)
    assert jam.description.endswith(", 10 min delay")
    assert (slow.kind, slow.delay_min) == ("slow", 0.75)


def test_a_closure_listed_once_per_direction_becomes_one_for_both():
    north = [DOWNTOWN, destination_point(DOWNTOWN, 0, 150), destination_point(DOWNTOWN, 0, 300)]
    south = [destination_point(point, 90, 10) for point in reversed(north)]  # its own line, drawn a little apart
    one_way = [destination_point(DOWNTOWN, 90, 1_000), destination_point(DOWNTOWN, 90, 1_300)]
    both, alone = tomtom_events([tomtom_incident(8, north), tomtom_incident(8, south), tomtom_incident(8, one_way)])
    assert (both.along, both.direction, len(both.path)) == (False, None, 3)
    assert both.description == "Closed (W Wells St to W Michigan St, both directions)"
    assert alone.along


def tomtom_feed(incidents, tmp_path, *, status: int = 200, daily: int = 80):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"incidents": incidents})

    clock, usage = Clock(), DailyUsage(tmp_path / "tomtom.json", daily)
    feed = TomTomFeed("tt-secret", httpx.Client(transport=httpx.MockTransport(handler)), usage, box=METRO, clock=clock)
    return feed, requests, clock, usage


def test_tomtom_is_asked_about_the_whole_map_at_most_every_ten_minutes(tmp_path):
    line = [DOWNTOWN, destination_point(DOWNTOWN, 0, 300)]
    feed, requests, clock, usage = tomtom_feed([tomtom_incident(8, line)], tmp_path)
    assert len(feed.events(BBox(43.03, -87.92, 43.05, -87.90))) == 1
    assert feed.events(BBox(42.90, -88.10, 42.95, -88.00)) == []  # another trip, answered from the same copy
    (sent,) = requests
    assert sent.url.params["bbox"] == f"{METRO.west},{METRO.south},{METRO.east},{METRO.north}"
    assert sent.url.params["key"] == "tt-secret" and usage.used_today() == 1
    clock.now = 601
    feed.events(METRO)
    assert len(requests) == 2


def test_trips_planned_at_once_share_one_request_to_tomtom(tmp_path):
    line = [DOWNTOWN, destination_point(DOWNTOWN, 0, 300)]
    requests: list[httpx.Request] = []

    def slow(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        time.sleep(0.2)  # long enough for every trip to arrive while the first one's request is out
        return httpx.Response(200, json={"incidents": [tomtom_incident(8, line)]})

    usage = DailyUsage(tmp_path / "tomtom.json", 80)
    feed = TomTomFeed("tt-secret", httpx.Client(transport=httpx.MockTransport(slow)), usage, box=METRO, clock=Clock())
    with ThreadPoolExecutor(6) as trips:
        answers = list(trips.map(lambda _: len(feed.events(METRO)), range(6)))
    assert answers == [1] * 6 and len(requests) == 1 and usage.used_today() == 1


def test_a_restarted_server_reuses_tomtoms_answer_for_ten_minutes(tmp_path):
    line = [DOWNTOWN, destination_point(DOWNTOWN, 0, 300)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"incidents": [tomtom_incident(8, line)]})

    http, wall = httpx.Client(transport=httpx.MockTransport(handler)), Clock()

    def started_server() -> TomTomFeed:  # each with its own clock, as a new process would have
        usage = DailyUsage(tmp_path / "usage.json", 80)
        return TomTomFeed("tt-secret", http, usage, box=METRO, saved=tmp_path / "tomtom.json", clock=Clock(), now=wall)

    wall.now = NOW
    assert len(started_server().events(METRO)) == 1
    wall.now += 300  # restarted five minutes later
    assert len(started_server().events(METRO)) == 1 and len(requests) == 1
    wall.now += 400  # restarted again, eleven minutes after the answer
    started_server().events(METRO)
    assert len(requests) == 2
    assert "tt-secret" not in (tmp_path / "tomtom.json").read_text(encoding="utf-8")


def test_tomtom_keeps_to_its_daily_limit_and_never_shows_the_key(tmp_path):
    feed, requests, _, _ = tomtom_feed([], tmp_path, daily=0)
    with pytest.raises(EventSourceError, match="ROUTES_TOMTOM_DAILY_REQUESTS"):
        feed.events(METRO)
    assert requests == []
    feed, _, _, _ = tomtom_feed([], tmp_path / "refused", status=403)
    with pytest.raises(EventSourceError, match="refused the key") as error:
        feed.events(METRO)
    assert "tt-secret" not in str(error.value)


def area_km2(box: BBox) -> float:
    height = haversine_m((box.south, box.west), (box.north, box.west)) / 1000
    return height * haversine_m((box.south, box.west), (box.south, box.east)) / 1000  # the wider, southern edge


def test_a_box_too_big_for_one_request_is_split():
    (whole,) = split_box(METRO, TOMTOM_MAX_BOX_KM2)  # the metro, about 80 by 65 km, takes one request
    assert whole == METRO
    wisconsin = BBox(42.5, -92.9, 47.1, -86.8)
    parts = split_box(wisconsin, TOMTOM_MAX_BOX_KM2)
    assert len(parts) > 1 and all(area_km2(part) <= TOMTOM_MAX_BOX_KM2 for part in parts)
    assert (parts[0].south, parts[0].west) == (wisconsin.south, wisconsin.west)
    assert (parts[-1].north, parts[-1].east) == pytest.approx((wisconsin.north, wisconsin.east))


def test_tomtom_jams_replace_511_travel_times():
    stretch = (DOWNTOWN, destination_point(DOWNTOWN, 90, 500))
    jam_511 = RoadEvent("wi511", "jam", stretch, along=True, category="traffic", slowdown=3.0)
    crash_511 = RoadEvent("wi511", "lanes", (DOWNTOWN,), category="incident")
    jam = RoadEvent("tomtom", "jam", stretch, along=True, category="traffic", delay_min=4.0)
    report = collect(
        ["wzdx+wi511+tomtom"],
        METRO,
        wzdx=None,
        wi511=FixedSource([jam_511, crash_511]),
        tomtom=FixedSource([jam]),
        apify=None,
        google_ready=False,
    )
    assert (report.source, report.events, report.skipped) == ("wi511+tomtom", [crash_511, jam], ["wzdx"])


def test_closures_an_official_feed_reports_are_not_counted_again_from_tomtom():
    east = tuple(straight_east(600))
    north_of = tuple(destination_point(point, 0, 500) for point in east)  # a parallel street
    official = RoadEvent("wzdx", "closed", east, along=True)
    same = RoadEvent("tomtom", "closed", east[2:10], along=True)
    lanes_in_it = RoadEvent("tomtom", "lanes", east, along=True)
    other_way = RoadEvent("tomtom", "closed", east[::-1], along=True)
    official_lanes = RoadEvent("wi511", "lanes", north_of, along=True)
    closed_there = RoadEvent("tomtom", "closed", north_of, along=True)  # more than the official lane closure says
    kept = without_repeats([official, same, lanes_in_it, other_way, official_lanes, closed_there])
    assert kept == [official, other_way, official_lanes, closed_there]


def test_a_route_through_fog_counts_one_weather_hazard_the_way_it_drives():
    route = straight_east(1_000)
    fog = RoadEvent("tomtom", "hazard", tuple(route[4:12]), along=True, category="weather")
    assert weather_on_route(route, [fog]) == 1
    assert weather_on_route(list(reversed(route)), [fog]) == 0  # the other carriageway


def test_closures_are_drawn_when_they_are_near_a_route():
    route = straight_east(2_000)
    beside = RoadEvent("tomtom", "closed", (destination_point(route[10], 0, 150), destination_point(route[12], 0, 150)))
    far = RoadEvent("tomtom", "closed", (destination_point(route[10], 0, 3_000),))
    assert near_routes([beside, far], [route], 300) == [beside]
