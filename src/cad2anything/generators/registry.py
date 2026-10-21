"""Declarative generator registry: specs, impact analysis, idempotent runs, and approval states."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from cad2anything.model import ChangeSet, ChangeType, SpatialModel

SPEC_DIR = Path(__file__).with_name("specs")
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}
BUILTIN_MODULES = ("cad2anything.generators.spatial", "cad2anything.generators.business")


@dataclass(frozen=True)
class GeneratorSpec:
    name: str
    version: str
    title_ko: str
    description: str
    entity_kinds: tuple[str, ...]
    data_inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    trigger_change_types: tuple[str, ...]
    risk_tier: str
    approval_mode: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeneratorSpec:
        inputs = data.get("inputs") or {}
        triggers = data.get("triggers") or {}
        change_types = tuple(str(ChangeType(c)) for c in triggers.get("change_types", []))
        spec = cls(
            name=data["name"],
            version=str(data["version"]),
            title_ko=data.get("title_ko", data["name"]),
            description=data.get("description", ""),
            entity_kinds=tuple(inputs.get("entity_kinds", [])),
            data_inputs=tuple(inputs.get("data", [])),
            outputs=tuple(data.get("outputs", [])),
            trigger_change_types=change_types,
            risk_tier=data.get("risk_tier", "medium"),
            approval_mode=data.get("approval_mode", "pre_publish"),
        )
        if spec.risk_tier not in RISK_ORDER:
            raise ValueError(f"{spec.name}: unknown risk_tier {spec.risk_tier}")
        if spec.approval_mode not in ("pre_publish", "post_publish"):
            raise ValueError(f"{spec.name}: unknown approval_mode {spec.approval_mode}")
        return spec


def load_specs(spec_dir: Path = SPEC_DIR) -> dict[str, GeneratorSpec]:
    specs: dict[str, GeneratorSpec] = {}
    for path in sorted(spec_dir.glob("*.yaml")):
        spec = GeneratorSpec.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
        specs[spec.name] = spec
    return specs


@dataclass
class Issue:
    severity: str
    message: str
    entity_ids: list[str] = field(default_factory=list)


@dataclass
class GeneratorContext:
    model: SpatialModel
    out_dir: Path
    data: dict[str, Any] = field(default_factory=dict)
    floors: list[str] | None = None


@dataclass
class GeneratorResult:
    files: list[Path]
    summary: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)


GeneratorFn = Callable[[GeneratorContext], GeneratorResult]
_IMPLEMENTATIONS: dict[str, GeneratorFn] = {}


def generator(name: str) -> Callable[[GeneratorFn], GeneratorFn]:
    def register(fn: GeneratorFn) -> GeneratorFn:
        _IMPLEMENTATIONS[name] = fn
        return fn

    return register


def implementation(name: str) -> GeneratorFn:
    if name not in _IMPLEMENTATIONS:
        for module in BUILTIN_MODULES:
            try:
                importlib.import_module(module)
            except ModuleNotFoundError as exc:
                if exc.name != module:
                    raise
    if name not in _IMPLEMENTATIONS:
        raise KeyError(f"no implementation registered for generator '{name}'")
    return _IMPLEMENTATIONS[name]


@dataclass
class ImpactEntry:
    generator: str
    title_ko: str
    risk_tier: str
    approval_mode: str
    floors: list[str]
    reasons: list[str]


def plan_impact(change_set: ChangeSet, specs: dict[str, GeneratorSpec]) -> list[ImpactEntry]:
    """Select generators whose declared triggers match the change set."""
    plan: list[ImpactEntry] = []
    for spec in specs.values():
        matched = [item for item in change_set.items if str(item.change_type) in spec.trigger_change_types]
        if not matched:
            continue
        reasons = [
            item.summary or f"{item.change_type} {', '.join(item.unit_codes_after or item.unit_codes_before)}"
            for item in matched
        ]
        plan.append(
            ImpactEntry(
                generator=spec.name,
                title_ko=spec.title_ko,
                risk_tier=spec.risk_tier,
                approval_mode=spec.approval_mode,
                floors=sorted({item.floor for item in matched}),
                reasons=reasons,
            )
        )
    plan.sort(key=lambda entry: (RISK_ORDER[entry.risk_tier], entry.generator))
    return plan


@dataclass
class ArtifactRecord:
    job_id: str
    generator: str
    generator_version: str
    rev_id: str
    status: str
    approval_mode: str
    files: list[dict[str, Any]]
    summary: dict[str, Any]
    issues: list[dict[str, Any]]
    created_at: str
    reviewed_by: str | None = None
    review_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path) -> ArtifactRecord:
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _fingerprint(spec: GeneratorSpec, ctx: GeneratorContext) -> str:
    digest = hashlib.sha256()
    digest.update(f"{spec.name}|{spec.version}|{ctx.floors}".encode())
    digest.update(json.dumps(ctx.model.to_dict(), sort_keys=True, ensure_ascii=False).encode())
    for key in sorted(ctx.data):
        value = ctx.data[key]
        cheap = getattr(value, "attrs", {}).get("fingerprint") if hasattr(value, "attrs") else None
        if cheap:
            digest.update(f"{key}|{cheap}".encode())
        elif hasattr(value, "to_csv"):
            digest.update(key.encode())
            digest.update(value.to_csv(index=False).encode())
        else:
            digest.update(json.dumps({key: value}, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_generator(
    name: str,
    ctx: GeneratorContext,
    specs: dict[str, GeneratorSpec] | None = None,
    force: bool = False,
) -> ArtifactRecord:
    """Run a generator idempotently: identical inputs reuse the existing artifact record."""
    specs = specs or load_specs()
    spec = specs[name]
    job_id = _fingerprint(spec, ctx)
    job_dir = ctx.out_dir / ctx.model.revision.rev_id / name
    record_path = job_dir / "artifact.json"
    if record_path.exists() and not force:
        existing = ArtifactRecord.load(record_path)
        if existing.job_id == job_id:
            return existing
    job_dir.mkdir(parents=True, exist_ok=True)
    result = implementation(name)(
        GeneratorContext(model=ctx.model, out_dir=job_dir, data=ctx.data, floors=ctx.floors)
    )
    has_error = any(issue.severity == "error" for issue in result.issues)
    if has_error:
        status = "qa_failed"
    elif spec.approval_mode == "pre_publish":
        status = "awaiting_approval"
    else:
        status = "published"
    record = ArtifactRecord(
        job_id=job_id,
        generator=name,
        generator_version=spec.version,
        rev_id=ctx.model.revision.rev_id,
        status=status,
        approval_mode=spec.approval_mode,
        files=[
            {
                "path": path.relative_to(job_dir).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(result.files)
        ],
        summary=result.summary,
        issues=[asdict(issue) for issue in result.issues],
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    record.save(record_path)
    return record


def _review(record_path: Path, status: str, reviewer: str, note: str | None) -> ArtifactRecord:
    record = ArtifactRecord.load(record_path)
    if record.status != "awaiting_approval":
        raise ValueError(
            f"artifact {record.generator}/{record.rev_id} is {record.status}, not awaiting_approval"
        )
    record.status = status
    record.reviewed_by = reviewer
    record.review_note = note
    record.save(record_path)
    return record


def approve(record_path: Path, reviewer: str, note: str | None = None) -> ArtifactRecord:
    return _review(record_path, "published", reviewer, note)


def reject(record_path: Path, reviewer: str, note: str | None = None) -> ArtifactRecord:
    return _review(record_path, "rejected", reviewer, note)
