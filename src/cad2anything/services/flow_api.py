"""REST route for corridor flow overlays (docs/specs/atlas-api.md §1 ``GET /api/spatial/flow``, §3 ``flow``).

``build_flow_router(toolkit)`` returns an ``APIRouter``; the toolkit provides ``FlowTools.flow_object``.
Invalid arguments are 400 (``ToolError``) and an unknown revision or floor is 404 (``NotFoundToolError``).
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException

from cad2anything.services.errors import NotFoundToolError, ToolError


def _number(value: str | None, name: str) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name}는 숫자여야 해요: {value}") from exc
    if not math.isfinite(number):
        raise HTTPException(status_code=400, detail=f"{name}는 숫자여야 해요: {value}")
    return number


def build_flow_router(toolkit: Any) -> APIRouter:
    """``GET /api/spatial/flow`` bound to ``toolkit`` (a Toolkit with ``FlowTools``)."""
    router = APIRouter(tags=["spatial"])

    @router.get("/api/spatial/flow")
    def spatial_flow(
        mode: str = "traffic",
        rev: str | None = None,
        floor: str | None = None,
        segment: str | None = None,
        day_type: str | None = None,
        hours: str | None = None,
        category: str | None = None,
        min_width_m: str | None = None,
        max_travel_m: str | None = None,
    ) -> dict[str, Any]:
        widths = _number(min_width_m, "min_width_m")
        travel = _number(max_travel_m, "max_travel_m")
        try:
            return toolkit.flow_object(
                mode=mode,
                rev=rev or None,
                floor=floor or None,
                segment=segment or None,
                day_type=day_type or None,
                hours=hours or None,
                category=category or None,
                min_width_m=widths,
                max_travel_m=travel,
            )
        except NotFoundToolError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
