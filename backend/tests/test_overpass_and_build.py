import gzip
import json

import httpx
import pytest

from app import build_graph as build_cli
from app.overpass import AREAS, BBox, OverpassError, build_query, download_area, iter_elements
from app.roadgraph import RoadGraph
from tests.osm_factory import grid_city

SMALL = BBox(43.0, -88.0, 43.1, -87.9)


OK = (200, {"elements": [{"type": "node", "id": 1, "lat": 43.0, "lon": -88.0}]})
OVERLOADED = (502, {})


class Overpass:
    """MockTransport handler: replays (status, payload) replies, then repeats `then` forever."""

    def __init__(self, *replies, then=None) -> None:
        self.replies = list(replies)
        self.then = then
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.queries.append(request.content.decode())
        status, payload = self.replies.pop(0) if self.replies else self.then
        return httpx.Response(status, json=payload)


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_tiles_cover_the_box():
    tiles = SMALL.tiles(2, 2)
    assert len(tiles) == 4
    assert min(t.south for t in tiles) == SMALL.south and max(t.north for t in tiles) == pytest.approx(SMALL.north)
    assert AREAS["milwaukee-metro"].contains(43.0389, -87.9065)  # downtown Milwaukee


def test_query_asks_for_roads_signals_and_restrictions():
    query = build_query(SMALL)
    assert "(43.0,-88.0,43.1,-87.9)" in query
    assert "traffic_signals" in query and '"type"="restriction"' in query


def test_download_saves_tiles_and_reuses_them(tmp_path):
    elements = {"elements": [{"type": "node", "id": 1, "lat": 43.0, "lon": -88.0}]}
    handler = Overpass(*[(200, elements)] * 4)
    paths = download_area("small", SMALL, tmp_path, tiles=2, http=client(handler), sleep=lambda s: None)
    assert len(paths) == 4 and all(p.exists() for p in paths)
    assert len(list(iter_elements(paths))) == 4

    download_area("small", SMALL, tmp_path, tiles=2, http=client(handler), sleep=lambda s: None)
    assert len(handler.queries) == 4  # nothing fetched the second time


def test_busy_server_is_retried(tmp_path):
    waits = []
    handler = Overpass((429, {}), (200, {"elements": []}))
    download_area("small", SMALL, tmp_path, tiles=1, http=client(handler), sleep=waits.append)
    assert waits == [30]


def test_partial_results_split_the_tile(tmp_path):
    handler = Overpass((200, {"elements": [], "remark": "runtime error: Query timed out"}), then=OK)
    paths = download_area("small", SMALL, tmp_path, tiles=1, http=client(handler), sleep=lambda s: None)
    assert [p.name for p in paths] == [f"tile-1x1-00{x}.json.gz" for x in "abcd"]


def test_overloaded_tile_is_split_and_a_rerun_reuses_the_pieces(tmp_path):
    handler = Overpass(*[OVERLOADED] * 4, then=OK)
    paths = download_area("small", SMALL, tmp_path, tiles=1, http=client(handler), sleep=lambda s: None)
    assert len(paths) == 4 and len(handler.queries) == 4 + 4

    again = Overpass(then=OK)
    assert download_area("small", SMALL, tmp_path, tiles=1, http=client(again), sleep=lambda s: None) == paths
    assert again.queries == []  # finished from the saved pieces, without retrying the whole tile


def test_splitting_stops_at_the_depth_limit(tmp_path):
    handler = Overpass(then=OVERLOADED)
    with pytest.raises(OverpassError, match="502"):
        download_area("small", SMALL, tmp_path, tiles=1, http=client(handler), sleep=lambda s: None)
    assert len(handler.queries) == 4 * 3  # whole tile, its first piece, that piece's first piece


def test_unreachable_server_is_not_split(tmp_path):
    def blocked(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(OverpassError, match="network error"):
        download_area("small", SMALL, tmp_path, tiles=1, http=client(blocked), sleep=lambda s: None)
    assert list(tmp_path.iterdir()) == []


def test_bad_query_is_not_retried(tmp_path):
    handler = Overpass((400, {"error": "syntax"}))
    with pytest.raises(OverpassError, match="400"):
        download_area("small", SMALL, tmp_path, tiles=1, http=client(handler), sleep=lambda s: None)
    assert len(handler.queries) == 1


def test_build_command_uses_tiles_already_on_disk(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_cli, "BACKEND_DIR", tmp_path)
    raw = tmp_path / "data" / "osm" / "milwaukee-metro"
    raw.mkdir(parents=True)
    with gzip.open(raw / "tile-1x1-00.json.gz", "wt", encoding="utf-8") as handle:
        json.dump({"elements": grid_city().osm.elements}, handle)

    assert build_cli.main(["milwaukee-metro", "--tiles", "1"]) == 0
    graph = RoadGraph.load(tmp_path / "data" / "graphs" / "milwaukee-metro.json.gz")
    assert graph.node_count == 10
    assert "OpenStreetMap" in capsys.readouterr().out
