"""Drawing and metadata uploads (docs/specs/atlas-api.md §1.2): jobs, validation, metadata and REST routes."""

from __future__ import annotations

import gzip
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shapely import affinity

from cad2anything.mockgen.dxf_writer import write_clean_floor
from cad2anything.mockgen.upload_samples import CUSTOMER_PROFILE_YAML
from cad2anything.model import EntityKind, RevisionKind, SpatialModel
from cad2anything.services import upload_tools, uploads
from cad2anything.services.errors import ToolError
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
    assert job["files"] == [{"name": "R3_6F.dxf", "floor": FLOOR, "bytes": len(floor_dxf)}]
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
    assert generate["status"] == "done" and job["artifacts"]
    assert [a["generator"] for a in job["artifacts"]] == [entry["generator"] for entry in job["impact"]]
    assert all(a["status"] != "failed" for a in job["artifacts"])
    assert (toolkit.ws.artifacts_dir / "R4" / "area_schedule" / "artifact.json").is_file()


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
    assert job["parse"]["units"] == len(toolkit.ws.model("R2").units(FLOOR)) and job["changes"]["total"] == 0
    rows = client.get("/api/uploads/jobs").json()
    assert rows[0]["job_id"] == job_id and set(rows[0]) == {
        "job_id",
        "kind",
        "status",
        "rev_id",
        "created_at",
        "updated_at",
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
    assert [row["kind"] for row in status][-1] == "drawing_profile" and len(status) == 6
