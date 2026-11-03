"""Recorded crashes for the crash-prone junction rule: download fatal ones, or import all of them.

Run from the backend/ directory:

    python -m app.crash_data milwaukee-metro
    python -m app.crash_data milwaukee-metro --years 2018-2024
    python -m app.crash_data milwaukee-metro --cyclists     # for bike routes: where cyclists died
    python -m app.crash_data import crashes.csv

Download (the default): NHTSA's Fatality Analysis Reporting System (FARS):
every fatal crash in the US, one public file per year (about 30 MB each, no
key). The yearly files are kept under data/crashes/fars/, so a rerun only
downloads new years. The crashes inside the area are written to
data/crashes/<area>.csv (lat,lng,crashes,year,fatalities), which
ROUTES_CRASH_DATA points at. FARS has fatal crashes only: far fewer than all
crashes, so a single one near a junction is a strong signal. Use
ROUTES_CRASH_MIN_COUNT=1 with this data.

Import: a CSV of every reported crash, such as one exported by hand from
Wisconsin's Community Maps (UW-Madison TOPS Lab, from police crash reports).
Its terms forbid automated or bulk downloading, so the file is exported from
the website and imported here. The columns are found by name: latitude and
longitude (lat, latitude, latdecdg, ...), and optionally severity and date.
Injury crashes count by severity (fatal 3, serious injury 2, other injury 1);
property-damage-only crashes are left out unless --include-property-damage
(0.25 each). Written to data/crashes/<area>-all.csv.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import sys
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import httpx

from app.config import BACKEND_DIR
from app.overpass import AREAS, BBox

logger = logging.getLogger(__name__)

FARS_URL = "https://static.nhtsa.gov/nhtsa/downloads/FARS/{year}/National/FARS{year}NationalCSV.zip"
DEFAULT_YEARS = "2020-2024"


def download_year(year: int, folder: Path, http: httpx.Client, *, refresh: bool = False) -> Path:
    """The FARS file for year, downloaded unless it's already on disk."""
    path = folder / f"FARS{year}NationalCSV.zip"
    if path.exists() and not refresh:
        return path
    folder.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    logger.info("Downloading FARS %d ...", year)
    with http.stream("GET", FARS_URL.format(year=year), timeout=300.0, follow_redirects=True) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    partial.replace(path)
    return path


CYCLIST_TYPES = {"6", "7"}  # FARS person types: bicyclist, other pedalcyclist


def crashes_in(path: Path, area: BBox, *, cyclists_only: bool = False) -> Iterator[dict[str, str | float | int]]:
    """Fatal crashes inside area from one FARS yearly file (its accident table); with cyclists_only,
    just those where a cyclist was involved (its pbtype table)."""
    with zipfile.ZipFile(path) as archive:
        cases: set[str] | None = None
        if cyclists_only:
            member = next((name for name in archive.namelist() if name.lower().endswith("pbtype.csv")), None)
            if member is None:
                raise ValueError(f"{path.name} has no pbtype.csv")
            with archive.open(member) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1", newline=""))
                cases = {row["ST_CASE"] for row in reader if row.get("PBPTYPE") in CYCLIST_TYPES}
        member = next((name for name in archive.namelist() if name.lower().endswith("accident.csv")), None)
        if member is None:
            raise ValueError(f"{path.name} has no accident.csv")
        with archive.open(member) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1", newline=""))
            columns = {name.strip().upper(): name for name in reader.fieldnames or []}
            for row in reader:
                try:
                    lat = float(row[columns["LATITUDE"]])
                    lng = float(row[columns["LONGITUD"]])  # sic: FARS spells it this way
                except (KeyError, TypeError, ValueError):
                    continue
                # Unknown locations are coded as 77.7777, 88.8888, 99.9999 and the like: outside any area.
                if not area.contains(lat, lng):
                    continue
                if cases is not None and row.get(columns.get("ST_CASE", ""), "") not in cases:
                    continue
                yield {
                    "lat": round(lat, 6),
                    "lng": round(lng, 6),
                    "crashes": 1,
                    "year": int(row.get(columns.get("YEAR", ""), 0) or 0),
                    "fatalities": int(row.get(columns.get("FATALS", ""), 1) or 1),
                }


# Importing a crash export: which columns hold what, by name (exact first, then containing).
_LAT_NAMES = ("lat", "latitude", "latdecdg", "lat_dd", "y_coord")
_LNG_NAMES = ("lng", "lon", "long", "longitude", "londecdg", "lon_dd", "x_coord")
_SEVERITY_HINTS = ("injsvr", "severity", "svr", "kabco", "injury")
_DATE_HINTS = ("date", "crshdate", "year")
SEVERITY_WEIGHTS = {"fatal": 3.0, "serious": 2.0, "injury": 1.0, "unknown": 1.0, "property": 0.0}
PROPERTY_DAMAGE_WEIGHT = 0.25


def _column(names: Sequence[str], exact: Sequence[str], contains: Sequence[str]) -> str | None:
    lowered = {name.strip().lower(): name for name in names}
    for candidate in exact:
        if candidate in lowered:
            return lowered[candidate]
    return next((name for low, name in lowered.items() if any(hint in low for hint in contains)), None)


def severity_of(value: str) -> str:
    """A crash's severity as fatal, serious, injury, property or unknown, from words or KABCO letters."""
    text = value.strip().lower()
    if not text:
        return "unknown"
    if text in ("o", "pdo") or any(words in text for words in ("property", "no apparent", "no injury", "none")):
        return "property"
    if text == "k" or "fatal" in text or "killed" in text:
        return "fatal"
    if text == "a" or "serious" in text or "incapacitat" in text:
        return "serious"
    if text in ("b", "c") or any(word in text for word in ("injur", "minor", "possible")):
        return "injury"
    return "unknown"


def import_crashes(
    path: Path, area: BBox, *, include_property_damage: bool = False
) -> tuple[list[dict[str, str | float | int]], set[int]]:
    """The crashes in area from an exported CSV, weighed by severity; and the years they span."""
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle)
        names = reader.fieldnames or []
        lat_col = _column(names, _LAT_NAMES, ("latitude", "lat_"))
        lng_col = _column(names, _LNG_NAMES, ("longitude", "lon_", "lng"))
        if lat_col is None or lng_col is None:
            raise ValueError(f"{path.name} needs latitude and longitude columns; it has: {', '.join(names)}")
        severity_col = _column(names, (), _SEVERITY_HINTS)
        date_col = _column(names, (), _DATE_HINTS)
        rows: list[dict[str, str | float | int]] = []
        years: set[int] = set()
        for row in reader:
            try:
                lat, lng = float(row[lat_col]), float(row[lng_col])
            except (TypeError, ValueError):
                continue
            if not area.contains(lat, lng):
                continue
            severity = severity_of(row.get(severity_col) or "") if severity_col else "unknown"
            weight = SEVERITY_WEIGHTS[severity]
            if severity == "property" and include_property_damage:
                weight = PROPERTY_DAMAGE_WEIGHT
            if weight <= 0:
                continue
            year = next((int(found) for found in re.findall(r"\b(19\d\d|20\d\d)\b", row.get(date_col) or "")), 0) if date_col else 0
            if year:
                years.add(year)
            rows.append({"lat": round(lat, 6), "lng": round(lng, 6), "crashes": weight, "severity": severity, "year": year})
    return rows, years


def import_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.crash_data import", description="Import a CSV of all reported crashes.")
    parser.add_argument("file", type=Path, help="the exported CSV")
    parser.add_argument("--area", choices=AREAS, default="milwaukee-metro", help="area to keep crashes for")
    parser.add_argument("--include-property-damage", action="store_true", help="count crashes without injuries too")
    args = parser.parse_args(argv)
    try:
        rows, years = import_crashes(args.file, AREAS[args.area], include_property_damage=args.include_property_damage)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print(f"error: no crashes with injuries inside {args.area} in {args.file.name}", file=sys.stderr)
        return 1
    out = BACKEND_DIR / "data" / "crashes" / f"{args.area}-all.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["lat", "lng", "crashes", "severity", "year"])
        writer.writeheader()
        writer.writerows(rows)
    span = (max(years) - min(years) + 1) if years else None
    # About two injury crashes a year at one junction marks it out; at least 3 points in all.
    min_count = max(3, 2 * span) if span else 6
    counts = {kind: sum(1 for row in rows if row["severity"] == kind) for kind in SEVERITY_WEIGHTS}
    print(f"\nWrote {len(rows):,} crashes to {out}")
    print("  " + ", ".join(f"{kind}: {count:,}" for kind, count in counts.items() if count))
    if span:
        print(f"  covering {min(years)}-{max(years)} ({span} year{'s' if span > 1 else ''})")
    print("Point the server at it in backend/.env:")
    print(f"  ROUTES_CRASH_DATA=data/crashes/{args.area}-all.csv")
    print(f"  ROUTES_CRASH_MIN_COUNT={min_count}")
    print("Then check /health (hazards.crash_locations) and raise the count if too many junctions are flagged.")
    return 0


def parse_years(text: str) -> list[int]:
    """'2020-2024' or '2021,2023' -> a list of years."""
    years: list[int] = []
    for part in text.split(","):
        first, _, last = part.strip().partition("-")
        years += range(int(first), int(last or first) + 1)
    return years


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["import"]:
        return import_main(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m app.crash_data", description=__doc__.splitlines()[0])
    parser.add_argument("area", choices=AREAS, help="area to keep crashes for")
    parser.add_argument("--years", default=DEFAULT_YEARS, help=f"years to include, e.g. 2018-2024 (default {DEFAULT_YEARS})")
    parser.add_argument("--refresh", action="store_true", help="download the yearly files again")
    parser.add_argument("--cyclists", action="store_true", help="only crashes where a cyclist died, for bike routes")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    folder = BACKEND_DIR / "data" / "crashes" / "fars"
    rows: list[dict[str, str | float | int]] = []
    with httpx.Client() as http:
        for year in parse_years(args.years):
            try:
                path = download_year(year, folder, http, refresh=args.refresh)
            except httpx.HTTPError as exc:
                print(f"error: couldn't download FARS {year}: {exc}", file=sys.stderr)
                return 1
            found = list(crashes_in(path, AREAS[args.area], cyclists_only=args.cyclists))
            logger.info("FARS %d: %d fatal %scrashes in %s", year, len(found), "cyclist " if args.cyclists else "", args.area)
            rows += found

    out = BACKEND_DIR / "data" / "crashes" / f"{args.area}{'-bike' if args.cyclists else ''}.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["lat", "lng", "crashes", "year", "fatalities"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows):,} fatal {'cyclist ' if args.cyclists else ''}crashes to {out}")
    print("Point the server at it in backend/.env:")
    if args.cyclists:
        print(f"  ROUTES_BIKE_CRASH_DATA=data/crashes/{args.area}-bike.csv")
    else:
        print(f"  ROUTES_CRASH_DATA=data/crashes/{args.area}.csv")
        print("  ROUTES_CRASH_MIN_COUNT=1")
    print("Source: NHTSA Fatality Analysis Reporting System (FARS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
