"""Importing an export of every reported crash for the crash-prone junction rule."""

import pytest

from app.crash_data import import_crashes, main, severity_of
from app.overpass import AREAS

METRO = AREAS["milwaukee-metro"]


@pytest.mark.parametrize(
    ("value", "severity"),
    [
        ("Fatal", "fatal"),
        ("K", "fatal"),
        ("A - Suspected Serious Injury", "serious"),
        ("Possible Injury", "injury"),
        ("C", "injury"),
        ("No Apparent Injury", "property"),
        ("Property Damage", "property"),
        ("", "unknown"),
    ],
)
def test_severity_reads_words_and_kabco_letters(value, severity):
    assert severity_of(value) == severity


def test_an_export_of_all_crashes_is_weighed_by_severity(tmp_path):
    export = tmp_path / "crashes.csv"
    export.write_text(
        "CRSHDATE,LATDECDG,LONDECDG,INJSVR\n"
        "2021-04-02,43.04,-87.91,Fatal\n"
        "06/01/2023,43.05,-87.92,Suspected Serious Injury\n"
        "2022-01-05,43.03,-87.93,Possible Injury\n"
        "2022-02-02,43.02,-87.95,Property Damage\n"
        "2022-02-03,44.50,-88.00,Fatal\n"  # Green Bay: outside the metro
        "2022-03-03,,,Fatal\n",  # no location
        encoding="utf-8",
    )
    rows, years = import_crashes(export, METRO)
    assert [(row["severity"], row["crashes"]) for row in rows] == [("fatal", 3.0), ("serious", 2.0), ("injury", 1.0)]
    assert years == {2021, 2022, 2023}
    with_property, _ = import_crashes(export, METRO, include_property_damage=True)
    assert (with_property[-1]["severity"], with_property[-1]["crashes"]) == ("property", 0.25)


def test_an_export_without_coordinates_names_the_columns_it_has(tmp_path, capsys):
    export = tmp_path / "trafficaccident.csv"
    export.write_text('"CASENUMBER","CASEDATE","CRASHLOC"\n"1","2024-01-01","N WATER ST"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="CRASHLOC"):
        import_crashes(export, METRO)
    assert main(["import", str(export)]) == 1
    assert "latitude and longitude" in capsys.readouterr().err
