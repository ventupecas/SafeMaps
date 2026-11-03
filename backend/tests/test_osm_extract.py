"""Reading an OpenStreetMap extract (.osm.pbf) as the Overpass queries would answer (app.osm_extract)."""

import gzip
import json

import pytest

from app.osm_extract import extract, kinds_of_way

osmium = pytest.importorskip("osmium")


def elements(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)["elements"]


def test_ways_go_to_the_queries_that_ask_for_them():
    assert kinds_of_way({"highway": "residential"}) == ["roads"]
    assert kinds_of_way({"highway": "cycleway"}) == ["bike", "walk"]
    assert kinds_of_way({"highway": "footway", "footway": "sidewalk"}) == ["walk"]  # no sidewalks on the bike map
    assert kinds_of_way({"highway": "steps"}) == ["walk"]
    assert kinds_of_way({"highway": "motorway"}) == ["roads"] and kinds_of_way({"building": "yes"}) == []


def test_an_extract_becomes_overpass_style_files(tmp_path):
    pbf = tmp_path / "town.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        points = {1: (43.04, -87.91), 2: (43.04, -87.90), 3: (43.05, -87.90), 4: (43.05, -87.91), 5: (43.041, -87.905)}
        tags = {2: {"highway": "traffic_signals"}, 5: {"amenity": "school"}}
        for node, (lat, lon) in points.items():
            writer.add_node(osmium.osm.mutable.Node(id=node, location=(lon, lat), tags=tags.get(node, {})))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2], tags={"highway": "primary", "name": "Main St"}))
        writer.add_way(osmium.osm.mutable.Way(id=11, nodes=[2, 3], tags={"highway": "footway", "footway": "sidewalk"}))
        writer.add_way(osmium.osm.mutable.Way(id=12, nodes=[3, 4], tags={"highway": "cycleway", "name": "Oak Leaf Trail"}))
        writer.add_way(osmium.osm.mutable.Way(id=13, nodes=[1, 4], tags={"building": "house"}))
        restriction = {"type": "restriction", "restriction": "no_left_turn"}
        members = [("w", 10, "from"), ("n", 2, "via"), ("w", 10, "to")]
        writer.add_relation(osmium.osm.mutable.Relation(id=20, members=members, tags=restriction))
    paths = extract(pbf, tmp_path / "out")

    roads = elements(paths["roads"])
    assert [e["id"] for e in roads if e["type"] == "way"] == [10]
    assert {e["id"] for e in roads if e["type"] == "relation"} == {20}
    signal = next(e for e in roads if e["type"] == "node" and e["id"] == 2)
    assert signal["tags"] == {"highway": "traffic_signals"} and signal["lat"] == pytest.approx(43.04)
    assert any(e.get("tags", {}).get("amenity") == "school" for e in roads)
    assert [e["id"] for e in elements(paths["bike"]) if e["type"] == "way"] == [12]
    assert [e["id"] for e in elements(paths["walk"]) if e["type"] == "way"] == [11, 12]
    assert {e["id"] for e in elements(paths["walk"]) if e["type"] == "node"} == {2, 3, 4}

    # Converted once: a rebuild reads the files, not the extract.
    before = paths["roads"].stat().st_mtime_ns
    assert extract(pbf, tmp_path / "out") == paths and paths["roads"].stat().st_mtime_ns == before
