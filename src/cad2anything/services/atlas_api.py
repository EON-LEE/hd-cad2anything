"""Atlas REST + chat API (docs/specs/atlas-api.md)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import mapping
from starlette.concurrency import iterate_in_threadpool

from cad2anything.catalog import CATEGORIES, FACILITY_CODE, FACILITY_NAME
from cad2anything.generators import approve, load_specs, plan_impact, reject
from cad2anything.generators.spatial import _category_color
from cad2anything.model import EntityKind
from cad2anything.services import metrics
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
    floors: list[str]
    constraints: dict[str, Any] = Field(default_factory=dict)


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
        revisions = [
            r.to_dict() | {"model_source": ws.model_meta(r.rev_id).get("source")} for r in ws.revisions()
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
        if level != "unit":
            raise HTTPException(status_code=400, detail="현재 level=unit만 지원합니다.")
        try:
            return metrics.productivity_rows(toolkit, rev, floor, top)
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
        handler = getattr(toolkit, "layout_generate_alternatives", None)
        if handler is None:
            raise HTTPException(status_code=503, detail="레이아웃 엔진을 사용할 수 없습니다.")
        try:
            return handler(rev=request.rev, floors=request.floors, **request.constraints)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    web_dir = web_dir or (Path(os.environ["ATLAS_WEB_DIR"]) if os.environ.get("ATLAS_WEB_DIR") else None)
    if web_dir and web_dir.exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
