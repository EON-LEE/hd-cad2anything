"""Tests for the CAD understanding pipeline (``cad2anything.cadcore``)."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from shapely.geometry import box

from cad2anything.cadcore import (
    SEMANTICS,
    LayerDecision,
    LlmClassifier,
    compare_with_truth,
    parse_revision,
)
from cad2anything.cadcore.classify import merge_second_opinion
from cad2anything.cadcore.fixtures import FixtureRecord, assign_ids, resolve_fixture_type
from cad2anything.cadcore.geometry import layer_geometry
from cad2anything.cadcore.labels import label_units, link_info, parse_zone_label, text_kind
from cad2anything.cadcore.pipeline import main as pipeline_main
from cad2anything.cadcore.reader import Primitive, read_floor, repair_text, resolve_scale
from cad2anything.catalog import FLOOR_CODES
from cad2anything.mockgen import brands_to_rows
from cad2anything.mockgen.drawings import build_all
from cad2anything.mockgen.dxf_writer import messy_layer_names, write_revision_drawings
from cad2anything.model import EntityKind, SpatialModel

SEED = 42
HANGUL = re.compile(r"[\uac00-\ud7a3]")
SHARED_REVISIONS = ("R0", "R1", "R2")


# --- shared synthetic data (generated once per session, reused by test_revision_diff.py) -------------


def shared_root(factory: pytest.TempPathFactory) -> Path:
    """Drawings (R0..R2, clean+messy), truth JSON and brand rows under the session base temp dir."""
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
    """Parsed model and report dict, cached as JSON next to the shared drawings."""
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


def truth_model(root: Path, rev: str) -> SpatialModel:
    return SpatialModel.from_dict(
        json.loads((root / "truth" / f"layout_{rev}.json").read_text(encoding="utf-8"))
    )


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return shared_root(tmp_path_factory)


@pytest.fixture(scope="module")
def clean_r0(root: Path) -> tuple[SpatialModel, dict[str, Any], dict[str, Any]]:
    model, report = parsed_revision(root, "R0", "clean")
    return model, report, compare_with_truth(model, truth_model(root, "R0"))


@pytest.fixture(scope="module")
def messy_r0(root: Path) -> tuple[SpatialModel, dict[str, Any], dict[str, Any]]:
    model, report = parsed_revision(root, "R0", "messy")
    return model, report, compare_with_truth(model, truth_model(root, "R0"))


def messy_truth_semantics() -> dict[tuple[str, str], str]:
    return {(f, name): sem for f in FLOOR_CODES for sem, name in messy_layer_names(f, SEED).items()}


# --- fake LLM ----------------------------------------------------------------------------------------


class _FakeResponses:
    def __init__(self, answers: dict[tuple[str, str], str], fail: bool) -> None:
        self.answers = answers
        self.fail = fail
        self.requests: list[tuple[str, str, Any]] = []

    def create(self, *, model: str, input: list[dict[str, str]], text: Any = None, **_: Any) -> Any:
        payload = json.loads(input[-1]["content"])
        self.requests.append((payload["floor"], payload["layer"], text))
        if self.fail:
            raise RuntimeError("service unavailable")
        semantic = self.answers.get((payload["floor"], payload["layer"]), "noise")
        body = {"layer": payload["layer"], "semantic": semantic, "confidence": 0.9, "evidence": "fake"}
        return SimpleNamespace(output_text=json.dumps(body))


class FakeClient:
    """Mimics ``client.responses.create(...)`` of the OpenAI client returned by Foundry."""

    def __init__(self, answers: dict[tuple[str, str], str], fail: bool = False) -> None:
        self.responses = _FakeResponses(answers, fail)


# --- end-to-end quality ------------------------------------------------------------------------------


def test_clean_r0_parse_quality(clean_r0: tuple[SpatialModel, dict[str, Any], dict[str, Any]]) -> None:
    model, report, metrics = clean_r0
    units, brands, fixtures = metrics["units"], metrics["brands"], metrics["fixtures"]
    assert units["recall"] >= 0.99 and units["precision"] >= 0.99
    assert units["area_rel_error_median"] < 0.005
    assert brands["brand_name_accuracy"] >= 0.99 and brands["vacancy_accuracy"] >= 0.99
    assert fixtures["matched_rate"] >= 0.98
    assert metrics["vertical_links"]["per_floor"] == {floor: 6 for floor in FLOOR_CODES}
    assert metrics["vertical_links"]["recall"] == 1.0
    assert metrics["amenities"]["recall"] >= 0.95 and metrics["openings"]["recall"] == 1.0
    assert metrics["zones"]["recall"] >= 0.95 and brands["zone_code_accuracy"] >= 0.95
    assert brands["brand_id_accuracy"] >= 0.99 and brands["category_accuracy"] >= 0.99
    assert metrics["walkways"]["union_iou_min"] >= 0.99
    assert all(row["method"] == "manifest" for row in report["layer_table"])
    assert report["review_queue"] == []
    revision = model.revision
    assert (revision.rev_id, str(revision.kind), revision.effective_date) == ("R0", "actual", "2024-10-01")
    assert [f.code for f in model.floors] == FLOOR_CODES
    prefixes = {"unit": "U-", "fixture": "F-", "walkway": "W-", "vertical_link": "V-", "amenity": "A-"}
    prefixes |= {"opening": "O-", "zone": "Z-", "exterior": "X-"}
    for entity in model.entities.values():
        assert entity.entity_id.startswith(prefixes[str(entity.kind)]), entity.entity_id
    unit = model.unit_by_code("1F-C01")
    assert unit is not None and unit.entity_id == "U-1F-C01"
    assert {"unit_code", "brand_name", "brand_id", "category", "vacant", "area_m2", "zone_code"} <= set(
        unit.attributes
    )
    contains = {(r.from_id, r.to_id) for r in model.relations if r.type == "contains"}
    fixture = next(f for f in model.by_kind(EntityKind.FIXTURE) if f.attributes["unit_code"] == "1F-C01")
    assert ("U-1F-C01", fixture.entity_id) in contains
    assert (f"Z-{unit.attributes['zone_code']}", "U-1F-C01") in contains


def test_messy_r0_parse_quality(
    messy_r0: tuple[SpatialModel, dict[str, Any], dict[str, Any]], root: Path
) -> None:
    model, report, metrics = messy_r0
    units, brands = metrics["units"], metrics["brands"]
    assert units["recall"] >= 0.95 and units["precision"] >= 0.95
    assert units["area_rel_error_median"] < 0.01
    assert brands["brand_name_accuracy"] >= 0.95 and brands["vacancy_accuracy"] >= 0.95
    assert metrics["fixtures"]["matched_rate"] >= 0.95
    assert metrics["vertical_links"]["per_floor"] == {floor: 6 for floor in FLOOR_CODES}
    totals = report["stats"]["totals"]
    assert totals["hangul_texts"] > 100 and totals["mojibake_suspect"] == 0
    truth = truth_model(root, "R0")
    korean = [u for u in truth.units() if HANGUL.search(u.attributes.get("brand_name") or "")]
    assert len(korean) > 20
    for unit in korean:
        parsed = model.unit_by_code(unit.attributes["unit_code"])
        assert parsed is not None and parsed.attributes["brand_name"] == unit.attributes["brand_name"]
    counts = report["issue_counts"]
    assert counts.get("auto_closed", 0) > 0 and counts.get("polygonized_from_lines", 0) > 0
    assert counts.get("fixture_id_generated", 0) > 0
    sources = Counter(u.attributes["geometry_source"] for u in model.units())
    assert sources["closed"] > sources["auto_closed"] > 0 and sources["lines"] > 0
    generated = [f for f in model.by_kind(EntityKind.FIXTURE) if f.attributes["id_generated"]]
    assert generated and all(re.fullmatch(r".+-F\d{3}", f.attributes["fixture_id"]) for f in generated)
    assert report["stats"]["scale_to_m"] == [0.001] and report["stats"]["dxf_versions"] == ["AC1015"]


def test_messy_layer_semantics_and_review_queue(
    messy_r0: tuple[SpatialModel, dict[str, Any], dict[str, Any]],
) -> None:
    _, report, _ = messy_r0
    truth = messy_truth_semantics()
    rows = report["layer_table"]
    meaningful = [r for r in rows if (r["floor"], r["layer"]) in truth]
    correct = [r for r in meaningful if truth[(r["floor"], r["layer"])] == r["semantic"]]
    assert len(meaningful) >= 100
    assert len(correct) / len(meaningful) >= 0.9
    assert all(r["method"] == "heuristic" for r in rows)
    queue = report["review_queue"]
    assert queue, "ambiguous layers should be queued for human review"
    assert {(q["floor"], q["layer"]) for q in queue} == {
        (r["floor"], r["layer"]) for r in rows if r["confidence"] < 0.7
    }
    assert all(q["confidence"] < 0.7 and q["evidence"] and q["question"] for q in queue)
    assert report["stats"]["review_queue_size"] == len(queue)
    assert all(r["semantic"] in SEMANTICS for r in rows)


def test_fake_llm_resolves_ambiguous_layers(root: Path) -> None:
    folder = root / "drawings" / "R0" / "messy"
    floors = ["5F", "7F"]
    _, baseline = parse_revision(folder, relations="basic", floors=floors)
    pending = {(q["floor"], q["layer"]) for q in baseline.review_queue}
    assert pending
    client = FakeClient(messy_truth_semantics())
    classifier = LlmClassifier(client, "fake-mini")
    model, report = parse_revision(folder, classifier=classifier, relations="basic", floors=floors)
    asked = {(floor, layer) for floor, layer, _ in client.responses.requests}
    assert asked == pending
    assert all(fmt["format"]["type"] == "json_schema" for _, _, fmt in client.responses.requests)
    assert report.review_queue == []
    truth = messy_truth_semantics()
    rows = {(r["floor"], r["layer"]): r for r in report.layer_table}
    for key in pending:
        assert "llm" in rows[key]["method"] and rows[key]["semantic"] == truth.get(key, "noise")
        assert rows[key]["confidence"] >= 0.7
    assert report.stats["llm"]["calls"] == len(pending) and not report.stats["llm"]["errors"]
    assert len(model.units()) == baseline.stats["totals"]["units"] > 0


def test_llm_failure_keeps_review_queue(root: Path) -> None:
    folder = root / "drawings" / "R0" / "messy"
    classifier = LlmClassifier(FakeClient({}, fail=True), "fake-mini")
    _, report = parse_revision(folder, classifier=classifier, relations="basic", floors=["7F"])
    assert report.review_queue and report.stats["llm"]["errors"]
    assert all(row["method"] == "heuristic" for row in report.layer_table)


def test_relations_full_and_basic(root: Path) -> None:
    folder = root / "drawings" / "R0" / "clean"
    full, full_report = parse_revision(folder, relations="full", floors=["3F", "4F"])
    basic, _ = parse_revision(folder, relations="basic", floors=["3F", "4F"])
    basic_types = Counter(str(r.type) for r in basic.relations)
    assert set(basic_types) == {"contains", "on_floor"}
    assert basic_types["on_floor"] == len(basic.entities)
    full_types = Counter(str(r.type) for r in full.relations)
    assert full_types["contains"] == basic_types["contains"] and full_types["on_floor"] > 0
    codes = full_report.issue_counts()
    assert "relations_failed" not in codes
    if "relations_basic" not in codes:
        assert {"adjacent_to", "faces", "connected_to"} <= set(full_types)


def test_cli_writes_model_and_report(tmp_path: Path, root: Path) -> None:
    _, models, _ = build_all(SEED)
    folder = tmp_path / "drawings" / "R1" / "messy"
    write_revision_drawings(models["R1"], folder, "messy", SEED, floors=["4F"])
    brands_csv = tmp_path / "brands.csv"
    rows = json.loads((root / "brands.json").read_text(encoding="utf-8"))
    with open(brands_csv, "w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["brand_id", "brand_name", "category"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    out = tmp_path / "parsed" / "R1_messy.json"
    assert (
        pipeline_main(
            ["--rev-dir", str(folder), "--out", str(out), "--brands", str(brands_csv), "--relations", "basic"]
        )
        == 0
    )
    model = SpatialModel.from_dict(json.loads(out.read_text(encoding="utf-8")))
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert model.revision.rev_id == "R1" and model.revision.parent_rev_id == "R0"
    assert [f.code for f in model.floors] == ["4F"]
    popup = model.unit_by_code("4F-P01")
    assert (
        popup is not None and popup.attributes["brand_id"] and popup.attributes["category"] == "ACCESSORIES"
    )
    assert report["stats"]["totals"]["units"] == len(model.units()) and report["layer_table"]


# --- building blocks ---------------------------------------------------------------------------------


def test_units_and_text_repair() -> None:
    assert resolve_scale(4, None, None) == (0.001, "header")
    assert resolve_scale(6, "mm", None) == (1.0, "header")
    assert resolve_scale(0, "mm", None) == (0.001, "manifest")
    assert resolve_scale(0, None, 120_000.0) == (0.001, "extent")
    garbled = "라온반찬".encode("cp949").decode("latin-1")
    assert repair_text(garbled) == ("라온반찬", "fixed")
    assert repair_text("\\U+AC00\\U+B098") == ("가나", None)
    assert repair_text("Café Größe") == ("Café Größe", None)
    assert repair_text("주얼리·시계") == ("주얼리·시계", None)


def test_wrong_codepage_is_repaired(tmp_path: Path, root: Path) -> None:
    source = root / "drawings" / "R0" / "messy" / "flagship_R0_B1.dxf"
    target = tmp_path / "flagship_R0_B1.dxf"
    target.write_bytes(source.read_bytes().replace(b"ANSI_949", b"ANSI_1252"))
    drawing = read_floor(target, "B1", "mm")
    assert drawing.text_stats["mojibake_fixed"] > 0 and drawing.text_stats["mojibake_suspect"] == 0
    assert "mojibake_fixed" in {i.code for i in drawing.issues}
    assert any(HANGUL.search(layer) for layer in drawing.layers)
    assert any(p.text == "WC 화장실" for p in drawing.primitives if p.kind == "text")


def _prim(kind: str, points: list[tuple[float, float]], closed: bool = False, handle: str = "1") -> Primitive:
    return Primitive(kind, "L", handle, kind.upper(), points=points, closed=closed)


def test_geometry_reconstruction() -> None:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    prims = [
        _prim("polyline", square, closed=True, handle="a"),
        _prim("polyline", [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0), (20.0, 0.03)], handle="b"),
        _prim("polyline", [(x + 0.0, y + 0.0) for x, y in square], closed=True, handle="dup"),
        _prim("polyline", [(50.0, 0.0), (60.0, 0.0), (60.0, 10.0), (50.0, 10.0), (50.0, 2.0)], handle="far"),
    ]
    for i, (a, b) in enumerate(
        [
            ((40.0, 0.0), (45.02, 0.0)),
            ((45.0, 0.0), (45.0, 5.0)),
            ((45.0, 5.0), (40.0, 5.0)),
            ((40.0, 5.0), (40.0, 0.03)),
        ]
    ):
        prims.append(_prim("line", [a, b], handle=f"l{i}"))
    result = layer_geometry("3F", "L", prims)
    sources = Counter(r.source for r in result.polygons)
    assert sources == {"closed": 1, "auto_closed": 1, "lines": 1}
    areas = sorted(round(r.polygon.area, 1) for r in result.polygons)
    assert areas == [25.0, 100.0, 100.0]
    codes = {i.code for i in result.issues}
    assert {"auto_closed", "polygonized_from_lines", "duplicate_removed", "open_polyline_unclosed"} <= codes
    assert result.counts["duplicates"] == 1


def test_label_parsing() -> None:
    assert text_kind("3F-C05 (공실)") == "unit_combined" and text_kind("6F-S02A") == "unit_code"
    assert text_kind("3F-Z1 여성 컨템포러리") == "zone_code" and text_kind("에스컬레이터 ESC-B") == "link"
    assert text_kind("ENT-1 정문") == "opening" and text_kind("WC 화장실") == "amenity"
    assert text_kind("12000") == "numeric" and text_kind("MD팀 확인 요망") == "free"
    assert link_info("엘리베이터 EV-2") == ("EV-2", "elevator") and link_info("계단") == (None, "stair")
    assert parse_zone_label("Z3 잡화", "4F") == ("4F-Z3", "잡화")
    polygons = [box(0, 0, 6, 10), box(6, 0, 12, 10), box(12, 0, 18, 10), box(18, 0, 24, 10)]

    def text(value: str, x: float, y: float) -> Primitive:
        return Primitive("text", "T", value, "TEXT", points=[(x, y)], text=value, height=0.45, anchor=(x, y))

    texts = [
        text("3F-C01\nVesane Femme", 3, 5),
        text("3F-C02", 9, 5.4),
        text("라온반찬", 9, 4.6),
        text("3F-C03 공실", 15, 5),
        text("3F-C04", 21, 5.4),
        text("(공실)", 21, 4.6),
    ]
    labels, issues = label_units(polygons, texts, "3F")
    assert [(lab.unit_code, lab.brand_name, lab.vacant) for lab in labels] == [
        ("3F-C01", "Vesane Femme", False),
        ("3F-C02", "라온반찬", False),
        ("3F-C03", None, True),
        ("3F-C04", None, True),
    ]
    assert [lab.method for lab in labels] == ["combined", "separate", "combined", "separate"]
    assert not issues


def test_fixture_types_and_ids() -> None:
    assert resolve_fixture_type("FX_COUNTER", {}).fixture_type == "COUNTER"
    assert resolve_fixture_type("행거_01", {}).fixture_type == "HANGER"
    assert resolve_fixture_type("식탁_4인", {}).fixture_type == "DINING"
    assert resolve_fixture_type("곤돌라_A", {}).fixture_type == "GONDOLA"
    assert resolve_fixture_type("BLK-17", {"TYPE": "쇼케이스"}).fixture_type == "SHOWCASE"
    unknown = resolve_fixture_type("BLK-99", {})
    assert unknown.fixture_type == "UNKNOWN"
    hooked = resolve_fixture_type("BLK-99", {}, resolver=lambda block, attrs: ("TABLE", 0.8, "hook"))
    assert (hooked.fixture_type, hooked.method) == ("TABLE", "llm")

    def record(fixture_id: str, x: float, y: float) -> FixtureRecord:
        prim = Primitive(
            "insert",
            "F",
            fixture_id or f"h{x}",
            "INSERT",
            attributes={"FIXTURE_ID": fixture_id} if fixture_id else {},
        )
        typing = resolve_fixture_type("FX_TABLE", {})
        return FixtureRecord(
            prim, typing, box(x, y, x + 1, y + 1), 1.6, 1.0, 0.0, "block", unit_code="3F-C01"
        )

    records = assign_ids(
        [record("3F-C01-F002", 0, 0), record("", 5, 5), record("", 2, 1), record("3F-C01-F002", 9, 9)], "3F"
    )
    ids = [(r.fixture_id, r.id_generated) for r in records]
    assert ids[0] == ("3F-C01-F002", False)
    assert sorted(i for i, generated in ids if generated) == ["3F-C01-F001", "3F-C01-F003", "3F-C01-F004"]
    assert len({i for i, _ in ids}) == 4


def test_second_opinion_policy() -> None:
    wall = LayerDecision("wall", 0.68, "heuristic", ["loops"], {})
    agree = merge_second_opinion(wall, LayerDecision("wall", 0.8, "llm", ["LLM: walls"]))
    assert (agree.semantic, agree.method) == ("wall", "heuristic+llm") and agree.confidence >= 0.9
    conflict = merge_second_opinion(wall, LayerDecision("zone", 0.9, "llm", ["LLM: zone"]))
    assert (conflict.semantic, conflict.confidence, conflict.method) == ("wall", 0.68, "heuristic")
    assert conflict.alternatives == {"zone": 0.9} and any("불일치" in e for e in conflict.evidence)
    weak = merge_second_opinion(
        LayerDecision("noise", 0.2, "heuristic"), LayerDecision("fixture", 0.85, "llm")
    )
    assert (weak.semantic, weak.confidence, weak.method) == ("fixture", 0.85, "llm")


@pytest.mark.skipif(
    os.environ.get("CAD2ANYTHING_LIVE") != "1", reason="live Foundry LLM test (set CAD2ANYTHING_LIVE=1)"
)
def test_live_llm_classifies_ambiguous_layers(root: Path) -> None:
    from cad2anything.cadcore.classify import foundry_llm_classifier

    folder = root / "drawings" / "R0" / "messy"
    floors = ["5F", "7F"]
    _, baseline = parse_revision(folder, relations="basic", floors=floors)
    pending = {(q["floor"], q["layer"]) for q in baseline.review_queue}
    classifier = foundry_llm_classifier()
    _, report = parse_revision(folder, classifier=classifier, relations="basic", floors=floors)
    assert classifier.calls == len(pending) and not classifier.errors, classifier.errors
    rows = [r for r in report.layer_table if (r["floor"], r["layer"]) in pending]
    for row in rows:
        assert row["semantic"] in SEMANTICS and 0.0 <= row["confidence"] <= 1.0
        assert "llm" in row["method"] or any(e.startswith("LLM(") for e in row["evidence"]), row
    print(
        json.dumps(
            [
                {k: r[k] for k in ("floor", "layer", "semantic", "confidence", "method", "evidence")}
                for r in rows
            ],
            ensure_ascii=False,
        )
    )
