"""Drawing upload jobs (docs/specs/atlas-api.md §1.2): file intake, job store, single-worker runner, pipeline.

An upload becomes a job stored at ``uploads/jobs/{job_id}.json``. The job saves the DXF files under
``drawings/{rev}/uploaded/`` with a generated manifest, parses the uploaded floors with the chosen drawing
profile, merges them with the parent revision's other floors, diffs against the parent, checks the drawing
against lease data, plans and regenerates impacted artifacts and compares a selected relayout plan. Jobs run
one at a time: inline in sync mode or on one daemon worker thread.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import logging
import queue
import re
import shutil
import threading
import time
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from ezdxf import DXFError

from cad2anything.cadcore.profile import (
    DrawingProfile,
    merge_layers,
    normalize_floor,
    profile_to_dict,
)
from cad2anything.cadcore.reader import Issue
from cad2anything.model import ChangeSet, Entity, Floor, Revision, RevisionKind, SpatialModel
from cad2anything.services.errors import NotFoundToolError, ToolError

logger = logging.getLogger("cad2anything.uploads")

KST = timezone(timedelta(hours=9), "KST")
MB = 1024 * 1024
MAX_FILE_BYTES = 30 * MB
MAX_TOTAL_BYTES = 100 * MB
MAX_FILES = 50
JOB_DIR = "uploads/jobs"
STAGING_DIR = "uploads/staging"
UPLOAD_VARIANT = "uploaded"
JOB_ID_RE = re.compile(r"^J(\d{8})-(\d{4})$")
REV_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,15}$")
HYPOTHETICAL_ID_RE = re.compile(r"^H\d+$")
STEP_LABELS: dict[str, str] = {
    "save": "파일 저장",
    "parse": "도면 읽기",
    "merge": "층 합치기",
    "diff": "변경 비교",
    "qa": "계약 정보 대조",
    "impact": "영향 분석",
    "generate": "산출물 재생성",
    "relayout_check": "개편안 대조",
}
STEPS: tuple[str, ...] = tuple(STEP_LABELS)
STATUS_LABELS: dict[str, str] = {
    "queued": "대기 중",
    "running": "처리 중",
    "needs_review": "검토 필요",
    "done": "완료",
    "failed": "실패",
}
STEP_STATUS_LABELS: dict[str, str] = {
    "pending": "대기",
    "running": "진행 중",
    "done": "완료",
    "skipped": "건너뜀",
    "failed": "실패",
}
CHANGE_LABELS: dict[str, str] = {
    "add": "신설",
    "remove": "삭제",
    "move": "이동",
    "resize": "면적 변경",
    "reassign": "교체",
    "split": "분할",
    "merge": "병합",
    "fixture_change": "집기 변경",
    "walkway_change": "통로 변경",
}
ACTIVE_STATUSES = ("queued", "running")
RESTART_MESSAGE = "서버가 다시 시작되어 작업이 중단되었습니다. 다시 올려 주세요."
_FLOOR_IN_NAME = re.compile(
    r"(?<![A-Za-z0-9])(지하\s*\d{1,2}\s*층|B\d{1,2}F?|\d{1,3}\s*층|\d{1,3}F)(?![A-Za-z0-9])", re.IGNORECASE
)
_DRIVE = re.compile(r"^[A-Za-z]:$")
_FACILITY_SOURCE = re.compile(r"^cad2anything\.[a-z]+:([^/\s]+)/")


class ConflictError(ToolError):
    """The request conflicts with existing data (REST 409)."""


class NotFoundError(NotFoundToolError):
    """Unknown upload job or drawing profile (REST 404)."""


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def size_text(size: int) -> str:
    return f"{size / MB:g}MB" if size >= MB else f"{size:,}바이트"


def remove_file(ws: Any, rel_path: str) -> None:
    """Delete a workspace file locally and from the mirror when it supports deletes (best effort)."""
    (ws.root / rel_path).unlink(missing_ok=True)
    delete = getattr(ws.mirror, "delete", None)
    if callable(delete):
        try:
            delete(rel_path)
        except Exception:
            logger.warning("could not delete %s from the workspace mirror", rel_path)


# --- file intake ------------------------------------------------------------------------------------------


@dataclass
class DrawingFile:
    name: str
    floor: str
    data: bytes


def base_name(name: Any) -> str:
    """File name without any directory part (``a/b\\c.dxf`` -> ``c.dxf``)."""
    return PurePosixPath(str(name or "").replace("\\", "/")).name.strip()


def detect_floor(file_name: str) -> str | None:
    """Canonical floor in a file name: ``R3_4F.dxf``, ``4F.dxf``, ``b1.dxf``, ``4층.dxf``, ``지하1층.dxf``."""
    stem = PurePosixPath(file_name).stem
    floors = list(
        dict.fromkeys(f for m in _FLOOR_IN_NAME.finditer(stem) if (f := normalize_floor(m.group(1))))
    )
    if len(floors) > 1:
        raise ToolError(f"{file_name}: 파일 이름에 층 코드가 여러 개 있습니다({', '.join(floors)}).")
    return floors[0] if floors else None


def _check_dxf(name: str, data: bytes) -> None:
    if not data:
        raise ToolError(f"{name}: 빈 파일입니다.")
    head = data[:4096]
    if head.startswith(b"AutoCAD Binary DXF"):
        return
    if head.startswith(b"AC10"):
        raise ToolError(f"{name}: DWG 파일은 받지 않습니다. CAD 프로그램에서 DXF로 저장해 올려 주세요.")
    if b"SECTION" not in head.upper():
        raise ToolError(f"{name}: DXF 형식의 파일이 아닙니다.")


def _zip_path(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:  # not flagged UTF-8: Korean Windows tools write CP949 names
        with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
            name = name.encode("cp437").decode("cp949")
    return name.replace("\\", "/")


def _zip_members(name: str, data: bytes) -> list[tuple[str, bytes]]:
    """DXF members of a ZIP (base names only); rejects unsafe paths, encrypted members and oversize files."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ToolError(f"{name}: ZIP 파일을 열 수 없습니다.") from exc
    members: list[tuple[str, bytes]] = []
    total = 0
    with archive:
        for info in archive.infolist():
            path = _zip_path(info)
            parts = [part for part in path.split("/") if part not in ("", ".")]
            if path.startswith("/") or ".." in parts or (parts and _DRIVE.match(parts[0])):
                raise ToolError(f"{name}: 안전하지 않은 경로가 들어 있습니다: {info.filename}")
            if info.is_dir() or not parts:
                continue
            member = parts[-1]
            if not member.lower().endswith(".dxf") or member.startswith("._") or "__MACOSX" in parts:
                continue
            if info.flag_bits & 0x1:
                raise ToolError(f"{name}: 암호가 걸린 파일은 열 수 없습니다: {member}")
            if info.file_size > MAX_FILE_BYTES:
                raise ToolError(f"{member}: 파일 하나는 {size_text(MAX_FILE_BYTES)}까지 올릴 수 있습니다.")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ToolError(
                    f"전체 파일은 {size_text(MAX_TOTAL_BYTES)}까지 올릴 수 있습니다(ZIP 압축 해제 기준)."
                )
            try:
                with archive.open(info) as handle:
                    content = handle.read(MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
                raise ToolError(f"{name}: {member}의 압축을 풀 수 없습니다.") from exc
            if len(content) > MAX_FILE_BYTES:
                raise ToolError(f"{member}: 파일 하나는 {size_text(MAX_FILE_BYTES)}까지 올릴 수 있습니다.")
            members.append((member, content))
    if not members:
        raise ToolError(f"{name} 안에 DXF 파일이 없습니다.")
    return members


def collect_drawing_files(files: Sequence[tuple[str, bytes]]) -> list[DrawingFile]:
    """Validated DXF files with their floors from several DXF files or one ZIP (Korean ``ToolError``)."""
    if not files:
        raise ToolError("도면 파일(files)이 없습니다. DXF 파일 여러 개나 ZIP 파일 1개를 올리세요.")
    named = [(base_name(name), bytes(data or b"")) for name, data in files]
    if any(not name for name, _ in named):
        raise ToolError("이름이 없는 파일이 있습니다.")
    zips = [item for item in named if item[0].lower().endswith(".zip")]
    if zips and len(named) != 1:
        raise ToolError("ZIP 파일은 1개만, 다른 파일 없이 올려야 합니다.")
    if zips and len(zips[0][1]) > MAX_TOTAL_BYTES:
        raise ToolError(f"전체 파일은 {size_text(MAX_TOTAL_BYTES)}까지 올릴 수 있습니다.")
    entries = _zip_members(*zips[0]) if zips else named
    if len(entries) > MAX_FILES:
        raise ToolError(f"도면 파일이 너무 많습니다(최대 {MAX_FILES}개).")
    result: list[DrawingFile] = []
    seen: dict[str, str] = {}
    total = 0
    for name, data in entries:
        if not name.lower().endswith(".dxf"):
            hint = (
                " DWG는 CAD 프로그램에서 DXF로 저장해 올려 주세요." if name.lower().endswith(".dwg") else ""
            )
            raise ToolError(f"{name}: DXF(.dxf) 파일이나 ZIP 파일만 올릴 수 있습니다.{hint}")
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(f"{name}: 파일 하나는 {size_text(MAX_FILE_BYTES)}까지 올릴 수 있습니다.")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ToolError(f"전체 파일은 {size_text(MAX_TOTAL_BYTES)}까지 올릴 수 있습니다.")
        _check_dxf(name, data)
        floor = detect_floor(name)
        if floor is None:
            raise ToolError(
                f"{name}: 파일 이름에서 층을 찾을 수 없습니다. 예: R3_4F.dxf, 6F.dxf, B1.dxf, 4층.dxf"
            )
        if floor in seen:
            raise ToolError(f"같은 층({floor}) 파일이 둘 있습니다: {seen[floor]}, {name}")
        seen[floor] = name
        result.append(DrawingFile(name, floor, data))
    return result


# --- job records ------------------------------------------------------------------------------------------


def new_steps() -> list[dict[str, Any]]:
    return [
        {"name": name, "label": label, "status": "pending", "message": ""}
        for name, label in STEP_LABELS.items()
    ]


def reset_job(job: dict[str, Any]) -> dict[str, Any]:
    """Queued job with pending steps and no results (for re-runs)."""
    job.update(
        status="queued",
        steps=new_steps(),
        parse=None,
        changes=None,
        impact=None,
        artifacts=None,
        qa=None,
        relayout_check=None,
        error=None,
        started_at=None,
        finished_at=None,
    )
    return job


def job_list_row(job: Mapping[str, Any]) -> dict[str, Any]:
    return {key: job.get(key) for key in ("job_id", "kind", "status", "rev_id", "created_at", "updated_at")}


class JobStore:
    """Upload job records at ``uploads/jobs/{job_id}.json``; every read and write holds one lock."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.lock = threading.RLock()

    @staticmethod
    def rel_path(job_id: str) -> str:
        return f"{JOB_DIR}/{job_id}.json"

    def load(self, job_id: Any) -> dict[str, Any]:
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise NotFoundError(f"작업 {job_id}가 없습니다.")
        with self.lock:
            data = self.ws.read_json(self.rel_path(job_id))
        if data is None:
            raise NotFoundError(f"작업 {job_id}가 없습니다.")
        return data

    def save(self, job: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            job["updated_at"] = now_iso()
            self.ws.write_json(self.rel_path(job["job_id"]), job)
        return job

    def create(self, job: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            day = datetime.now(KST).strftime("%Y%m%d")
            numbers = [
                int(match.group(2))
                for path in (self.ws.root / JOB_DIR).glob(f"J{day}-*.json")
                if (match := JOB_ID_RE.match(path.stem))
            ]
            job["job_id"] = f"J{day}-{max(numbers, default=0) + 1:04d}"
            job["created_at"] = now_iso()
            return self.save(job)

    def all(self) -> list[dict[str, Any]]:
        """Every job, newest first."""
        jobs: list[dict[str, Any]] = []
        with self.lock:
            for path in (self.ws.root / JOB_DIR).glob("J*.json"):
                if not JOB_ID_RE.match(path.stem):
                    continue
                try:
                    jobs.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    logger.warning("unreadable upload job file %s", path.name)
        return sorted(
            jobs, key=lambda j: (str(j.get("created_at") or ""), str(j.get("job_id") or "")), reverse=True
        )

    def recover(self) -> int:
        """Fail jobs left queued or running by a previous process; drop leftover staging files."""
        count = 0
        with self.lock:
            for job in self.all():
                if job.get("status") not in ACTIVE_STATUSES:
                    continue
                for step in job.get("steps") or []:
                    if step.get("status") == "running":
                        step.update(status="failed", message=RESTART_MESSAGE)
                job.update(status="failed", error=RESTART_MESSAGE, finished_at=now_iso())
                self.save(job)
                count += 1
        shutil.rmtree(self.ws.root / STAGING_DIR, ignore_errors=True)
        return count


class JobRunner:
    """Runs jobs one at a time: inline (``sync``) or on a single daemon worker thread fed by a queue."""

    def __init__(self, process: Callable[[str], None], sync: bool) -> None:
        self.process = process
        self.sync = sync
        self.queue: queue.Queue[str] = queue.Queue()
        self._run_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, job_id: str) -> None:
        if self.sync:
            self._run(job_id)
            return
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="atlas-upload-worker", daemon=True)
                self._thread.start()
        self.queue.put(job_id)

    def _loop(self) -> None:
        while True:
            job_id = self.queue.get()
            try:
                self._run(job_id)
            finally:
                self.queue.task_done()

    def _run(self, job_id: str) -> None:
        with self._run_lock:
            try:
                self.process(job_id)
            except Exception:
                logger.exception("upload job %s crashed", job_id)

    def join(self, timeout: float | None = None) -> bool:
        """Wait until every submitted job has finished; False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.queue.unfinished_tasks:
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True


# --- manifest and merge -----------------------------------------------------------------------------------


def apply_layer_mappings(manifest: dict[str, Any], mappings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Write human layer answers into ``layers_by_floor`` (with floor) or ``layers`` (without); a layer keeps
    one semantic per scope and later answers win."""
    layers = {sem: list(names) for sem, names in dict(manifest.get("layers") or {}).items()}
    by_floor = {
        floor: {sem: list(names) for sem, names in dict(mapping).items()}
        for floor, mapping in dict(manifest.get("layers_by_floor") or {}).items()
    }
    for item in mappings:
        target = by_floor.setdefault(str(item["floor"]), {}) if item.get("floor") else layers
        for names in target.values():
            if item["layer"] in names:
                names.remove(item["layer"])
        target.setdefault(str(item["semantic"]), []).append(str(item["layer"]))
    layers = {sem: names for sem, names in layers.items() if names}
    by_floor = {f: {s: n for s, n in m.items() if n} for f, m in by_floor.items()}
    manifest.pop("layers", None)
    manifest.pop("layers_by_floor", None)
    if layers:
        manifest["layers"] = layers
    if any(by_floor.values()):
        manifest["layers_by_floor"] = {f: m for f, m in by_floor.items() if m}
    return manifest


def build_manifest(
    job: Mapping[str, Any], parent: SpatialModel, profile: DrawingProfile, facility: str
) -> dict[str, Any]:
    """``manifest.json`` for the uploaded floors (levels copied from the parent revision's floors)."""
    floors = {f.code: f for f in parent.floors}
    manifest: dict[str, Any] = {
        "facility": facility,
        "revision": job["rev_id"],
        "kind": "actual",
        "parent_revision": job["parent_rev"],
        "effective_date": job["effective_date"],
        "variant": UPLOAD_VARIANT,
        "levels": [
            {
                "floor": code,
                "ordinal": floors[code].ordinal,
                "name": floors[code].name,
                "file": f"{code}.dxf",
                "elevation_m": floors[code].elevation_m,
                "height_m": floors[code].height_m,
            }
            for code in job["floors_uploaded"]
        ],
        "upload_job": job["job_id"],
        "drawing_profile": profile_to_dict(profile),
    }
    if job.get("units"):
        manifest["units"] = job["units"]
    if profile.layers:
        manifest["layers"] = merge_layers(profile.layers, None)
    return apply_layer_mappings(manifest, job.get("layer_mappings") or [])


def facility_code(ws: Any, parent: SpatialModel) -> str:
    """Facility of the parent revision (model source, else its drawing manifests), else ``FACILITY``."""
    match = _FACILITY_SOURCE.match(str(parent.revision.source or ""))
    if match:
        return match.group(1)
    folder = ws.root / "drawings" / parent.revision.rev_id
    for path in sorted(folder.glob("*/manifest.json")) if folder.is_dir() else []:
        try:
            facility = json.loads(path.read_text(encoding="utf-8")).get("facility")
        except (OSError, ValueError):
            continue
        if facility:
            return str(facility)
    return "FACILITY"


def merge_floors(
    parent: SpatialModel, parsed: SpatialModel, floors: Sequence[str], revision: Revision
) -> tuple[SpatialModel, list[Issue]]:
    """Parent entities on the other floors plus parsed entities on ``floors`` (parent floor order)."""
    uploaded = set(floors)
    model = SpatialModel(revision=revision, floors=[Floor(**f.to_dict()) for f in parent.floors])
    issues: list[Issue] = []
    for entity in parent.entities.values():
        if entity.floor not in uploaded:
            model.add(_copy_entity(entity, entity.entity_id))
    for entity in parsed.entities.values():
        if entity.floor not in uploaded:
            continue
        entity_id, n = entity.entity_id, 2
        while entity_id in model.entities:
            entity_id, n = f"{entity.entity_id}~{n}", n + 1
        if entity_id != entity.entity_id:
            issues.append(
                Issue(
                    "warning",
                    "entity_id_conflict",
                    f"{entity.floor} 요소 {entity.entity_id}가 다른 층 요소와 ID가 겹쳐 {entity_id}로 저장했다.",
                    entity.floor,
                    entity_ids=[entity_id],
                )
            )
        model.add(_copy_entity(entity, entity_id))
    order = {f.code: i for i, f in enumerate(model.floors)}
    model.entities = dict(sorted(model.entities.items(), key=lambda kv: order.get(kv[1].floor or "", -1)))
    return model, issues


def _copy_entity(entity: Entity, entity_id: str) -> Entity:
    # Shapely geometries are immutable, so sharing them with the cached source model is safe.
    return Entity(
        entity_id, entity.kind, entity.floor, entity.geometry, entity.label, copy.deepcopy(entity.attributes)
    )


# --- summaries --------------------------------------------------------------------------------------------


def review_item(row: Mapping[str, Any]) -> dict[str, Any]:
    alternatives = row.get("alternatives") or {}
    if isinstance(alternatives, Mapping):
        names = sorted(alternatives, key=lambda name: -float(alternatives[name] or 0))
    else:
        names = list(alternatives)
    return {
        "floor": row.get("floor"),
        "layer": row.get("layer"),
        "semantic": row.get("semantic"),
        "semantic_ko": row.get("semantic_ko"),
        "confidence": row.get("confidence"),
        "alternatives": names[:4],
        "samples": list(row.get("sample_texts") or [])[:6],
        "entities": {k: v for k, v in dict(row.get("entities") or {}).items() if v},
        "evidence": list(row.get("evidence") or [])[:3],
        "question": row.get("question"),
    }


def parse_summary(report: Mapping[str, Any], model: SpatialModel, floors: Sequence[str]) -> dict[str, Any]:
    issues = list(report.get("issues") or [])
    severities = {
        sev: sum(1 for i in issues if i.get("severity") == sev) for sev in ("error", "warning", "info")
    }
    queue_rows = list(report.get("review_queue") or [])
    units = model.units()
    codes: dict[str, int] = {}
    for issue in issues:
        codes[issue["code"]] = codes.get(issue["code"], 0) + 1
    return {
        "profile": (report.get("stats") or {}).get("profile_id"),
        "units": len(units),
        "vacant_units": sum(1 for u in units if u.attributes.get("vacant")),
        "brands_resolved": sum(1 for u in units if u.attributes.get("brand_id")),
        "by_floor": {
            floor: {
                "units": len(model.units(floor)),
                "review_layers": sum(1 for row in queue_rows if row.get("floor") == floor),
            }
            for floor in floors
        },
        "issues": severities,
        "issue_codes": dict(sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))[:12]),
        "review_queue": [review_item(row) for row in queue_rows[:30]],
        "review_total": len(queue_rows),
    }


def changes_message(by_type: Mapping[str, int], total: int) -> str:
    if not total:
        return "변경 없음"
    parts = ", ".join(f"{CHANGE_LABELS.get(kind, kind)} {count}" for kind, count in by_type.items())
    return f"변경 {total}건({parts})"


def qa_message(qa: Mapping[str, Any]) -> str:
    if not qa.get("total"):
        return f"계약 {qa.get('leases_checked', 0)}건과 일치"
    parts = [
        f"{label} {qa.get(key, 0)}건"
        for key, label in (("errors", "오류"), ("warnings", "경고"), ("infos", "참고"))
    ]
    return ", ".join(part for part in parts if not part.endswith(" 0건"))


def _failure_message(step: str, exc: BaseException) -> str:
    if isinstance(exc, ToolError):
        return str(exc)
    if isinstance(exc, DXFError):
        return "DXF 파일을 읽을 수 없습니다. 파일이 손상되었거나 DXF 형식이 아닙니다."
    return f"{STEP_LABELS.get(step, step)} 중 오류가 발생했습니다({type(exc).__name__})."


# --- pipeline ---------------------------------------------------------------------------------------------


@dataclass
class _RunState:
    profile: DrawingProfile | None = None
    parent: SpatialModel | None = None
    facility: str = "FACILITY"
    parsed: SpatialModel | None = None
    merged: SpatialModel | None = None
    change_set: ChangeSet | None = None
    plan: list[Any] | None = None


StepResult = str | tuple[str, str]


class DrawingPipeline:
    """Runs one drawing upload job through the §1.2 steps against a host toolkit.

    The host provides ``ws``, ``_model``, ``_table``, ``registry_run``, ``revision_diff``, ``metadata_qa`` and
    ``_upload_profile``; ``relayout_after_upload(rev, parent_rev)`` is optional.
    """

    def __init__(self, host: Any, store: JobStore, relations: str = "full") -> None:
        self.host = host
        self.store = store
        self.relations = relations

    @property
    def ws(self) -> Any:
        return self.host.ws

    def run(self, job_id: str) -> None:
        job = self.store.load(job_id)
        try:
            self._run(job)
        except Exception as exc:
            logger.exception("upload job %s failed unexpectedly", job_id)
            error = (
                str(exc)
                if isinstance(exc, ToolError)
                else f"작업 처리 중 오류가 발생했습니다({type(exc).__name__})."
            )
            job.update(status="failed", error=error, finished_at=now_iso())
            self.store.save(job)

    def _run(self, job: dict[str, Any]) -> None:
        job.update(status="running", started_at=now_iso(), finished_at=None, error=None)
        self.store.save(job)
        state = _RunState()
        for name, step in (("save", self._save), ("parse", self._parse), ("merge", self._merge)):
            error = self._execute(job, name, step, state)
            if error is not None:
                for later in job["steps"]:
                    if later["status"] == "pending":
                        later.update(status="skipped", message="앞 단계가 실패해 건너뛰었습니다.")
                job.update(status="failed", error=error, finished_at=now_iso())
                self.store.save(job)
                return
        for name, step in (
            ("diff", self._diff),
            ("qa", self._qa),
            ("impact", self._impact),
            ("generate", self._generate),
            ("relayout_check", self._relayout_check),
        ):
            self._execute(job, name, step, state)
        job["status"] = "needs_review" if (job.get("parse") or {}).get("review_queue") else "done"
        job["finished_at"] = now_iso()
        self.store.save(job)

    def _execute(
        self,
        job: dict[str, Any],
        name: str,
        step: Callable[[dict[str, Any], _RunState], StepResult],
        state: _RunState,
    ) -> str | None:
        """Run one step and record it; returns the failure message or None."""
        record = next(s for s in job["steps"] if s["name"] == name)
        record.update(status="running", message="")
        self.store.save(job)
        started = time.perf_counter()
        try:
            outcome = step(job, state)
        except Exception as exc:
            if not isinstance(exc, ToolError):
                logger.exception("upload job %s: step %s failed", job["job_id"], name)
            message = _failure_message(name, exc)
            record.update(status="failed", message=message, elapsed_s=round(time.perf_counter() - started, 2))
            self.store.save(job)
            return message
        status, message = outcome if isinstance(outcome, tuple) else ("done", outcome)
        record.update(status=status, message=message, elapsed_s=round(time.perf_counter() - started, 2))
        self.store.save(job)
        return None

    def _remove(self, rel_path: str) -> None:
        remove_file(self.ws, rel_path)

    # Steps ---------------------------------------------------------------------------------------------
    def _save(self, job: dict[str, Any], state: _RunState) -> StepResult:
        state.parent = self.host._model(job["parent_rev"])
        state.profile = self.host._upload_profile(job["profile"])
        rev, floors = job["rev_id"], list(job["floors_uploaded"])
        folder = f"drawings/{rev}/{UPLOAD_VARIANT}"
        target = self.ws.drawing_dir(rev, UPLOAD_VARIANT)
        staging = self.ws.root / STAGING_DIR / job["job_id"]
        if staging.is_dir():
            keep = {f"{floor}.dxf" for floor in floors}
            for old in sorted(target.glob("*.dxf")) if target.is_dir() else []:
                if old.name not in keep:
                    self._remove(f"{folder}/{old.name}")
            for floor in floors:
                self.ws.write_bytes(f"{folder}/{floor}.dxf", (staging / f"{floor}.dxf").read_bytes())
            shutil.rmtree(staging, ignore_errors=True)
        elif any(not (target / f"{floor}.dxf").is_file() for floor in floors):
            raise ToolError("올린 도면 파일을 찾을 수 없습니다. 도면을 다시 올려 주세요.")
        state.facility = facility_code(self.ws, state.parent)
        manifest = build_manifest(job, state.parent, state.profile, state.facility)
        self.ws.write_json(f"{folder}/manifest.json", manifest)
        return f"도면 {len(floors)}개 저장({', '.join(floors)})"

    def _parse(self, job: dict[str, Any], state: _RunState) -> StepResult:
        from cad2anything.cadcore.pipeline import parse_revision

        rev, floors = job["rev_id"], list(job["floors_uploaded"])
        brands = self.host._table("brands", required=False)
        model, report = parse_revision(
            self.ws.drawing_dir(rev, UPLOAD_VARIANT),
            brand_registry=brands,
            rev_id=rev,
            relations="basic",
            floors=floors,
            profile=state.profile,
        )
        report_dict = report.to_dict()
        report_dict["upload_job"] = job["job_id"]
        self.ws.save_report(rev, report_dict)
        job["parse"] = parse_summary(report_dict, model, floors)
        empty = [floor for floor in floors if not model.units(floor)]
        if empty:
            raise ToolError(
                f"{', '.join(empty)} 도면에서 매장을 찾지 못했습니다. 도면 규약 프로필(현재 {job['profile']})과 "
                "레이어 매핑을 확인하세요."
            )
        state.parsed = model
        message = f"매장 {job['parse']['units']}개"
        if job["parse"]["review_total"]:
            message += f", 검토 필요 레이어 {job['parse']['review_total']}개"
        return message

    def _merge(self, job: dict[str, Any], state: _RunState) -> StepResult:
        from cad2anything.cadcore.pipeline import model_relations

        assert state.parent is not None and state.parsed is not None
        rev, parent_rev = job["rev_id"], job["parent_rev"]
        replacing = self.ws.has_model(rev)
        if replacing and self.ws.model_meta(rev).get("source") != "uploaded":
            raise ConflictError(
                f"개정 {rev}가 이미 있습니다(업로드로 만든 개정이 아님). 다른 개정 ID로 올려 주세요."
            )
        revision = Revision(
            rev,
            RevisionKind.ACTUAL,
            parent_rev,
            job["effective_date"],
            source=f"cad2anything.upload:{state.facility}/{rev}/{UPLOAD_VARIANT}",
            notes=f"업로드 도면({job['job_id']}, {', '.join(job['floors_uploaded'])})",
        )
        merged, issues = merge_floors(state.parent, state.parsed, job["floors_uploaded"], revision)
        relations, relation_issues = model_relations(merged, self.relations)
        merged.relations = relations
        merged.issues = [*state.parsed.issues, *(issue.to_dict() for issue in [*issues, *relation_issues])]
        if replacing:
            self._clear_outputs(rev)
        self.ws.save_model(
            merged,
            {
                "source": "uploaded",
                "variant": UPLOAD_VARIANT,
                "job_id": job["job_id"],
                "parent": parent_rev,
                "profile": job["profile"],
                "floors_uploaded": list(job["floors_uploaded"]),
            },
        )
        state.merged = merged
        uploaded = set(job["floors_uploaded"])
        job["floors_inherited"] = [f.code for f in merged.floors if f.code not in uploaded]
        return (
            f"올린 층 {len(uploaded)}개({', '.join(job['floors_uploaded'])}) + {parent_rev}의 나머지 층 "
            f"{len(job['floors_inherited'])}개"
        )

    def _clear_outputs(self, rev: str) -> None:
        """Drop change sets into ``rev`` and its artifacts left by an earlier upload of the same revision."""
        for path in sorted((self.ws.root / "changes").glob(f"*__{rev}.json")):
            self._remove(f"changes/{path.name}")
        folder = self.ws.artifacts_dir / rev
        if folder.is_dir():
            for path in sorted(p for p in folder.rglob("*") if p.is_file()):
                self._remove(path.relative_to(self.ws.root).as_posix())
            shutil.rmtree(folder, ignore_errors=True)

    def _diff(self, job: dict[str, Any], state: _RunState) -> StepResult:
        from cad2anything.revision.diff import diff_models

        assert state.parent is not None and state.merged is not None
        change_set = diff_models(state.parent, state.merged)
        self.ws.save_change_set(change_set, {"source": "diff", "job_id": job["job_id"]})
        state.change_set = change_set
        summary = self.host.revision_diff(job["parent_rev"], job["rev_id"], limit=30)
        job["changes"] = {
            key: summary.get(key) for key in ("total", "by_type", "by_floor", "items", "truncated")
        }
        return changes_message(summary.get("by_type") or {}, int(summary.get("total") or 0))

    def _qa(self, job: dict[str, Any], state: _RunState) -> StepResult:
        assert state.merged is not None
        result = self.host.metadata_qa(state.merged, job["effective_date"], floors=job["floors_uploaded"])
        if result.get("skipped"):
            job["qa"] = None
            return "skipped", str(result.get("message") or "계약 정보가 없어 대조하지 않았습니다.")
        job["qa"] = result
        return qa_message(result)

    def _impact(self, job: dict[str, Any], state: _RunState) -> StepResult:
        from cad2anything.generators import load_specs, plan_impact

        if state.change_set is None:
            return "skipped", "변경 비교 결과가 없어 건너뛰었습니다."
        state.plan = plan_impact(state.change_set, load_specs())
        job["impact"] = [asdict(entry) for entry in state.plan]
        return f"다시 만들 산출물 {len(state.plan)}개" if state.plan else "다시 만들 산출물이 없습니다."

    def _generate(self, job: dict[str, Any], state: _RunState) -> StepResult:
        if not job.get("run_generators", True):
            return "skipped", "요청에 따라 산출물을 다시 만들지 않았습니다."
        if state.plan is None:
            return "skipped", "영향 분석 결과가 없어 건너뛰었습니다."
        if not state.plan:
            return "skipped", "다시 만들 산출물이 없습니다."
        artifacts: list[dict[str, Any]] = []
        for entry in state.plan:
            row: dict[str, Any] = {"generator": entry.generator, "title_ko": entry.title_ko}
            try:
                result = self.host.registry_run(entry.generator, job["rev_id"])
                row.update(
                    status=result.get("status"),
                    approval_mode=result.get("approval_mode"),
                    issue_count=int(result.get("issue_count") or 0),
                )
            except Exception as exc:
                if not isinstance(exc, ToolError):
                    logger.exception("upload job %s: generator %s failed", job["job_id"], entry.generator)
                row.update(status="failed", error=_failure_message("generate", exc), issue_count=0)
            artifacts.append(row)
            job["artifacts"] = artifacts
            self.store.save(job)
        failed = [row["title_ko"] or row["generator"] for row in artifacts if row["status"] == "failed"]
        if failed:
            raise ToolError(
                f"산출물 {len(artifacts)}개 중 {len(failed)}개를 만들지 못했습니다: {', '.join(failed)}"
            )
        return f"산출물 {len(artifacts)}개 재생성"

    def _relayout_check(self, job: dict[str, Any], state: _RunState) -> StepResult:
        hook = getattr(self.host, "relayout_after_upload", None)
        if not callable(hook):
            return "skipped", "개편안 대조 기능을 사용할 수 없습니다."
        result = hook(job["rev_id"], job["parent_rev"])
        if not result:
            job["relayout_check"] = None
            return "skipped", f"{job['parent_rev']}를 기준으로 선택된 개편안이 없습니다."
        job["relayout_check"] = dict(result)
        if result.get("summary"):
            return str(result["summary"])
        return f"개편안 일치율 {result.get('conformance_pct')}%"
