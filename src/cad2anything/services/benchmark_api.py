"""REST routes for the multi-store comparison and the relayout performance review (docs/specs/atlas-api.md §1).

``build_benchmark_router(toolkit)`` returns an ``APIRouter``; the toolkit provides the ``BenchmarkTools`` methods.
``NotFoundToolError`` (an unknown revision) maps to 404 and any other ``ToolError`` to 400, with a Korean
``detail``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from cad2anything.services.errors import NotFoundToolError, ToolError


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except NotFoundToolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def build_benchmark_router(toolkit: Any) -> APIRouter:
    """Routes bound to ``toolkit`` (a Toolkit with ``BenchmarkTools``)."""
    router = APIRouter(tags=["benchmark"])

    @router.get("/api/analytics/stores")
    def store_compare(
        level: str = "floor",
        metric: str = "sales_per_m2",
        floor: str | None = None,
        category: str | None = None,
        start: str | None = None,
        end: str | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        return _call(
            toolkit.analytics_store_compare,
            level=level,
            metric=metric,
            floor=floor or None,
            category=category or None,
            start=start or None,
            end=end or None,
            top=top,
        )

    @router.get("/api/analytics/relayout-review")
    def relayout_review(rev: str, window_days: int = 90) -> dict[str, Any]:
        return _call(toolkit.analytics_relayout_review, rev, window_days)

    @router.get("/api/reports/relayout-review/{rev}", response_class=HTMLResponse)
    def relayout_review_report(rev: str, window_days: int = 90) -> HTMLResponse:
        return HTMLResponse(_call(toolkit.relayout_review_html, rev, window_days))

    return router
