"""Atlas REST + chat API (docs/specs/atlas-api.md)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import mapping
from starlette.concurrency import iterate_in_threadpool

from cad2anything.catalog import CATEGORIES, FACILITY_CODE, FACILITY_NAME
from cad2anything.generators import approve, load_specs, plan_impact, reject
from cad2anything.generators.spatial import _category_color
from cad2anything.model import EntityKind
from cad2anything.services import metrics
from cad2anything.services.errors import NotFoundToolError
from cad2anything.services.tools import ToolError, Toolkit

FEATURE_KINDS = (
    EntityKind.EXTERIOR,
    EntityKind.WALKWAY,
    EntityKind.ZONE,
    EntityKind.UNIT,
    EntityKind.VERTICAL_LINK,
    EntityKind.AMENITY,
    EntityKind.OPENING,
)
PROPERTY_KEYS = (
    "unit_code",
    "brand_id",
    "brand_name",
    "category",
    "vacant",
    "zone_code",
    "zone_name",
    "link_id",
    "link_type",
    "amenity_type",
    "opening_id",
    "opening_type",
    "fixture_type",
    "fixture_id",
)

RuntimeFactory = Callable[[Toolkit], Any]


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reviewer: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None
    ui_state: dict[str, Any] = Field(default_factory=dict)


class LayoutRequest(BaseModel):
    rev: str
    floors: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class RelayoutPlanRequest(BaseModel):
    objective: str | None = Field(default=None, pattern="^(sales|commission)$")
    n_alternatives: int = Field(default=3, ge=1, le=5)
    max_moves: int | None = Field(default=None, ge=0, le=30)
    downtime_weeks: float | None = Field(default=None, ge=0, le=26)
    movable: str | None = Field(default=None, pattern="^(expiring|all)$")
    extra_exits: list[str] = Field(default_factory=list, max_length=10)
    extra_entries: list[str] = Field(default_factory=list, max_length=10)


class RelayoutSelectRequest(BaseModel):
    alt_id: str = Field(min_length=1, max_length=16)
    decided_by: str = Field(min_length=1, max_length=60)


class RelayoutCheckRequest(BaseModel):
    rev: str = Field(min_length=1, max_length=32)


LAYOUT_OPTIONS = {
    "keep_anchors",
    "area_tolerance",
    "max_moves",
    "n_alternatives",
    "movable_categories",
    "allow_cross_floor",
    "save",
}


def _round_coords(geometry: dict[str, Any]) -> dict[str, Any]:
    def walk(value: Any) -> Any:
        if isinstance(value, list | tuple):
            if value and isinstance(value[0], int | float):
                return [round(float(v), 3) for v in value]
            return [walk(v) for v in value]
        return value

    return {"type": geometry["type"], "coordinates": walk(geometry["coordinates"])}


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _tool_error(exc: ToolError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _latest_actual_rev(toolkit: Toolkit) -> str:
    try:
        return toolkit.latest_sales_rev()
    except ToolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _relayout_error(exc: ToolError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, NotFoundToolError) else 400, detail=str(exc))


def create_api(
    toolkit: Toolkit,
    runtime_factory: RuntimeFactory | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    web_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="CAD2Anything Atlas", version="0.1.0", lifespan=lifespan)
    ws = toolkit.ws
    runtime_holder: dict[str, Any] = {}

    def runtime() -> Any:
        if "runtime" not in runtime_holder:
            if runtime_factory is None:
                raise HTTPException(status_code=503, detail="에이전트 런타임이 구성되지 않았습니다.")
            runtime_holder["runtime"] = runtime_factory(toolkit)
        return runtime_holder["runtime"]

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "revisions": [r.rev_id for r in ws.revisions()]}

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        through = toolkit.data_through()
        revisions = [
            r.to_dict()
            | {
                "model_source": ws.model_meta(r.rev_id).get("source"),
                "has_sales": bool(
                    through is not None
                    and r.kind.value == "actual"
                    and r.effective_date
                    and r.effective_date <= through.isoformat()
                ),
            }
            for r in ws.revisions()
        ]
        floors: list[dict[str, Any]] = []
        if revisions:
            floors = [
                {"code": f.code, "ordinal": f.ordinal, "name": f.name}
                for f in sorted(ws.model(revisions[0]["rev_id"]).floors, key=lambda f: f.ordinal)
            ]
        categories = [
            {"code": code, "name_ko": category.name_ko, "color": _category_color(code)}
            for code, category in CATEGORIES.items()
        ]
        return {
            "facility": {"code": FACILITY_CODE, "name": FACILITY_NAME},
            "data_through": through.isoformat() if through else None,
            "revisions": revisions,
            "floors": floors,
            "categories": categories,
        }

    @app.get("/api/floors/{rev}/{floor}")
    def floor_geojson(rev: str, floor: str, include: str = "") -> dict[str, Any]:
        try:
            model = toolkit._model(rev)
        except ToolError as exc:
            raise _tool_error(exc) from exc
        kinds = list(FEATURE_KINDS) + ([EntityKind.FIXTURE] if "fixtures" in include.split(",") else [])
        features = []
        for kind in kinds:
            for entity in sorted(model.by_kind(kind, floor), key=lambda e: e.entity_id):
                props = {"kind": str(entity.kind), "label": entity.label}
                props |= {k: entity.attributes[k] for k in PROPERTY_KEYS if k in entity.attributes}
                if kind in (EntityKind.UNIT, EntityKind.ZONE):
                    props["area_m2"] = round(entity.geometry.area, 1)
                features.append(
                    {
                        "type": "Feature",
                        "id": entity.entity_id,
                        "geometry": _round_coords(mapping(entity.geometry)),
                        "properties": props,
                    }
                )
        if not any(f["properties"]["kind"] == "unit" for f in features):
            raise HTTPException(status_code=404, detail=f"{rev} {floor}층 데이터가 없습니다.")
        return {"type": "FeatureCollection", "features": features, "crs": "local-meters"}

    @app.get("/api/changes/{rev_from}/{rev_to}")
    def changes(rev_from: str, rev_to: str) -> dict[str, Any]:
        try:
            change_set, source = toolkit._change_set(rev_from, rev_to)
        except ToolError as exc:
            raise _tool_error(exc) from exc
        plan = plan_impact(change_set, load_specs())
        return {
            "change_set": change_set.to_dict() | {"source": source},
            "impact": [entry.__dict__ for entry in plan],
        }

    def _artifact_dir(rev: str, generator: str) -> Path:
        relative = generator.replace("--", "/")
        base = (ws.artifacts_dir / rev).resolve()
        target = (base / relative).resolve()
        if base not in target.parents:
            raise HTTPException(status_code=400, detail="잘못된 산출물 경로입니다.")
        return target

    @app.get("/api/artifacts/{rev}")
    def artifacts(rev: str) -> list[dict[str, Any]]:
        specs = load_specs()
        base = ws.artifacts_dir / rev
        results = []
        for record_path in sorted(base.rglob("artifact.json")) if base.exists() else []:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            generator_dir = record_path.parent.relative_to(base).as_posix()
            generator_id = generator_dir.replace("/", "--")
            files = record.get("files", [])
            file_names = [f["path"] if isinstance(f, dict) else f for f in files]
            spec = specs.get(record.get("generator", ""))
            results.append(
                {
                    "generator": generator_id,
                    "title_ko": spec.title_ko if spec else record.get("generator", generator_id),
                    "status": record.get("status"),
                    "approval_mode": record.get("approval_mode", "post_publish"),
                    "files": [
                        {"path": name, "url": f"/api/artifacts/{rev}/{generator_id}/{name}"}
                        for name in file_names
                    ],
                    "summary": record.get("summary", {}),
                    "issues": record.get("issues", [])[:50],
                    "reviewed_by": record.get("reviewed_by"),
                }
            )
        return results

    @app.get("/api/artifacts/{rev}/{generator}/{file_name}")
    def artifact_file(rev: str, generator: str, file_name: str) -> FileResponse:
        directory = _artifact_dir(rev, generator)
        target = (directory / file_name).resolve()
        if directory not in target.parents or not target.is_file() or target.name == "artifact.json":
            raise HTTPException(status_code=404, detail="파일이 없습니다.")
        media = {
            ".svg": "image/svg+xml",
            ".csv": "text/csv; charset=utf-8",
            ".yaml": "text/yaml; charset=utf-8",
        }
        return FileResponse(target, media_type=media.get(target.suffix), filename=target.name)

    @app.post("/api/artifacts/{rev}/{generator}/review")
    def review(rev: str, generator: str, request: ReviewRequest) -> dict[str, Any]:
        record_path = _artifact_dir(rev, generator) / "artifact.json"
        if not record_path.exists():
            raise HTTPException(status_code=404, detail="산출물이 없습니다.")
        action = approve if request.decision == "approve" else reject
        try:
            record = action(record_path, reviewer=request.reviewer, note=request.note)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ws.publish_tree(record_path.parent)
        return record.to_dict()

    @app.post("/api/revisions/{rev}/process")
    def process(rev: str, variant: str = Query("clean", pattern="^(clean|messy)$")) -> dict[str, Any]:
        try:
            return toolkit.revision_process(rev, variant)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/analytics/productivity")
    def productivity(
        rev: str, floor: str | None = None, level: str = "unit", top: int | None = None
    ) -> dict[str, Any]:
        try:
            return metrics.productivity_rows(toolkit, rev, floor, top, level=level)
        except ToolError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/analytics/cross-shopping")
    def cross_shopping(
        level: str = "category",
        focal: str | None = None,
        segment: str | None = None,
        start: str | None = None,
        end: str | None = None,
        top: int = Query(20, ge=1, le=50),
    ) -> dict[str, Any]:
        try:
            result = toolkit.analytics_cross_shopping(
                level=level, focal=focal, segment=segment, start=start, end=end, top=top
            )
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if focal:
            pairs = [
                {
                    "a": result["focal"],
                    "b": p["partner"],
                    "a_name": result.get("focal_name") or result["focal"],
                    "b_name": p["partner_name"],
                    "lift": p["lift"],
                    "jaccard": p["jaccard"],
                    "confidence_ab": p["confidence"],
                    "n_customers": p["n_customers"],
                }
                for p in result.pop("partners")
            ]
        else:
            pairs = [
                {
                    "a": p["item_a"],
                    "b": p["item_b"],
                    "a_name": p["name_a"],
                    "b_name": p["name_b"],
                    "lift": p["lift"],
                    "jaccard": p["jaccard"],
                    "confidence_ab": p["confidence_a_b"],
                    "n_customers": p["n_customers"],
                }
                for p in result.pop("pairs")
            ]
        return result | {"pairs": pairs}

    @app.get("/api/analytics/transitions")
    def transitions(
        segment: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        try:
            return toolkit.analytics_floor_transitions(segment=segment, start=start, end=end)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/analytics/journey")
    def journey(
        customer_id: str | None = None,
        date: str | None = None,
        segment: str | None = None,
        rank: int = Query(1, ge=1, le=500),
        min_stops: int = Query(3, ge=1, le=12),
        rev: str | None = None,
    ) -> dict[str, Any]:
        try:
            return toolkit.customer_journey(
                customer_id=customer_id,
                date=date,
                segment=segment,
                rank=rank,
                min_stops=min_stops,
                rev=rev,
            )
        except ToolError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/spatial/route")
    def spatial_route(
        origin: str,
        rev: str | None = None,
        destination: str | None = None,
        destination_category: str | None = None,
        destination_floor: str | None = None,
        via: str = "escalator",
    ) -> dict[str, Any]:
        revision = rev or _latest_actual_rev(toolkit)
        try:
            return toolkit.route_object(
                revision,
                origin,
                destination,
                destination_category,
                destination_floor,
                via,
                with_geometry=True,
            )
        except ToolError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/chat")
    def chat(request: ChatRequest) -> StreamingResponse:
        agent_runtime = runtime()
        ui_state = dict(request.ui_state)
        events = agent_runtime.run(request.message, ui_state, conversation_id=request.thread_id)
        return StreamingResponse(
            iterate_in_threadpool(_sse(events)),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    @app.post("/api/layout/alternatives")
    def layout_alternatives(request: LayoutRequest) -> dict[str, Any]:
        unknown = sorted(set(request.constraints) - LAYOUT_OPTIONS)
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"지원하지 않는 제약 조건입니다: {', '.join(unknown)}"
            )
        try:
            result = toolkit.layout_generate_alternatives(
                rev=request.rev, floors=request.floors or None, **request.constraints
            )
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        alternatives = []
        for alt in result["alternatives"]:
            before = alt["objective_before"]
            alternatives.append(
                {
                    "id": alt["alt_id"],
                    "rev_id": alt.get("rev_id"),
                    "summary": alt["summary"],
                    "moves": alt["moves"],
                    # Expected annual sales in 억원 so the UI can show compact objective values.
                    "objective_before": round(before / 1e8, 2),
                    "objective_after": round(alt["objective_after"] / 1e8, 2),
                    "kpi_delta": {
                        "sales_krw": alt["delta_krw"],
                        "low": alt["objective_low"] - before,
                        "high": alt["objective_high"] - before,
                    },
                    "constraints": [
                        {
                            "name": c.get("constraint"),
                            "ok": bool(c.get("satisfied")),
                            "detail": c.get("detail"),
                        }
                        for c in alt["constraint_checks"]
                    ],
                    "kpi_by_floor": alt["kpi_deltas"],
                    "explanations": alt["explanations"],
                }
            )
        keys = ("rev", "fit_rev", "period", "constraints", "label", "caveats", "note")
        return {key: result.get(key) for key in keys} | {"alternatives": alternatives}

    @app.get("/api/relayout/briefs")
    def relayout_briefs() -> list[dict[str, Any]]:
        return toolkit.relayout_briefs()

    @app.get("/api/relayout/{brief_id}")
    def relayout_project(brief_id: str) -> dict[str, Any]:
        try:
            return toolkit.relayout_project(brief_id)
        except ToolError as exc:
            raise _relayout_error(exc) from exc

    @app.post("/api/relayout/{brief_id}/diagnose")
    def relayout_diagnose(brief_id: str) -> dict[str, Any]:
        try:
            return toolkit.relayout_diagnose(brief_id)
        except ToolError as exc:
            raise _relayout_error(exc) from exc

    @app.post("/api/relayout/{brief_id}/plan")
    def relayout_plan(brief_id: str, request: RelayoutPlanRequest | None = None) -> dict[str, Any]:
        body = request or RelayoutPlanRequest()
        try:
            return toolkit.relayout_plan(brief_id, **body.model_dump())
        except ToolError as exc:
            raise _relayout_error(exc) from exc

    @app.post("/api/relayout/{brief_id}/select")
    def relayout_select(brief_id: str, request: RelayoutSelectRequest) -> dict[str, Any]:
        try:
            return toolkit.relayout_select(brief_id, request.alt_id, request.decided_by)
        except ToolError as exc:
            raise _relayout_error(exc) from exc

    @app.post("/api/relayout/{brief_id}/check")
    def relayout_check(brief_id: str, request: RelayoutCheckRequest) -> dict[str, Any]:
        try:
            return toolkit.relayout_check_drawing(brief_id, request.rev)
        except ToolError as exc:
            raise _relayout_error(exc) from exc

    @app.get("/api/relayout/{brief_id}/report", response_class=HTMLResponse)
    def relayout_report(brief_id: str) -> HTMLResponse:
        try:
            return HTMLResponse(toolkit.relayout_report_html(brief_id))
        except ToolError as exc:
            raise _relayout_error(exc) from exc

    if hasattr(toolkit, "upload_drawings"):
        from cad2anything.services.upload_api import build_upload_router

        app.include_router(build_upload_router(toolkit))

    web_dir = web_dir or (Path(os.environ["ATLAS_WEB_DIR"]) if os.environ.get("ATLAS_WEB_DIR") else None)
    if web_dir and web_dir.exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
