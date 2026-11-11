"""Data workspace shared by the MCP tools and the Atlas API.

Layout under the root directory (mirrored to PostgreSQL or Azure Blob Storage when configured)::

    drawings/{rev}/{variant}/manifest.json, *.dxf   CAD inputs (uploads use variant "uploaded")
    models/{rev}.json                                SpatialModel.to_dict() plus a "meta" block
    changes/{rev_from}__{rev_to}.json                ChangeSet.to_dict() plus a "meta" block
    reports/{rev}.json                               CAD parse reports
    business/*.csv[.gz], planted_effects.json        business tables (synthetic or uploaded)
    business/relayout_briefs/{brief_id}.yaml         seasonal relayout briefs (human input)
    business/drawing_profiles/{profile_id}.yaml      drawing convention profiles (human input)
    artifacts/{rev}/{generator}/...                  generator outputs and artifact.json
    projects/relayout/{brief_id}.json                relayout projects (diagnosis, plan, decision, check)
    uploads/jobs/{job_id}.json                       upload job state and results
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from cad2anything.model import ChangeSet, Revision, RevisionKind, SpatialModel

ACTUAL_ORDER = {RevisionKind.ACTUAL: 0, RevisionKind.HYPOTHETICAL: 1}
SUBDIRS = ("drawings", "models", "changes", "reports", "business", "artifacts", "projects", "uploads")
logger = logging.getLogger("cad2anything.workspace")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace a file only after its complete contents have been written beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Workspace:
    def __init__(self, root: Path, mirror: Any | None = None):
        self.root = Path(root)
        self.mirror = mirror
        self._lock = threading.RLock()
        self._sync_lock = threading.RLock()
        self._models: dict[str, tuple[int, SpatialModel, dict[str, Any]]] = {}
        self._headers: dict[str, tuple[tuple[int, int], Revision, dict[str, Any]]] = {}
        self._tables: dict[str, tuple[int, pd.DataFrame]] = {}
        self.mirror_publish_errors = 0
        self._untracked_mirror_errors = 0
        self._mirror_available = True
        self._last_mirror_error: str | None = None
        self._last_mirror_error_at: str | None = None
        self._pending_mirror: set[str] = set()
        self.mirror_pull_pending = False
        for sub in SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # Paths -----------------------------------------------------------------------------------------
    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    def drawing_dir(self, rev_id: str, variant: str = "clean") -> Path:
        return self.root / "drawings" / rev_id / variant

    def _model_path(self, rev_id: str) -> Path:
        return self.root / "models" / f"{rev_id}.json"

    def _change_path(self, rev_from: str, rev_to: str) -> Path:
        return self.root / "changes" / f"{rev_from}__{rev_to}.json"

    def _publish(self, path: Path) -> None:
        if self.mirror is not None:
            path = Path(path).resolve()
            root = self.root.resolve()
            name = path.relative_to(root).as_posix()
            previous = getattr(self.mirror, "error_count", None)
            try:
                self.mirror.push(path, root)
            except Exception as exc:
                self.note_mirror_error(exc, previous, name)
                raise
            with self._lock:
                self._mirror_available = True
                self._pending_mirror.discard(name)

    def note_mirror_error(
        self, exc: Exception, previous_count: int | None = None, pending_path: str | None = None
    ) -> None:
        """Record failures not already counted by the mirror, without logging sensitive details."""
        with self._lock:
            if pending_path is not None:
                self.mirror_publish_errors += 1
            if previous_count is None or getattr(self.mirror, "error_count", None) == previous_count:
                self._untracked_mirror_errors += 1
            self._mirror_available = False
            self._last_mirror_error = type(exc).__name__
            self._last_mirror_error_at = datetime.now(UTC).isoformat()
            if pending_path is not None:
                self._pending_mirror.add(pending_path)
        logger.warning("workspace mirror operation failed error_type=%s", type(exc).__name__)

    def mirror_status(self) -> dict[str, Any]:
        """Cached mirror status; never contact the database from an HTTP health endpoint."""
        mirror = self.mirror
        if mirror is None:
            return {
                "db": "disabled",
                "mirror_errors": 0,
                "last_mirror_error": None,
                "last_mirror_error_at": None,
                "writes_available": True,
                "pending_mirror_writes": 0,
            }
        health = getattr(mirror, "health", None)
        status = health() if callable(health) else {}
        db = status.get("db", "disabled")
        with self._lock:
            errors = self._untracked_mirror_errors
            available = self._mirror_available
            last_error = self._last_mirror_error
            last_error_at = self._last_mirror_error_at
            pending = len(self._pending_mirror)
        remote_error_at = status.get("last_mirror_error_at")
        if remote_error_at and (last_error_at is None or remote_error_at > last_error_at):
            last_error_at = remote_error_at
            last_error = status.get("last_mirror_error")
        return {
            "db": db,
            "mirror_errors": status.get("mirror_errors", 0) + errors,
            "last_mirror_error": last_error,
            "last_mirror_error_at": last_error_at,
            "writes_available": (db == "ok" if db != "disabled" else available),
            "pending_mirror_writes": pending,
        }

    def retry_pending_mirror(self) -> int:
        """Replay locally committed files or deletions after the mirror recovers."""
        if self.mirror is None:
            return 0
        with self._sync_lock:
            with self._lock:
                pending = sorted(self._pending_mirror)
            recovered = 0
            for name in pending:
                path = self.root / name
                try:
                    if path.is_file():
                        self._publish(path)
                    else:
                        self._delete_mirror(name)
                except Exception:
                    continue
                recovered += 1
            return recovered

    def pull_mirror(self) -> int | None:
        """Pull remote files only when no local writes are waiting to be mirrored."""
        if self.mirror is None:
            return 0
        with self._sync_lock:
            with self._lock:
                if self._pending_mirror:
                    return None
            count = self.mirror.pull(self.root)
            self.mirror_pull_pending = False
            with self._lock:
                self._models.clear()
                self._headers.clear()
                self._tables.clear()
            return count

    # Models ----------------------------------------------------------------------------------------
    def _header(self, path: Path) -> tuple[Revision, dict[str, Any]]:
        """Revision and meta block of a model file, cached per file version (no geometry is built)."""
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._headers.get(path.stem)
            if cached and cached[0] == key:
                return cached[1], cached[2]
        data = json.loads(path.read_text(encoding="utf-8"))
        revision, meta = Revision.from_dict(data["revision"]), data.get("meta", {})
        with self._lock:
            self._headers[path.stem] = (key, revision, meta)
        return revision, meta

    def revisions(self) -> list[Revision]:
        revisions = [self._header(path)[0] for path in sorted((self.root / "models").glob("*.json"))]
        return sorted(
            revisions, key=lambda r: (ACTUAL_ORDER[r.kind], r.effective_date or "9999-12-31", r.rev_id)
        )

    def has_model(self, rev_id: str) -> bool:
        return self._model_path(rev_id).exists()

    def model(self, rev_id: str) -> SpatialModel:
        return self._load_model(rev_id)[0]

    def model_meta(self, rev_id: str) -> dict[str, Any]:
        path = self._model_path(rev_id)
        if not path.exists():
            raise KeyError(f"revision {rev_id} has no spatial model")
        return self._header(path)[1]

    def _load_model(self, rev_id: str) -> tuple[SpatialModel, dict[str, Any]]:
        path = self._model_path(rev_id)
        if not path.exists():
            raise KeyError(f"revision {rev_id} has no spatial model")
        mtime = path.stat().st_mtime_ns
        with self._lock:
            cached = self._models.get(rev_id)
            if cached and cached[0] == mtime:
                return cached[1], cached[2]
            data = json.loads(path.read_text(encoding="utf-8"))
            model = SpatialModel.from_dict(data)
            meta = data.get("meta", {})
            self._models[rev_id] = (mtime, model, meta)
            return model, meta

    def save_model(self, model: SpatialModel, meta: dict[str, Any] | None = None) -> Path:
        data = model.to_dict()
        data["meta"] = meta or {}
        path = self._model_path(model.revision.rev_id)
        with self._sync_lock:
            atomic_write_bytes(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))
            with self._lock:
                self._models.pop(model.revision.rev_id, None)
                self._headers.pop(model.revision.rev_id, None)
            self._publish(path)
        return path

    # Change sets -----------------------------------------------------------------------------------
    def change_set(self, rev_from: str, rev_to: str) -> ChangeSet | None:
        path = self._change_path(rev_from, rev_to)
        if not path.exists():
            return None
        return ChangeSet.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def change_meta(self, rev_from: str, rev_to: str) -> dict[str, Any]:
        path = self._change_path(rev_from, rev_to)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get("meta", {})

    def save_change_set(self, change_set: ChangeSet, meta: dict[str, Any] | None = None) -> Path:
        data = change_set.to_dict()
        data["meta"] = meta or {}
        path = self._change_path(change_set.rev_from, change_set.rev_to)
        with self._sync_lock:
            atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
            self._publish(path)
        return path

    # Reports ---------------------------------------------------------------------------------------
    def save_report(self, rev_id: str, report: dict[str, Any]) -> Path:
        path = self.root / "reports" / f"{rev_id}.json"
        with self._sync_lock:
            atomic_write_bytes(
                path, json.dumps(report, ensure_ascii=False, indent=1, default=str).encode("utf-8")
            )
            self._publish(path)
        return path

    def report(self, rev_id: str) -> dict[str, Any] | None:
        path = self.root / "reports" / f"{rev_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    # Business tables -------------------------------------------------------------------------------
    def _table_path(self, name: str) -> Path | None:
        for suffix in (".csv", ".csv.gz"):
            path = self.root / "business" / f"{name}{suffix}"
            if path.exists():
                return path
        return None

    def has_table(self, name: str) -> bool:
        return self._table_path(name) is not None

    def table_path(self, name: str) -> Path | None:
        """Path of a business table (``.csv`` or ``.csv.gz``), or None."""
        return self._table_path(name)

    def table(self, name: str) -> pd.DataFrame:
        path = self._table_path(name)
        if path is None:
            raise KeyError(f"business table {name} not found")
        mtime = path.stat().st_mtime_ns
        with self._lock:
            cached = self._tables.get(name)
            if cached and cached[0] == mtime:
                return cached[1]
            frame = pd.read_csv(path, low_memory=False)
            stat = path.stat()
            frame.attrs["fingerprint"] = f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
            self._tables[name] = (mtime, frame)
            return frame

    def business_json(self, name: str) -> dict[str, Any] | None:
        path = self.root / "business" / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    # Generic files ---------------------------------------------------------------------------------
    def write_bytes(self, rel_path: str | Path, data: bytes, publish: bool = True) -> Path:
        """Atomically write ``data`` under the root (parents created) and mirror it unless ``publish`` is False."""
        path = self.root / rel_path
        with self._sync_lock:
            atomic_write_bytes(path, data)
            if publish:
                self._publish(path)
        return path

    def write_text(self, rel_path: str | Path, text: str, publish: bool = True) -> Path:
        return self.write_bytes(rel_path, text.encode("utf-8"), publish)

    def write_json(self, rel_path: str | Path, data: Any, publish: bool = True) -> Path:
        text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
        return self.write_bytes(rel_path, text.encode("utf-8"), publish)

    def read_json(self, rel_path: str | Path) -> Any | None:
        path = self.root / rel_path
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def publish(self, path: Path) -> None:
        """Mirror one file under the root (no-op without a mirror)."""
        with self._sync_lock:
            self._publish(Path(path))

    def _delete_mirror(self, name: str) -> None:
        delete = getattr(self.mirror, "delete", None)
        if not callable(delete):
            return
        previous = getattr(self.mirror, "error_count", None)
        try:
            delete(name)
        except Exception as exc:
            self.note_mirror_error(exc, previous, name)
            raise
        with self._lock:
            self._pending_mirror.discard(name)
            self._mirror_available = True

    def delete_file(self, rel_path: str | Path) -> bool:
        """Delete a workspace file and its mirrored copy; True when the local file existed."""
        with self._sync_lock:
            path = self.root / rel_path
            existed = path.is_file()
            path.unlink(missing_ok=True)
            with self._lock:
                if path.parent.name == "models":
                    self._models.pop(path.stem, None)
                    self._headers.pop(path.stem, None)
            self._delete_mirror(Path(rel_path).as_posix())
            return existed

    def publish_tree(self, path: Path) -> None:
        """Mirror every file under ``path`` (for example a generator output directory)."""
        if self.mirror is None:
            return
        with self._sync_lock:
            failures = 0
            for file in sorted(
                p
                for p in Path(path).rglob("*")
                if p.is_file() and not (p.name.startswith(".") and p.suffix == ".tmp")
            ):
                try:
                    self._publish(file)
                except Exception:
                    failures += 1
            if failures:
                raise RuntimeError(f"{failures} workspace files could not be mirrored")
