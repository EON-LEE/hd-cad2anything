"""Drawing and metadata uploads (docs/specs/atlas-api.md §1.2): jobs, validation, metadata and REST routes."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import re
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from shapely import affinity

from cad2anything.mockgen.dxf_writer import write_clean_floor
from cad2anything.mockgen.upload_samples import CUSTOMER_PROFILE_YAML
from cad2anything.model import EntityKind, RevisionKind, SpatialModel
from cad2anything.services import metadata, upload_tools, uploads
from cad2anything.services.errors import ToolError
from cad2anything.services.mcp_server import build_mcp
from cad2anything.services.tools import Toolkit
from cad2anything.services.upload_api import build_upload_router
from cad2anything.services.upload_tools import UploadTools
from cad2anything.services.uploads import (
    RESTART_MESSAGE,
    STATUS_LABELS,
    STEP_LABELS,
    ConflictError,
    NotFoundError,
    collect_drawing_files,
    detect_floor,
    reset_job,
)
from cad2anything.workspace import Workspace

FLOOR = "6F"
DATE = "2026-10-01"
SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples" / "uploads"
# The coordinator adds UploadTools to Toolkit; until then the mixin goes first.
_BASES = (Toolkit,) if issubclass(Toolkit, UploadTools) else (UploadTools, Toolkit)


class _Toolkit(*_BASES):  # type: ignore[misc]
    def __init__(self, workspace: Any, sync: bool = True, relations: str = "basic") -> None:
        super().__init__(workspace, use_postgis=False)
        self._init_uploads(sync=sync, relations=relations)


class _NoBriefToolkit(_Toolkit):
    relayout_validate_brief = None


class _StubBriefToolkit(_Toolkit):
    def relayout_validate_brief(self, data: Any) -> tuple[dict[str, Any], list[str], list[str]]:
        if data.get("season") not in ("SS", "FW"):
            return {}, ["season은 SS 또는 FW여야 해요."], []
        return {**data, "brief_id": str(data["brief_id"]), "status": "draft"}, [], ["스텁 경고"]


@pytest.fixture(scope="module")
def r2(synthetic_store) -> SpatialModel:
    return synthetic_store["models"]["R2"]


@pytest.fixture(scope="module")
def edit(r2) -> dict[str, Any]:
    """R2 with one brand moved into the vacant unit and one unit narrowed by 10 % on FLOOR."""
    model = SpatialModel.from_dict(r2.to_dict())
    units = sorted(model.units(FLOOR), key=lambda u: u.attributes["unit_code"])
    target = next(u for u in units if u.attributes.get("vacant"))
    source = next(u for u in units if not u.attributes.get("vacant"))
    moved = {k: source.attributes.get(k) for k in ("brand_id", "brand_name", "category", "category_name")}
    target.attributes.update(moved, vacant=False)
    source.attributes.update(brand_id=None, brand_name=None, vacant=True)
    occupied = [u for u in units if not u.attributes.get("vacant")]
    resized = next(u for u in occupied if u is not target)
    area = resized.geometry.area
    resized.geometry = affinity.scale(resized.geometry, xfact=0.9, yfact=1.0, origin="centroid")
    return {
        "model": model,
        "from": source.attributes["unit_code"],
        "to": target.attributes["unit_code"],
        "brand_id": moved["brand_id"],
        "resized": resized.attributes["unit_code"],
        "area": area,
        "occupied": [u.attributes["unit_code"] for u in occupied],
    }


def _floor_bytes(folder: Path, model: SpatialModel) -> bytes:
    path = folder / f"{FLOOR}.dxf"
    write_clean_floor(model, FLOOR, path)
    return path.read_bytes()


@pytest.fixture(scope="module")
def floor_dxf(tmp_path_factory, edit) -> bytes:
    return _floor_bytes(tmp_path_factory.mktemp("edited-floor"), edit["model"])


@pytest.fixture(scope="module")
def plain_dxf(tmp_path_factory, r2) -> bytes:
    return _floor_bytes(tmp_path_factory.mktemp("plain-floor"), r2)


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> _Toolkit:
    return _Toolkit(populate(tmp_path_factory.mktemp("uploads-ws")))


@pytest.fixture(scope="module")
def meta_toolkit(tmp_path_factory, populate) -> _Toolkit:
    return _Toolkit(populate(tmp_path_factory.mktemp("metadata-ws")))


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def write_leases(ws: Any, edit: dict[str, Any]) -> dict[str, int]:
    """Leases for FLOOR (one area mismatch, one brand mismatch, one unit missing in the drawing, one expired
    and one on another floor); returns the expected QA counts for the edited drawing."""
    units = {u.attributes["unit_code"]: u for u in edit["model"].units(FLOOR)}
    resized = edit["resized"]
    spare = [c for c in edit["occupied"] if c not in (edit["to"], resized)]
    ok, mismatch, expired = spare[0], spare[1], spare[2]
    brands = [units[c].attributes["brand_id"] for c in spare]

    def lease(
        lease_id: str, code: str, brand: Any, start: str = "2025-09-01", end: str = "2027-08-31", area=None
    ):
        area = round(units[code].geometry.area, 1) if area is None and code in units else area
        return {
            "lease_id": lease_id,
            "brand_id": brand,
            "unit_code": code,
            "lease_start": start,
            "lease_end": end,
            "contract_area_m2": area,
            "commission_rate": 0.25,
        }

    rows = [
        lease("L001", resized, units[resized].attributes["brand_id"], area=round(edit["area"], 1)),
        lease("L002", ok, units[ok].attributes["brand_id"]),
        lease("L003", mismatch, next(b for b in brands if b != units[mismatch].attributes["brand_id"])),
        lease("L004", f"{FLOOR}-N99", "BR0001", area=120.0),
        lease("L005", expired, units[expired].attributes["brand_id"], "2024-10-01", "2025-08-31"),
        lease("L006", "5F-C01", "BR0002", area=100.0),
    ]
    ws.write_text("business/leases.csv", pd.DataFrame(rows).to_csv(index=False))
    occupied = [c for c in edit["occupied"] if c != edit["from"]]
    return {
        "area_mismatch": 1,
        "brand_mismatch": 1,
        "unit_missing_in_drawing": 1,
        "unit_without_lease": len(occupied) - 3,
    }


# --- drawing jobs ---------------------------------------------------------------------------------------------


def test_upload_job_end_to_end_and_reupload(toolkit, floor_dxf, edit) -> None:
    ws = toolkit.ws
    (ws.root / "business" / "leases.csv").unlink(missing_ok=True)
    first = toolkit.upload_drawings(
        [("R3_6F.dxf", floor_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    assert re.fullmatch(r"J\d{8}-\d{4}", first["job_id"]) and first["status"] == "done"
    job = toolkit.upload_job(first["job_id"])
    assert [s["name"] for s in job["steps"]] == list(STEP_LABELS)
    assert [s["label"] for s in job["steps"]] == list(STEP_LABELS.values())
    statuses = {s["name"]: s["status"] for s in job["steps"]}
    assert statuses == {
        "save": "done",
        "parse": "done",
        "merge": "done",
        "diff": "done",
        "qa": "skipped",
        "impact": "done",
        "generate": "skipped",
        "relayout_check": "skipped",
    }
    drawing_hash = hashlib.sha256(floor_dxf).hexdigest()
    assert job["files"] == [
        {"name": "R3_6F.dxf", "floor": FLOOR, "bytes": len(floor_dxf), "sha256": drawing_hash}
    ]
    assert job["source_files"] == [{"name": "R3_6F.dxf", "bytes": len(floor_dxf), "sha256": drawing_hash}]
    assert job["revision_available"] and job["review_reason"] is None and job["stage_errors"] == []
    assert job["superseded_by"] is None
    assert (
        job["metadata_snapshot"]["brands"] == hashlib.sha256(ws.table_path("brands").read_bytes()).hexdigest()
    )
    assert (
        job["floors_uploaded"] == [FLOOR]
        and FLOOR not in job["floors_inherited"]
        and len(job["floors_inherited"]) == 8
    )
    assert job["parse"]["units"] == len(edit["model"].units(FLOOR)) and job["parse"]["review_queue"] == []

    model, parent = ws.model("R3"), ws.model("R2")
    assert (model.revision.kind, model.revision.parent_rev_id, model.revision.effective_date) == (
        RevisionKind.ACTUAL,
        "R2",
        DATE,
    )
    assert [f.code for f in model.floors] == [f.code for f in parent.floors]
    meta = ws.model_meta("R3")
    assert (meta["source"], meta["job_id"], meta["floors_uploaded"]) == ("uploaded", job["job_id"], [FLOOR])
    assert (
        meta["file_sha256"] == {FLOOR: drawing_hash} and meta["metadata_snapshot"] == job["metadata_snapshot"]
    )
    assert meta["source_files"] == job["source_files"]
    for floor in job["floors_inherited"]:
        inherited = [e.to_dict() for e in model.entities.values() if e.floor == floor]
        assert inherited == [e.to_dict() for e in parent.entities.values() if e.floor == floor]
    units = {u.attributes["unit_code"]: u for u in model.units(FLOOR)}
    assert (
        units[edit["to"]].attributes["brand_id"] == edit["brand_id"]
        and units[edit["from"]].attributes["vacant"]
    )
    assert units[edit["resized"]].geometry.area == pytest.approx(0.9 * edit["area"], rel=0.01)
    assert {"move", "resize"} <= set(job["changes"]["by_type"]) and set(job["changes"]["by_floor"]) == {FLOOR}
    assert {entry["generator"] for entry in job["impact"]} >= {"area_schedule"}

    manifest = ws.read_json("drawings/R3/uploaded/manifest.json")
    level = next(f for f in parent.floors if f.code == FLOOR)
    assert manifest["levels"] == [
        {
            "floor": FLOOR,
            "ordinal": level.ordinal,
            "name": level.name,
            "file": f"{FLOOR}.dxf",
            "elevation_m": level.elevation_m,
            "height_m": level.height_m,
        }
    ]
    assert (manifest["upload_job"], manifest["parent_revision"], manifest["variant"]) == (
        job["job_id"],
        "R2",
        "uploaded",
    )
    assert (
        manifest["drawing_profile"]["profile_id"] == "default"
        and (ws.drawing_dir("R3", "uploaded") / "6F.dxf").is_file()
    )
    assert not (ws.root / uploads.STAGING_DIR / job["job_id"]).exists()
    assert ws.report("R3")["upload_job"] == job["job_id"] and ws.change_meta("R2", "R3")["source"] == "diff"

    expected = write_leases(ws, edit)
    second = toolkit.upload_drawings([("6층.dxf", floor_dxf)], "R3", DATE, run_generators=False)
    job2 = toolkit.upload_job(second["job_id"])
    assert job2["job_id"] != job["job_id"] and job2["parent_rev"] == "R2" and job2["status"] == "done"
    assert ws.model_meta("R3")["job_id"] == job2["job_id"]
    old_job = toolkit.upload_job(job["job_id"])
    assert old_job["superseded_by"] == job2["job_id"] and old_job["revision_available"] is False
    assert job2["revision_available"] and job2["superseded_by"] is None
    assert [row["job_id"] for row in toolkit.upload_jobs() if row["rev_id"] == "R3"][:2] == [
        job2["job_id"],
        job["job_id"],
    ]
    qa = job2["qa"]
    assert next(s for s in job2["steps"] if s["name"] == "qa")["status"] == "done"
    assert qa["by_code"] == expected and qa["errors"] == 0 and qa["warnings"] == 3
    area = next(issue for issue in qa["issues"] if issue["code"] == "area_mismatch")
    assert area["unit_code"] == edit["resized"] and area["delta_pct"] == pytest.approx(10.0, abs=0.5)
    assert re.fullmatch(r"계약 면적 [\d.]+㎡와 도면 면적 [\d.]+㎡가 \d+\.\d% 다릅니다\.", area["message"])
    missing = next(issue for issue in qa["issues"] if issue["code"] == "unit_missing_in_drawing")
    assert missing["unit_code"] == f"{FLOOR}-N99"
    info = next(issue for issue in qa["issues"] if issue["code"] == "unit_without_lease")
    assert info["severity"] == "info" and "계약을 등록해야 합니다" in info["message"]


def test_upload_runs_generators(toolkit, floor_dxf) -> None:
    result = toolkit.upload_drawings([("R4_6F.dxf", floor_dxf)], "R4", DATE, parent_rev="R2")
    job = toolkit.upload_job(result["job_id"])
    generate = next(s for s in job["steps"] if s["name"] == "generate")
    assert generate["status"] == "failed" and job["status"] == "partial" and job["artifacts"]
    assert job["revision_available"] and job["stage_errors"][0]["step"] == "generate"
    assert [a["generator"] for a in job["artifacts"]] == [entry["generator"] for entry in job["impact"]]
    assert any(a["status"] == "qa_failed" for a in job["artifacts"])
    assert (toolkit.ws.artifacts_dir / "R4" / "area_schedule" / "artifact.json").is_file()


@pytest.mark.parametrize("stage", ["diff", "qa", "impact", "generate", "relayout_check"])
def test_downstream_failure_is_partial(tmp_path, populate, plain_dxf, monkeypatch, stage: str) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))

    def fail(_pipeline: Any, _job: Any, _state: Any) -> str:
        raise ToolError(f"{stage} 실패")

    monkeypatch.setattr(uploads.DrawingPipeline, f"_{stage}", fail)
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    job = toolkit.upload_job(result["job_id"])
    assert job["status"] == "partial" and job["revision_available"] is True
    assert job["review_reason"] is None
    assert job["stage_errors"] == [{"step": stage, "message": f"{stage} 실패"}]
    assert next(step for step in job["steps"] if step["name"] == stage)["status"] == "failed"
    assert toolkit.ws.model_meta("R3")["job_id"] == job["job_id"]
    brief = toolkit.upload_list_jobs(job["job_id"])
    assert brief["stage_errors"] == job["stage_errors"] and brief["revision_available"] is True


@pytest.mark.parametrize("fail_qa", [False, True])
def test_review_queue_and_partial_are_distinct(
    tmp_path, populate, plain_dxf, monkeypatch, fail_qa: bool
) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    parse = uploads.DrawingPipeline._parse

    def with_review(pipeline: Any, job: Any, state: Any) -> Any:
        message = parse(pipeline, job, state)
        job["parse"]["review_queue"] = [{"layer": "ambiguous", "floor": FLOOR}]
        job["parse"]["review_total"] = 1
        return message

    monkeypatch.setattr(uploads.DrawingPipeline, "_parse", with_review)
    if fail_qa:
        monkeypatch.setattr(uploads.DrawingPipeline, "_qa", lambda *_args: ("failed", "계약 대조 실패"))
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    job = toolkit.upload_job(result["job_id"])
    assert job["status"] == ("partial" if fail_qa else "needs_review")
    assert job["review_reason"] == "layer_review_queue" and job["revision_available"] is True
    assert job["stage_errors"] == ([{"step": "qa", "message": "계약 대조 실패"}] if fail_qa else [])


@pytest.mark.parametrize("stage", ["diff", "qa", "impact", "generate", "relayout_check"])
def test_downstream_error_results_are_not_reported_as_success(
    tmp_path, populate, floor_dxf, monkeypatch, stage: str
) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    failure = {"ok": False, "error": "후속 단계 결과가 실패했어요."}
    if stage == "diff":
        monkeypatch.setattr(toolkit, "revision_diff", lambda *_args, **_kwargs: failure)
    elif stage == "qa":
        monkeypatch.setattr(toolkit, "metadata_qa", lambda *_args, **_kwargs: failure)
    elif stage == "impact":
        import cad2anything.generators

        monkeypatch.setattr(cad2anything.generators, "plan_impact", lambda *_args: failure)
    elif stage == "generate":
        monkeypatch.setattr(toolkit, "registry_run", lambda *_args: failure)
    else:
        monkeypatch.setattr(toolkit, "relayout_after_upload", lambda *_args: failure)
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", floor_dxf)], "R3", DATE, parent_rev="R2", run_generators=stage == "generate"
    )
    job = toolkit.upload_job(result["job_id"])
    assert job["status"] == "partial" and job["revision_available"] is True
    assert any(error["step"] == stage for error in job["stage_errors"])
    assert next(step for step in job["steps"] if step["name"] == stage)["status"] == "failed"


def test_multiple_malformed_stage_results_are_all_reported(
    tmp_path, populate, floor_dxf, monkeypatch
) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    monkeypatch.setattr(toolkit, "revision_diff", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(toolkit, "metadata_qa", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(toolkit, "registry_run", lambda *_args: {})
    monkeypatch.setattr(toolkit, "relayout_after_upload", lambda *_args: {})
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", floor_dxf)], "R3", DATE, parent_rev="R2", run_generators=True
    )
    job = toolkit.upload_job(result["job_id"])
    assert job["status"] == "partial" and job["revision_available"] is True
    assert [error["step"] for error in job["stage_errors"]] == ["diff", "qa", "generate", "relayout_check"]


@pytest.mark.parametrize(
    "hook_result",
    [
        None,
        {"skipped": True, "reason": "선택한 대안이 없어요."},
        {"conformance_pct": 75, "summary": "일치율 75%"},
    ],
)
def test_relayout_check_records_result_and_skip(
    tmp_path, populate, plain_dxf, monkeypatch, hook_result
) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    monkeypatch.setattr(toolkit, "relayout_after_upload", lambda _rev, _parent: hook_result)
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    job = toolkit.upload_job(result["job_id"])
    step = next(step for step in job["steps"] if step["name"] == "relayout_check")
    assert job["status"] == "done" and job["stage_errors"] == []
    if hook_result is None:
        assert job["relayout_check"]["skipped"] and job["relayout_check"]["reason"] == step["message"]
        assert step["status"] == "skipped"
    elif hook_result.get("skipped"):
        assert job["relayout_check"] == hook_result
        assert step["status"] == "skipped" and step["message"] == hook_result["reason"]
    else:
        assert job["relayout_check"] == hook_result
        assert step["status"] == "done" and step["message"] == hook_result["summary"]


def test_failed_reupload_does_not_supersede_saved_model(tmp_path, populate, plain_dxf, monkeypatch) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    first = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )

    def fail_parse(*_args: Any) -> str:
        raise ToolError("다시 올린 도면을 읽지 못했어요.")

    monkeypatch.setattr(uploads.DrawingPipeline, "_parse", fail_parse)
    second = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    old, new = toolkit.upload_job(first["job_id"]), toolkit.upload_job(second["job_id"])
    assert new["status"] == "failed" and new["revision_available"] is False
    assert old["revision_available"] is True and old["superseded_by"] is None
    assert toolkit.ws.model_meta("R3")["job_id"] == first["job_id"]


def test_legacy_job_fields_and_failed_stages_are_recovered(tmp_path, populate, plain_dxf) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    first = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    store = toolkit._upload_store
    raw = toolkit.ws.read_json(store.rel_path(first["job_id"]))
    for key in ("revision_available", "review_reason", "stage_errors", "superseded_by", "metadata_snapshot"):
        raw.pop(key)
    next(step for step in raw["steps"] if step["name"] == "qa").update(
        status="failed", message="이전 대조 실패"
    )
    toolkit.ws.write_json(store.rel_path(raw["job_id"]), raw)
    detail = toolkit.upload_job(raw["job_id"])
    assert detail["revision_available"] is True and detail["status"] == "partial"
    assert detail["stage_errors"] == [{"step": "qa", "message": "이전 대조 실패"}]
    assert detail["metadata_snapshot"] == {} and detail["superseded_by"] is None

    second = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    previous = toolkit.upload_job(first["job_id"])
    assert previous["status"] == "partial" and previous["revision_available"] is False
    assert previous["superseded_by"] == second["job_id"]


def test_layer_mapping_reruns_job(toolkit, plain_dxf) -> None:
    first = toolkit.upload_drawings(
        [("R5_6F.dxf", plain_dxf)], "R5", DATE, parent_rev="R2", run_generators=False
    )
    job_id = first["job_id"]
    for bad in (
        [{"layer": "A-CORR", "semantic": "corridor"}],
        [{"layer": "A-CORR", "semantic": "noise", "floor": "5F"}],
        [{"semantic": "noise"}],
        [],
    ):
        with pytest.raises(ToolError) as info:
            toolkit.upload_layer_mapping(job_id, bad)
        assert not isinstance(info.value, NotFoundError | ConflictError)
    for unknown in ("J20990101-0001", "../../etc/passwd"):
        with pytest.raises(NotFoundError):
            toolkit.upload_layer_mapping(unknown, [{"layer": "A-CORR", "semantic": "noise"}])
    job = toolkit.upload_layer_mapping(job_id, [{"layer": "A-CORR", "semantic": "noise", "floor": "6층"}])
    assert job["job_id"] == job_id and job["status"] == "done"
    assert job["layer_mappings"] == [{"layer": "A-CORR", "semantic": "noise", "floor": FLOOR}]
    manifest = toolkit.ws.read_json("drawings/R5/uploaded/manifest.json")
    assert manifest["layers_by_floor"] == {FLOOR: {"noise": ["A-CORR"]}}
    row = next(r for r in toolkit.ws.report("R5")["layer_table"] if r["layer"] == "A-CORR")
    assert (row["semantic"], row["method"]) == ("noise", "manifest")
    assert not toolkit.ws.model("R5").by_kind(EntityKind.WALKWAY, FLOOR)
    assert job["changes"]["by_type"].get("walkway_change")


def test_failed_layer_mapping_rerun_does_not_claim_or_relabel_the_previous_model(toolkit, plain_dxf) -> None:
    first = toolkit.upload_drawings(
        [("R6_6F.dxf", plain_dxf)], "R6", DATE, parent_rev="R2", run_generators=False
    )
    report_before = toolkit.ws.report("R6")
    unit_layer = next(row["layer"] for row in report_before["layer_table"] if row.get("semantic") == "unit")

    failed = toolkit.upload_layer_mapping(
        first["job_id"], [{"layer": unit_layer, "semantic": "noise", "floor": FLOOR}]
    )

    assert failed["status"] == "failed"
    assert failed["revision_available"] is False
    assert toolkit.ws.model_meta("R6")["upload_attempt"] == 0
    assert toolkit.ws.report("R6") == report_before


@pytest.mark.parametrize(
    ("name", "floor"),
    [
        ("R3_4F.dxf", "4F"),
        ("4F.dxf", "4F"),
        ("b1.dxf", "B1"),
        ("4층.dxf", "4F"),
        ("지하1층.dxf", "B1"),
        ("flagship_R2_6F.dxf", "6F"),
        ("6F_평면도_v2.dxf", "6F"),
        ("B1F.dxf", "B1"),
        ("plan_R3.dxf", None),
    ],
)
def test_detect_floor(name: str, floor: str | None) -> None:
    assert detect_floor(name) == floor


def test_collect_drawing_files_from_zip(plain_dxf) -> None:
    legacy = "지하1층.dxf".encode("cp949")  # Korean Windows tools store CP949 names without the UTF-8 flag
    placeholder = b"X" * (len(legacy) - 4) + b".dxf"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("drawings/6층.dxf", plain_dxf)
        archive.writestr(placeholder.decode("ascii"), plain_dxf)
        archive.writestr("__MACOSX/drawings/._6층.dxf", b"junk")
        archive.writestr("readme.txt", b"ignored")
    data = buffer.getvalue().replace(placeholder, legacy)
    files = collect_drawing_files([("upload.zip", data)])
    assert [(f.name, f.floor) for f in files] == [("6층.dxf", "6F"), ("지하1층.dxf", "B1")]


def test_upload_validation_errors(toolkit, plain_dxf, monkeypatch) -> None:
    def upload(
        files: list[tuple[str, bytes]] | None = None, rev: str = "R9", day: str = DATE, **kwargs: Any
    ) -> None:
        toolkit.upload_drawings(
            files or [("R9_6F.dxf", plain_dxf)], rev, day, **{"parent_rev": "R2", **kwargs}
        )

    for existing in ("R2", "R1"):
        with pytest.raises(ConflictError):
            upload(rev=existing)
    invalid: list[dict[str, Any]] = [
        {"rev": "r9"},
        {"rev": "R9 X"},
        {"rev": "TOO-LONG-REVISION-ID"},
        {"rev": "H7"},
        {"day": "2026/10/01"},
        {"day": "2026-13-01"},
        {"day": "2025-09-01"},
        {"parent_rev": "R9"},
        {"parent_rev": "H1"},
        {"profile": "nope"},
        {"units": "km"},
        {"files": [("plan.dxf", plain_dxf)]},
        {"files": [("R9_6F.dwg", plain_dxf)]},
        {"files": [("6F.dxf", plain_dxf), ("R9_6F.dxf", plain_dxf)]},
        {"files": [("6F.dxf", b"hello")]},
        {"files": [("6F.dxf", b"AC1032 binary dwg")]},
        {"files": [("12F.dxf", plain_dxf)]},
        {"files": [("4F_5F.dxf", plain_dxf)]},
        {"files": [("6F.zip", _zip({"../6F.dxf": plain_dxf}))]},
        {"files": [("6F.zip", _zip({"6F.dxf": plain_dxf})), ("5F.dxf", plain_dxf)]},
        {"files": [("6F.zip", _zip({"readme.txt": b"x"}))]},
        {"files": [("6F.zip", b"not a zip")]},
    ]
    for case in invalid:
        with pytest.raises(ToolError) as info:
            upload(**case)
        assert not isinstance(info.value, ConflictError), case
    with pytest.raises(ToolError, match="도면 파일"):
        toolkit.upload_drawings([], "R9", DATE, parent_rev="R2")
    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 1000)
    for files in ([("6F.dxf", plain_dxf)], [("a.zip", _zip({"6F.dxf": plain_dxf}))]):
        with pytest.raises(ToolError, match="파일 하나는"):
            upload(files)
    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 30 * uploads.MB)
    monkeypatch.setattr(uploads, "MAX_TOTAL_BYTES", len(plain_dxf) + 10)
    with pytest.raises(ToolError, match="전체 파일"):
        upload([("5F.dxf", plain_dxf), ("6F.dxf", plain_dxf)])
    store = toolkit._upload_store
    busy = store.create(
        reset_job({"job_id": None, "kind": "drawing", "rev_id": "R9", "floors_uploaded": [FLOOR]})
    )
    try:
        with pytest.raises(ConflictError, match="아직 끝나지 않았습니다"):
            upload()
    finally:
        busy["status"] = "failed"
        store.save(busy)
    assert not toolkit.ws.has_model("R9")


def test_async_worker_and_restart_recovery(toolkit, plain_dxf) -> None:
    worker = _Toolkit(toolkit.ws, sync=False)
    result = worker.upload_drawings(
        [("R6_6F.dxf", plain_dxf)], "R6", DATE, parent_rev="R2", run_generators=False
    )
    assert result["status"] in ("queued", "running", "done")
    assert worker._upload_runner.join(timeout=600)
    assert worker.upload_job(result["job_id"])["status"] == "done"

    store = worker._upload_store
    stuck = store.create(
        reset_job({"job_id": None, "kind": "drawing", "rev_id": "R7", "floors_uploaded": [FLOOR]})
    )
    stuck["status"] = "running"
    stuck["steps"][1]["status"] = "running"
    store.save(stuck)
    toolkit.ws.write_bytes(f"{uploads.STAGING_DIR}/{stuck['job_id']}/6F.dxf", b"partial", publish=False)
    restarted = _Toolkit(toolkit.ws)
    job = restarted.upload_job(stuck["job_id"])
    assert (job["status"], job["error"], job["steps"][1]["status"]) == ("failed", RESTART_MESSAGE, "failed")
    assert not (toolkit.ws.root / uploads.STAGING_DIR).exists()


def test_restart_reports_a_saved_model_and_its_owner(tmp_path, populate, plain_dxf) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    store = toolkit._upload_store
    job = store.load(result["job_id"])
    job["status"] = "running"
    next(step for step in job["steps"] if step["name"] == "diff")["status"] = "running"
    store.save(job)
    assert store.recover() == 1
    recovered = store.load(job["job_id"])
    assert recovered["status"] == "failed" and recovered["revision_available"] is True
    assert recovered["error"] == uploads.RESTART_MODEL_MESSAGE and "다시 처리" in recovered["error"]

    unrelated = store.create(reset_job({"kind": "drawing", "rev_id": "R3", "floors_uploaded": [FLOOR]}))
    unrelated["status"] = "running"
    store.save(unrelated)
    assert store.recover() == 1
    recovered = store.load(unrelated["job_id"])
    assert recovered["revision_available"] is False
    assert (
        recovered["error"] == uploads.RESTART_EXISTING_MODEL_MESSAGE and "모델은 남아" in recovered["error"]
    )


def test_agent_tools(toolkit, edit) -> None:
    write_leases(toolkit.ws, edit)
    listing = toolkit.upload_list_jobs()
    assert listing["total"] >= 1 and listing["jobs"][0]["status_ko"] in STATUS_LABELS.values()
    detail = toolkit.upload_list_jobs(job_id=listing["jobs"][0]["job_id"])
    assert detail["summary"].startswith(detail["rev_id"]) and detail["steps"][0]["label"] == "파일 저장"
    with pytest.raises(ToolError):
        toolkit.upload_list_jobs(job_id="J1")
    check = toolkit.revision_check_metadata("R2", floors=["6층"])
    assert check["checked"] and check["floors"] == [FLOOR] and check["summary"].startswith("R2 6F에서 계약")
    assert check["counts"]["brand_mismatch"] == 1 and check["counts"]["unit_missing_in_drawing"] == 1
    assert "area_mismatch" not in check["counts"]
    for bad in (("H1", None), ("R2", ["12F"]), ("R9", None)):
        with pytest.raises(ToolError):
            toolkit.revision_check_metadata(*bad)


# --- metadata -------------------------------------------------------------------------------------------------


def _csv(frame: pd.DataFrame, encoding: str = "utf-8") -> bytes:
    return frame.to_csv(index=False).encode(encoding)


def test_metadata_replacement_is_blocked_by_queued_or_running_jobs(tmp_path, populate) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    store = toolkit._upload_store
    job = store.create(reset_job({"kind": "drawing", "rev_id": "R9"}))
    before = toolkit.ws.table_path("brands").read_bytes()
    app = FastAPI()
    app.include_router(build_upload_router(toolkit))
    client = TestClient(app)
    message = (
        "도면 처리 작업이 진행 중이라 지금은 메타데이터를 바꿀 수 없어요. 작업이 끝난 뒤 다시 올려 주세요."
    )
    for state in ("queued", "running"):
        job["status"] = state
        store.save(job)
        for kind in metadata.KINDS:
            with pytest.raises(ConflictError) as error:
                toolkit.upload_metadata(kind, "input.csv", b"x")
            assert str(error.value) == message
        response = client.post(
            "/api/uploads/metadata",
            data={"kind": "brands"},
            files={"file": ("brands.csv", before, "text/csv")},
        )
        assert response.status_code == 409 and response.json()["detail"] == message
        assert toolkit.ws.table_path("brands").read_bytes() == before
        assert toolkit.ws.read_json(metadata.PROVENANCE_INDEX) is None
    job["status"] = "done"
    store.save(job)
    assert toolkit.upload_metadata("brands", "brands.csv", before)["status"] == "accepted"


def test_running_job_holds_metadata_stable_and_records_snapshot(
    tmp_path, populate, plain_dxf, monkeypatch
) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"), sync=False)
    entered, release = threading.Event(), threading.Event()
    parse = uploads.DrawingPipeline._parse

    def paused(pipeline: Any, job: Any, state: Any) -> Any:
        entered.set()
        if not release.wait(60):
            raise RuntimeError("test release timed out")
        return parse(pipeline, job, state)

    monkeypatch.setattr(uploads.DrawingPipeline, "_parse", paused)
    original_hash = metadata.file_sha256(toolkit.ws.table_path("brands"))
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    try:
        assert entered.wait(60)
        in_progress = toolkit.upload_job(result["job_id"])
        assert in_progress["status"] == "running"
        assert in_progress["metadata_snapshot"]["brands"] == original_hash
        brands = toolkit.ws.table("brands").copy()
        brands.loc[brands.index[0], "brand_name"] += " 변경"
        with pytest.raises(ConflictError):
            toolkit.upload_metadata("brands", "brands.csv", _csv(brands))
    finally:
        release.set()
        assert toolkit._upload_runner.join(timeout=120)
    assert toolkit.upload_job(result["job_id"])["status"] == "done"
    assert toolkit.upload_metadata("brands", "brands.csv", _csv(brands))["status"] == "accepted"
    assert metadata.file_sha256(toolkit.ws.table_path("brands")) != original_hash
    assert toolkit.upload_job(result["job_id"])["metadata_snapshot"]["brands"] == original_hash


def test_job_cannot_start_during_metadata_replacement(tmp_path, populate, plain_dxf, monkeypatch) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    entered, release, draw_started, draw_finished = (threading.Event() for _ in range(4))
    original = metadata._upload_table
    outcomes: dict[str, Any] = {}
    brands = toolkit.ws.table("brands").copy()
    brands.loc[brands.index[0], "brand_name"] += " 변경"

    def paused(host: Any, kind: str, filename: str, data: bytes) -> dict[str, Any]:
        entered.set()
        if not release.wait(60):
            raise RuntimeError("test release timed out")
        return original(host, kind, filename, data)

    def write_metadata() -> None:
        try:
            outcomes["metadata"] = toolkit.upload_metadata("brands", "new.csv", _csv(brands))
        except Exception as exc:
            outcomes["metadata"] = exc

    def write_drawing() -> None:
        draw_started.set()
        try:
            outcomes["drawing"] = toolkit.upload_drawings(
                [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
            )
        except Exception as exc:
            outcomes["drawing"] = exc
        finally:
            draw_finished.set()

    monkeypatch.setattr(metadata, "_upload_table", paused)
    metadata_thread = threading.Thread(target=write_metadata)
    drawing_thread = threading.Thread(target=write_drawing)
    metadata_thread.start()
    try:
        assert entered.wait(60)
        drawing_thread.start()
        assert draw_started.wait(60)
        assert not draw_finished.wait(0.2)
    finally:
        release.set()
        metadata_thread.join(120)
        if drawing_thread.ident is not None:
            drawing_thread.join(120)
    assert not metadata_thread.is_alive() and not drawing_thread.is_alive()
    assert outcomes["metadata"]["status"] == "accepted"
    job = toolkit.upload_job(outcomes["drawing"]["job_id"])
    assert job["status"] == "done"
    assert job["metadata_snapshot"]["brands"] == metadata.file_sha256(toolkit.ws.table_path("brands"))


def test_metadata_and_data_provenance_track_current_inputs(tmp_path, populate, plain_dxf) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    ws = toolkit.ws
    initial = {item["kind"]: item for item in toolkit.metadata_status()}
    brands = initial["brands"]
    assert brands["source"] == "synthetic_demo" and brands["filename"] == "brands.csv"
    assert brands["source_basis"] == "default_assumption"
    assert brands["uploaded_at"] is None and brands["sha256"] == metadata.file_sha256(ws.table_path("brands"))
    assert initial["pos_terminals"]["label_ko"] == "POS 단말 매핑"
    assert "거래·매출 업로드는 지원하지" in initial["pos_terminals"]["description_ko"]

    raw = _csv(ws.table("brands")).replace(b"\n", b"\r\n")
    accepted = toolkit.upload_metadata("brands", "fictional_brands.csv", raw)
    assert accepted["status"] == "accepted"
    status = {item["kind"]: item for item in toolkit.metadata_status()}
    brands = status["brands"]
    assert brands["source"] == "uploaded" and brands["filename"] == "fictional_brands.csv"
    assert brands["source_basis"] == "upload_index"
    assert brands["uploaded_at"] and brands["sha256"] == hashlib.sha256(raw).hexdigest()
    assert brands["stored_sha256"] == metadata.file_sha256(ws.table_path("brands"))
    assert brands["stored_sha256"] != brands["sha256"] and brands["record_matches_content"] is True
    events = ws.read_json(metadata.PROVENANCE_INDEX)["events"]
    assert (
        len(events) == 1 and events[0]["sha256"] == brands["sha256"] and events[0]["rows"] == brands["rows"]
    )
    rejected = toolkit.upload_metadata("brands", "bad.csv", b"brand_id\nBR1\n")
    assert rejected["status"] == "rejected" and len(ws.read_json(metadata.PROVENANCE_INDEX)["events"]) == 1

    stored = ws.table_path("brands").read_bytes()
    ws.write_bytes("business/brands.csv", stored + b"\n")
    assert {item["kind"]: item for item in toolkit.metadata_status()}["brands"][
        "record_matches_content"
    ] is False
    ws.write_bytes("business/brands.csv", stored)
    assert (
        toolkit.upload_metadata("drawing_profile", "fictional.yaml", CUSTOMER_PROFILE_YAML.encode())["status"]
        == "accepted"
    )
    result = toolkit.upload_drawings(
        [("R3_6F.dxf", plain_dxf)], "R3", DATE, parent_rev="R2", run_generators=False
    )
    provenance = toolkit.data_provenance()
    domains = {item["domain"]: item for item in provenance["domains"]}
    assert provenance["ok"] is True and provenance["default_dataset"] == "synthetic_demo"
    assert "허구" in provenance["note"] and "익명화한 것이 아니에요" in provenance["note"]
    assert "직접 교체한 파일의 출처" in provenance["note"]
    assert set(domains) == {
        "drawings",
        "transactions",
        "customers",
        "brands",
        "leases",
        "pos_terminals",
        "stores",
        "store_kpis",
        "briefs",
        "profiles",
    }
    assert domains["transactions"]["source"] == domains["customers"]["source"] == "synthetic_demo"
    assert domains["brands"]["source"] == "uploaded" and domains["brands"]["sha256"] == brands["sha256"]
    assert domains["profiles"]["source"] == "mixed"
    assert {item["source"] for item in domains["profiles"]["items"]} == {"synthetic_demo", "uploaded"}
    assert domains["drawings"]["source"] == "mixed"
    rev = next(row for row in domains["drawings"]["revisions"] if row["rev_id"] == "R3")
    assert rev["source"] == "uploaded" and rev["job_id"] == result["job_id"]
    assert rev["files"][0]["sha256"] == hashlib.sha256(plain_dxf).hexdigest()

    async def call_mcp() -> None:
        async with Client(build_mcp(toolkit)) as client:
            response = await client.call_tool("data_provenance", {})
            payload = json.loads(response.content[0].text)
            assert not response.is_error and len(response.content) == 1
            assert payload["default_dataset"] == "synthetic_demo" and payload["ok"] is True

    asyncio.run(call_mcp())


def test_document_provenance_uses_event_order_when_timestamps_tie(tmp_path, populate) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    for profile_id in ("z", "a"):
        content = json.dumps({"profile_id": profile_id, "title": f"가상 {profile_id}"}).encode("utf-8")
        assert (
            toolkit.upload_metadata("drawing_profile", f"{profile_id}.json", content)["status"] == "accepted"
        )
    index = toolkit.ws.read_json(metadata.PROVENANCE_INDEX)
    for event in index["events"]:
        event["uploaded_at"] = "2026-10-01T10:00:00+09:00"
    toolkit.ws.write_json(metadata.PROVENANCE_INDEX, index)
    profiles = next(row for row in toolkit.metadata_status() if row["kind"] == "drawing_profile")
    assert profiles["source"] == "mixed" and profiles["filename"] == "a.json"
    assert profiles["items"] == ["default", "a", "z"]


def test_invalid_provenance_index_does_not_change_metadata(tmp_path, populate) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    old = toolkit.ws.table_path("brands").read_bytes()
    toolkit.ws.write_json(metadata.PROVENANCE_INDEX, {"version": 1, "events": [{"kind": "brands", "id": []}]})
    with pytest.raises(ToolError, match="출처 기록"):
        toolkit.metadata_status()
    with pytest.raises(ToolError, match="출처 기록"):
        toolkit.upload_metadata("brands", "brands.csv", old)
    assert toolkit.ws.table_path("brands").read_bytes() == old


def test_failed_provenance_record_rolls_back_table_and_document(tmp_path, populate, monkeypatch) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    profile = json.dumps({"profile_id": "p", "title": "가상 최초"}).encode("utf-8")
    assert toolkit.upload_metadata("drawing_profile", "p.json", profile)["status"] == "accepted"
    ws = toolkit.ws
    before_index = (ws.root / metadata.PROVENANCE_INDEX).read_bytes()
    before_brands = ws.table_path("brands").read_bytes()
    before_profile = (ws.root / "business" / "drawing_profiles" / "p.yaml").read_bytes()
    changed = ws.table("brands").copy()
    changed.loc[changed.index[0], "brand_name"] += " 변경"

    def fail_record(*_args: Any) -> None:
        raise OSError("test index write failure")

    monkeypatch.setattr(metadata, "_record_upload", fail_record)
    with pytest.raises(ToolError, match="업로드를 취소"):
        toolkit.upload_metadata("brands", "changed.csv", _csv(changed))
    with pytest.raises(ToolError, match="업로드를 취소"):
        toolkit.upload_metadata(
            "drawing_profile", "p.json", json.dumps({"profile_id": "p", "title": "가상 변경"}).encode()
        )
    assert ws.table_path("brands").read_bytes() == before_brands
    assert (ws.root / "business" / "drawing_profiles" / "p.yaml").read_bytes() == before_profile
    assert (ws.root / metadata.PROVENANCE_INDEX).read_bytes() == before_index


def test_failed_metadata_write_restores_previous_content(tmp_path, populate, monkeypatch) -> None:
    toolkit = _Toolkit(populate(tmp_path / "workspace"))
    ws = toolkit.ws
    before = ws.table_path("brands").read_bytes()
    changed = ws.table("brands").copy()
    changed.loc[changed.index[0], "brand_name"] += " 변경"
    write = metadata.write_table

    def write_then_fail(*args: Any, **kwargs: Any) -> None:
        write(*args, **kwargs)
        raise OSError("test interrupted table write")

    monkeypatch.setattr(metadata, "write_table", write_then_fail)
    with pytest.raises(ToolError, match="업로드를 취소"):
        toolkit.upload_metadata("brands", "changed.csv", _csv(changed))
    assert ws.table_path("brands").read_bytes() == before
    assert ws.read_json(metadata.PROVENANCE_INDEX) is None


def test_upload_file_deletion_uses_workspace_mirror_retry(tmp_path) -> None:
    class Mirror:
        def __init__(self) -> None:
            self.fail = True
            self.deleted: list[str] = []

        def delete(self, name: str) -> None:
            if self.fail:
                raise OSError("test mirror unavailable")
            self.deleted.append(name)

    mirror = Mirror()
    ws = Workspace(tmp_path / "workspace", mirror=mirror)
    ws.write_bytes("business/obsolete.csv", b"fixture", publish=False)
    with pytest.raises(OSError):
        uploads.remove_file(ws, "business/obsolete.csv")
    assert not (ws.root / "business" / "obsolete.csv").exists()
    assert ws.mirror_status()["pending_mirror_writes"] == 1
    mirror.fail = False
    assert ws.retry_pending_mirror() == 1 and mirror.deleted == ["business/obsolete.csv"]


def test_table_metadata_uploads(meta_toolkit) -> None:
    ws = meta_toolkit.ws
    brands = ws.table("brands").copy()
    before = (ws.root / "business" / "brands.csv").read_bytes()
    rejected = {
        "category": brands.drop(columns=["category"]),
        "중복": pd.concat([brands, brands.head(1)]),
        "attractiveness": brands.assign(attractiveness=-1.0),
    }
    for needle, frame in rejected.items():
        outcome = meta_toolkit.upload_metadata("brands", "brands.csv", _csv(frame))
        assert outcome["status"] == "rejected" and any(needle in e for e in outcome["errors"]), outcome
    assert (ws.root / "business" / "brands.csv").read_bytes() == before
    outcome = meta_toolkit.upload_metadata(
        "brands", "brands.csv", _csv(brands.assign(category="UNKNOWN_CAT"))
    )
    assert outcome["status"] == "accepted" and any(
        "카탈로그에 없는 카테고리" in w for w in outcome["warnings"]
    )
    assert (
        outcome["backup"].startswith("business/_backup/brands.")
        and (ws.root / outcome["backup"]).read_bytes() == before
    )
    restored = meta_toolkit.upload_metadata("brands", "brands.csv", before)
    assert restored["status"] == "accepted" and restored["rows"] == len(brands)
    assert set(ws.table("brands")["category"]) == set(brands["category"])

    tenancies = ws.table("tenancies").copy()
    assert meta_toolkit.upload_metadata("tenancies", "tenancies.csv", _csv(tenancies))["status"] == "accepted"
    bad_date = tenancies.astype(str).assign(valid_from="2024/10/01")
    assert "YYYY-MM-DD" in meta_toolkit.upload_metadata("tenancies", "t.csv", _csv(bad_date))["errors"][0]
    reversed_period = tenancies.astype(str).assign(valid_to="2020-01-01")
    errors = meta_toolkit.upload_metadata("tenancies", "t.csv", _csv(reversed_period))["errors"]
    assert any("앞선 행" in e for e in errors)

    pos = ws.table("pos_terminals").copy()
    assert pos["pos_id"].duplicated().any()
    assert meta_toolkit.upload_metadata("pos_terminals", "pos.csv", _csv(pos))["status"] == "accepted"
    first = pos.iloc[[0]].assign(valid_from="2025-01-01", valid_to="2025-12-31")
    overlap = pd.concat([pos, first.assign(valid_from="2024-12-01")])
    errors = meta_toolkit.upload_metadata("pos_terminals", "pos.csv", _csv(overlap))["errors"]
    assert any("겹칩니다" in e for e in errors)


def test_lease_metadata_formats(meta_toolkit) -> None:
    ws = meta_toolkit.ws
    leases = pd.DataFrame(
        [
            ["L1", "BR0001", "1F-C01", "2025-09-01", "2027-08-31", 120.5, 0.27, "갱신 예정"],
            ["L2", "BR0002", "1F-C02", "2025-09-01", "2027-08-31", "", "", "신규"],
        ],
        columns=[
            "lease_id",
            "brand_id",
            "unit_code",
            "lease_start",
            "lease_end",
            "contract_area_m2",
            "commission_rate",
            "memo",
        ],
    )
    (ws.root / "business" / "leases.csv").unlink(missing_ok=True)
    gz = ws.root / "business" / "leases.csv.gz"
    gz.write_bytes(gzip.compress(_csv(leases.head(1))))
    outcome = meta_toolkit.upload_metadata("leases", "leases.csv", _csv(leases, "cp949"))
    assert outcome["status"] == "accepted" and outcome["rows"] == 2 and not gz.exists()
    assert (ws.root / outcome["backup"]).read_text(encoding="utf-8").startswith("lease_id,")
    assert list(ws.table("leases")["memo"]) == ["갱신 예정", "신규"]
    for needle, frame in {
        "lease_id": pd.concat([leases, leases.head(1)]),
        "0~1": leases.assign(commission_rate=27),
        "숫자": leases.assign(contract_area_m2="abc"),
    }.items():
        outcome = meta_toolkit.upload_metadata("leases", "leases.csv", _csv(frame))
        assert outcome["status"] == "rejected" and any(needle in e for e in outcome["errors"]), outcome
    excel = leases.assign(lease_start=[datetime(2025, 9, 1), datetime(2025, 10, 1)])
    buffer = io.BytesIO()
    excel.to_excel(buffer, index=False)
    outcome = meta_toolkit.upload_metadata("leases", "leases.xlsx", buffer.getvalue())
    assert outcome["status"] == "accepted", outcome
    assert list(ws.table("leases")["lease_start"]) == ["2025-09-01", "2025-10-01"]
    overlapping = leases.assign(unit_code="1F-C01", lease_start=["2025-09-01", "2027-01-01"])
    outcome = meta_toolkit.upload_metadata("leases", "leases.csv", _csv(overlapping))
    assert outcome["status"] == "accepted" and any("기간이 겹치는 계약" in w for w in outcome["warnings"])
    for name, data, needle in (
        ("leases.yaml", _csv(leases), "CSV나 XLSX"),
        ("leases.xls", b"x", "XLS"),
        ("leases.csv", b"", "빈 파일"),
    ):
        assert any(needle in e for e in meta_toolkit.upload_metadata("leases", name, data)["errors"])
    with pytest.raises(ToolError):
        meta_toolkit.upload_metadata("transactions", "t.csv", _csv(leases))


def test_document_metadata_uploads(meta_toolkit, monkeypatch) -> None:
    ws = meta_toolkit.ws
    outcome = meta_toolkit.upload_metadata(
        "drawing_profile", "customer_style.yaml", CUSTOMER_PROFILE_YAML.encode()
    )
    assert (outcome["status"], outcome["id"], outcome["backup"]) == ("accepted", "customer_style", None)
    again = meta_toolkit.upload_metadata(
        "drawing_profile", "customer_style.yaml", CUSTOMER_PROFILE_YAML.encode()
    )
    assert again["backup"].startswith("business/_backup/drawing_profiles/customer_style.")
    assert [p["profile_id"] for p in meta_toolkit.upload_profiles()] == ["default", "customer_style"]
    minimal = json.dumps({"profile_id": "plain-m", "title": "미터 도면", "units": "m"}).encode()
    assert meta_toolkit.upload_metadata("drawing_profile", "plain.json", minimal)["status"] == "accepted"
    for name, data, needle in (
        ("p.yaml", b"profile_id: bad\ntitle: t\nunit_code:\n  pattern: '(?P<floor>'\n", "정규식 오류"),
        ("p.yaml", b"profile_id: default\ntitle: t\n", "default"),
        ("p.yaml", b"profile_id: [unclosed\n", "YAML 형식"),
        ("p.txt", b"profile_id: x", "YAML"),
    ):
        outcome = meta_toolkit.upload_metadata("drawing_profile", name, data)
        assert outcome["status"] == "rejected" and any(needle in e for e in outcome["errors"]), outcome

    brief = (
        "brief_id: FW2026\ntitle: 2026 F/W 개편\nseason: FW\nyear: 2026\neffective_date: 2026-10-01\n"
        "base_rev: R2\nfloors: [4F, 5F]\n"
    ).encode()
    lacking = _NoBriefToolkit(ws).upload_metadata("relayout_brief", "fw.yaml", brief)
    assert lacking["status"] == "rejected" and lacking["errors"] == [
        "개편 브리프 검증기를 사용할 수 없습니다."
    ]
    stub = _StubBriefToolkit(ws)
    accepted = stub.upload_metadata("relayout_brief", "fw.yaml", brief)
    assert (accepted["status"], accepted["id"], accepted["warnings"]) == ("accepted", "FW2026", ["스텁 경고"])
    assert (
        (ws.root / "business" / "relayout_briefs" / "FW2026.yaml")
        .read_text(encoding="utf-8")
        .startswith("brief_id: FW2026")
    )
    rejected = stub.upload_metadata("relayout_brief", "fw.yaml", brief.replace(b"season: FW", b"season: XX"))
    assert rejected["status"] == "rejected" and rejected["errors"] == ["season은 SS 또는 FW여야 해요."]
    monkeypatch.setattr(upload_tools, "MAX_METADATA_BYTES", 10)
    with pytest.raises(ToolError, match="까지"):
        stub.upload_metadata("relayout_brief", "fw.yaml", brief)

    status = {row["kind"]: row for row in meta_toolkit.metadata_status()}
    assert list(status) == [
        "brands",
        "tenancies",
        "leases",
        "pos_terminals",
        "relayout_brief",
        "drawing_profile",
        "stores",
        "store_kpis",
    ]
    assert status["brands"]["present"] and status["brands"]["rows"] > 0 and status["brands"]["items"] == []
    assert status["relayout_brief"]["items"] == ["FW2026"] and status["relayout_brief"]["updated_at"]
    assert status["drawing_profile"]["items"] == ["default", "customer_style", "plain-m"]
    assert status["leases"]["required_columns"] == [
        "lease_id",
        "brand_id",
        "unit_code",
        "lease_start",
        "lease_end",
    ]


# --- REST -----------------------------------------------------------------------------------------------------


def test_revision_id_rules_are_shared_by_api_schema_and_validation(toolkit) -> None:
    app = FastAPI()
    app.include_router(build_upload_router(toolkit))
    rules = TestClient(app).get("/api/uploads/rules").json()
    assert rules == {
        "rev_id_pattern": uploads.REV_ID_RE.pattern,
        "reserved_patterns": [uploads.HYPOTHETICAL_ID_RE.pattern],
        "max_length": 16,
    }
    schema = app.openapi()["paths"]["/api/uploads/drawings"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    assert schema["properties"]["rev_id"]["pattern"] == rules["rev_id_pattern"]
    for candidate in ("R3", "H7A", "0", "A" * 16, "9-FOO"):
        assert re.fullmatch(rules["rev_id_pattern"], candidate)
        assert not any(re.fullmatch(pattern, candidate) for pattern in rules["reserved_patterns"])
        assert toolkit._upload_rev_id(candidate) == candidate
    for candidate in ("", "_BAD", "-BAD", "R2 X", "H123", "lower", "A" * 17):
        valid = re.fullmatch(rules["rev_id_pattern"], candidate) and not any(
            re.fullmatch(pattern, candidate) for pattern in rules["reserved_patterns"]
        )
        assert not valid
        with pytest.raises(ToolError):
            toolkit._upload_rev_id(candidate)


def test_rest_routes(toolkit, plain_dxf) -> None:
    app = FastAPI()
    app.include_router(build_upload_router(toolkit))
    client = TestClient(app)
    profile = client.post(
        "/api/uploads/metadata",
        data={"kind": "drawing_profile"},
        files={"file": ("customer_style.yaml", CUSTOMER_PROFILE_YAML.encode(), "application/yaml")},
    )
    assert profile.status_code == 200 and profile.json()["status"] == "accepted"
    assert [p["profile_id"] for p in client.get("/api/uploads/profiles").json()][:2] == [
        "default",
        "customer_style",
    ]
    sample = (SAMPLES / "customer_style_6F.zip").read_bytes()
    form = {
        "rev_id": "R8",
        "parent_rev": "R2",
        "effective_date": DATE,
        "profile": "customer_style",
        "run_generators": "false",
    }
    response = client.post(
        "/api/uploads/drawings",
        data=form,
        files=[("files", ("customer_style_6F.zip", sample, "application/zip"))],
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    job = client.get(f"/api/uploads/jobs/{job_id}").json()
    assert (job["status"], job["profile"], job["files"][0]["floor"]) == ("done", "customer_style", FLOOR)
    assert job["source_files"] == [
        {"name": "customer_style_6F.zip", "bytes": len(sample), "sha256": hashlib.sha256(sample).hexdigest()}
    ]
    assert (
        job["files"][0]["sha256"]
        == hashlib.sha256((toolkit.ws.drawing_dir("R8", "uploaded") / "6F.dxf").read_bytes()).hexdigest()
    )
    assert toolkit.ws.model_meta("R8")["source_files"] == job["source_files"]
    assert job["parse"]["units"] == len(toolkit.ws.model("R2").units(FLOOR)) and job["changes"]["total"] == 0
    rows = client.get("/api/uploads/jobs").json()
    assert rows[0]["job_id"] == job_id and set(rows[0]) == {
        "job_id",
        "kind",
        "status",
        "rev_id",
        "created_at",
        "updated_at",
        "revision_available",
        "review_reason",
        "stage_errors",
        "superseded_by",
    }
    assert client.get("/api/uploads/jobs/J20990101-0001").status_code == 404
    assert client.get("/api/uploads/jobs/nope").status_code == 404

    dxf = [("files", ("R8_6F.dxf", plain_dxf, "application/dxf"))]
    conflict = client.post("/api/uploads/drawings", data={**form, "rev_id": "R2"}, files=dxf)
    assert conflict.status_code == 409 and "R2" in conflict.json()["detail"]
    bad_name = client.post(
        "/api/uploads/drawings", data=form, files=[("files", ("plan.dxf", plain_dxf, "application/dxf"))]
    )
    assert bad_name.status_code == 400 and "층" in bad_name.json()["detail"]
    for broken in ({k: v for k, v in form.items() if k != "rev_id"}, {**form, "run_generators": "maybe"}):
        assert client.post("/api/uploads/drawings", data=broken, files=dxf).status_code == 400
    assert client.post("/api/uploads/drawings", data=form).status_code == 400

    mapping_url = f"/api/uploads/jobs/{job_id}/layer-mapping"
    assert (
        client.post(mapping_url, json={"mappings": [{"layer": "보행통로", "semantic": "bogus"}]}).status_code
        == 400
    )
    assert client.post(mapping_url, json={"mappings": []}).status_code == 422
    unknown = "/api/uploads/jobs/J20990101-0001/layer-mapping"
    assert client.post(unknown, json={"mappings": [{"layer": "A", "semantic": "unit"}]}).status_code == 404
    remapped = client.post(
        mapping_url, json={"mappings": [{"layer": "보행통로", "semantic": "walkway", "floor": "6F"}]}
    )
    assert (
        remapped.status_code == 200
        and remapped.json()["job_id"] == job_id
        and remapped.json()["status"] == "done"
    )

    leases = "lease_id,brand_id,unit_code,lease_start,lease_end\nL9,BR0001,6F-C01,2025-09-01,2027-08-31\n"
    accepted = client.post(
        "/api/uploads/metadata",
        data={"kind": "leases"},
        files={"file": ("leases.csv", leases.encode(), "text/csv")},
    )
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    rejected = client.post(
        "/api/uploads/metadata",
        data={"kind": "leases"},
        files={"file": ("leases.csv", b"lease_id\nL1\n", "text/csv")},
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    bogus = client.post(
        "/api/uploads/metadata", data={"kind": "bogus"}, files={"file": ("x.csv", b"a\n1\n", "text/csv")}
    )
    assert bogus.status_code == 400
    status = client.get("/api/uploads/metadata").json()
    kinds = [row["kind"] for row in status]
    assert kinds[5:] == ["drawing_profile", "stores", "store_kpis"] and len(status) == 8
