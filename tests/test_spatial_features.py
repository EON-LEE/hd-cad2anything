import math
import time

import networkx as nx
import pytest
from shapely.geometry import LineString, box

from cad2anything.model import Entity, EntityKind, Floor, RelationType, Revision, SpatialModel
from cad2anything.spatial import (
    access_point,
    access_points,
    adjacency_pairs,
    build_floor_network,
    compute_relations,
    floor_codes,
    multi_source_dijkstra,
    shared_length,
    unit_features,
    walkway_grid_graph,
)

SQRT2 = math.sqrt(2.0)
HALF_DIAG = SQRT2 / 2  # offset from a boundary midpoint on integer coordinates to its nearest cell center


def octile(di: int, dj: int) -> float:
    a, b = abs(di), abs(dj)
    return min(a, b) * SQRT2 + (max(a, b) - min(a, b))


def _unit(floor: str, code: str, geometry, category: str = "COSMETICS") -> Entity:
    return Entity(
        f"U-{code}",
        EntityKind.UNIT,
        floor,
        geometry,
        label=code,
        attributes={
            "unit_code": code,
            "brand_id": f"BR-{code}",
            "brand_name": f"브랜드 {code}",
            "category": category,
            "vacant": False,
            "zone_code": f"{floor}-Z1",
        },
    )


def _link(floor: str, link_id: str, link_type: str, geometry) -> Entity:
    return Entity(
        f"V-{floor}-{link_id}",
        EntityKind.VERTICAL_LINK,
        floor,
        geometry,
        attributes={"link_id": link_id, "link_type": link_type},
    )


def _ring(model: SpatialModel, floor: str) -> None:
    """Ring walkway around the island x 4..44, y 12..20 (corridors 4 m wide)."""
    for key, geometry in (
        ("S", box(0, 8, 48, 12)),
        ("N", box(0, 20, 48, 24)),
        ("W", box(0, 12, 4, 20)),
        ("E", box(44, 12, 48, 20)),
    ):
        model.add(Entity(f"W-{floor}-{key}", EntityKind.WALKWAY, floor, geometry))


def ring_model() -> SpatialModel:
    """1F: 12 units around a ring walkway with an island escalator and an entrance; 2F: 3 units incl. one on
    an isolated walkway and one 0.08 m away from its neighbor; 3F: only elevators."""
    model = SpatialModel(
        revision=Revision("R0"),
        floors=[
            Floor("1F", 0, elevation_m=0.0),
            Floor("2F", 1, elevation_m=5.0),
            Floor("3F", 2, elevation_m=10.0),
        ],
    )
    _ring(model, "1F")
    for i in range(4):
        model.add(_unit("1F", f"1F-S0{i + 1}", box(12 * i, 0, 12 * (i + 1), 8)))
    for code, x0, x1 in (("1F-C01", 4, 14), ("1F-C02", 14, 22), ("1F-C03", 26, 34), ("1F-C04", 34, 44)):
        model.add(_unit("1F", code, box(x0, 12, x1, 20)))
    for code, x0, x1 in (("1F-N01", 0, 12), ("1F-N02", 12, 24), ("1F-N03", 24, 36), ("1F-N04", 36.03, 44)):
        model.add(_unit("1F", code, box(x0, 24, x1, 32)))
    model.add(_link("1F", "ESC-A", "escalator", box(22, 12, 26, 20)))
    model.add(_link("1F", "EV-1", "elevator", box(44, 24, 48, 28)))
    model.add(_link("1F", "EV-2", "elevator", box(44, 28, 48, 32)))
    model.add(
        Entity(
            "O-1F-ENT-1",
            EntityKind.OPENING,
            "1F",
            LineString([(0, 9), (0, 11)]),
            attributes={"opening_id": "ENT-1", "opening_type": "entrance"},
        )
    )
    _ring(model, "2F")
    model.add(Entity("W-2F-X", EntityKind.WALKWAY, "2F", box(0, 26, 6, 30)))
    model.add(_unit("2F", "2F-S01", box(0, 0, 12, 8)))
    model.add(_unit("2F", "2F-S02", box(12.08, 0, 24, 8)))
    model.add(_unit("2F", "2F-P01", box(6, 26, 12, 30)))
    model.add(_link("2F", "ESC-A", "escalator", box(22, 12, 26, 20)))
    model.add(_link("2F", "EV-1", "elevator", box(44, 24, 48, 28)))
    model.add(_link("3F", "EV-1", "elevator", box(44, 24, 48, 28)))
    model.add(_link("3F", "EV-2", "elevator", box(44, 28, 48, 32)))
    return model


@pytest.fixture(scope="module")
def model() -> SpatialModel:
    return ring_model()


@pytest.fixture(scope="module")
def features(model: SpatialModel):
    return unit_features(model).set_index("unit_code")


def test_shared_length_ignores_corner_slivers_and_respects_tolerance():
    unit = box(12, 0, 24, 8)
    corridor = box(0, 8, 48, 12)
    assert shared_length(unit, corridor) == pytest.approx(12.0)
    assert shared_length(box(12, 0, 24, 7.97), corridor) == pytest.approx(12.0)
    assert shared_length(box(12, 0, 24, 7.9), corridor) == 0.0
    assert shared_length(box(0, 0, 10, 10), box(10, 10, 20, 20)) == 0.0


def test_walkway_grid_graph_nodes_edges_and_hole(model: SpatialModel):
    graph = walkway_grid_graph(model, "1F")

    assert graph.number_of_nodes() == 448  # 48x16 ring minus the 40x8 island, one node per m2
    assert graph[(0, 8)][(1, 8)]["weight"] == pytest.approx(1.0)
    assert graph[(0, 8)][(1, 9)]["weight"] == pytest.approx(SQRT2)
    assert graph.nodes[(5, 8)]["pos"] == (5.5, 8.5)
    assert not graph.has_node((10, 15))
    around = nx.shortest_path_length(graph, (10, 11), (10, 20), weight="weight")
    assert around > 9 + 4  # must walk around the island, not through it
    assert walkway_grid_graph(model, "3F").number_of_nodes() == 0


def test_grid_does_not_connect_walkways_across_a_thin_gap():
    model = SpatialModel(revision=Revision("T"))
    model.add(Entity("W-A", EntityKind.WALKWAY, "1F", box(0, 0, 10, 1)))
    model.add(Entity("W-B", EntityKind.WALKWAY, "1F", box(0, 1.2, 10, 2.2)))

    graph = walkway_grid_graph(model, "1F")

    assert graph.number_of_nodes() == 20
    assert nx.number_connected_components(graph) == 2


def test_multi_source_dijkstra_labels_nearest_source():
    graph = nx.path_graph(6)
    nx.set_edge_attributes(graph, 1.0, "weight")

    dist, label = multi_source_dijkstra(graph, {0: "A", 5: "B"})

    assert dist == {0: 0, 1: 1, 2: 2, 3: 2, 4: 1, 5: 0}
    assert label[2] == "A" and label[3] == "B"


def test_access_points_follow_longest_shared_segment(model: SpatialModel):
    network = build_floor_network(model, "1F")
    walkable = network.walkable

    assert access_point(model.entities["U-1F-S02"].geometry, walkable).coords[0] == (18.0, 8.0)
    assert access_point(model.entities["U-1F-C01"].geometry, walkable).coords[0] == (9.0, 12.0)
    assert access_point(model.entities["U-1F-N04"].geometry, walkable).coords[0] == pytest.approx(
        (40.015, 24.0)
    )
    escalator = [p.coords[0] for p in access_points(model.entities["V-1F-ESC-A"].geometry, walkable)]
    assert escalator == [(24.0, 12.0), (24.0, 20.0)]
    assert network.access_points["O-1F-ENT-1"].coords[0] == (0.0, 10.0)
    assert network.access_nodes["V-1F-ESC-A"] == ("AP:V-1F-ESC-A", "AP:V-1F-ESC-A#2")
    inner = box(10, 14, 12, 18)  # touches no walkway: nearest boundary point to the walkways
    fallback = access_point(inner, walkable)
    assert inner.boundary.distance(fallback) < 1e-9
    assert fallback.distance(walkable) == pytest.approx(2.0)
    assert network.walk_distance("U-1F-C02", "V-1F-ESC-A") == pytest.approx(6 + SQRT2, abs=1e-6)


def test_unit_features_frontage_and_area(features):
    assert len(features) == 15
    assert features.loc["1F-S02", "frontage_m"] == pytest.approx(12.0)
    assert features.loc["1F-C01", "frontage_m"] == pytest.approx(28.0)  # 10 south + 10 north + 8 west
    assert features.loc["1F-C02", "frontage_m"] == pytest.approx(16.0)
    assert features.loc["1F-N04", "frontage_m"] == pytest.approx(7.97)
    assert features.loc["2F-S02", "frontage_m"] == pytest.approx(11.92)
    assert features.loc["2F-P01", "frontage_m"] == pytest.approx(4.0)
    assert features.loc["1F-S01", "area_m2"] == pytest.approx(96.0)
    assert features.loc["1F-C02", ["centroid_x", "centroid_y"]].tolist() == [18.0, 16.0]
    assert features.loc["1F-S01", "floor"] == "1F" and not features.loc["1F-S01", "vacant"]


def test_unit_features_walking_distances_match_hand_calculation(features):
    # access point -> nearest cell center (half diagonal) -> octile grid path -> escalator access point
    expected = {
        "1F-C02": HALF_DIAG + octile(6, 0) + HALF_DIAG,
        "1F-S02": HALF_DIAG + octile(6, 3) + HALF_DIAG,
        "1F-N02": HALF_DIAG + octile(6, 3) + HALF_DIAG,  # reaches the north landing of the escalator
        "1F-N04": math.hypot(0.485, 0.5) + octile(17, 3) + HALF_DIAG,
    }
    for code, value in expected.items():
        assert features.loc[code, "walk_to_escalator_m"] == pytest.approx(value, abs=1e-3), code
        assert features.loc[code, "nearest_escalator_id"] == "V-1F-ESC-A"
        assert not features.loc[code, "escalator_fallback"]
    assert features.loc["1F-S01", "walk_to_entrance_m"] == pytest.approx(
        HALF_DIAG + octile(5, 1) + HALF_DIAG, abs=1e-3
    )
    assert features.loc["1F-S01", "nearest_entrance_id"] == "O-1F-ENT-1"


def test_unit_features_fallbacks(features):
    isolated = features.loc["2F-P01"]
    assert isolated["escalator_fallback"]
    assert isolated["walk_to_escalator_m"] == pytest.approx(
        math.hypot(18, 8), abs=1e-3
    )  # straight to (24, 20)
    assert math.isnan(features.loc["2F-S01", "walk_to_entrance_m"])  # no opening on 2F
    assert not features.loc["2F-S01", "entrance_fallback"]


def test_adjacency_pairs_with_tolerance(model: SpatialModel):
    pairs = adjacency_pairs(model)

    keyed = {(r.unit_a, r.unit_b): r.shared_length_m for r in pairs.itertuples()}
    assert len(keyed) == 8
    assert keyed[("1F-S01", "1F-S02")] == pytest.approx(8.0)
    assert keyed[("1F-N03", "1F-N04")] == pytest.approx(8.0)  # 0.03 m gap is within tolerance
    assert ("1F-C02", "1F-C03") not in keyed  # separated by the escalator
    assert ("2F-S01", "2F-S02") not in keyed  # 0.08 m gap exceeds tolerance
    assert set(pairs["floor"]) == {"1F"}
    assert len(adjacency_pairs(model, min_shared_m=9.0)) == 0


def test_compute_relations_types_and_metrics(model: SpatialModel):
    relations = compute_relations(model)
    by_type: dict[RelationType, list] = {}
    for relation in relations:
        by_type.setdefault(relation.type, []).append(relation)

    assert len(by_type[RelationType.ON_FLOOR]) == len(model.entities) == 32
    assert len(by_type[RelationType.ADJACENT_TO]) == 8
    faces = {(r.from_id, r.to_id): r.metrics["frontage_m"] for r in by_type[RelationType.FACES]}
    assert len(faces) == 21
    assert faces[("U-1F-C01", "W-1F-W")] == pytest.approx(8.0)
    assert faces[("U-2F-P01", "W-2F-X")] == pytest.approx(4.0)
    near = by_type[RelationType.NEAR]
    assert len(near) == 15 + 12  # every unit -> escalator; 1F units -> entrance
    p01 = next(r for r in near if r.from_id == "U-2F-P01")
    assert p01.to_id == "V-2F-ESC-A" and p01.metrics["euclidean_fallback"] == 1.0
    connected = {(r.from_id, r.to_id) for r in by_type[RelationType.CONNECTED_TO]}
    assert connected == {
        ("V-1F-ESC-A", "V-2F-ESC-A"),
        ("V-1F-EV-1", "V-2F-EV-1"),
        ("V-2F-EV-1", "V-3F-EV-1"),
    }  # EV-2 skips 2F, so 1F and 3F are not consecutive
    assert all(r.metrics["vertical_m"] == 5.0 for r in by_type[RelationType.CONNECTED_TO])
    assert floor_codes(model) == ["1F", "2F", "3F"]


def _store_model(n_floors: int = 9) -> SpatialModel:
    """Synthetic store: 9 floors x 46 units x 6 vertical links on a 120 m x 80 m plate."""
    codes = ["B1", "1F", "2F", "3F", "4F", "5F", "6F", "7F", "8F"][:n_floors]
    model = SpatialModel(revision=Revision("PERF"), floors=[Floor(c, i - 1) for i, c in enumerate(codes)])
    links = {
        "ESC-A": (30, 34),
        "ESC-B": (86, 90),
        "EV-1": (10, 13),
        "EV-2": (107, 110),
        "ST-1": (50, 53),
        "ST-2": (67, 70),
    }
    for floor in codes:
        for key, geometry in (
            ("S", box(0, 12, 120, 18)),
            ("N", box(0, 62, 120, 68)),
            ("W", box(0, 18, 6, 62)),
            ("E", box(114, 18, 120, 62)),
            ("M", box(6, 37, 114, 43)),
        ):
            model.add(Entity(f"W-{floor}-{key}", EntityKind.WALKWAY, floor, geometry))
        for i in range(12):
            model.add(_unit(floor, f"{floor}-S{i + 1:02d}", box(10 * i, 0, 10 * (i + 1), 12)))
            model.add(_unit(floor, f"{floor}-N{i + 1:02d}", box(10 * i, 68, 10 * (i + 1), 80)))
        width = 108 / 11
        for i in range(11):
            model.add(_unit(floor, f"{floor}-C{i + 1:02d}", box(6 + width * i, 18, 6 + width * (i + 1), 37)))
            model.add(_unit(floor, f"{floor}-D{i + 1:02d}", box(6 + width * i, 43, 6 + width * (i + 1), 62)))
        for link_id, (x0, x1) in links.items():
            link_type = (
                "escalator"
                if link_id.startswith("ESC")
                else "elevator"
                if link_id.startswith("EV")
                else "stair"
            )
            model.add(_link(floor, link_id, link_type, box(x0, 38, x1, 42)))
    return model


def test_full_store_runs_in_seconds():
    model = _store_model()
    started = time.perf_counter()
    features = unit_features(model)
    relations = compute_relations(model)
    elapsed = time.perf_counter() - started

    assert len(features) == 9 * 46
    assert not features["escalator_fallback"].any()
    assert sum(r.type == RelationType.CONNECTED_TO for r in relations) == 8 * 6
    assert elapsed < 20.0
