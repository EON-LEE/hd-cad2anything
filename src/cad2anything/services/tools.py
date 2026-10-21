"""Domain tools behind the MCP server. Every tool is deterministic and returns compact JSON-ready dicts."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from cad2anything.catalog import AMENITY_TYPES, CATEGORIES, FIXTURE_TYPES, floor_program
from cad2anything.generators import GeneratorContext, load_specs, plan_impact, run_generator
from cad2anything.generators.spatial import _write_tables
from cad2anything.model import (
    ChangeItem,
    ChangeSet,
    ChangeType,
    EntityKind,
    RelationType,
    Revision,
    RevisionKind,
    SpatialModel,
)
from cad2anything.services.analytics_tools import AnalyticsTools
from cad2anything.services.errors import ToolError
from cad2anything.workspace import Workspace

DEFAULT_LIMIT = 50
BUSINESS_TABLES = ("tenancies", "pos_terminals", "transactions")
ADHOC_KINDS = {k.value for k in EntityKind}

__all__ = ["DEFAULT_LIMIT", "ToolError", "Toolkit", "apply_moves", "workspace_from_env"]


def _round(value: Any, digits: int = 2) -> Any:
    return round(float(value), digits) if isinstance(value, int | float) else value


def _category_name(code: str | None) -> str:
    return CATEGORIES[code].name_ko if code in CATEGORIES else (code or "")


class Toolkit(AnalyticsTools):
    def __init__(self, workspace: Workspace, use_postgis: bool | None = None):
        self.ws = workspace
        self.use_postgis = bool(os.environ.get("POSTGRES_HOST")) if use_postgis is None else use_postgis
        self._features: dict[tuple[str, float], pd.DataFrame] = {}
        self._init_analytics()

    # Helpers ---------------------------------------------------------------------------------------
    def _model(self, rev: str) -> SpatialModel:
        try:
            return self.ws.model(rev)
        except KeyError as exc:
            known = ", ".join(r.rev_id for r in self.ws.revisions())
            raise ToolError(f"개정 {rev}가 없습니다. 사용 가능한 개정: {known}") from exc

    def _unit(self, model: SpatialModel, unit_code: str):
        unit = model.unit_by_code(unit_code)
        if unit is None:
            raise ToolError(f"{model.revision.rev_id}에 매장 {unit_code}가 없습니다.")
        return unit

    def unit_features(self, rev: str) -> pd.DataFrame | None:
        """Walking-distance features from cad2anything.spatial when available (cached per model version)."""
        try:
            from cad2anything.spatial.features import unit_features
        except ImportError:
            return None
        path = self.ws.root / "models" / f"{rev}.json"
        key = (rev, path.stat().st_mtime if path.exists() else 0.0)
        if key not in self._features:
            self._features[key] = unit_features(self._model(rev))
        return self._features[key]

    def business_data(self) -> dict[str, pd.DataFrame]:
        return {name: self.ws.table(name) for name in BUSINESS_TABLES if self.ws.has_table(name)}

    @staticmethod
    def _unit_row(unit) -> dict[str, Any]:
        attrs = unit.attributes
        return {
            "unit_code": attrs.get("unit_code"),
            "floor": unit.floor,
            "brand_id": attrs.get("brand_id"),
            "brand_name": attrs.get("brand_name"),
            "category": attrs.get("category"),
            "category_name": _category_name(attrs.get("category")),
            "area_m2": round(unit.geometry.area, 1),
            "vacant": bool(attrs.get("vacant")),
        }

    # Spatial ---------------------------------------------------------------------------------------
    def spatial_list_revisions(self) -> dict[str, Any]:
        revisions = []
        for revision in self.ws.revisions():
            meta = self.ws.model_meta(revision.rev_id)
            revisions.append(revision.to_dict() | {"model_source": meta.get("source")})
        return {"revisions": revisions}

    def spatial_floor_summary(self, rev: str, floor: str) -> dict[str, Any]:
        model = self._model(rev)
        units = model.units(floor)
        if not units:
            raise ToolError(f"{rev} {floor}층에 매장이 없습니다.")
        by_category: dict[str, list[float]] = {}
        for unit in units:
            if not unit.attributes.get("vacant"):
                by_category.setdefault(unit.attributes.get("category") or "UNKNOWN", []).append(
                    unit.geometry.area
                )
        gross = sum(e.geometry.area for e in model.by_kind(EntityKind.EXTERIOR, floor))
        try:
            floor_name = floor_program(floor).name_ko
        except KeyError:
            floor_name = ""
        return {
            "rev": rev,
            "floor": floor,
            "floor_name": floor_name,
            "units": len(units),
            "vacant_units": [u.attributes.get("unit_code") for u in units if u.attributes.get("vacant")],
            "gross_m2": round(gross, 1),
            "exclusive_m2": round(sum(u.geometry.area for u in units), 1),
            "categories": sorted(
                (
                    {
                        "category": c,
                        "name_ko": _category_name(c),
                        "units": len(a),
                        "area_m2": round(sum(a), 1),
                    }
                    for c, a in by_category.items()
                ),
                key=lambda row: -row["area_m2"],
            ),
            "vertical_links": sorted(
                {v.attributes.get("link_id") for v in model.by_kind(EntityKind.VERTICAL_LINK, floor)}
            ),
            "amenities": sorted(
                {
                    AMENITY_TYPES.get(a.attributes.get("amenity_type"), "")
                    for a in model.by_kind(EntityKind.AMENITY, floor)
                }
            ),
        }

    def spatial_find_units(
        self,
        rev: str,
        floor: str | None = None,
        category: str | None = None,
        brand_query: str | None = None,
        vacant: bool | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        model = self._model(rev)
        query = (brand_query or "").strip().lower()
        rows = []
        for unit in sorted(model.units(floor), key=lambda u: u.attributes.get("unit_code") or ""):
            attrs = unit.attributes
            if (
                category
                and attrs.get("category") != category
                and _category_name(attrs.get("category")) != category
            ):
                continue
            if (
                query
                and query not in (attrs.get("brand_name") or "").lower()
                and query != (attrs.get("brand_id") or "").lower()
            ):
                continue
            if vacant is not None and bool(attrs.get("vacant")) != vacant:
                continue
            rows.append(self._unit_row(unit))
        limit = max(1, min(int(limit), 200))
        return {"rev": rev, "count": len(rows), "units": rows[:limit], "truncated": len(rows) > limit}

    def spatial_unit_details(self, rev: str, unit_code: str) -> dict[str, Any]:
        model = self._model(rev)
        unit = self._unit(model, unit_code)
        result = self._unit_row(unit) | {"rev": rev, "zone_code": unit.attributes.get("zone_code")}
        neighbors = []
        for relation in model.relations_of(unit.entity_id, RelationType.ADJACENT_TO):
            other_id = relation.to_id if relation.from_id == unit.entity_id else relation.from_id
            other = model.entities.get(other_id)
            if other is not None and other.kind == EntityKind.UNIT:
                neighbors.append(
                    self._unit_row(other) | {"shared_m": _round(relation.metrics.get("shared_length_m", 0))}
                )
        if not neighbors:
            for other in model.units(unit.floor):
                if other.entity_id != unit.entity_id and unit.geometry.buffer(0.05).intersects(
                    other.geometry
                ):
                    shared = unit.geometry.boundary.intersection(other.geometry.buffer(0.05)).length
                    if shared >= 0.5:
                        neighbors.append(self._unit_row(other) | {"shared_m": round(shared, 2)})
        result["neighbors"] = sorted(neighbors, key=lambda row: -row["shared_m"])
        features = self.unit_features(rev)
        if features is not None:
            match = features[features["unit_code"] == unit_code]
            if not match.empty:
                row = match.iloc[0]
                for column in ("frontage_m", "walk_to_escalator_m", "walk_to_entrance_m"):
                    if column in row and pd.notna(row[column]):
                        result[column] = round(float(row[column]), 1)
        fixtures = [
            f
            for f in model.by_kind(EntityKind.FIXTURE, unit.floor)
            if f.attributes.get("unit_code") == unit_code
        ]
        counts = Counter(f.attributes.get("fixture_type") for f in fixtures)
        result["fixtures"] = {
            FIXTURE_TYPES[k].name_ko if k in FIXTURE_TYPES else k: v for k, v in sorted(counts.items())
        }
        return result

    def spatial_nearby_units(self, rev: str, unit_code: str, radius_m: float = 15.0) -> dict[str, Any]:
        radius_m = max(0.0, min(float(radius_m), 60.0))
        if self.use_postgis:
            try:
                from cad2anything.store import postgis

                with postgis.connect() as conn:
                    rows = postgis.nearby_units(conn, rev, unit_code, radius_m)
                return {
                    "rev": rev,
                    "unit_code": unit_code,
                    "radius_m": radius_m,
                    "engine": "postgis",
                    "units": rows,
                }
            except Exception as exc:  # fall back to in-memory geometry
                engine_note = f"PostGIS 사용 불가({type(exc).__name__}), 메모리 계산으로 대체"
        else:
            engine_note = "메모리 계산"
        model = self._model(rev)
        unit = self._unit(model, unit_code)
        rows = []
        for other in model.units(unit.floor):
            if other.entity_id == unit.entity_id:
                continue
            distance = unit.geometry.distance(other.geometry)
            if distance <= radius_m:
                rows.append(
                    self._unit_row(other) | {"distance_m": round(distance, 2), "touches": distance < 0.05}
                )
        rows.sort(key=lambda row: (row["distance_m"], row["unit_code"]))
        return {
            "rev": rev,
            "unit_code": unit_code,
            "radius_m": radius_m,
            "engine": engine_note,
            "units": rows,
        }

    # Revisions -------------------------------------------------------------------------------------
    def _change_set(self, rev_from: str, rev_to: str) -> tuple[ChangeSet, str]:
        change_set = self.ws.change_set(rev_from, rev_to)
        if change_set is not None:
            return change_set, self.ws.change_meta(rev_from, rev_to).get("source", "stored")
        try:
            from cad2anything.revision.diff import diff_models
        except ImportError as exc:
            raise ToolError(f"{rev_from}→{rev_to} 변경 집합이 없고 diff 엔진도 없습니다.") from exc
        change_set = diff_models(self._model(rev_from), self._model(rev_to))
        self.ws.save_change_set(change_set, {"source": "diff"})
        return change_set, "diff"

    def revision_diff(self, rev_from: str, rev_to: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        change_set, source = self._change_set(rev_from, rev_to)
        counts = Counter(str(item.change_type) for item in change_set.items)
        items = [
            {
                "change_type": str(item.change_type),
                "floor": item.floor,
                "units_before": item.unit_codes_before,
                "units_after": item.unit_codes_after,
                "brands_before": item.brand_ids_before,
                "brands_after": item.brand_ids_after,
                "summary": item.summary,
            }
            for item in change_set.items
        ]
        return {
            "rev_from": rev_from,
            "rev_to": rev_to,
            "source": source,
            "total": len(items),
            "by_type": dict(sorted(counts.items())),
            "by_floor": dict(sorted(Counter(item.floor for item in change_set.items).items())),
            "items": items[:limit],
            "truncated": len(items) > limit,
        }

    def revision_process(
        self, rev: str, variant: str = "clean", run_generators: bool = True
    ) -> dict[str, Any]:
        """Parse a revision's drawings, diff against its parent, plan impact, and regenerate drafts."""
        try:
            from cad2anything.cadcore.pipeline import parse_revision
        except ImportError as exc:
            raise ToolError("CAD 이해 파이프라인을 사용할 수 없습니다.") from exc
        rev_dir = self.ws.drawing_dir(rev, variant)
        if not (rev_dir / "manifest.json").exists():
            raise ToolError(f"{rev}/{variant} 도면이 업로드되지 않았습니다.")
        brands = self.ws.table("brands") if self.ws.has_table("brands") else None
        model, report = parse_revision(rev_dir, brand_registry=brands, rev_id=rev)
        report_dict = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        self.ws.save_report(rev, report_dict)
        self.ws.save_model(model, {"source": "parsed", "variant": variant})
        result: dict[str, Any] = {
            "rev": rev,
            "variant": variant,
            "units": len(model.units()),
            "parse_issues": Counter(i.get("severity", "info") for i in report_dict.get("issues", [])),
            "review_queue": report_dict.get("review_queue", [])[:10],
        }
        parent = model.revision.parent_rev_id
        if parent and self.ws.has_model(parent):
            from cad2anything.revision.diff import diff_models

            change_set = diff_models(self.ws.model(parent), model)
            self.ws.save_change_set(change_set, {"source": "diff"})
            result["changes"] = self.revision_diff(parent, rev, limit=20)
            plan = plan_impact(change_set, load_specs())
            result["impact"] = [entry.__dict__ for entry in plan]
            if run_generators:
                result["artifacts"] = [self.registry_run(entry.generator, rev) for entry in plan]
        return result

    def revision_create_hypothetical(
        self, base_rev: str, moves: list[dict[str, str]], rev_id: str | None = None, notes: str = ""
    ) -> dict[str, Any]:
        base = self._model(base_rev)
        model, change_set = apply_moves(base, moves, rev_id or self._next_hypothetical_id(), notes)
        self.ws.save_model(model, {"source": "hypothetical", "base": base_rev})
        self.ws.save_change_set(change_set, {"source": "hypothetical"})
        return {
            "rev_id": model.revision.rev_id,
            "base_rev": base_rev,
            "changes": [item.summary for item in change_set.items],
            "note": "가상 개정입니다. 실제 도면이 아니며 검토용입니다.",
        }

    def _next_hypothetical_id(self) -> str:
        existing = {r.rev_id for r in self.ws.revisions()}
        index = 2
        while f"H{index}" in existing:
            index += 1
        return f"H{index}"

    # Registry --------------------------------------------------------------------------------------
    def registry_list_generators(self) -> dict[str, Any]:
        return {
            "generators": [
                {
                    "name": spec.name,
                    "title_ko": spec.title_ko,
                    "description": spec.description,
                    "outputs": list(spec.outputs),
                    "triggers": list(spec.trigger_change_types),
                    "risk_tier": spec.risk_tier,
                    "approval_mode": spec.approval_mode,
                }
                for spec in load_specs().values()
            ]
        }

    def registry_impact(self, rev_from: str, rev_to: str) -> dict[str, Any]:
        change_set, source = self._change_set(rev_from, rev_to)
        plan = plan_impact(change_set, load_specs())
        return {
            "rev_from": rev_from,
            "rev_to": rev_to,
            "change_source": source,
            "plan": [e.__dict__ for e in plan],
        }

    def registry_run(self, generator: str, rev: str, floors: list[str] | None = None) -> dict[str, Any]:
        specs = load_specs()
        if generator not in specs:
            raise ToolError(f"생성기 {generator}가 없습니다. 사용 가능: {', '.join(specs)}")
        ctx = GeneratorContext(
            model=self._model(rev),
            out_dir=self.ws.artifacts_dir,
            data=self.business_data(),
            floors=floors or None,
        )
        record = run_generator(generator, ctx, specs)
        self.ws.publish_tree(self.ws.artifacts_dir / rev / generator)
        return {
            "generator": generator,
            "rev": rev,
            "status": record.status,
            "approval_mode": record.approval_mode,
            "files": [f["path"] for f in record.files],
            "summary": record.summary,
            "issues": record.issues[:20],
            "issue_count": len(record.issues),
            "note": "승인과 반려는 사람이 개정 검토 화면에서 합니다."
            if record.status == "awaiting_approval"
            else "",
        }

    def registry_validate(self, rev: str, generator: str) -> dict[str, Any]:
        path = self.ws.artifacts_dir / rev / generator / "artifact.json"
        if not path.exists():
            raise ToolError(f"{rev}의 {generator} 산출물이 아직 없습니다. registry_run을 먼저 실행하세요.")
        record = json.loads(path.read_text(encoding="utf-8"))
        issues = record.get("issues", [])
        return {
            "generator": generator,
            "rev": rev,
            "status": record.get("status"),
            "errors": [i for i in issues if i.get("severity") == "error"][:20],
            "warnings": [i for i in issues if i.get("severity") == "warning"][:20],
            "counts": dict(Counter(i.get("severity") for i in issues)),
        }

    # Ad hoc artifacts ------------------------------------------------------------------------------
    def render_adhoc_table(
        self,
        rev: str,
        entity_kind: str,
        fields: list[str],
        title: str,
        floor: str | None = None,
        group_by: str | None = None,
    ) -> dict[str, Any]:
        if entity_kind not in ADHOC_KINDS:
            raise ToolError(f"entity_kind는 {sorted(ADHOC_KINDS)} 중 하나여야 합니다.")
        model = self._model(rev)
        entities = sorted(model.by_kind(EntityKind(entity_kind), floor), key=lambda e: e.entity_id)
        available = {"entity_id", "floor", "label", "area_m2", "length_m"}
        available |= {key for entity in entities for key in entity.attributes}
        unknown = [name for name in fields if name not in available]
        if unknown:
            raise ToolError(
                f"{entity_kind}에 없는 필드입니다: {unknown}. 사용 가능한 필드: {sorted(available)}"
            )
        rows = []
        for entity in entities:
            base = {
                "entity_id": entity.entity_id,
                "floor": entity.floor,
                "label": entity.label,
                "area_m2": round(entity.geometry.area, 2),
                "length_m": round(entity.geometry.length, 2),
            }
            row = {name: base.get(name, entity.attributes.get(name)) for name in fields}
            rows.append(row)
        frame = pd.DataFrame(rows, columns=fields)
        if group_by:
            if group_by not in fields:
                raise ToolError("group_by는 fields에 포함돼야 합니다.")
            numeric = [c for c in fields if c != group_by and pd.api.types.is_numeric_dtype(frame[c])]
            grouped = frame.groupby(group_by, dropna=False)
            frame = grouped.size().rename("count").to_frame().join(grouped[numeric].sum()).reset_index()
        slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", title).strip("_")[:40] or "adhoc"
        out_dir = self.ws.artifacts_dir / rev / "adhoc" / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        header = list(frame.columns)
        values = [
            [v.item() if hasattr(v, "item") else v for v in row] for row in frame.itertuples(index=False)
        ]
        _write_tables(
            out_dir / f"{slug}.xlsx",
            out_dir / f"{slug}.csv",
            {title[:31] or "표": (header, values)},
            title[:31] or "표",
        )
        proposal = {
            "name": f"adhoc_{slug}".lower(),
            "version": "0.1",
            "title_ko": title,
            "description": f"즉석 생성 산출물({entity_kind})을 등록하기 위한 제안",
            "inputs": {"entity_kinds": [entity_kind], "data": []},
            "outputs": ["xlsx", "csv"],
            "triggers": {"change_types": [str(t) for t in ChangeType]},
            "risk_tier": "low",
            "approval_mode": "post_publish",
        }
        (out_dir / "proposed_spec.yaml").write_text(
            yaml.safe_dump(proposal, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        record = {
            "generator": f"adhoc/{slug}",
            "rev": rev,
            "status": "draft",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "rows": len(frame),
            "files": [f"{slug}.xlsx", f"{slug}.csv", "proposed_spec.yaml"],
        }
        (out_dir / "artifact.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.ws.publish_tree(out_dir)
        return record | {
            "preview": values[:10],
            "columns": header,
            "note": "초안입니다. 반복해서 쓰려면 proposed_spec.yaml을 검토해 레지스트리에 등록하세요.",
        }


def apply_moves(
    base: SpatialModel, moves: list[dict[str, str]], rev_id: str, notes: str = ""
) -> tuple[SpatialModel, ChangeSet]:
    """Create a hypothetical revision by relocating brands; occupied targets swap brands."""
    model = SpatialModel.from_dict(base.to_dict())
    model.revision = Revision(
        rev_id=rev_id,
        kind=RevisionKind.HYPOTHETICAL,
        parent_rev_id=base.revision.rev_id,
        effective_date=None,
        source="what-if",
        notes=notes,
    )
    keys = ("brand_id", "brand_name", "category", "vacant")
    items: list[ChangeItem] = []
    for move in moves:
        brand_id, target_code = move.get("brand_id"), move.get("to_unit")
        source_unit = next((u for u in model.units() if u.attributes.get("brand_id") == brand_id), None)
        target_unit = model.unit_by_code(target_code or "")
        if source_unit is None or target_unit is None:
            raise ToolError(f"이동 불가: 브랜드 {brand_id} 또는 매장 {target_code}를 찾을 수 없습니다.")
        if source_unit.entity_id == target_unit.entity_id:
            continue
        moving = {k: source_unit.attributes.get(k) for k in keys}
        displaced = {k: target_unit.attributes.get(k) for k in keys}
        for key in keys:
            target_unit.attributes[key] = moving[key]
        if displaced.get("brand_id"):
            for key in keys:
                source_unit.attributes[key] = displaced[key]
        else:
            source_unit.attributes.update({"brand_id": None, "brand_name": None, "vacant": True})
        from_code = source_unit.attributes.get("unit_code")
        summary = f"{moving['brand_name']}: {from_code} → {target_code}"
        if displaced.get("brand_id"):
            summary += f" (기존 {displaced['brand_name']}는 {from_code}로 교환)"
        items.append(
            ChangeItem(
                ChangeType.MOVE,
                target_unit.floor or "",
                [from_code],
                [target_code],
                [brand_id],
                [brand_id],
                [source_unit.entity_id],
                [target_unit.entity_id],
                {"swap_with": displaced.get("brand_id")},
                summary,
            )
        )
    return model, ChangeSet(base.revision.rev_id, rev_id, items)


def workspace_from_env() -> Workspace:
    from cad2anything.store.pgmirror import mirror_from_env

    root = Path(os.environ.get("WORKSPACE_DIR", "data/workspace"))
    return Workspace(root, mirror=mirror_from_env())
