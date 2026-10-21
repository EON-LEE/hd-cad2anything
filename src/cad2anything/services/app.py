"""Single ASGI process for Atlas: REST/chat API, static web, and the MCP endpoint at /mcp.

Run locally:
    uv run --no-sync uvicorn cad2anything.services.app:create_app --factory --port 8000
"""

from __future__ import annotations

import logging
import os
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

logger = logging.getLogger("cad2anything")


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
        heatmap_provider=lambda metric, rev, floor: metrics.unit_metric(toolkit, metric, rev, floor)
    )
    return AgentRuntime.from_project(endpoint, credential, ui_handler=handler)


def create_app(
    toolkit: Toolkit | None = None, runtime_factory: Any = foundry_runtime_factory
) -> PathDispatcher:
    if toolkit is None:
        workspace = workspace_from_env()
        if workspace.mirror is not None and os.environ.get("WORKSPACE_PULL", "1") == "1":
            pulled = workspace.mirror.pull(workspace.root)
            logger.info("pulled %s files from blob storage", pulled)
        toolkit = Toolkit(workspace)
    server = build_mcp(toolkit)
    mcp_app = ApiKeyGate(build_mcp_app(server))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    api = create_api(toolkit, runtime_factory=runtime_factory, lifespan=lifespan)
    return PathDispatcher(api, mcp_app)
