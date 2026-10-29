import pytest

from app.geo import destination_point
from app.hotspots import CrashHotspots

TIMES_SQUARE = (40.7580, -73.9855)


def test_counts_crashes_within_the_radius():
    near = destination_point(TIMES_SQUARE, 45, 20)
    far = destination_point(TIMES_SQUARE, 45, 80)
    hotspots = CrashHotspots([(TIMES_SQUARE, 1), (near, 2), (far, 5)])
    assert hotspots.crashes_near(TIMES_SQUARE, 35) == 3
    assert hotspots.crashes_near(TIMES_SQUARE, 100) == 8
    assert (hotspots.locations, hotspots.total_crashes) == (3, 8)


def test_finds_crashes_across_grid_cells():
    hotspots = CrashHotspots([((40.75801, -73.98551), 4)])
    assert hotspots.crashes_near((40.75799, -73.98549), 35) == 4


def test_high_latitude_searches_enough_longitude_cells():
    tromso = (69.65, 18.95)  # 0.001 degrees of longitude is only ~39 m here
    east = destination_point(tromso, 90, 60)
    assert CrashHotspots([(east, 1)]).crashes_near(tromso, 100) == 1


def test_loads_raw_crash_rows_and_skips_unusable_ones(tmp_path):
    path = tmp_path / "crashes.csv"
    path.write_text(
        "LATITUDE,LONGITUDE,BOROUGH\n"
        "40.7580,-73.9855,MANHATTAN\n"
        "40.7580,-73.9855,MANHATTAN\n"
        ",,BROOKLYN\n"
        "0,0,QUEENS\n"
        "abc,def,BRONX\n",
        encoding="utf-8",
    )
    hotspots = CrashHotspots.from_csv(path)
    assert (hotspots.locations, hotspots.total_crashes) == (1, 2)
    assert hotspots.crashes_near(TIMES_SQUARE, 35) == 2


def test_crashes_column_weights_each_row(tmp_path):
    path = tmp_path / "hotspots.csv"
    path.write_text("lat,lng,crashes\n40.7580,-73.9855,7\n40.7600,-73.9800,\n", encoding="utf-8")
    hotspots = CrashHotspots.from_csv(path)
    assert hotspots.crashes_near(TIMES_SQUARE, 35) == 7
    assert hotspots.total_crashes == 8  # a blank count means one crash


def test_csv_without_coordinates_is_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("x,y\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="latitude and longitude"):
        CrashHotspots.from_csv(path)
