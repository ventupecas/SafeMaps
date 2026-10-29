"""Command-line runner for tuning the engine without a frontend.

Run from the backend/ directory:

    python -m app.cli --from 37.7955,-122.3937 --to 37.4419,-122.1430
    python -m app.cli --from ... --to ... --profile scenic --set scenic_weight=0.9
    python -m app.cli --from ... --to ... --all-profiles
    python -m app.cli --from ... --to ... --profile calm --json
    python -m app.cli --from 51.5007,-0.1246 --to 51.5194,-0.1270 --country GB --turns

Candidates are fetched once per run through the cache and ranked locally, so
re-running with different weights costs no API calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import replace

from app.cache import CacheMiss, CacheMode
from app.candidates import CandidateOptions
from app.config import Settings
from app.engine import NoRouteFoundError, build_candidates, load_hotspots, make_turn_context, plan_from_candidates
from app.features import TurnContext
from app.models import CandidateRoute, LatLng, RoutePlan
from app.profiles import PRESETS, get_profile
from app.routes_client import RoutesApiError, RoutesClient


def coordinate(text: str) -> LatLng:
    # Named for argparse's error message: "invalid coordinate value: ...".
    return LatLng.parse(text)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = Settings.from_env()
        if args.cache_mode:
            settings = replace(settings, cache_mode=CacheMode(args.cache_mode))
        profile = get_profile(args.profile, _parse_overrides(args.overrides))
        context = make_turn_context(settings, load_hotspots(settings), args.country)
    except (ValueError, OSError) as exc:
        return _fail(exc)

    options = CandidateOptions(traffic_aware=settings.traffic_aware)
    with RoutesClient.from_settings(settings) as client:
        try:
            candidates = build_candidates(client, args.origin, args.destination, options, context)
        except (RoutesApiError, CacheMiss, NoRouteFoundError) as exc:
            return _fail(exc)
        usage = (
            f"Google API: {client.live_requests} live request(s), "
            f"{client.cache.hits} served from cache ({settings.cache_mode} mode)"
        )

    if args.all_profiles:
        plans = [plan_from_candidates(candidates, preset) for preset in PRESETS.values()]
    else:
        plans = [plan_from_candidates(candidates, profile)]

    if args.json:
        payload = [plan.model_dump(mode="json") for plan in plans]
        print(json.dumps(payload if args.all_profiles else payload[0], indent=2))
    else:
        print(_describe_context(context, args.country or settings.default_country))
        if args.all_profiles:
            _print_comparison(candidates, plans)
        else:
            _print_plan(plans[0])
        if args.turns:
            _print_turns(candidates)

    print(usage, file=sys.stderr)  # stderr keeps --json output pipeable
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Rank Google route candidates under a personalized driver profile.",
    )
    parser.add_argument("--from", dest="origin", type=coordinate, required=True, metavar="LAT,LNG")
    parser.add_argument("--to", dest="destination", type=coordinate, required=True, metavar="LAT,LNG")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--profile", choices=PRESETS, default="fastest", help="preset to rank with (default: fastest)")
    which.add_argument("--all-profiles", action="store_true", help="show which route every preset picks")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="WEIGHT=VALUE",
        help="override one weight of --profile, e.g. --set highway_weight=2 (repeatable)",
    )
    parser.add_argument(
        "--country",
        metavar="CC",
        help="two-letter country of the trip; decides which turns cross traffic (default: ROUTES_DEFAULT_COUNTRY)",
    )
    parser.add_argument("--turns", action="store_true", help="list every turn across oncoming traffic")
    parser.add_argument("--cache-mode", choices=[mode.value for mode in CacheMode], help="override ROUTES_CACHE_MODE")
    parser.add_argument("--json", action="store_true", help="print the API-shaped JSON instead of a table")
    parser.add_argument("-v", "--verbose", action="store_true", help="log cache activity")
    args = parser.parse_args(argv)
    if args.all_profiles and args.overrides:
        parser.error("--set applies to a single --profile, not --all-profiles")
    return args


def _parse_overrides(pairs: Sequence[str]) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"--set expects WEIGHT=VALUE, got {pair!r}")
        try:
            overrides[key.strip()] = float(value)
        except ValueError:
            raise ValueError(f"--set {key}: {value!r} is not a number") from None
    return overrides


def _fail(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 1


FEATURE_HEADER = f"{'km':>6} {'min':>6} {'hwy':>6} {'delay':>6} {'turns':>5} {'x-busy':>6} {'x-hot':>5} {'wind':>6}"
LEGEND = (
    "(G) = Google's default. hwy, delay and wind are minutes; cost is perceived minutes.\n"
    "x-busy / x-hot = turns across oncoming traffic at busy / crash-prone junctions."
)


def _feature_cells(route: CandidateRoute) -> str:
    f = route.features
    return (
        f"{f.distance_km:6.1f} {f.duration_min:6.1f} {f.highway_min:6.1f} {f.traffic_delay_min:6.1f} "
        f"{f.maneuver_count:5d} {f.busy_crossing_turns:6d} {f.hotspot_crossing_turns:5d} {f.winding_min:6.1f}"
    )


def _label(route: CandidateRoute) -> str:
    return route.id + (" (G)" if route.is_google_default else "")


def _describe_context(context: TurnContext, country: str | None) -> str:
    where = country.strip().upper() if country else "default"
    if context.hotspots is None:
        crashes = "no crash data (set ROUTES_CRASH_DATA)"
    else:
        locations = context.hotspots.locations
        crashes = (
            f"{context.hotspots.total_crashes:,.0f} crashes at {locations:,} location{'' if locations == 1 else 's'}, "
            f"crash-prone at {context.min_hotspot_crashes:g}+"
        )
    return f"Traffic keeps {context.driving_side} ({where}); {crashes}\n"


def _print_plan(plan: RoutePlan) -> None:
    custom = plan.profile.model_dump(exclude_defaults=True, exclude={"name"})
    weights = ", ".join(f"{key}={value:g}" for key, value in custom.items()) or "default weights"
    print(f"Profile: {plan.profile.name} ({weights})\n")

    width = max(len(_label(route)) for route in plan.candidates)
    print(f"    {'#':>2}  {'route':<{width}}  {FEATURE_HEADER} {'cost':>7}  road")
    for rank, route in enumerate(plan.candidates, start=1):
        marker = "->" if route.id == plan.winner.id else "  "
        note = "" if route.eligible else "  [too slow for this profile]"
        print(
            f"  {marker}{rank:>2}  {_label(route):<{width}}  {_feature_cells(route)} "
            f"{route.breakdown.total:7.1f}  {route.description[:28]}{note}"
        )

    print(f"\n{LEGEND}")
    if plan.overrides_google:
        default = next((route.id for route in plan.candidates if route.is_google_default), "none")
        print(f"Winner: {plan.winner.id}, overriding Google's default ({default}).")
    else:
        print(f"Winner: {plan.winner.id}, the same as Google's default.")


def _print_comparison(candidates: Sequence[CandidateRoute], plans: Sequence[RoutePlan]) -> None:
    width = max(len(_label(route)) for route in candidates)
    print(f"  {'route':<{width}}  {FEATURE_HEADER}  road")
    for route in candidates:
        print(f"  {_label(route):<{width}}  {_feature_cells(route)}  {route.description[:28]}")
    print(f"\n{LEGEND}")

    print(f"\n  {'profile':<16} {'winner':<{width}} {'cost':>7}  vs Google")
    for plan in plans:
        verdict = "override" if plan.overrides_google else "same"
        print(f"  {plan.profile.name:<16} {plan.winner.id:<{width}} {plan.winner.breakdown.total:7.1f}  {verdict}")


def _print_turns(candidates: Sequence[CandidateRoute]) -> None:
    print("\nTurns across oncoming traffic:")
    for route in candidates:
        turns = route.features.crossing_turns
        print(f"  {_label(route)}: {len(turns) or 'none'}")
        for turn in turns:
            where = f"{turn.location.lat:.5f},{turn.location.lng:.5f}" if turn.location else "location unknown"
            flags = [flag for flag, on in (("busy", turn.busy), ("crash-prone", turn.hotspot)) if on]
            print(
                f"    {turn.maneuver:<17} at {where}  road {turn.road_speed_kmh:3.0f} km/h  "
                f"crashes {turn.nearby_crashes:3.0f}  [{', '.join(flags) or 'quiet'}]"
            )


if __name__ == "__main__":
    sys.exit(main())
