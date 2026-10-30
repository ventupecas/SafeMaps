"""Command-line runner for tuning the engine without a frontend.

Run from the backend/ directory:

    python -m app.cli --from 37.7955,-122.3937 --to 37.4419,-122.1430
    python -m app.cli --from ... --to ... --profile scenic --set scenic_weight=0.9
    python -m app.cli --from ... --to ... --all-profiles
    python -m app.cli --from ... --to ... --profile calm --json
    python -m app.cli --from 51.5007,-0.1246 --to 51.5194,-0.1270 --country GB --turns

When a road graph has been built (python -m app.build_graph milwaukee-metro),
our own router designs routes too and Google prices them with live traffic.
--no-pricing keeps our routes on the graph's estimates (no extra API calls);
--no-router uses Google's routes only.

Google's routes are fetched once per run through the cache and ranked
locally. Re-ranking them with different weights costs no API calls. In router
mode our designs change with the weights, and each new design is a new
pricing call.
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
from app.engine import (
    NoRouteFoundError,
    build_candidates,
    build_router_candidates,
    load_graph,
    load_hotspots,
    make_turn_context,
    plan_from_candidates,
)
from app.features import TurnContext
from app.models import CandidateRoute, LatLng, RoutePlan
from app.profiles import DEFAULT_PROFILE, PRESETS, get_profile
from app.roadgraph import RoadGraph
from app.router import Router
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
        # The graph judges junctions on every route, even with --no-router.
        graph = load_graph(settings)
        context = make_turn_context(settings, load_hotspots(settings), args.country, graph)
    except (ValueError, OSError) as exc:
        return _fail(exc)
    if args.no_router:
        graph = None

    profiles = list(PRESETS.values()) if args.all_profiles else [profile]
    notes: list[str] = []
    with RoutesClient.from_settings(settings) as client:
        try:
            if graph is not None:
                pool = build_router_candidates(
                    client, Router(graph, context), args.origin, args.destination, profiles, price=not args.no_pricing
                )
                candidates, notes = pool.candidates, pool.notes
            else:
                options = CandidateOptions(traffic_aware=settings.traffic_aware)
                candidates = build_candidates(client, args.origin, args.destination, options, context)
        except (RoutesApiError, CacheMiss, NoRouteFoundError) as exc:
            return _fail(exc)
        cap = f" of {settings.daily_limit}" if settings.daily_limit else ""
        usage = (
            f"Google API: {client.live_requests} live request(s), "
            f"{client.cache.hits} served from cache ({settings.cache_mode} mode); "
            f"{client.usage.used_today()}{cap} used today"
        )

    plans = [plan_from_candidates(candidates, each) for each in profiles]
    if args.json:
        payload = [plan.model_dump(mode="json") for plan in plans]
        print(json.dumps(payload if args.all_profiles else payload[0], indent=2))
    else:
        print(_describe_context(context, args.country or settings.default_country))
        print(_describe_router(graph, args, settings.graph_path))
        if args.all_profiles:
            _print_comparison(candidates, plans)
        else:
            _print_plan(plans[0])
        if args.turns:
            _print_turns(candidates)
        if notes:
            print("\nNotes:")
            for note in notes:
                print(f"  {note}")

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
    which.add_argument(
        "--profile", choices=PRESETS, default=DEFAULT_PROFILE, help=f"preset to rank with (default: {DEFAULT_PROFILE})"
    )
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
    parser.add_argument("--no-router", action="store_true", help="use Google's routes only, even if a road graph exists")
    parser.add_argument(
        "--no-pricing",
        action="store_true",
        help="keep our routes on the graph's no-traffic estimates instead of asking Google (fewer API calls)",
    )
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


FEATURE_HEADER = (
    f"{'km':>6} {'min':>6} {'fuel L':>6} {'hwy':>6} {'delay':>6} {'turns':>5} {'x-busy':>6} {'x-hot':>5} {'wind':>6}"
)
LEGEND = (
    "(G) = Google's default. hwy, delay and wind are minutes; fuel is a rough estimate in liters;\n"
    "cost is perceived minutes.\n"
    "x-busy / x-hot = turns across oncoming traffic at busy / crash-prone junctions.\n"
    "own-N = our router's design; [NN%] = how much of Google's priced version follows it;\n"
    "~ = estimated time without live traffic."
)


def _feature_cells(route: CandidateRoute) -> str:
    f = route.features
    return (
        f"{f.distance_km:6.1f} {f.duration_min:6.1f} {f.fuel_l:6.2f} {f.highway_min:6.1f} {f.traffic_delay_min:6.1f} "
        f"{f.maneuver_count:5d} {f.busy_crossing_turns:6d} {f.hotspot_crossing_turns:5d} {f.winding_min:6.1f}"
    )


def _label(route: CandidateRoute) -> str:
    label = route.id + (" (G)" if route.is_google_default else "")
    if route.design_match is not None:
        label += f" [{route.design_match:.0%}]"
    if route.source == "own" and not route.live_traffic:
        label += " ~"
    return label


def _describe_router(graph: RoadGraph | None, args: argparse.Namespace, graph_path: object) -> str:
    if args.no_router:
        return "Own router: off (--no-router)\n"
    if graph is None:
        return f"Own router: off (no road graph at {graph_path}; run python -m app.build_graph milwaukee-metro)\n"
    pricing = "estimated times only (--no-pricing)" if args.no_pricing else "priced by Google with live traffic"
    return f"Own router: {graph.area} graph, {graph.node_count:,} junctions; {pricing}\n"


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
    weights = (
        ", ".join(f"{key}={value:g}" if isinstance(value, float) else f"{key}={value}" for key, value in custom.items())
        or "default weights"
    )
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
    if plan.profile.ranking == "priority":
        print("\nRanked by safety first, then fuel, then time:")
        for route in plan.candidates:
            print(f"  {route.id:<{width}}  {route.reason}")
        print()
    if plan.hidden:
        copies = ", ".join(f"{hidden} (follows {better})" for hidden, better in plan.hidden.items())
        print(f"Left out as near-copies of better routes: {copies}\n")
    default = next((route.id for route in plan.candidates if route.is_google_default), None)
    if default is None:
        print(f"Winner: {plan.winner.id} (no Google route to compare against).")
    elif plan.overrides_google:
        print(f"Winner: {plan.winner.id}, overriding Google's default ({default}).")
    else:
        print(f"Winner: {plan.winner.id}, the same as Google's default.")


def _print_comparison(candidates: Sequence[CandidateRoute], plans: Sequence[RoutePlan]) -> None:
    width = max(len(_label(route)) for route in candidates)
    print(f"  {'route':<{width}}  {FEATURE_HEADER}  road")
    for route in candidates:
        print(f"  {_label(route):<{width}}  {_feature_cells(route)}  {route.description[:28]}")
    print(f"\n{LEGEND}")

    has_google = any(route.is_google_default for route in candidates)
    print(f"\n  {'profile':<16} {'winner':<{width}} {'cost':>7}  vs Google")
    for plan in plans:
        verdict = ("override" if plan.overrides_google else "same") if has_google else "n/a"
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
