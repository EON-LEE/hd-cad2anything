"""Tests for revision change detection (``cad2anything.revision.diff``)."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import box

from cad2anything.cadcore import parse_revision
from cad2anything.mockgen import brands_to_rows
from cad2anything.mockgen.drawings import build_all
from cad2anything.mockgen.dxf_writer import write_revision_drawings
from cad2anything.model import (
    ChangeItem,
    ChangeSet,
    ChangeType,
    Entity,
    EntityKind,
    Floor,
    Revision,
    SpatialModel,
)
from cad2anything.revision.diff import diff_models, evaluate_diff, items_match
from cad2anything.revision.diff import main as diff_main

SEED = 42
HANGUL = re.compile(r"[\uac00-\ud7a3]")
SHARED_REVISIONS = ("R0", "R1", "R2")
PAIRS = (("R0", "R1"), ("R1", "R2"))


# --- shared synthetic data (same layout and cache as test_cadcore.py) --------------------------------


def shared_root(factory: pytest.TempPathFactory) -> Path:
    root = factory.getbasetemp() / f"cad2anything-shared-seed{SEED}"
    marker = root / "complete.txt"
    if marker.exists():
        return root
    brands, models, changes = build_all(SEED)
    for rev in SHARED_REVISIONS:
        for variant in ("clean", "messy"):
            write_revision_drawings(models[rev], root / "drawings" / rev / variant, variant, SEED)
        (root / "truth").mkdir(parents=True, exist_ok=True)
        (root / "truth" / f"layout_{rev}.json").write_text(
            json.dumps(models[rev].to_dict()), encoding="utf-8"
        )
    for change_set in changes.values():
        name = f"changes_{change_set.rev_from}_{change_set.rev_to}.json"
        (root / "truth" / name).write_text(json.dumps(change_set.to_dict()), encoding="utf-8")
    (root / "brands.json").write_text(json.dumps(brands_to_rows(brands)), encoding="utf-8")
    marker.write_text("ok", encoding="utf-8")
    return root


def parsed_revision(root: Path, rev: str, variant: str) -> tuple[SpatialModel, dict[str, Any]]:
    model_path = root / "parsed" / f"{rev}_{variant}.json"
    report_path = root / "parsed" / f"{rev}_{variant}.report.json"
    if model_path.exists() and report_path.exists():
        model = SpatialModel.from_dict(json.loads(model_path.read_text(encoding="utf-8")))
        return model, json.loads(report_path.read_text(encoding="utf-8"))
    brands = json.loads((root / "brands.json").read_text(encoding="utf-8"))
    model, report = parse_revision(root / "drawings" / rev / variant, brands, relations="basic")
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps(model.to_dict(), ensure_ascii=False), encoding="utf-8")
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")
    return model, report.to_dict()


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return shared_root(tmp_path_factory)


@pytest.fixture(scope="module")
def clean_models(root: Path) -> dict[str, SpatialModel]:
    return {rev: parsed_revision(root, rev, "clean")[0] for rev in SHARED_REVISIONS}


@pytest.fixture(scope="module")
def truth_changes(root: Path) -> dict[str, ChangeSet]:
    result = {}
    for a, b in PAIRS:
        data = json.loads((root / "truth" / f"changes_{a}_{b}.json").read_text(encoding="utf-8"))
        result[f"{a}_{b}"] = ChangeSet.from_dict(data)
    return result


def _table(metrics: dict[str, Any]) -> str:
    rows = [
        f"{t:15} pred={r['predicted']:2} truth={r['truth']:2} P={r['precision']} R={r['recall']}"
        for t, r in metrics["per_type"].items()
    ]
    return "\n".join([f"{metrics['rev_from']}->{metrics['rev_to']} overall={metrics['overall']}", *rows])


# --- parsed drawings vs truth ------------------------------------------------------------------------


def test_clean_diffs_match_truth(
    clean_models: dict[str, SpatialModel], truth_changes: dict[str, ChangeSet]
) -> None:
    detected: set[str] = set()
    for a, b in PAIRS:
        predicted = diff_models(clean_models[a], clean_models[b])
        truth = truth_changes[f"{a}_{b}"]
        metrics = evaluate_diff(predicted, truth)
        print(_table(metrics))
        assert (predicted.rev_from, predicted.rev_to) == (a, b)
        assert metrics["overall"]["recall"] >= 0.9, metrics["missed"]
        assert metrics["overall"]["precision"] >= 0.9, metrics["false_positives"]
        detected |= set(metrics["types_detected"])
        changed = {c for item in truth.items for c in item.unit_codes_before + item.unit_codes_after}
        for item in predicted.items:
            assert item.summary and HANGUL.search(item.summary), item
            if item.change_type in (ChangeType.MOVE, ChangeType.REASSIGN):
                assert set(item.unit_codes_before + item.unit_codes_after) <= changed, item.summary
    assert detected == {str(t) for t in ChangeType}


def test_clean_change_items_carry_ids_and_details(
    clean_models: dict[str, SpatialModel], truth_changes: dict[str, ChangeSet]
) -> None:
    predicted = diff_models(clean_models["R0"], clean_models["R1"])
    truth = truth_changes["R0_R1"]
    for expected in truth.items:
        if expected.change_type == ChangeType.WALKWAY_CHANGE:
            continue
        match = [p for p in predicted.items if items_match(p, expected)]
        assert len(match) == 1, expected.summary
        item = match[0]
        assert item.unit_codes_before == expected.unit_codes_before
        assert item.unit_codes_after == expected.unit_codes_after
        assert item.entity_ids_before == expected.entity_ids_before
        assert item.entity_ids_after == expected.entity_ids_after
        if expected.change_type in (ChangeType.MOVE, ChangeType.REASSIGN, ChangeType.MERGE, ChangeType.SPLIT):
            assert item.brand_ids_before == expected.brand_ids_before
            assert item.brand_ids_after == expected.brand_ids_after
        for key in ("area_before_m2", "area_after_m2", "fixture_count_before", "fixture_count_after"):
            if key in expected.details and key in item.details:
                assert item.details[key] == pytest.approx(expected.details[key], abs=0.05), (
                    key,
                    expected.summary,
                )
    walkway = [p for p in predicted.items if p.change_type == ChangeType.WALKWAY_CHANGE]
    assert sorted(p.floor for p in walkway) == ["4F", "4F", "5F"]
    assert all(p.details["added_m2"] + p.details["removed_m2"] > 1.0 for p in walkway)


def test_messy_diff_r0_r1(root: Path, truth_changes: dict[str, ChangeSet]) -> None:
    before, _ = parsed_revision(root, "R0", "messy")
    after, _ = parsed_revision(root, "R1", "messy")
    metrics = evaluate_diff(diff_models(before, after), truth_changes["R0_R1"])
    print(_table(metrics))
    assert metrics["overall"]["recall"] >= 0.8, metrics["missed"]


def test_identical_models_have_no_changes(clean_models: dict[str, SpatialModel]) -> None:
    assert diff_models(clean_models["R1"], clean_models["R1"]).items == []


def test_diff_cli(tmp_path: Path, root: Path, clean_models: dict[str, SpatialModel]) -> None:
    paths = {}
    for rev in ("R1", "R2"):
        paths[rev] = tmp_path / f"parsed_{rev}.json"
        paths[rev].write_text(json.dumps(clean_models[rev].to_dict(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "changes.json"
    truth = root / "truth" / "changes_R1_R2.json"
    args = [
        "--before",
        str(paths["R1"]),
        "--after",
        str(paths["R2"]),
        "--out",
        str(out),
        "--truth",
        str(truth),
    ]
    assert diff_main(args) == 0
    changes = ChangeSet.from_dict(json.loads(out.read_text(encoding="utf-8")))
    assert (changes.rev_from, changes.rev_to) == ("R1", "R2") and changes.items


# --- handcrafted models ------------------------------------------------------------------------------

Rect = tuple[float, float, float, float]


def _model(
    rev: str,
    units: dict[str, tuple[Rect, str | None]],
    fixtures: dict[str, list[tuple[str, float, float]]] | None = None,
    walkways: tuple[Rect, ...] = ((0, 10, 90, 14),),
    holes: tuple[Rect, ...] = (),
) -> SpatialModel:
    model = SpatialModel(Revision(rev), floors=[Floor("1F", 0)])
    for number, rect in enumerate(walkways, 1):
        geometry = box(*rect)
        for hole in holes:
            geometry = geometry.difference(box(*hole))
        model.add(Entity(f"W-1F-{number}", EntityKind.WALKWAY, "1F", geometry))
    for code, (rect, brand) in units.items():
        attributes = {"unit_code": code, "brand_name": brand, "brand_id": f"ID-{brand}" if brand else None}
        model.add(
            Entity(
                f"U-{code}", EntityKind.UNIT, "1F", box(*rect), code, {**attributes, "vacant": brand is None}
            )
        )
    for code, items in (fixtures or {}).items():
        for number, (fixture_type, x, y) in enumerate(items, 1):
            fixture_id = f"{code}-F{number:03d}"
            attributes = {"fixture_id": fixture_id, "fixture_type": fixture_type, "unit_code": code}
            model.add(
                Entity(
                    f"F-{fixture_id}",
                    EntityKind.FIXTURE,
                    "1F",
                    box(x, y, x + 1, y + 1),
                    fixture_id,
                    attributes,
                )
            )
    return model


def test_handcrafted_change_types() -> None:
    before = _model(
        "B",
        {
            "1F-S01": ((0, 0, 10, 10), "Alpha"),
            "1F-S02": ((10, 0, 20, 10), "Beta"),
            "1F-S03": ((20, 0, 30, 10), None),
            "1F-S05": ((40, 0, 50, 10), "Theta"),
            "1F-S06": ((50, 0, 60, 10), "Iota"),
            "1F-S07": ((60, 0, 70, 10), "Kappa"),
            "1F-S08": ((70, 0, 80, 10), "Mu"),
            "1F-S09": ((80, 0, 90, 10), "Xi"),
            "1F-N01": ((0, 14, 20, 24), "Delta"),
            "1F-N02": ((20, 14, 26, 24), "Eps"),
            "1F-N03": ((26, 14, 32, 24), "Zeta"),
            "1F-N04": ((32, 14, 40, 24), "Eta"),
        },
        {"1F-S01": [("COUNTER", 1, 1)], "1F-S02": [("TABLE", 12, 2)], "1F-S06": [("COUNTER", 51, 1)]},
    )
    after = _model(
        "A",
        {
            "1F-S01": ((0, 0, 10, 10), None),
            "1F-S02": ((10, 0, 20, 10), "Beta"),
            "1F-S03": ((20, 0, 30, 10), "Alpha"),
            "1F-S05": ((40, 0, 48, 10), "Theta"),
            "1F-S06": ((50, 0, 60, 10), "Iota"),
            "1F-S07": ((60, 0, 70, 10), "Lambda"),
            "1F-S08": ((70, 0, 80, 10), None),
            "1F-S10": ((80, 0, 90, 10), "Xi"),
            "1F-N01A": ((0, 14, 11, 24), "Delta"),
            "1F-N01B": ((11, 14, 20, 24), None),
            "1F-N02M": ((20, 14, 32, 24), "Zeta"),
            "1F-P01": ((45, 11, 47, 13), "Nu"),
        },
        {
            "1F-S02": [("TABLE", 12, 2)],
            "1F-S03": [("COUNTER", 21, 1)],
            "1F-S06": [("COUNTER", 51, 1), ("HANGER", 55, 5)],
        },
        walkways=((0, 10, 90, 14), (32, 14, 40, 24)),
        holes=((45, 11, 47, 13),),
    )
    changes = diff_models(before, after)
    found = Counter(
        (str(i.change_type), tuple(i.unit_codes_before), tuple(i.unit_codes_after)) for i in changes.items
    )
    assert found == Counter(
        {
            ("move", ("1F-S01",), ("1F-S03",)): 1,
            ("split", ("1F-N01",), ("1F-N01A", "1F-N01B")): 1,
            ("merge", ("1F-N02", "1F-N03"), ("1F-N02M",)): 1,
            ("remove", ("1F-N04",), ()): 1,
            ("resize", ("1F-S05",), ("1F-S05",)): 1,
            ("fixture_change", ("1F-S06",), ("1F-S06",)): 1,
            ("reassign", ("1F-S07",), ("1F-S07",)): 1,
            ("reassign", ("1F-S08",), ("1F-S08",)): 1,
            ("add", (), ("1F-P01",)): 1,
            ("walkway_change", (), ()): 2,
        }
    )
    items = {(str(i.change_type), tuple(i.unit_codes_before)): i for i in changes.items}
    move = items[("move", ("1F-S01",))]
    assert (
        move.brand_ids_before == move.brand_ids_after == ["ID-Alpha"]
        and move.details["vacated_unit_code"] == "1F-S01"
    )
    vacated = items[("reassign", ("1F-S08",))]
    assert vacated.brand_ids_after == [] and vacated.details["brand_name_after"] is None
    merge = items[("merge", ("1F-N02", "1F-N03"))]
    assert merge.details["departed_brand_names"] == ["Eps"] and merge.brand_ids_after == ["ID-Zeta"]
    fixture = items[("fixture_change", ("1F-S06",))]
    assert fixture.details["added_fixture_types"] == ["HANGER"] and fixture.entity_ids_after == [
        "F-1F-S06-F002"
    ]
    assert items[("remove", ("1F-N04",))].details["converted_to"]["walkway_m2"] == pytest.approx(80.0)
    assert items[("add", ())].details["previous_use"]["walkway_m2"] == pytest.approx(4.0)
    resize = items[("resize", ("1F-S05",))]
    assert resize.details["area_delta_m2"] == pytest.approx(-20.0)


def test_evaluate_diff_tolerant_matching() -> None:
    def item(change_type: ChangeType, floor: str, before: list[str], after: list[str]) -> ChangeItem:
        return ChangeItem(change_type, floor, before, after, summary=f"{change_type} {before}->{after}")

    truth = ChangeSet(
        "R0",
        "R1",
        [
            item(ChangeType.MOVE, "3F", ["3F-W04"], ["3F-C05"]),
            item(ChangeType.WALKWAY_CHANGE, "4F", [], []),
            item(ChangeType.WALKWAY_CHANGE, "4F", [], []),
            item(ChangeType.RESIZE, "2F", ["2F-S04"], ["2F-S04"]),
        ],
    )
    predicted = ChangeSet(
        "R0",
        "R1",
        [
            item(ChangeType.MOVE, "3F", ["3F-W04"], ["3F-C06"]),
            item(ChangeType.WALKWAY_CHANGE, "4F", [], []),
            item(ChangeType.RESIZE, "2F", ["2F-S04", "2F-S05"], ["2F-S04", "2F-S05"]),
            item(ChangeType.ADD, "4F", [], ["4F-P01"]),
        ],
    )
    metrics = evaluate_diff(predicted, truth)
    per_type = metrics["per_type"]
    assert (per_type["move"]["precision"], per_type["move"]["recall"]) == (0.0, 0.0)
    assert (per_type["walkway_change"]["precision"], per_type["walkway_change"]["recall"]) == (1.0, 1.0)
    assert (per_type["resize"]["precision"], per_type["resize"]["recall"]) == (1.0, 1.0)
    assert per_type["resize"]["exact_truth_matches"] == 0
    assert per_type["add"]["recall"] is None and per_type["add"]["precision"] == 0.0
    assert metrics["overall"] == {"predicted": 4, "truth": 4, "precision": 0.5, "recall": 0.75}
    assert metrics["types_detected"] == ["resize", "walkway_change"]
    assert len(metrics["missed"]) == 1 and len(metrics["false_positives"]) == 2
