"""Upload tools for the Toolkit (docs/specs/atlas-api.md §1.2): drawing and metadata uploads, job status and
the drawing-versus-lease check.

``UploadTools`` is a mixin. The host provides ``ws``, ``_model(rev)``, ``registry_run(generator, rev, floors)``,
``revision_diff(rev_from, rev_to, limit)``, ``_actual_revisions()`` and ``_table(name, required)``. Optional
hooks: ``relayout_after_upload(rev, parent_rev)`` (relayout plan check after an upload) and
``relayout_validate_brief(data)`` (brief metadata validation). Call ``_init_uploads()`` from ``__init__``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import pandas as pd
import yaml

from cad2anything.cadcore.classify import SEMANTICS
from cad2anything.cadcore.profile import (
    DEFAULT_PROFILE,
    DEFAULT_PROFILE_ID,
    PROFILE_DIR,
    PROFILE_ID_RE,
    DrawingProfile,
    load_profile,
    normalize_floor,
)
from cad2anything.model import RevisionKind, SpatialModel
from cad2anything.services import metadata
from cad2anything.services.errors import ToolError
from cad2anything.services.uploads import (
    ACTIVE_STATUSES,
    HYPOTHETICAL_ID_RE,
    MB,
    REV_ID_RE,
    STAGING_DIR,
    STATUS_LABELS,
    STEP_STATUS_LABELS,
    UPLOAD_VARIANT,
    ConflictError,
    DrawingPipeline,
    JobRunner,
    JobStore,
    NotFoundError,
    apply_layer_mappings,
    base_name,
    changes_message,
    collect_drawing_files,
    job_list_row,
    reset_job,
    size_text,
)

logger = logging.getLogger("cad2anything.uploads")

MAX_METADATA_BYTES = 30 * MB
MAX_MAPPINGS = 200
UNITS = ("m", "mm")
QA_LABELS: dict[str, str] = {
    "area_mismatch": "면적 불일치",
    "brand_mismatch": "브랜드 불일치",
    "unit_missing_in_drawing": "도면에 없는 계약 매장",
    "unit_without_lease": "계약 없는 매장",
}


def _steps_text(job: Mapping[str, Any]) -> str:
    return " · ".join(
        f"{step.get('label')} {STEP_STATUS_LABELS.get(step.get('status'), step.get('status'))}"
        for step in job.get("steps") or []
    )


def job_row(job: Mapping[str, Any]) -> dict[str, Any]:
    """One line of the job list for the agent."""
    return {
        "job_id": job.get("job_id"),
        "rev_id": job.get("rev_id"),
        "parent_rev": job.get("parent_rev"),
        "status": job.get("status"),
        "status_ko": STATUS_LABELS.get(str(job.get("status")), job.get("status")),
        "created_at": job.get("created_at"),
        "floors_uploaded": job.get("floors_uploaded"),
        "steps": _steps_text(job),
        "error": job.get("error"),
    }


def qa_summary(qa: Mapping[str, Any], scope: str) -> str:
    counts = [
        f"{QA_LABELS.get(code, code)} {count}건" for code, count in (qa.get("by_code") or {}).items() if count
    ]
    head = f"{scope}에서 계약 {qa.get('leases_checked', 0)}건과 매장 {qa.get('units_checked', 0)}개를 대조했습니다."
    return f"{head} {', '.join(counts)}." if counts else f"{head} 계약과 다른 점이 없습니다."


def job_brief(job: Mapping[str, Any]) -> dict[str, Any]:
    """Compact status and results of one job for the agent, with a one-line Korean summary."""
    parse = job.get("parse") or None
    changes = job.get("changes") or None
    qa = job.get("qa") or None
    status_ko = STATUS_LABELS.get(str(job.get("status")), str(job.get("status")))
    floors = ", ".join(job.get("floors_uploaded") or [])
    parts = [f"{job.get('rev_id')} 도면 업로드({floors}) 작업은 '{status_ko}' 상태입니다."]
    if parse:
        parts.append(
            f"매장 {parse.get('units')}개를 읽었고 검토할 레이어는 {parse.get('review_total', 0)}개입니다."
        )
    if changes:
        parts.append(
            f"{job.get('parent_rev')} 대비 {changes_message(changes.get('by_type') or {}, changes.get('total') or 0)}."
        )
    if qa:
        parts.append(f"계약 대조 경고 {qa.get('warnings', 0)}건, 참고 {qa.get('infos', 0)}건.")
    if job.get("error"):
        parts.append(f"오류: {job['error']}")
    return {
        "job_id": job.get("job_id"),
        "rev_id": job.get("rev_id"),
        "parent_rev": job.get("parent_rev"),
        "effective_date": job.get("effective_date"),
        "profile": job.get("profile"),
        "status": job.get("status"),
        "status_ko": status_ko,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "floors_uploaded": job.get("floors_uploaded"),
        "floors_inherited": job.get("floors_inherited"),
        "steps": [
            {k: step.get(k) for k in ("name", "label", "status", "message")}
            for step in job.get("steps") or []
        ],
        "parse": {
            "units": parse.get("units"),
            "issues": parse.get("issues"),
            "review_total": parse.get("review_total"),
            "review_top": [
                {k: item.get(k) for k in ("floor", "layer", "semantic", "confidence", "question")}
                for item in (parse.get("review_queue") or [])[:5]
            ],
        }
        if parse
        else None,
        "changes": {k: changes.get(k) for k in ("total", "by_type", "by_floor")} if changes else None,
        "qa": {
            "warnings": qa.get("warnings"),
            "infos": qa.get("infos"),
            "by_code": qa.get("by_code"),
            "top": (qa.get("issues") or [])[:10],
        }
        if qa
        else None,
        "artifacts": [
            {k: item.get(k) for k in ("generator", "title_ko", "status")}
            for item in job.get("artifacts") or []
        ],
        "relayout_check": job.get("relayout_check"),
        "error": job.get("error"),
        "summary": " ".join(parts),
    }


class UploadTools:
    """Mixin for Toolkit: drawing and metadata uploads (REST methods) plus two agent tools."""

    ws: Any

    def _init_uploads(self, sync: bool | None = None, relations: str = "full") -> None:
        """Set up the job store and runner; ``sync`` (default: ``ATLAS_UPLOAD_SYNC=1``) runs jobs inline.

        Jobs that a previous process left queued or running are marked failed.
        """
        if sync is None:
            sync = os.environ.get("ATLAS_UPLOAD_SYNC", "").strip() == "1"
        self._upload_store = JobStore(self.ws)
        self._upload_runner = JobRunner(DrawingPipeline(self, self._upload_store, relations).run, sync)
        self._metadata_lock = threading.Lock()
        recovered = self._upload_store.recover()
        if recovered:
            logger.warning("%d upload job(s) were interrupted by a restart and marked failed", recovered)

    # Validation --------------------------------------------------------------------------------------
    @staticmethod
    def _upload_rev_id(rev_id: Any) -> str:
        value = str(rev_id or "").strip()
        if not REV_ID_RE.match(value):
            raise ToolError(
                "rev_id는 영문 대문자·숫자·-·_로 1~16자여야 하고 영문 대문자나 숫자로 시작해야 합니다."
            )
        if HYPOTHETICAL_ID_RE.match(value):
            raise ToolError(f"{value}처럼 H 뒤에 숫자만 붙은 ID는 가상 개정용입니다. 다른 개정 ID를 쓰세요.")
        return value

    def _upload_conflicts(self, rev: str) -> None:
        if self.ws.has_model(rev) and self.ws.model_meta(rev).get("source") != "uploaded":
            raise ConflictError(
                f"개정 {rev}가 이미 있습니다. 업로드로 만든 개정만 다시 올려 바꿀 수 있습니다. 다른 개정 ID를 쓰세요."
            )
        active = [
            job
            for job in self._upload_store.all()
            if job.get("rev_id") == rev and job.get("status") in ACTIVE_STATUSES
        ]
        if active:
            raise ConflictError(
                f"개정 {rev}를 처리하는 작업 {active[0]['job_id']}가 아직 끝나지 않았습니다. 끝난 뒤 다시 올려 주세요."
            )

    def _upload_data_through(self) -> str | None:
        """Last date of the business data (``calendar`` table), or None when unknown."""
        calendar = self._table("calendar", required=False)
        if calendar is None or "date" not in calendar:
            return None
        last = pd.to_datetime(calendar["date"], errors="coerce").max()
        return None if pd.isna(last) else last.date().isoformat()

    def _default_parent(self) -> str:
        """Latest actual revision with sales data: the host's ``latest_sales_rev()`` when it has one, else the
        latest actual revision effective on or before the ``calendar`` table's last day."""
        latest = getattr(self, "latest_sales_rev", None)
        if callable(latest):
            return str(latest())
        actual = sorted(self._actual_revisions(), key=lambda r: (r.effective_date or "", r.rev_id))
        through = self._upload_data_through()
        if through is not None:
            candidates = [r for r in actual if str(r.effective_date) <= through]
        else:
            candidates = [r for r in actual if self.ws.model_meta(r.rev_id).get("source") != "uploaded"]
        if not candidates:
            raise ToolError("부모로 쓸 실제 개정이 없습니다. parent_rev를 지정하세요.")
        return candidates[-1].rev_id

    def _upload_parent(self, parent_rev: Any, rev: str) -> SpatialModel:
        parent_id = str(parent_rev or "").strip() or self._default_parent()
        if parent_id == rev:
            raise ToolError("부모 개정은 올리는 개정과 달라야 합니다.")
        if not self.ws.has_model(parent_id):
            known = ", ".join(r.rev_id for r in self._actual_revisions()) or "없음"
            raise ToolError(f"부모 개정 {parent_id}가 없습니다. 실제 개정: {known}")
        parent = self._model(parent_id)
        if parent.revision.kind != RevisionKind.ACTUAL:
            raise ToolError(f"{parent_id}는 가상 개정입니다. 실제 개정을 부모로 지정하세요.")
        return parent

    @staticmethod
    def _upload_date(effective_date: Any, parent: SpatialModel) -> str:
        text = str(effective_date or "").strip()
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                raise ValueError(text)
            day = date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ToolError("effective_date는 YYYY-MM-DD 형식의 날짜여야 합니다.") from exc
        parent_date = str(parent.revision.effective_date or "")[:10]
        if parent_date and day <= parent_date:
            raise ToolError(
                f"effective_date({day})는 부모 개정 {parent.revision.rev_id}의 적용일({parent_date})보다 뒤여야 합니다."
            )
        return day

    @staticmethod
    def _upload_units(units: Any) -> str | None:
        value = str(units or "").strip().lower()
        if value and value not in UNITS:
            raise ToolError(f"units는 {' 또는 '.join(UNITS)}여야 합니다.")
        return value or None

    def _upload_profile(self, profile_id: Any) -> DrawingProfile:
        """The built-in ``default`` or ``business/drawing_profiles/{id}.yaml`` (``NotFoundError`` when missing)."""
        value = str(profile_id or "").strip() or DEFAULT_PROFILE_ID
        if value == DEFAULT_PROFILE_ID:
            return DEFAULT_PROFILE
        path = self.ws.root / PROFILE_DIR / f"{value}.yaml"
        if not PROFILE_ID_RE.match(value) or not path.is_file():
            known = ", ".join(item["profile_id"] for item in self.upload_profiles())
            raise NotFoundError(f"도면 규약 프로필 {value}가 없습니다. 사용할 수 있는 프로필: {known}")
        try:
            profile = load_profile(path)
        except ValueError as exc:
            raise ToolError(f"{value} 프로필을 쓸 수 없습니다. {exc}") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise ToolError(f"{value} 프로필 파일을 읽을 수 없습니다. 프로필을 다시 올려 주세요.") from exc
        if profile.profile_id != value:
            raise ToolError(
                f"{value} 프로필 파일의 profile_id가 {profile.profile_id}입니다. 프로필을 다시 올려 주세요."
            )
        return profile

    # REST ----------------------------------------------------------------------------------------------
    def upload_drawings(
        self,
        files: Sequence[tuple[str, bytes]],
        rev_id: str,
        effective_date: str,
        parent_rev: str | None = None,
        units: str | None = None,
        profile: str = DEFAULT_PROFILE_ID,
        run_generators: bool = True,
    ) -> dict[str, Any]:
        """Validate a drawing upload (DXF files or one ZIP) and queue its job; returns ``{job_id, status}``.

        ``ToolError`` for bad input; ``ConflictError`` when ``rev_id`` is an existing revision that was not made
        by an upload, or a job for it is still queued or running.
        """
        rev = self._upload_rev_id(rev_id)
        self._upload_conflicts(rev)
        parent = self._upload_parent(parent_rev, rev)
        day = self._upload_date(effective_date, parent)
        try:
            drawing_profile = self._upload_profile(profile)
        except NotFoundError as exc:
            raise ToolError(str(exc)) from exc
        unit_name = self._upload_units(units) or drawing_profile.units
        drawings = collect_drawing_files(files)
        parent_floors = [f.code for f in parent.floors]
        for drawing in drawings:
            if drawing.floor not in parent_floors:
                raise ToolError(
                    f"{drawing.name}: {drawing.floor}층이 부모 개정 {parent.revision.rev_id}에 없습니다. "
                    f"층: {', '.join(parent_floors)}"
                )
        uploaded = [code for code in parent_floors if code in {d.floor for d in drawings}]
        order = {code: index for index, code in enumerate(parent_floors)}
        record = reset_job(
            {
                "job_id": None,
                "kind": "drawing",
                "rev_id": rev,
                "parent_rev": parent.revision.rev_id,
                "effective_date": day,
                "profile": drawing_profile.profile_id,
                "units": unit_name,
                "run_generators": bool(run_generators),
                "created_at": None,
                "updated_at": None,
                "files": [
                    {"name": d.name, "floor": d.floor, "bytes": len(d.data)}
                    for d in sorted(drawings, key=lambda d: order[d.floor])
                ],
                "floors_uploaded": uploaded,
                "floors_inherited": [code for code in parent_floors if code not in uploaded],
                "layer_mappings": [],
            }
        )
        with self._upload_store.lock:
            self._upload_conflicts(rev)
            job = self._upload_store.create(record)
        try:
            for drawing in drawings:
                self.ws.write_bytes(
                    f"{STAGING_DIR}/{job['job_id']}/{drawing.floor}.dxf", drawing.data, publish=False
                )
        except OSError as exc:
            job.update(status="failed", error="올린 파일을 저장하지 못했습니다. 다시 올려 주세요.")
            self._upload_store.save(job)
            raise ToolError(job["error"]) from exc
        self._upload_runner.submit(job["job_id"])
        return {"job_id": job["job_id"], "status": self._upload_store.load(job["job_id"])["status"]}

    def upload_job(self, job_id: str) -> dict[str, Any]:
        """Full job record (``NotFoundError`` for unknown or malformed IDs)."""
        return self._upload_store.load(job_id)

    def upload_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent jobs, newest first: ``[{job_id, kind, status, rev_id, created_at, updated_at}]``."""
        limit = max(1, min(int(limit or 20), 100))
        return [job_list_row(job) for job in self._upload_store.all()[:limit]]

    def _clean_mappings(self, job: Mapping[str, Any], mappings: Any) -> list[dict[str, Any]]:
        if not isinstance(mappings, list | tuple) or not mappings:
            raise ToolError("mappings에 {layer, semantic, floor?} 항목이 하나 이상 있어야 합니다.")
        if len(mappings) > MAX_MAPPINGS:
            raise ToolError(f"매핑은 한 번에 {MAX_MAPPINGS}개까지 보낼 수 있습니다.")
        floors = list(job.get("floors_uploaded") or [])
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(mappings, 1):
            if not isinstance(item, Mapping):
                raise ToolError(f"{index}번째 매핑은 {{layer, semantic, floor?}} 객체여야 합니다.")
            layer = str(item.get("layer") or "").strip()
            semantic = str(item.get("semantic") or "").strip()
            if not layer:
                raise ToolError(f"{index}번째 매핑에 layer가 없습니다.")
            if semantic not in SEMANTICS:
                raise ToolError(
                    f"{index}번째 매핑의 semantic '{semantic}'를 알 수 없습니다. 쓸 수 있는 값: {', '.join(SEMANTICS)}"
                )
            floor = None
            if item.get("floor") not in (None, ""):
                floor = normalize_floor(item["floor"]) or str(item["floor"]).strip()
                if floor not in floors:
                    raise ToolError(
                        f"{index}번째 매핑의 층 {item['floor']}는 이 작업에서 올린 층이 아닙니다. 올린 층: {', '.join(floors)}"
                    )
            cleaned.append({"layer": layer, "semantic": semantic, "floor": floor})
        return cleaned

    def upload_layer_mapping(self, job_id: str, mappings: list[dict[str, Any]]) -> dict[str, Any]:
        """Store human layer answers in the upload manifest and run the job again; returns the job."""
        cleaned = self._clean_mappings(self._upload_store.load(job_id), mappings)
        with self._upload_store.lock:
            job = self._upload_store.load(job_id)
            if job.get("status") in ACTIVE_STATUSES:
                raise ConflictError("작업이 아직 처리 중입니다. 끝난 뒤 다시 시도하세요.")
            folder = f"drawings/{job['rev_id']}/{UPLOAD_VARIANT}"
            manifest = self.ws.read_json(f"{folder}/manifest.json")
            if manifest and manifest.get("upload_job") not in (None, job["job_id"]):
                raise ConflictError(
                    f"개정 {job['rev_id']}는 이후 작업 {manifest['upload_job']}로 다시 올라왔습니다. 그 작업에서 매핑하세요."
                )
            if manifest:
                self.ws.write_json(f"{folder}/manifest.json", apply_layer_mappings(manifest, cleaned))
            job["layer_mappings"] = [*(job.get("layer_mappings") or []), *cleaned]
            self._upload_store.save(reset_job(job))
        self._upload_runner.submit(job["job_id"])
        return self._upload_store.load(job["job_id"])

    def upload_profiles(self) -> list[dict[str, Any]]:
        """Drawing profiles ``[{profile_id, title, description, units}]``; ``default`` comes first."""
        profiles = [DEFAULT_PROFILE.summary()]
        folder = self.ws.root / PROFILE_DIR
        for path in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
            if path.stem == DEFAULT_PROFILE_ID or not PROFILE_ID_RE.match(path.stem):
                continue
            try:
                profile = load_profile(path)
            except (ValueError, OSError, yaml.YAMLError):
                logger.warning("skipping invalid drawing profile %s", path.name)
                continue
            if profile.profile_id == path.stem:
                profiles.append(profile.summary())
        return profiles

    def upload_metadata(self, kind: str, filename: str, data: bytes) -> dict[str, Any]:
        """Validate and store one metadata file: ``{kind, status, rows, columns, errors, warnings, backup, id}``.

        Content problems give ``status: "rejected"``; an unknown ``kind``, a missing name or an oversize file
        raise ``ToolError``.
        """
        kind = str(kind or "").strip()
        if kind not in metadata.KINDS:
            raise ToolError(f"kind는 {', '.join(metadata.KINDS)} 중 하나여야 합니다.")
        name = base_name(filename)
        if not name:
            raise ToolError("파일 이름이 없습니다.")
        if len(data) > MAX_METADATA_BYTES:
            raise ToolError(f"메타데이터 파일은 {size_text(MAX_METADATA_BYTES)}까지 올릴 수 있습니다.")
        if not data:
            outcome = metadata.result(kind)
            outcome["errors"] = ["빈 파일입니다."]
            return outcome
        with self._metadata_lock:
            return metadata.upload_metadata(self, kind, name, bytes(data))

    def metadata_status(self) -> list[dict[str, Any]]:
        """Per metadata kind: ``{kind, present, rows, updated_at, required_columns, items}``."""
        return metadata.metadata_status(self.ws)

    def metadata_qa(
        self, model: SpatialModel, effective_date: str, floors: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Drawing units versus the leases valid on ``effective_date`` (``{"skipped": True, ...}`` without
        a ``leases`` table)."""
        leases = self._table("leases", required=False)
        if leases is None:
            return {
                "skipped": True,
                "message": "계약 정보(leases)가 없어 대조하지 않았습니다. leases 메타데이터를 올리면 대조합니다.",
            }
        return metadata.check_leases(
            model, effective_date, leases, self._table("brands", required=False), floors
        )

    # Agent tools ---------------------------------------------------------------------------------------
    def upload_list_jobs(self, job_id: str | None = None, limit: int = 5) -> dict[str, Any]:
        """Recent drawing upload jobs, or the status and results of one job (``job_id``)."""
        if job_id:
            return job_brief(self._upload_store.load(str(job_id).strip()))
        jobs = self._upload_store.all()
        limit = max(1, min(int(limit or 5), 20))
        return {
            "total": len(jobs),
            "jobs": [job_row(job) for job in jobs[:limit]],
            "note": "작업의 자세한 결과는 job_id로 다시 조회하세요."
            if jobs
            else "아직 도면 업로드 작업이 없습니다.",
        }

    def revision_check_metadata(self, rev: str, floors: list[str] | None = None) -> dict[str, Any]:
        """Compare an actual revision's drawing with the leases valid on its effective date."""
        model = self._model(rev)
        revision = model.revision
        if revision.kind != RevisionKind.ACTUAL:
            raise ToolError(f"{rev}는 가상 개정이라 계약 정보와 대조할 수 없습니다. 실제 개정을 지정하세요.")
        if not revision.effective_date:
            raise ToolError(f"{rev}에 적용일이 없어 유효한 계약을 고를 수 없습니다.")
        codes = [floor.code for floor in model.floors]
        selected: list[str] | None = None
        if floors:
            selected = []
            for item in floors:
                code = normalize_floor(item) or str(item).strip().upper()
                if code not in codes:
                    raise ToolError(f"{rev}에 {item}층이 없습니다. 층: {', '.join(codes)}")
                selected.append(code)
        qa = self.metadata_qa(model, str(revision.effective_date), selected)
        scope = f"{rev} {'·'.join(selected)}" if selected else f"{rev} 전 층"
        if qa.get("skipped"):
            return {
                "rev": rev,
                "effective_date": revision.effective_date,
                "floors": selected or codes,
                "checked": False,
                "summary": qa.get("message"),
            }
        return {
            "rev": rev,
            "effective_date": qa["effective_date"],
            "floors": qa["floors"],
            "checked": True,
            "leases_checked": qa["leases_checked"],
            "units_checked": qa["units_checked"],
            "counts": qa["by_code"],
            "warnings": qa["warnings"],
            "infos": qa["infos"],
            "issues": qa["issues"][:30],
            "truncated": qa["total"] > 30,
            "summary": qa_summary(qa, scope),
        }
