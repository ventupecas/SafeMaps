"""The ground's heights (app.elevation), the climb they give the bike map's segments, and the rider who goes around hills."""

import gzip

import numpy as np
import pytest

from app.bike import PROFILES, BikeRouter, plan
from app.elevation import Elevation, ElevationError, add_climb, tile_name
from app.geo import haversine_m
from app.models import LatLng
from app.roadgraph import RoadGraph, build_graph
from tests.osm_factory import grid_city, point


class Slope:
    """Ground that rises a meter every 100 m going north."""

    def heights(self, lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
        return (lat - 43.0) * 111_320.0 / 100.0


@pytest.fixture(scope="module")
def hilly() -> RoadGraph:
    graph = build_graph(grid_city().osm.elements, area="grid", mode="bike")
    add_climb(graph, Slope())
    return graph


def test_a_tile_is_named_by_its_southwest_corner():
    assert tile_name(43.5, -87.9) == "N43W088"
    assert tile_name(-0.5, 10.2) == "S01E010"


def test_heights_are_read_between_a_tiles_grid_points(tmp_path):
    # A small tile (1201 points a side, as the coarser surveys have): 500 m at its west edge, a meter higher each point east.
    grid = np.tile(500 + np.arange(1201), (1201, 1)).astype(">i2")
    (tmp_path / "N43W088.hgt.gz").write_bytes(gzip.compress(grid.tobytes()))
    ground = Elevation(tmp_path)
    heights = ground.heights(np.array([43.5, 43.2, 43.9]), np.array([-87.5, -87.25, -87.50625]))
    assert heights == pytest.approx([1100.0, 1400.0, 1092.5], abs=0.01)  # smoothing leaves an even slope as it is
    with pytest.raises(ElevationError, match="N44W088"):
        ground.heights(np.array([44.5]), np.array([-87.5]))  # not on disk, and no fetching here


def test_a_segment_climbs_going_up_and_not_coming_down(hilly):
    north = [edge for edge in range(hilly.edge_count) if hilly.bearing_start[edge] < 5 or hilly.bearing_start[edge] > 355]
    south = [edge for edge in range(hilly.edge_count) if 175 < hilly.bearing_start[edge] < 185]
    east = [edge for edge in range(hilly.edge_count) if 85 < hilly.bearing_start[edge] < 95]
    assert north and south and east
    for edge in north:
        assert hilly.climb_m[edge] == pytest.approx(hilly.length_m[edge] / 100, abs=0.2)
    assert all(hilly.climb_m[edge] == 0 for edge in south) and all(hilly.climb_m[edge] < 0.2 for edge in east)
    assert hilly.stats["climb_m"] > 0


def test_the_climb_is_kept_in_the_map_file(hilly, tmp_path):
    path = tmp_path / "hilly-bike.json.gz"
    hilly.save(path)
    from_json, from_fast_copy = RoadGraph.load(path), RoadGraph.load(path)
    assert list(from_json.climb_m) == pytest.approx(list(hilly.climb_m), abs=0.06)
    assert list(from_fast_copy.climb_m) == list(from_json.climb_m)
    flat = build_graph(grid_city().osm.elements, area="grid", mode="bike")  # built without heights
    assert set(flat.climb_m) == {0.0}


def test_a_routes_climb_is_counted_and_the_flat_rider_pays_for_it(hilly):
    router = BikeRouter(hilly)
    start, end = point(0, 1), point(2, 1)  # two blocks north, uphill all the way whichever streets are taken
    trip = plan(router, LatLng(lat=start[0], lng=start[1]), LatLng(lat=end[0], lng=end[1]), PROFILES["everyday"])
    assert trip["winner"]["features"]["climb_m"] == pytest.approx(haversine_m(start, end) / 100, abs=1.5)
    everyday, flat = router._base(PROFILES["everyday"], False), router._base(PROFILES["flat"], False)
    uphill = np.array(hilly.climb_m) > 0.5
    assert (flat[uphill] > everyday[uphill]).all()  # a climb costs the rider who avoids hills ...
    assert flat[~uphill] == pytest.approx(everyday[~uphill], abs=0.05)  # ... and level streets cost both the same
