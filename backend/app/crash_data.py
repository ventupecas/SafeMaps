"""Download recorded crashes for the crash-prone junction rule.

Run from the backend/ directory:

    python -m app.crash_data milwaukee-metro
    python -m app.crash_data milwaukee-metro --years 2018-2024

Source: NHTSA's Fatality Analysis Reporting System (FARS): every fatal crash in
the US, one public file per year (about 30 MB each, no key). The yearly files
are kept under data/crashes/fars/, so a rerun only downloads new years. The
crashes inside the area are written to data/crashes/<area>.csv
(lat,lng,crashes,year,fatalities), which ROUTES_CRASH_DATA points at.

FARS has fatal crashes only: far fewer than all crashes, so a single one near
a junction is a strong signal. Use ROUTES_CRASH_MIN_COUNT=1 with this data.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
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


def crashes_in(path: Path, area: BBox) -> Iterator[dict[str, str | float | int]]:
    """Fatal crashes inside area from one FARS yearly file (its accident table)."""
    with zipfile.ZipFile(path) as archive:
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
                yield {
                    "lat": round(lat, 6),
                    "lng": round(lng, 6),
                    "crashes": 1,
                    "year": int(row.get(columns.get("YEAR", ""), 0) or 0),
                    "fatalities": int(row.get(columns.get("FATALS", ""), 1) or 1),
                }


def parse_years(text: str) -> list[int]:
    """'2020-2024' or '2021,2023' -> a list of years."""
    years: list[int] = []
    for part in text.split(","):
        first, _, last = part.strip().partition("-")
        years += range(int(first), int(last or first) + 1)
    return years


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.crash_data", description=__doc__.splitlines()[0])
    parser.add_argument("area", choices=AREAS, help="area to keep crashes for")
    parser.add_argument("--years", default=DEFAULT_YEARS, help=f"years to include, e.g. 2018-2024 (default {DEFAULT_YEARS})")
    parser.add_argument("--refresh", action="store_true", help="download the yearly files again")
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
            found = list(crashes_in(path, AREAS[args.area]))
            logger.info("FARS %d: %d fatal crashes in %s", year, len(found), args.area)
            rows += found

    out = BACKEND_DIR / "data" / "crashes" / f"{args.area}.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["lat", "lng", "crashes", "year", "fatalities"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows):,} fatal crashes to {out}")
    print("Point the server at it in backend/.env:")
    print(f"  ROUTES_CRASH_DATA=data/crashes/{args.area}.csv")
    print("  ROUTES_CRASH_MIN_COUNT=1")
    print("Source: NHTSA Fatality Analysis Reporting System (FARS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
