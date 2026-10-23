"""REST routes for drawing and metadata uploads (docs/specs/atlas-api.md §1.2).

``build_upload_router(toolkit)`` returns an ``APIRouter``; the toolkit provides the ``UploadTools`` methods.
Multipart bodies are parsed here with size limits before the toolkit validates the content. Errors map to
HTTP status codes: ``ToolError`` 400, ``NotFoundError`` 404 and ``ConflictError`` 409 (Korean ``detail``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from cad2anything.services.errors import ToolError
from cad2anything.services.metadata import KINDS
from cad2anything.services.upload_tools import MAX_METADATA_BYTES
from cad2anything.services.uploads import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    MB,
    ConflictError,
    NotFoundError,
    size_text,
)

MULTIPART_SLACK = 1 * MB
CHUNK = 1 * MB
MAX_FORM_FIELDS = 20

_DRAWINGS_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["files", "rev_id", "effective_date"],
                    "properties": {
                        "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
                        "rev_id": {"type": "string", "pattern": "^[A-Z0-9][A-Z0-9_-]{0,15}$"},
                        "parent_rev": {"type": "string"},
                        "effective_date": {"type": "string", "format": "date"},
                        "units": {"type": "string", "enum": ["m", "mm"]},
                        "profile": {"type": "string", "default": "default"},
                        "run_generators": {"type": "boolean", "default": True},
                    },
                }
            }
        },
    }
}
_METADATA_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["kind", "file"],
                    "properties": {
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "file": {"type": "string", "format": "binary"},
                    },
                }
            }
        },
    }
}


class LayerMappingItem(BaseModel):
    layer: str = Field(min_length=1, max_length=255)
    semantic: str = Field(min_length=1, max_length=40)
    floor: str | None = Field(default=None, max_length=16)


class LayerMappingRequest(BaseModel):
    mappings: list[LayerMappingItem] = Field(min_length=1, max_length=200)


def _http_error(exc: ToolError) -> HTTPException:
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ToolError as exc:
        raise _http_error(exc) from exc


def _check_length(request: Request, limit: int) -> None:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit + MULTIPART_SLACK:
        raise HTTPException(status_code=400, detail=f"전체 파일은 {size_text(limit)}까지 올릴 수 있습니다.")


def _text(form: FormData, name: str) -> str | None:
    value = form.get(name)
    if value is None or isinstance(value, UploadFile):
        return None
    return str(value).strip() or None


def _flag(value: str | None, name: str, default: bool) -> bool:
    if value is None:
        return default
    lowered = value.lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    raise HTTPException(status_code=400, detail=f"{name}는 true 또는 false여야 합니다.")


async def _read(upload: UploadFile, limit: int) -> bytes:
    """File bytes, refusing more than ``limit`` without reading the rest."""
    name = upload.filename or "파일"
    if upload.size is not None and upload.size > limit:
        raise HTTPException(
            status_code=400, detail=f"{name}: 파일 하나는 {size_text(limit)}까지 올릴 수 있습니다."
        )
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(CHUNK):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=400, detail=f"{name}: 파일 하나는 {size_text(limit)}까지 올릴 수 있습니다."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def build_upload_router(toolkit: Any) -> APIRouter:
    """Routes of §1.2 bound to ``toolkit`` (a Toolkit with ``UploadTools``)."""
    router = APIRouter(tags=["uploads"])

    @router.post("/api/uploads/drawings", status_code=202, openapi_extra=_DRAWINGS_BODY)
    async def upload_drawings(request: Request) -> dict[str, Any]:
        _check_length(request, MAX_TOTAL_BYTES)
        async with request.form(max_files=MAX_FILES, max_fields=MAX_FORM_FIELDS) as form:
            uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
            if not uploads:
                raise HTTPException(
                    status_code=400, detail="도면 파일(files)이 없습니다. DXF 여러 개나 ZIP 1개를 올리세요."
                )
            files: list[tuple[str, bytes]] = []
            total = 0
            for upload in uploads:
                name = upload.filename or ""
                data = await _read(
                    upload, MAX_TOTAL_BYTES if name.lower().endswith(".zip") else MAX_FILE_BYTES
                )
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"전체 파일은 {size_text(MAX_TOTAL_BYTES)}까지 올릴 수 있습니다.",
                    )
                files.append((name, data))
            fields = {
                name: _text(form, name)
                for name in ("rev_id", "effective_date", "parent_rev", "units", "profile")
            }
            run_generators = _flag(_text(form, "run_generators"), "run_generators", True)
        for name in ("rev_id", "effective_date"):
            if not fields[name]:
                raise HTTPException(status_code=400, detail=f"{name}가 필요합니다.")
        return await run_in_threadpool(
            _call,
            toolkit.upload_drawings,
            files,
            fields["rev_id"],
            fields["effective_date"],
            parent_rev=fields["parent_rev"],
            units=fields["units"],
            profile=fields["profile"] or "default",
            run_generators=run_generators,
        )

    @router.get("/api/uploads/jobs")
    def upload_jobs() -> list[dict[str, Any]]:
        return _call(toolkit.upload_jobs, 20)

    @router.get("/api/uploads/jobs/{job_id}")
    def upload_job(job_id: str) -> dict[str, Any]:
        return _call(toolkit.upload_job, job_id)

    @router.post("/api/uploads/jobs/{job_id}/layer-mapping")
    def upload_layer_mapping(job_id: str, request: LayerMappingRequest) -> dict[str, Any]:
        mappings = [item.model_dump() for item in request.mappings]
        return _call(toolkit.upload_layer_mapping, job_id, mappings)

    @router.get("/api/uploads/profiles")
    def upload_profiles() -> list[dict[str, Any]]:
        return _call(toolkit.upload_profiles)

    @router.post("/api/uploads/metadata", openapi_extra=_METADATA_BODY)
    async def upload_metadata(request: Request) -> dict[str, Any]:
        _check_length(request, MAX_METADATA_BYTES)
        async with request.form(max_files=1, max_fields=MAX_FORM_FIELDS) as form:
            kind = _text(form, "kind")
            upload = form.get("file")
            if not kind:
                raise HTTPException(status_code=400, detail="kind가 필요합니다.")
            if not isinstance(upload, UploadFile):
                raise HTTPException(status_code=400, detail="file이 필요합니다.")
            name = upload.filename or ""
            data = await _read(upload, MAX_METADATA_BYTES)
        return await run_in_threadpool(_call, toolkit.upload_metadata, kind, name, data)

    @router.get("/api/uploads/metadata")
    def metadata_status() -> list[dict[str, Any]]:
        return _call(toolkit.metadata_status)

    return router
