"""Single ASGI process for Atlas: REST/chat API, static web, and the MCP endpoint at /mcp.

Run locally:
    uv run --no-sync uvicorn cad2anything.services.app:create_app --factory --port 8000
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from cad2anything.agents.runtime import AgentRuntime
from cad2anything.agents.ui_actions import UiActionHandler
from cad2anything.services import metrics
from cad2anything.services.atlas_api import create_api
from cad2anything.services.mcp_server import ApiKeyGate, build_mcp, build_mcp_app
from cad2anything.services.tools import Toolkit, workspace_from_env
from cad2anything.workspace import Workspace

logger = logging.getLogger("cad2anything")
_logging_lock = threading.Lock()
_logging_configured = False


def configure_logging() -> None:
    global _logging_configured
    with _logging_lock:
        if _logging_configured:
            return
        level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
        if not isinstance(level, int):
            level = logging.INFO
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        logger.setLevel(level)
        _logging_configured = True


def monitor_mirror(workspace: Workspace, stop: threading.Event, interval_s: float = 30.0) -> None:
    mirror = workspace.mirror
    probe = getattr(mirror, "healthcheck", None)
    if not callable(probe):
        return
    while not stop.wait(interval_s):
        previous = getattr(mirror, "error_count", None)
        try:
            healthy = probe()
        except Exception as exc:
            workspace.note_mirror_error(exc, previous)
            continue
        if not healthy:
            continue
        recovered = workspace.retry_pending_mirror()
        if recovered:
            logger.info("workspace mirror recovery published %d files", recovered)
        if workspace.mirror_pull_pending or not any((workspace.root / "models").glob("*.json")):
            previous = getattr(mirror, "error_count", None)
            try:
                pulled = workspace.pull_mirror()
                if pulled is None:
                    logger.warning("workspace mirror recovery pull deferred while local writes remain")
                else:
                    logger.info("workspace mirror recovery pulled %d files", pulled)
            except Exception as exc:
                workspace.note_mirror_error(exc, previous)
                continue


class PathDispatcher:
    """Route /mcp to the MCP app and everything else (including lifespan) to the API app."""

    def __init__(self, api_app: Any, mcp_app: Any, prefix: str = "/mcp"):
        self.api_app = api_app
        self.mcp_app = mcp_app
        self.prefix = prefix

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == self.prefix or path.startswith(self.prefix + "/")):
            await self.mcp_app(scope, receive, send)
        else:
            await self.api_app(scope, receive, send)


def foundry_runtime_factory(toolkit: Toolkit) -> AgentRuntime:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is not set")
    from azure.identity import DefaultAzureCredential

    client_id = os.environ.get("AZURE_CLIENT_ID")
    credential = (
        DefaultAzureCredential(managed_identity_client_id=client_id)
        if client_id
        else DefaultAzureCredential()
    )
    handler = UiActionHandler(
        heatmap_provider=lambda metric, rev, floor: metrics.unit_metric(toolkit, metric, rev, floor),
        journey_provider=lambda **params: toolkit.customer_journey(**params, with_routes=True),
        route_provider=lambda rev=None, **params: toolkit.route_object(
            rev or latest_actual_rev(toolkit), **params, with_geometry=True
        ),
        flow_provider=lambda rev=None, **params: toolkit.flow_object(rev=rev, **params),
        has_sales=toolkit._has_sales,
    )
    return AgentRuntime.from_project(endpoint, credential, ui_handler=handler)


def latest_actual_rev(toolkit: Toolkit) -> str:
    """Latest actual revision with transactions (uploaded future revisions have no sales yet)."""
    return toolkit.latest_sales_rev()


def warm_up(toolkit: Toolkit) -> None:
    """Load transactions and build caches so the first analytics and spatial questions answer quickly."""
    started = time.monotonic()
    try:
        toolkit._journey_rows()
        if toolkit._actual_revisions():
            toolkit.unit_table(toolkit.latest_sales_rev())
        logger.info("analytics caches warmed in %.1fs", time.monotonic() - started)
    except Exception as exc:  # warm-up is best effort; requests compute the same caches on demand
        logger.warning("analytics warm-up failed error_type=%s", type(exc).__name__)


def create_app(
    toolkit: Toolkit | None = None, runtime_factory: Any = foundry_runtime_factory
) -> PathDispatcher:
    configure_logging()
    if toolkit is None:
        workspace = workspace_from_env()
        if workspace.mirror is not None and os.environ.get("WORKSPACE_PULL", "1") == "1":
            workspace.mirror_pull_pending = True
            previous = getattr(workspace.mirror, "error_count", None)
            try:
                pulled = workspace.pull_mirror()
                if pulled is None:
                    raise RuntimeError("workspace mirror pull is blocked by pending local writes")
                backend = getattr(workspace.mirror, "backend", "blob")
                logger.info("pulled %d files from %s workspace mirror", pulled, backend)
            except Exception as exc:
                workspace.note_mirror_error(exc, previous)
        toolkit = Toolkit(workspace)
    server = build_mcp(toolkit)
    mcp_app = ApiKeyGate(build_mcp_app(server))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        monitor = None
        if callable(getattr(toolkit.ws.mirror, "healthcheck", None)):
            monitor = threading.Thread(
                target=monitor_mirror,
                args=(toolkit.ws, stop),
                name="workspace-mirror-monitor",
                daemon=True,
            )
            monitor.start()
        if os.environ.get("ATLAS_WARMUP", "1") == "1":
            threading.Thread(target=warm_up, args=(toolkit,), name="analytics-warmup", daemon=True).start()
        try:
            async with server.session_manager.run():
                yield
        finally:
            stop.set()
            if monitor is not None:
                monitor.join(timeout=1)

    api = create_api(toolkit, runtime_factory=runtime_factory, lifespan=lifespan)
    return PathDispatcher(api, mcp_app)
