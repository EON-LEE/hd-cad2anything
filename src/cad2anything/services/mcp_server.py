"""MCP server exposing the cad2anything domain tools (Streamable HTTP, stateless JSON responses)."""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from cad2anything.services.tools import Toolkit

INSTRUCTIONS = (
    "백화점 도면에서 만든 버전별 공간관계 모델, 개정 변경, 생성기, 분석, 레이아웃 도구입니다. "
    "숫자는 모두 결정론적으로 계산됩니다. 추정치에는 estimate와 caveat가 함께 반환됩니다. "
    "산출물 승인과 반려는 도구로 할 수 없으며 사람이 Atlas 화면에서 합니다."
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "spatial_list_revisions": "도면 개정 목록(실제·가상, 적용일, 부모, 모델 출처)을 반환한다.",
    "spatial_floor_summary": "개정과 층을 받아 매장 수, 공실, 면적, 카테고리 구성, 수직동선, 편의시설을 요약한다.",
    "spatial_find_units": "조건(층, 카테고리 코드나 한국어 이름, 브랜드명 일부, 공실 여부)으로 매장을 찾는다.",
    "spatial_unit_details": "매장 속성, 인접 매장(공유 경계 길이), 통로 접면, 에스컬레이터·입구 보행거리, 집기 구성을 반환한다.",
    "spatial_nearby_units": "매장 경계에서 반경(m) 안의 같은 층 매장을 거리순으로 반환한다(PostGIS 사용 가능 시 PostGIS).",
    "revision_diff": "두 개정 사이의 의미 단위 변경(이동, 교체, 신설, 삭제, 분할, 병합, 면적, 집기, 통로)을 요약한다.",
    "revision_process": "업로드된 개정 도면을 파싱해 모델을 만들고, 부모 개정과 비교해 영향 계획을 세우고 산출물 초안을 재생성한다.",
    "revision_create_hypothetical": "브랜드 이동안(moves: brand_id, to_unit)으로 가상 개정을 만든다. 대상 매장이 차 있으면 서로 교환한다.",
    "registry_list_generators": "등록된 산출물 생성기(입력, 출력, 트리거, 위험도, 승인 방식)를 나열한다.",
    "registry_impact": "두 개정의 변경으로 다시 만들어야 하는 산출물과 사유, 위험도, 승인 방식을 반환한다.",
    "registry_run": "생성기를 실행해 산출물 초안을 만든다. 같은 입력이면 기존 결과를 재사용한다. 승인은 하지 않는다.",
    "registry_validate": "생성된 산출물의 검증 결과(오류, 경고)를 반환한다.",
    "render_adhoc_table": "등록되지 않은 산출물을 즉석 표로 만들고(초안), 레지스트리 등록안을 함께 만든다.",
}


def _require(name: str) -> str:
    if name not in TOOL_DESCRIPTIONS:
        raise KeyError(f"missing description for tool {name}")
    return TOOL_DESCRIPTIONS[name]


def build_mcp(
    toolkit: Toolkit, extra_tools: dict[str, tuple[Callable[..., Any], str]] | None = None
) -> MCPServer:
    server = MCPServer(name="cad2anything", instructions=INSTRUCTIONS)
    for name in TOOL_DESCRIPTIONS:
        server.add_tool(getattr(toolkit, name), name=name, description=_require(name))
    for name, (fn, description) in (extra_tools or {}).items():
        server.add_tool(fn, name=name, description=description)
    return server


def build_mcp_app(server: MCPServer) -> Any:
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


class ApiKeyGate:
    """ASGI wrapper that requires an ``x-api-key`` header for MCP requests when a key is configured."""

    def __init__(self, app: Any, api_key: str | None = None):
        self.app = app
        self.api_key = api_key if api_key is not None else os.environ.get("MCP_API_KEY")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and self.api_key:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            supplied = headers.get("x-api-key", "")
            if not hmac.compare_digest(supplied.encode(), self.api_key.encode()):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)
