"""Tests for the synthetic drawing generator (``cad2anything.mockgen``)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import ezdxf
import pytest
from shapely.ops import unary_union
from shapely.strtree import STRtree

from cad2anything.catalog import CATEGORIES, FLOOR_CODES, VERTICAL_LINKS
from cad2anything.mockgen.brands import generate_brands, is_blocked_name, normalize_name
from cad2anything.mockgen.drawings import VARIANTS, build_all, generate_all, to_json, write_samples
from cad2anything.mockgen.dxf_writer import CLEAN_LAYERS, MESSY_BLOCK_NAMES, messy_layer_names
from cad2anything.mockgen.layout import FIXTURE_CLEARANCE_M, FIXTURE_GAP_M, PLANTED_LOW_EFFICIENCY
from cad2anything.model import ChangeSet, ChangeType, EntityKind, RevisionKind, SpatialModel

SEED = 42
HANGUL = re.compile(r"[\uac00-\ud7a3]")
PAIRS = {"R0_R1": ("R0", "R1"), "R1_R2": ("R1", "R2"), "R2_H1": ("R2", "H1")}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("generated")
    return out, generate_all(out, seed=SEED)


@pytest.fixture(scope="module")
def models(generated: tuple[Path, dict]) -> dict[str, SpatialModel]:
    out, _ = generated
    return {
        rev: SpatialModel.from_dict(
            json.loads((out / "truth" / f"layout_{rev}.json").read_text(encoding="utf-8"))
        )
        for rev in VARIANTS
    }


@pytest.fixture(scope="module")
def changes(generated: tuple[Path, dict]) -> dict[str, ChangeSet]:
    out, _ = generated
    result = {}
    for path in sorted((out / "truth").glob("changes_*.json")):
        change_set = ChangeSet.from_dict(json.loads(path.read_text(encoding="utf-8")))
        result[f"{change_set.rev_from}_{change_set.rev_to}"] = change_set
    return result


def test_same_seed_gives_identical_truth(generated: tuple[Path, dict]) -> None:
    out, _ = generated
    _, models, changes = build_all(SEED)
    for rev, model in models.items():
        assert to_json(model.to_dict()) == (out / "truth" / f"layout_{rev}.json").read_text(encoding="utf-8")
    for cs in changes.values():
        path = out / "truth" / f"changes_{cs.rev_from}_{cs.rev_to}.json"
        assert to_json(cs.to_dict()) == path.read_text(encoding="utf-8")


def test_brands_are_unique_fictional_and_sufficient(models: dict[str, SpatialModel]) -> None:
    brands = generate_brands(SEED)
    assert [b.brand_id for b in brands] == [f"BR{i:04d}" for i in range(1, len(brands) + 1)]
    assert len({normalize_name(b.brand_name) for b in brands}) == len(brands)
    assert not any(is_blocked_name(b.brand_name) for b in brands)
    per_category = defaultdict(int)
    for brand in brands:
        per_category[brand.category] += 1
        assert 1 <= brand.price_tier <= 3 and 0.6 <= brand.attractiveness <= 1.6
    max_units = defaultdict(int)
    for model in models.values():
        counts = defaultdict(int)
        for unit in model.units():
            counts[unit.attributes["category"]] += 1
        for category, n in counts.items():
            max_units[category] = max(max_units[category], n)
    for category in CATEGORIES:
        assert per_category[category] >= max_units[category] * 1.4


def test_unit_counts_and_vacancies(models: dict[str, SpatialModel]) -> None:
    r0 = models["R0"]
    assert 300 <= len(r0.units()) <= 420
    assert all(len(r0.units(floor)) >= 20 for floor in FLOOR_CODES)
    vacant = [u for u in r0.units() if u.attributes["vacant"]]
    assert 1 <= len(vacant) <= 12
    assert all(u.attributes["brand_id"] is None and u.attributes["brand_name"] is None for u in vacant)
    codes = [u.attributes["unit_code"] for u in r0.units()]
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"(B1|[1-8]F)-[SNWEC]\d{2}", code) for code in codes)


@pytest.mark.parametrize("rev", list(VARIANTS))
def test_spaces_are_valid_and_do_not_overlap(models: dict[str, SpatialModel], rev: str) -> None:
    model = models[rev]
    for floor in FLOOR_CODES:
        exterior = model.entities[f"X-{floor}"].geometry
        spaces = (
            model.units(floor)
            + model.by_kind(EntityKind.WALKWAY, floor)
            + model.by_kind(EntityKind.VERTICAL_LINK, floor)
            + model.by_kind(EntityKind.AMENITY, floor)
        )
        tree = STRtree([s.geometry for s in spaces])
        for i, space in enumerate(spaces):
            assert space.geometry.is_valid and space.geometry.area > 0, space.entity_id
            assert exterior.covers(space.geometry), space.entity_id
            for j in tree.query(space.geometry):
                if j > i:
                    overlap = space.geometry.intersection(spaces[j].geometry).area
                    assert overlap < 1e-6, (space.entity_id, spaces[j].entity_id, overlap)


@pytest.mark.parametrize("rev", list(VARIANTS))
def test_walkway_connected_and_frontage(models: dict[str, SpatialModel], rev: str) -> None:
    model = models[rev]
    for floor in FLOOR_CODES:
        walkway = unary_union([w.geometry for w in model.by_kind(EntityKind.WALKWAY, floor)])
        assert walkway.geom_type == "Polygon", floor
        touch = walkway.buffer(1e-6)
        for unit in model.units(floor):
            frontage = unit.geometry.exterior.intersection(touch).length
            assert frontage >= 2.0, unit.entity_id
            assert abs(frontage - unit.attributes["frontage_m"]) < 0.05
        for space in model.by_kind(EntityKind.VERTICAL_LINK, floor) + model.by_kind(
            EntityKind.AMENITY, floor
        ):
            assert space.geometry.exterior.intersection(touch).length >= 2.0, space.entity_id


@pytest.mark.parametrize("rev", list(VARIANTS))
def test_fixtures_inside_units_with_clearance(models: dict[str, SpatialModel], rev: str) -> None:
    model = models[rev]
    units = {u.attributes["unit_code"]: u for u in model.units()}
    by_unit = defaultdict(list)
    for fixture in model.by_kind(EntityKind.FIXTURE):
        by_unit[fixture.attributes["unit_code"]].append(fixture)
    for code, fixtures in by_unit.items():
        unit = units[code]
        assert not unit.attributes["vacant"]
        for f in fixtures:
            assert f.entity_id == f"F-{f.attributes['fixture_id']}" and f.attributes["fixture_id"].startswith(
                code + "-F"
            )
            assert f.attributes["fixture_type"] in CATEGORIES[unit.attributes["category"]].fixture_types
            assert unit.geometry.contains(f.geometry), f.entity_id
            assert f.geometry.distance(unit.geometry.exterior) >= FIXTURE_CLEARANCE_M - 1e-6, f.entity_id
        for i, a in enumerate(fixtures):
            for b in fixtures[i + 1 :]:
                assert a.geometry.distance(b.geometry) >= FIXTURE_GAP_M - 1e-6, (a.entity_id, b.entity_id)
    occupied = {code for code, u in units.items() if not u.attributes["vacant"]}
    assert occupied == set(by_unit)


def test_vertical_links_identical_across_floors(models: dict[str, SpatialModel]) -> None:
    for model in models.values():
        for link_id in VERTICAL_LINKS:
            shapes = {model.entities[f"V-{floor}-{link_id}"].geometry.wkt for floor in FLOOR_CODES}
            assert len(shapes) == 1, link_id


def test_zones_and_relations(models: dict[str, SpatialModel]) -> None:
    model = models["R0"]
    for floor in FLOOR_CODES:
        zones = model.by_kind(EntityKind.ZONE, floor)
        assert zones and all(re.fullmatch(rf"{floor}-Z\d+", z.attributes["zone_code"]) for z in zones)
        assert sum(z.attributes["unit_count"] for z in zones) == len(model.units(floor))
    contains = {(r.from_id, r.to_id) for r in model.relations if r.type == "contains"}
    on_floor = {r.from_id: r.to_id for r in model.relations if r.type == "on_floor"}
    for unit in model.units():
        assert (f"Z-{unit.attributes['zone_code']}", unit.entity_id) in contains
    for fixture in model.by_kind(EntityKind.FIXTURE):
        assert (f"U-{fixture.attributes['unit_code']}", fixture.entity_id) in contains
    assert all(on_floor[e.entity_id] == e.floor for e in model.entities.values())


def test_change_sets_cover_all_change_types(
    changes: dict[str, ChangeSet], models: dict[str, SpatialModel]
) -> None:
    assert set(changes) == set(PAIRS)
    covered = {item.change_type for key in ("R0_R1", "R1_R2") for item in changes[key].items}
    assert covered == set(ChangeType)
    assert sorted(str(i.change_type) for i in changes["R2_H1"].items) == ["move", "move", "resize"]
    assert models["H1"].revision.kind == RevisionKind.HYPOTHETICAL
    assert models["H1"].revision.parent_rev_id == "R2"
    assert [models[r].revision.effective_date for r in ("R0", "R1", "R2")] == [
        "2024-10-01",
        "2025-03-01",
        "2025-09-01",
    ]


def test_change_items_match_models(changes: dict[str, ChangeSet], models: dict[str, SpatialModel]) -> None:
    for key, change_set in changes.items():
        before, after = (models[r] for r in PAIRS[key])
        for item in change_set.items:
            assert item.summary and HANGUL.search(item.summary)
            assert all(before.unit_by_code(c) is not None for c in item.unit_codes_before)
            assert all(after.unit_by_code(c) is not None for c in item.unit_codes_after)
            assert all(e in before.entities for e in item.entity_ids_before)
            assert all(e in after.entities for e in item.entity_ids_after)
            if item.change_type == ChangeType.MOVE:
                brand = item.brand_ids_before[0]
                assert before.unit_by_code(item.unit_codes_before[0]).attributes["brand_id"] == brand
                assert after.unit_by_code(item.unit_codes_after[0]).attributes["brand_id"] == brand
            if item.change_type in (ChangeType.MERGE, ChangeType.SPLIT, ChangeType.REMOVE):
                assert not any(e in after.entities for e in item.entity_ids_before)
            if item.change_type == ChangeType.MERGE:
                assert item.unit_codes_after[0] == item.unit_codes_before[0] + "M"
            if item.change_type == ChangeType.SPLIT:
                assert item.unit_codes_after == [item.unit_codes_before[0] + s for s in "AB"]
            if item.change_type == ChangeType.ADD:
                assert all(e not in before.entities for e in item.entity_ids_after)


def test_unchanged_units_keep_ids_geometry_and_brand(
    changes: dict[str, ChangeSet], models: dict[str, SpatialModel]
) -> None:
    for key, change_set in changes.items():
        before, after = (models[r] for r in PAIRS[key])
        changed = {c for item in change_set.items for c in item.unit_codes_before + item.unit_codes_after}
        for unit in before.units():
            if unit.attributes["unit_code"] in changed:
                continue
            same = after.entities.get(unit.entity_id)
            assert same is not None, unit.entity_id
            assert (
                same.geometry.equals(unit.geometry)
                and same.attributes["brand_id"] == unit.attributes["brand_id"]
            )


def test_planted_low_efficiency_zone_unchanged(models: dict[str, SpatialModel]) -> None:
    floor, band = PLANTED_LOW_EFFICIENCY["floor"], PLANTED_LOW_EFFICIENCY["band"]
    snapshots = []
    for rev in ("R0", "R1", "R2"):
        units = sorted(
            (u for u in models[rev].units(floor) if u.attributes["band"] == band), key=lambda u: u.entity_id
        )
        snapshots.append([(u.entity_id, u.geometry.wkt, u.attributes["brand_id"]) for u in units])
    assert snapshots[0] and snapshots[0] == snapshots[1] == snapshots[2]


def test_manifests(generated: tuple[Path, dict]) -> None:
    out, summary = generated
    for rev, variants in VARIANTS.items():
        for variant in variants:
            folder = out / "drawings" / rev / variant
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["revision"] == rev and manifest["variant"] == variant
            assert [level["floor"] for level in manifest["levels"]] == FLOOR_CODES
            assert all((folder / level["file"]).is_file() for level in manifest["levels"])
            assert manifest["units"] == ("m" if variant == "clean" else "mm")
            assert ("layers" in manifest) == (variant == "clean")
    assert not (out / "drawings" / "H1" / "messy").exists()
    r0 = summary["revisions"]["R0"]
    assert r0["units_total"] == sum(r0["units_per_floor"].values())
    assert set(summary["changes"]) == set(PAIRS)


def test_clean_dxf(generated: tuple[Path, dict], models: dict[str, SpatialModel]) -> None:
    out, _ = generated
    for rev, floor in (("R0", "3F"), ("R1", "4F"), ("H1", "3F")):
        doc = ezdxf.readfile(out / "drawings" / rev / "clean" / f"flagship_{rev}_{floor}.dxf")
        assert doc.dxfversion == "AC1032" and doc.header["$INSUNITS"] == 6
        assert set(CLEAN_LAYERS.values()) <= {layer.dxf.name for layer in doc.layers}
        msp = doc.modelspace()
        labels = [m.text for m in msp.query('MTEXT[layer=="A-UNIT-IDEN"]')]
        units = models[rev].units(floor)
        assert len(labels) == len(units)
        assert all(re.fullmatch(rf"{floor}-[SNWECP]\d{{2}}[ABM]?\\P.+", text) for text in labels)
        assert len(msp.query('LWPOLYLINE[layer=="A-UNIT"]')) == len(units)
        inserts = msp.query('INSERT[layer=="A-FIXT"]')
        assert len(inserts) == len(models[rev].by_kind(EntityKind.FIXTURE, floor))
        tags = {a.dxf.tag for a in inserts.first.attribs}
        assert inserts.first.dxf.name.startswith("FX_") and tags == {"FIXTURE_ID", "TYPE", "W", "D"}


def test_messy_dxf(generated: tuple[Path, dict], models: dict[str, SpatialModel]) -> None:
    out, _ = generated
    styles = {"closed": 0, "open": 0, "lines": 0}
    for floor in FLOOR_CODES:
        doc = ezdxf.readfile(out / "drawings" / "R0" / "messy" / f"flagship_R0_{floor}.dxf")
        assert doc.dxfversion == "AC1015"
        assert doc.header["$DWGCODEPAGE"] == "ANSI_949" and doc.header["$INSUNITS"] == 4
        msp = doc.modelspace()
        texts = [t.dxf.text for t in msp.query("TEXT")]
        assert any(HANGUL.search(t) for t in texts) and any(
            re.match(rf"{floor}-[SNWEC]\d{{2}}", t) for t in texts
        )
        assert not msp.query("MTEXT")
        xmax = max(max(x for x, *_ in p.get_points()) for p in msp.query("LWPOLYLINE"))
        assert xmax == pytest.approx(120_000)
        inserts = msp.query("INSERT")
        assert {i.dxf.name for i in inserts} <= set(MESSY_BLOCK_NAMES.values())
        assert any(not i.attribs for i in inserts) and any(i.attribs for i in inserts)
        unit_layer = messy_layer_names(floor, SEED)["unit"]
        for polyline in msp.query(f'LWPOLYLINE[layer=="{unit_layer}"]'):
            if polyline.closed:
                styles["closed"] += 1
                continue
            styles["open"] += 1
            points = [(x, y) for x, y, *_ in polyline.get_points()]
            gap = ((points[0][0] - points[-1][0]) ** 2 + (points[0][1] - points[-1][1]) ** 2) ** 0.5
            assert 10 - 1e-6 <= gap <= 40 + 1e-6
        styles["lines"] += len(msp.query(f'LINE[layer=="{unit_layer}"]')) // 4
    total = len(models["R0"].units())
    assert sum(styles.values()) == total
    assert 0.55 <= styles["closed"] / total <= 0.85 and styles["open"] > 0 and styles["lines"] > 0


def test_samples(tmp_path: Path, generated: tuple[Path, dict]) -> None:
    out, _ = generated
    files = write_samples(tmp_path, seed=SEED)
    assert len(files) == 4
    for variant in ("clean", "messy"):
        folder = tmp_path / "drawings" / "R0" / variant
        assert sorted(p.name for p in folder.iterdir()) == ["flagship_R0_3F.dxf", "manifest.json"]
        dxf = folder / "flagship_R0_3F.dxf"
        assert dxf.stat().st_size < 500_000
        assert dxf.read_bytes() == (out / "drawings" / "R0" / variant / "flagship_R0_3F.dxf").read_bytes()
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert [level["floor"] for level in manifest["levels"]] == ["3F"]


def test_dxf_bytes_identical_across_processes(tmp_path: Path) -> None:
    script = "import sys; from pathlib import Path; from cad2anything.mockgen.drawings import write_samples; "
    script += f"write_samples(Path(sys.argv[1]), seed={SEED})"
    for run, hash_seed in enumerate(("1", "2")):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        subprocess.run([sys.executable, "-c", script, str(tmp_path / f"run{run}")], check=True, env=env)
    for variant in ("clean", "messy"):
        first, second = (
            (tmp_path / run / "drawings" / "R0" / variant / "flagship_R0_3F.dxf").read_bytes()
            for run in ("run0", "run1")
        )
        assert first == second, variant
