import asyncio
import json
from pathlib import Path

import pytest
from mcp import Client
from starlette.testclient import TestClient

from cad2anything.mockgen.drawings import build_all
from cad2anything.services.mcp_server import TOOL_DESCRIPTIONS, ApiKeyGate, build_mcp, build_mcp_app
from cad2anything.services.tools import Toolkit
from cad2anything.workspace import Workspace

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    workspace = Workspace(tmp_path_factory.mktemp("mcp-ws"))
    _, models, changes = build_all(42)
    for model in models.values():
        workspace.save_model(model, {"source": "truth"})
    for change_set in changes.values():
        workspace.save_change_set(change_set, {"source": "truth"})
    return build_mcp(Toolkit(workspace, use_postgis=False))


def test_tool_names_follow_the_contract():
    contract = (REPO / "docs" / "specs" / "mcp-tools.md").read_text(encoding="utf-8")
    for name in TOOL_DESCRIPTIONS:
        assert f"`{name}`" in contract, name


def test_tools_over_the_protocol(server):
    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(TOOL_DESCRIPTIONS) <= set(tools)
            assert tools["spatial_find_units"].input_schema["required"] == ["rev"]

            ok = await client.call_tool("spatial_floor_summary", {"rev": "R1", "floor": "3F"})
            assert not ok.is_error and json.loads(ok.content[0].text)["units"] == 42

            bad = await client.call_tool("spatial_floor_summary", {"rev": "R7", "floor": "3F"})
            payload = json.loads(bad.content[0].text)
            assert not bad.is_error and payload["ok"] is False
            assert "R7" in payload["error"] and "R0" in payload["error"]

    asyncio.run(scenario())


def test_api_key_gate_blocks_requests_without_key(server):
    app = ApiKeyGate(build_mcp_app(server), api_key="secret-for-test")
    with TestClient(app) as client:
        assert client.post("/mcp", json={}).status_code == 401
        allowed = client.post(
            "/mcp",
            headers={"x-api-key": "secret-for-test", "accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert allowed.status_code != 401
