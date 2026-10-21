"""MCP server exposing the cad2anything domain tools (Streamable HTTP, stateless JSON responses)."""

from __future__ import annotations

import functools
import hmac
import inspect
import os
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from cad2anything.services.tools import ToolError, Toolkit

INSTRUCTIONS = (
    "백화점 도면에서 만든 버전별 공간관계 모델, 개정 변경, 생성기, 분석, 레이아웃 도구입니다. "
    "숫자는 모두 결정론적으로 계산됩니다. 추정치에는 estimate와 caveat가 함께 반환됩니다. "
    "산출물 승인과 반려는 도구로 할 수 없으며 사람이 Atlas 화면에서 합니다."
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "spatial_list_revisions": "도면 개정 목록(실제·가상, 적용일, 부모, 모델 출처)을 반환한다.",
    "spatial_floor_summary": "개정과 층을 받아 매장 수, 공실, 면적, 카테고리 구성, 수직동선, 편의시설을 요약한다.",
    "spatial_find_units": "조건(층, 카테고리 코드나 한국어 이름, 브랜드명 일부, 공실 여부)으로 매장을 찾는다. sort_by로 에스컬레이터·입구 보행거리 가까운 순(walk_to_escalator_m, walk_to_entrance_m)이나 면적 큰 순(area_m2)으로 정렬한다.",
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
    "analytics_productivity": "실제 개정의 적용 기간 실적으로 매장·구역·층·카테고리별 연환산 ㎡당 매출, 평당 매출, GMROF, 백분위, 저효율 여부와 저효율 원인을 반환한다. 가상 개정은 지원하지 않는다.",
    "analytics_drivers": "매장 ㎡당 매출을 노출(에스컬레이터 보행거리), 통로 접면, 면적, 카테고리로 분해한 회귀 결과와 저효율 매장별 주요 하락 요인을 반환한다(추정치, 인과 아님).",
    "analytics_cross_shopping": "교차구매 lift, Jaccard, confidence를 반환한다. segment(age_band, gender, tier, home_area)로 고객군을 거르고, focal을 주면 함께 사는 상위 항목을 반환한다. 기본 장바구니는 같은 고객의 같은 날 구매다.",
    "analytics_floor_transitions": "같은 날 결제 순서로 본 층간 전이 확률 행렬과 분수효과(지하 식품 → 위층), 샤워효과(최상층 → 아래층) 지표를 반환한다. 실제 이동 경로가 아니다.",
    "analytics_adjacency_effect": "특정 카테고리 매장 근처(neighborhood=near: 경계 사이 radius_m 이내, 통로 건너편 포함. wall: 벽 공유) 매장의 ㎡당 매출 차이를 단순 비교와 노출 통제 추정으로 함께 보여주고, 공통 원인(노출) 의심 여부를 판정한다(추정치).",
    "analytics_before_after": "변경일 전후 같은 기간의 일평균 매출을 대상 매장(또는 key=brand_id로 브랜드)과 같은 층 비교군으로 비교한다. 인과 효과가 아니다.",
    "analytics_time_patterns": "시간대(hour), 요일(weekday), 날짜, 월과 카테고리·층 조합별 매출과 비중을 반환한다. category에는 카테고리 코드나 이름, 또는 식음료 전체를 뜻하는 'F&B'를 쓸 수 있다.",
    "layout_generate_alternatives": "실제 개정을 기준으로 제약(층, 앵커 유지, 면적 허용 오차, 최대 이동 수, 이동 가능 카테고리)을 지키며 기대 매출을 높이는 배치 대안을 만들고 가상 개정으로 저장한다. 이동별 기여도, 층별 변화 범위, 제약 충족표를 반환한다(추정치).",
    "whatif_score_moves": "브랜드 이동안(moves: brand_id 또는 브랜드명, to_unit)의 연간 기대 매출 변화와 범위, 층별 영향, 제약 충족 여부를 추정한다. 가상 개정은 만들지 않는다.",
    "analytics_customer_journey": "가명 고객 한 명의 하루 결제 순서(시각, 층, 매장, 금액, 이전 결제 후 경과 시간)를 반환한다. customer_id·date를 주거나, segment(age_band, gender, tier)와 rank로 조건에 맞는 고객 하루를 고른다. 결제 순서이며 실제 이동 경로가 아니다.",
}


def _require(name: str) -> str:
    if name not in TOOL_DESCRIPTIONS:
        raise KeyError(f"missing description for tool {name}")
    return TOOL_DESCRIPTIONS[name]


def recoverable(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return argument and data errors as a normal result so the model can correct its call.

    Foundry Agent Service fails the whole response (HTTP 400) when an MCP tool reports an error.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return {"ok": False, "tool": name, "error": str(exc), "hint": "인자를 고쳐 다시 호출하세요."}

    wrapper.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
    return wrapper


def build_mcp(
    toolkit: Toolkit, extra_tools: dict[str, tuple[Callable[..., Any], str]] | None = None
) -> MCPServer:
    server = MCPServer(name="cad2anything", instructions=INSTRUCTIONS)
    for name in TOOL_DESCRIPTIONS:
        server.add_tool(recoverable(name, getattr(toolkit, name)), name=name, description=_require(name))
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
