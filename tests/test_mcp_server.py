import asyncio
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client
from starlette.testclient import TestClient

from cad2anything.mockgen.drawings import build_all
from cad2anything.services.errors import DecisionExistsError, ToolError
from cad2anything.services.mcp_server import (
    TOOL_DESCRIPTIONS,
    ApiKeyGate,
    build_mcp,
    build_mcp_app,
    recoverable,
)
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
            # JSON text only: Foundry would append structured content to the output as a second copy.
            assert len(ok.content) == 1 and ok.structured_content is None
            assert all(tool.output_schema is None for tool in tools.values())

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


def test_tool_failures_become_results_the_model_can_act_on(caplog):
    def bad_args(rev: str) -> dict:
        raise ToolError(f"{rev} 개정이 없습니다.")

    def decided(brief_id: str, replace_decision: bool = False) -> dict:
        raise DecisionExistsError("이미 ALT-2로 결정했어요. 다시 만들려면 확인이 필요해요.")

    def broken(rev: str) -> dict:
        raise KeyError("secret-internal-detail")

    assert recoverable("spatial_floor_summary", bad_args)("R7") == {
        "ok": False,
        "tool": "spatial_floor_summary",
        "error": "R7 개정이 없습니다.",
        "hint": "인자를 고쳐 다시 호출하세요.",
    }
    decision = recoverable("relayout_plan", decided)("FW2026")
    assert decision["ok"] is False and decision["code"] == "decision_exists"
    assert "replace_decision=true" in decision["hint"] and "사용자" in decision["hint"]
    caplog.set_level(logging.ERROR, logger="cad2anything.services.mcp_server")
    internal = recoverable("spatial_query", broken)("R2")
    assert internal == {
        "ok": False,
        "tool": "spatial_query",
        "code": "internal_error",
        "error": "도구 내부 오류(KeyError)가 났어요.",
        "hint": internal["hint"],
    }
    assert "secret-internal-detail" not in json.dumps(internal, ensure_ascii=False)
    assert any(r.exc_info and "spatial_query" in r.getMessage() for r in caplog.records)
    assert list(inspect.signature(recoverable("relayout_plan", decided)).parameters) == [
        "brief_id",
        "replace_decision",
    ]


def test_unexpected_tool_errors_do_not_fail_the_protocol_call():
    def summary(rev: str, floor: str) -> dict:
        raise RuntimeError("boom")

    def other(rev: str) -> dict:
        return {"rev": rev}

    toolkit = SimpleNamespace(**{name: other for name in TOOL_DESCRIPTIONS})
    toolkit.spatial_floor_summary = summary

    async def scenario():
        async with Client(build_mcp(toolkit)) as client:
            result = await client.call_tool("spatial_floor_summary", {"rev": "R1", "floor": "3F"})
            payload = json.loads(result.content[0].text)
            assert not result.is_error and payload["ok"] is False and payload["code"] == "internal_error"
            assert "RuntimeError" in payload["error"] and "boom" not in payload["error"]

    asyncio.run(scenario())
