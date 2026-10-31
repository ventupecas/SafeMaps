"""Run a set of Milwaukee trips through the engine and summarize what it changes.

Run from the backend/ directory:

    python -m app.evaluate
    python -m app.evaluate --live-traffic

Each trip goes through the same API the web map uses (same profile, road
events, school hours), and the recommended route is compared with the
standard route (Google's own choice, or with the OpenStreetMap map our
fastest route): risky turns, lane closures, school zones, railroad
crossings, fuel and time. Writes RESULTS.md at the repo root,
data/results.csv, and frontend/results.json (the headline numbers for the
web map's "How it works").

Cost: on the OpenStreetMap map (ROUTES_MAP=osm), nothing from Google. On
Google's map, without --live-traffic, one Essentials request per trip for
Google's suggestions (free on a rerun: answers are cached); with it, up to
about 7 live-traffic requests per trip.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

from app.config import BACKEND_DIR, Settings
from app.main import create_app

LITERS_PER_GALLON = 3.78541
KM_TO_MI = 0.621371

PLACES = {
    "Downtown": (43.0416, -87.9095),
    "Fiserv Forum": (43.0451, -87.9172),
    "Third Ward": (43.0333, -87.9060),
    "Marquette University": (43.0383, -87.9286),
    "UW-Milwaukee": (43.0766, -87.8814),
    "American Family Field": (43.0280, -87.9712),
    "Milwaukee County Zoo": (43.0320, -88.0405),
    "State Fair Park": (43.0218, -88.0156),
    "Wauwatosa Village": (43.0494, -88.0076),
    "Mayfair Mall": (43.0606, -88.0466),
    "Brookfield Square": (43.0330, -88.1060),
    "Waukesha": (43.0117, -88.2315),
    "Menomonee Falls": (43.1789, -88.1173),
    "Bayshore": (43.1196, -87.9128),
    "Bay View": (42.9978, -87.8954),
    "Mitchell Airport": (42.9476, -87.8966),
    "Southridge Mall": (42.9478, -88.0128),
    "Oak Creek": (42.8859, -87.8631),
}

TRIPS = [
    ("Downtown", "Mitchell Airport"),
    ("Downtown", "Waukesha"),
    ("Downtown", "Mayfair Mall"),
    ("Third Ward", "UW-Milwaukee"),
    ("Bay View", "Wauwatosa Village"),
    ("Fiserv Forum", "American Family Field"),
    ("Marquette University", "Milwaukee County Zoo"),
    ("UW-Milwaukee", "Southridge Mall"),
    ("Bayshore", "Downtown"),
    ("Menomonee Falls", "Downtown"),
    ("Oak Creek", "Fiserv Forum"),
    ("Brookfield Square", "Third Ward"),
    ("Mitchell Airport", "Brookfield Square"),
    ("State Fair Park", "Bayshore"),
    ("Southridge Mall", "Marquette University"),
    ("Wauwatosa Village", "Bay View"),
    ("Mayfair Mall", "Oak Creek"),
    ("American Family Field", "UW-Milwaukee"),
    ("Waukesha", "Mitchell Airport"),
    ("Milwaukee County Zoo", "Menomonee Falls"),
]


@dataclass
class Outcome:
    trip: str
    recommended: str
    standard_found: bool
    differs: bool
    numbers: dict[str, tuple[float, float]]  # measure -> (standard, recommended)


MEASURES = (
    ("safety_score", "Safety score"),
    ("risky_turns", "Risky turns"),
    ("closures", "Closed roads"),
    ("lane_closures", "Lane closures"),
    ("school_zones", "School zones"),
    ("rail_crossings", "Rail crossings"),
    ("fuel_gal", "Fuel (gal)"),
    ("minutes", "Minutes"),
    ("miles", "Miles"),
)


def measure(route: dict[str, Any]) -> dict[str, float]:
    f = route["features"]
    return {
        "safety_score": route.get("safety_score", 100),
        "risky_turns": f["busy_crossing_turns"] + f["hotspot_crossing_turns"],
        "closures": f["closures"],
        "lane_closures": f["lane_closures"],
        "school_zones": f["school_zones"],
        "rail_crossings": f["rail_crossings"],
        "fuel_gal": f["fuel_l"] / LITERS_PER_GALLON,
        "minutes": f["duration_min"],
        "miles": f["distance_km"] * KM_TO_MI,
    }


def run(client: TestClient, origin: str, destination: str, *, live_traffic: bool, access_code: str | None) -> Outcome | str:
    body = {
        "origin": dict(zip(("lat", "lng"), PLACES[origin], strict=True)),
        "destination": dict(zip(("lat", "lng"), PLACES[destination], strict=True)),
        "live_traffic": live_traffic,
    }
    response = client.post("/route", json=body, headers={"X-Access-Code": access_code} if access_code else {})
    name = f"{origin} → {destination}"
    if response.status_code != 200:
        return f"{name}: the server answered {response.status_code} ({response.json().get('detail', '')})"
    plan = response.json()["plan"]
    winner = plan["winner"]
    standard = next((route for route in plan["candidates"] if route["is_standard"]), None)
    mine = measure(winner)
    theirs = measure(standard) if standard else mine
    return Outcome(
        trip=name,
        recommended="Standard route" if winner["is_standard"] else "Our safer design",
        standard_found=standard is not None,
        differs=standard is not None and standard["id"] != winner["id"],
        numbers={key: (theirs[key], mine[key]) for key in mine},
    )


def headline(outcomes: Sequence[Outcome], live_traffic: bool) -> dict[str, Any]:
    """The totals over the trips compared with a standard route, for frontend/results.json."""
    compared = [o for o in outcomes if o.standard_found]
    totals = {
        f"{key}_{side}": round(sum(o.numbers[key][which] for o in compared), 2)
        for key, _ in MEASURES
        for which, side in ((0, "standard"), (1, "ours"))
    }
    return {
        "generated": f"{datetime.now():%Y-%m-%d}",
        "live_traffic": live_traffic,
        "trips": len(compared),
        "different": sum(o.differs for o in compared),
        **totals,
        "minutes_added_per_trip": round((totals["minutes_ours"] - totals["minutes_standard"]) / len(compared), 1)
        if compared
        else 0.0,
    }


def report(outcomes: Sequence[Outcome], failures: Sequence[str], live_traffic: bool, health: dict[str, Any]) -> str:
    compared = [o for o in outcomes if o.standard_found]
    differs = [o for o in compared if o.differs]
    hazards = health.get("hazards") or {}
    hazards_note = (
        " (The road map has no school or railroad data yet: rebuild it with `python -m app.build_graph milwaukee-metro --refresh`.)"
        if not hazards.get("schools") and not hazards.get("rail_crossings")
        else "" if hazards.get("school_hours_now") else " (School zones count only at drop-off and pickup times.)"
    )

    def total(key: str, which: int) -> float:
        return sum(o.numbers[key][which] for o in compared)

    lines = [
        "# Results: our safer routes vs the standard route",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m app.evaluate`"
        f" ({len(outcomes)} Milwaukee trips, profile safe-economy, live traffic {'on' if live_traffic else 'off'}).",
        "",
    ]
    if compared:
        risky_avoided = total("risky_turns", 0) - total("risky_turns", 1)
        minutes_added = (total("minutes", 1) - total("minutes", 0)) / len(compared)
        fuel_saved = total("fuel_gal", 0) - total("fuel_gal", 1)
        fuel = (
            f"ours use {fuel_saved:.2f} gal less in all" if fuel_saved >= 0 else f"ours use {-fuel_saved:.2f} gal more in all"
        )
        lines += [
            "## Headline",
            "",
            f"- **{len(differs)} of {len(compared)} trips** got a different route than the standard one.",
            f"- **Safety score** (0-100, higher is safer): {total('safety_score', 0) / len(compared):.0f} on average"
            f" for the standard routes, **{total('safety_score', 1) / len(compared):.0f}** for ours.",
            f"- **{risky_avoided:g} risky turns avoided** in all ({risky_avoided / len(compared):.1f} per trip):"
            f" {total('risky_turns', 0):g} on the standard routes, {total('risky_turns', 1):g} on ours.",
            f"- **Closed roads driven through:** {total('closures', 0):g} standard, {total('closures', 1):g} ours."
            f" **Lane closures:** {total('lane_closures', 0):g} vs {total('lane_closures', 1):g}.",
            f"- **School zones:** {total('school_zones', 0):g} vs {total('school_zones', 1):g}."
            f" **Rail crossings:** {total('rail_crossings', 0):g} vs {total('rail_crossings', 1):g}.{hazards_note}",
            f"- **Fuel:** {fuel} ({total('fuel_gal', 0):.2f} gal standard, {total('fuel_gal', 1):.2f} ours).",
            f"- **Time:** {minutes_added:+.1f} min per trip on average: the price of the safer choice.",
            "",
        ]
    lines += [
        "## Trip by trip",
        "",
        "Standard → ours. A risky turn crosses oncoming traffic at a busy or crash-prone junction.",
        "",
        "| Trip | Recommended | Risky turns | Closed roads | Lane closures | Fuel (gal) | Minutes |",
        "|---|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        n = o.numbers
        lines.append(
            f"| {o.trip} | {o.recommended} | {n['risky_turns'][0]:g} → {n['risky_turns'][1]:g}"
            f" | {n['closures'][0]:g} → {n['closures'][1]:g}"
            f" | {n['lane_closures'][0]:g} → {n['lane_closures'][1]:g}"
            f" | {n['fuel_gal'][0]:.2f} → {n['fuel_gal'][1]:.2f}"
            f" | {n['minutes'][0]:.0f} → {n['minutes'][1]:.0f} |"
        )
    lines += [
        "",
        "## How to read this",
        "",
        "- Safety comes first in the ranking, then fuel, then time, so extra minutes are expected where a safer route exists.",
        "- With the OpenStreetMap map, the standard route is our router's fastest, so both are timed by the same"
        " model, with live delays from 511 and TomTom. With Google's map and no live traffic, the standard route's"
        " time is Google's no-traffic estimate: close, but not the same model. Run with `--live-traffic` for live"
        " times on both.",
        "- Lane closures come from the live work-zone feed, so they change from day to day; school zones count only"
        " during school drop-off and pickup times.",
    ]
    if failures:
        lines += ["", "## Trips that didn't run", ""] + [f"- {failure}" for failure in failures]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluate", description=__doc__.splitlines()[0])
    parser.add_argument("--live-traffic", action="store_true", help="use live traffic (up to ~7 Google requests a trip)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    outcomes: list[Outcome] = []
    failures: list[str] = []
    with TestClient(create_app(settings)) as client:
        health = client.get("/health").json()
        for number, (origin, destination) in enumerate(TRIPS, start=1):
            print(f"[{number}/{len(TRIPS)}] {origin} to {destination}", flush=True)  # plain text: any console prints it
            result = run(client, origin, destination, live_traffic=args.live_traffic, access_code=settings.access_code)
            (failures if isinstance(result, str) else outcomes).append(result)

    out = BACKEND_DIR.parent / "RESULTS.md"
    out.write_text(report(outcomes, failures, args.live_traffic, health), encoding="utf-8")
    table = BACKEND_DIR / "data" / "results.csv"
    table.parent.mkdir(parents=True, exist_ok=True)
    with table.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["trip", "recommended", *(f"{key}_{side}" for key, _ in MEASURES for side in ("standard", "ours"))])
        for o in outcomes:
            writer.writerow([o.trip, o.recommended, *(round(v, 3) for key, _ in MEASURES for v in o.numbers[key])])
    numbers = BACKEND_DIR.parent / "frontend" / "results.json"
    numbers.write_text(json.dumps(headline(outcomes, args.live_traffic), indent=1) + "\n", encoding="utf-8")
    print(f"\nWrote {out}, {table} and {numbers}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
