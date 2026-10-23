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
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from cad2anything.model import ChangeSet, Revision, RevisionKind, SpatialModel

ACTUAL_ORDER = {RevisionKind.ACTUAL: 0, RevisionKind.HYPOTHETICAL: 1}
SUBDIRS = ("drawings", "models", "changes", "reports", "business", "artifacts", "projects", "uploads")


class Workspace:
    def __init__(self, root: Path, mirror: Any | None = None):
        self.root = Path(root)
        self.mirror = mirror
        self._lock = threading.RLock()
        self._models: dict[str, tuple[float, SpatialModel, dict[str, Any]]] = {}
        self._headers: dict[str, tuple[tuple[int, int], Revision, dict[str, Any]]] = {}
        self._tables: dict[str, tuple[float, pd.DataFrame]] = {}
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
            self.mirror.push(path, self.root)

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
        mtime = path.stat().st_mtime
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
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self._lock:
            self._models.pop(model.revision.rev_id, None)
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
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        self._publish(path)
        return path

    # Reports ---------------------------------------------------------------------------------------
    def save_report(self, rev_id: str, report: dict[str, Any]) -> Path:
        path = self.root / "reports" / f"{rev_id}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
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
        mtime = path.stat().st_mtime
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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
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
        self._publish(Path(path))

    def delete_file(self, rel_path: str | Path) -> bool:
        """Delete a workspace file and its mirrored copy; True when the local file existed."""
        path = self.root / rel_path
        existed = path.is_file()
        path.unlink(missing_ok=True)
        with self._lock:
            if path.parent.name == "models":
                self._models.pop(path.stem, None)
                self._headers.pop(path.stem, None)
        delete = getattr(self.mirror, "delete", None)
        if callable(delete):
            delete(Path(rel_path).as_posix())
        return existed

    def publish_tree(self, path: Path) -> None:
        """Mirror every file under ``path`` (for example a generator output directory)."""
        if self.mirror is None:
            return
        for file in sorted(p for p in Path(path).rglob("*") if p.is_file()):
            self.mirror.push(file, self.root)
