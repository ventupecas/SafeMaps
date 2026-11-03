"""Many visitors at once, before the expo: can the server keep up?

Run from backend/ against a running server:

    python -m app.loadtest                      # 30 visitors, 3 trips each, at http://127.0.0.1:3143
    python -m app.loadtest --users 50 --trips 2 --url https://<the tunnel>.trycloudflare.com

Each visitor loads the page and the server's settings, then plans trips one after another
between well-known Milwaukee places, as phones at the booth would, all visitors at the same
time. Trips are planned without live traffic and marked as not counted, so they cost no paid
requests and stay out of the stats screen. With a shared demo's access code, pass --code.

It prints how long trips took (median, 95th percentile, slowest), any errors, and how much
of today's TomTom allowance the run used (from /health).
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

PLACES = [
    (43.0451, -87.9172),  # Fiserv Forum
    (42.9978, -87.8954),  # Bay View
    (43.028, -87.9712),  # American Family Field
    (43.118, -87.91),  # Bayshore
    (43.0546, -87.9055),  # Lakefront Brewery
    (43.0255, -87.9455),  # Mitchell Park Domes
    (43.0605, -88.048),  # Mayfair Mall
    (43.0225, -88.013),  # State Fair Park
    (43.089, -87.887),  # Shorewood
    (42.946, -88.01),  # Southridge Mall
    (43.0766, -87.8814),  # UW-Milwaukee
    (42.9476, -87.8966),  # Mitchell Airport
]


@dataclass
class Results:
    trip_seconds: list[float] = field(default_factory=list)
    page_seconds: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def visitor(client: httpx.Client, url: str, trips: int, code: str | None, results: Results, seed: int) -> None:
    chooser = random.Random(seed)
    headers = {"X-Access-Code": code} if code else {}
    started = time.perf_counter()
    try:
        for path in ("/", "/app.js", "/app-config", "/profiles"):
            client.get(url + path).raise_for_status()
        results.page_seconds.append(time.perf_counter() - started)
    except httpx.HTTPError as exc:
        results.errors.append(f"page: {exc}")
        return
    for _ in range(trips):
        origin, destination = chooser.sample(PLACES, 2)
        trip = {
            "origin": {"lat": origin[0], "lng": origin[1]},
            "destination": {"lat": destination[0], "lng": destination[1]},
            "live_traffic": False,
            "counted": False,
        }
        started = time.perf_counter()
        try:
            response = client.post(url + "/route", json=trip, headers=headers)
            if response.status_code != 200:
                results.errors.append(f"route: {response.status_code} {response.text[:120]}")
                continue
            results.trip_seconds.append(time.perf_counter() - started)
        except httpx.HTTPError as exc:
            results.errors.append(f"route: {type(exc).__name__} {exc}")


def tomtom_used(client: httpx.Client, url: str) -> tuple[int, int] | None:
    try:
        events = client.get(url + "/health").json()["road_events"]
        return events.get("tomtom_requests_today", 0), events.get("tomtom_tiles_today", 0)
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:3143", help="the server, as visitors reach it")
    parser.add_argument("--users", type=int, default=30, help="visitors at the same time")
    parser.add_argument("--trips", type=int, default=3, help="trips each visitor plans, one after another")
    parser.add_argument("--code", help="the shared demo's access code (ROUTES_ACCESS_CODE), if it has one")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds before a request counts as failed")
    args = parser.parse_args(argv)
    url = args.url.rstrip("/")

    results = Results()
    with httpx.Client(timeout=args.timeout) as client:
        before = tomtom_used(client, url)
        print(f"{args.users} visitors planning {args.trips} trips each at {url} ...", flush=True)
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.users) as pool:
            for n in range(args.users):
                pool.submit(visitor, client, url, args.trips, args.code, results, n)
        elapsed = time.perf_counter() - started
        after = tomtom_used(client, url)

    done = results.trip_seconds
    print(f"\nTrips planned: {len(done)} of {args.users * args.trips} in {elapsed:.0f} s")
    if done:
        ordered = sorted(done)
        p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
        print(f"Time per trip: median {statistics.median(done):.1f} s, 95% within {p95:.1f} s, slowest {ordered[-1]:.1f} s")
    if results.page_seconds:
        print(f"Page load (files and settings): median {statistics.median(results.page_seconds):.2f} s")
    if before and after:
        print(f"TomTom used by this run: {after[0] - before[0]} traffic requests, {after[1] - before[1]} map squares")
    if results.errors:
        print(f"\nErrors: {len(results.errors)}")
        for error in sorted(set(results.errors))[:10]:
            print("  ", error)
    return 1 if results.errors else 0


if __name__ == "__main__":
    sys.exit(main())
