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
    "analytics_store_compare": (
        "우리 점포와 다른 점포를 비교한다. level=store(점포 전체), floor(층별), category(카테고리별)로 metric(sales_per_m2 ㎡당 매출, "
        "sales_per_pyeong 평효율, sales_krw 연환산 매출, area_m2 면적, growth_pct 직전 같은 기간 대비 성장률)을 점포마다 계산하고, "
        "다른 점포 중앙값, 순위(1위가 가장 큼), 중앙값 대비 차이(gap_pct)와 한국어 요약을 반환한다. floor·category로 거를 수 있다. "
        "기본 기간은 최근 12개월이다. 다른 점포 실적은 사람이 올린 store_kpis 메타데이터이고 집계 기준이 다를 수 있다."
    ),
    "analytics_relayout_review": (
        "지난 개편(실제 개정 rev)의 성과를 추적한다. 부모 개정과 비교해 바뀐 매장(이동, 교체, 신설, 분할·병합, 면적 변경 등)마다 "
        "적용일 앞뒤 같은 기간(window_days, 기본 90일)의 일평균 매출 변화를 계산하고, 같은 층의 바뀌지 않은 매장(비교군) 변화와 "
        "비교한다. 변경 유형별·층별 합계, 전체 합계, 한국어 요약, 보고서 링크(report_url)를 준다. 인과 효과가 아니다."
    ),
    "spatial_query": (
        "자유로운 공간 질문용 범용 질의. 매장을 조건으로 거르고, 관계로 찾고, 묶어서 집계하고, 정렬한다. "
        "합계·평균·비율은 직접 계산하지 말고 metrics로 받는다. "
        "필드: unit_code, floor, zone_code, zone_name, brand_id, brand_name, category(코드·한글명·'F&B'), "
        "category_name, vacant, anchor, area_m2, frontage_m(통로 접면), walk_to_escalator_m, walk_to_entrance_m, "
        "side(도면 기준 방위 북서·북·북동·서·중앙·동·남서·남·남동. '동'은 북동·남동 포함), frontage_sides, corner(직각인 두 면이 "
        "통로에 접함), escalator_front(에스컬레이터 정면), escalator_view_m, front_escalator, entrance_front(출입구 정면), "
        "entrance_view_m, traffic_potential(0~1, 수직동선·출입구 사이 최단 경로가 매장 앞을 지나는 정도, 관측 유동 아님), "
        "main_walkway(메인 동선). 실제 개정의 연환산 실적: sales_krw, receipts, aov_krw, sales_per_m2, sales_per_pyeong, gmrof, "
        "commission_rate, commission_revenue, percentile, floor_percentile, low_efficiency. 가상 개정은 expected_sales_krw, "
        "expected_sales_per_m2(추정). "
        'filters: {필드: 값 | [값...] | {"gte": 10, "lt": 30, "ne": 값, "in": [...], "contains": "글자"}}. '
        '관계 조건: {"adjacent_to": "화장품" | "3F-C05" | {"brand": "이름"}}는 벽을 맞댄 매장, '
        '{"near": {"to": "ESC-A" | "3F-C05" | "ENT-1" | "화장실", "within_m": 30, "floor": "3F"}}는 걸어서 가까운 매장(distance_m 추가). '
        "group_by: 필드나 목록. metrics: count, sum:필드, mean:필드, median:필드, min:필드, max:필드, "
        "rate:sales_krw/area_m2(합계 비율=면적 가중 평균). sort_by: 필드나 집계 이름, '-'를 앞에 붙이면 내림차순. "
        'fields: 행 결과에 보일 필드. 예) 층별 공실 면적: filters {"vacant": true}, group_by "floor", '
        'metrics ["count", "sum:area_m2"].'
    ),
    "spatial_route": (
        "두 지점 사이의 통로망 최단 경로와 걷는 거리(walk_m, 층별 legs)를 반환한다. origin·destination은 매장 코드(3F-C05), "
        "브랜드명, 출입구(ENT-1, 정문), 층을 붙인 수직동선·편의시설(1F:ESC-A, 8F:화장실)이다. destination 대신 "
        "destination_category와 destination_floor를 주면 그 층에서 가장 가까운 해당 카테고리 매장으로 가고 다른 후보도 준다. "
        "via는 escalator(기본), elevator, stairs. 걷는 거리에는 수직 이동이 들어가지 않는다. 도면에 그리려면 ui_show_route를 쓴다."
    ),
    "spatial_describe_floor": (
        "층의 지도 카드: 크기, 수직동선·출입구·편의시설의 도면 기준 방위, 구역과 방위, 앵커 매장, 에스컬레이터 정면 매장, "
        "메인 동선 매장, 공실, 코너 매장 수와 한국어 요약. 층의 생김새, '어디쯤', '정면', '안쪽' 같은 질문에 먼저 쓴다."
    ),
    "analytics_flow_map": (
        "고객군의 통로별 추정 유동. 같은 날 연속한 두 결제 매장 사이를 통로망 최단 경로로 잇고 표본 가중치를 곱해 모은다. "
        "segment('age_band:40s;tier:VIP' 또는 {age_band, gender, tier}), day_type(all·weekday·weekend), hours('10-14'), floor, "
        "start·end로 거른다. 많이 지난 통로(top_corridors: value 기간 합계, per_day 하루 평균, share_of_floor 층 이동 중 비율)와 "
        "앞을 많이 지나는 매장(top_units), 층별 이동 수를 반환한다. 결제 순서를 이은 추정이며 관측한 유동이 아니다. "
        "도면에 그리려면 ui_show_flow(mode=traffic)를 쓴다."
    ),
    "spatial_popup_candidates": (
        "팝업 매장·행사 매대 후보 위치. 같은 조건의 추정 유동(기본 주말)이 많고 에스컬레이터에서 잘 보이는 통로 위 공간"
        "(부스 min_area_m2가 들어가고 양옆 통로 폭이 남는 곳)과 공실·팝업 매장을 점수 순으로 반환한다. category를 주면 그 상품과 "
        "함께 잘 팔리는 카테고리 매장이 가까운 곳에 가점을 준다. 후보마다 층, 위치 설명, 하루 추정 통행, 보이는 거리, 이유가 있다. "
        "도면에 그리려면 ui_show_flow(mode=popup)를 쓴다."
    ),
    "spatial_egress_check": (
        "도면으로 계산한 통로 폭·피난 거리 점검. 기준 폭(min_width_m, 기본 2.4m)보다 좁은 통로 구간과, 매장 출입구에서 계단·"
        "출입구까지 걷는 거리가 기준(max_travel_m, 기본 50m)을 넘는 매장, 통로로 이어지지 않는 매장을 층별로 반환한다. "
        "기준은 검토용 예시이며 법규 판정이 아니다. 도면에 그리려면 ui_show_flow(mode=egress)를 쓴다."
    ),
    "relayout_list_briefs": (
        "사람이 올린 시즌 개편 브리프(시즌, 적용일, 기준 개정, 대상 층, 목적, 입점·퇴점 수)와 각 개편 프로젝트의 진행 상태"
        "(draft·diagnosed·planned·selected·checked), 선택된 대안을 반환한다. 개편·MD 개편·시즌 개편 질문의 시작점이다."
    ),
    "relayout_diagnose": (
        "브리프 기준 진단: 카테고리 시즌 지수(1보다 크면 그 시즌에 강함), 대상 층의 ㎡당 매출과 1년 전 같은 시즌 대비 증감, "
        "계약 만료·저효율 후보 매장, 공실, 입점 브랜드별로 들어갈 수 있는 매장 수, 퇴점 브랜드 실적을 반환하고 프로젝트에 저장한다."
    ),
    "relayout_plan": (
        "브리프로 개편 대안을 만든다. 퇴점을 빼고 입점 브랜드를 빈 매장에 넣은 기준안(ALT-0)과, 계약 만료 브랜드(movable=all이면 전체)를 "
        "옮겨 시즌 가중 목표(objective=commission이면 수수료 수익, sales면 매출)에서 이전 공사 손실(downtime_weeks)을 뺀 순효과를 "
        "높이는 대안(ALT-1~)을 반환한다. 대안마다 변경 목록(입점·퇴점·이전), 기준안 대비 순효과·목표 변화·이전 비용·매출 변화, 지금 "
        "대비 매출 변화, 제약 충족표가 있고 가상 개정(rev_id)으로 저장된다. extra_exits·extra_entries로 퇴점·입점을 더 가정할 수 있다. "
        "모든 금액은 연 단위 추정치다."
    ),
    "relayout_select": (
        "사람이 고른 개편 대안(alt_id)과 결정자(decided_by)를 기록한다. 사용자가 이 대안으로 하겠다고 분명히 말했을 때만 쓴다."
    ),
    "relayout_check_drawing": (
        "CAD팀이 그려 올린 실제 개정(rev)을 선택된 개편 대안과 대조한다. 계획한 입점·퇴점·이전마다 일치, 다른 매장, 도면에 없음, "
        "퇴점 안 됨을 판정하고 일치율, 계획에 없는 변경, 3% 넘는 면적 변화를 반환한다."
    ),
    "relayout_report": (
        "개편 제안서(HTML: 개편 조건, 진단, 대안 비교, 선택안 상세와 변경 도면, 공사 순서, 도면 대조, 가정과 한계)를 만들고 링크를 반환한다."
    ),
    "relayout_entry_options": (
        "신규 브랜드를 어디에 넣으면 좋은지 추천한다. brand(카탈로그 브랜드 ID·이름) 또는 category(그 카테고리의 평균 브랜드)를 주면 "
        "면적·선호 층에 맞는 공실과 6개월 안에 계약이 끝나는 매장을 기대 매출(추정, 범위 포함) 순으로 반환한다. 각 후보에 현재 브랜드, "
        "계약 만료일, 에스컬레이터 정면·코너·메인 동선 여부가 붙는다. season(SS·FW)을 주면 시즌 지수를 곱한다."
    ),
    "relayout_unit_options": (
        "매장 하나(공실이나 곧 나갈 매장, unit_code)에 무엇을 넣으면 좋은지 추천한다. 면적·층·옆 매장과 맞는 카테고리를 그 카테고리 "
        "평균 브랜드의 기대 매출(objective=commission이면 수수료 수익) 순으로 반환하고, 카테고리마다 아직 입점하지 않은 카탈로그 "
        "브랜드 상위 3개, 지금 브랜드 대비 비율, 제외한 카테고리와 이유를 준다. season(SS·FW)을 주면 시즌 지수를 곱한다. "
        "'○○가 나가면 그 자리에 뭘 넣을까', '공실 5F-W03 채우기' 질문에 쓴다(추정치)."
    ),
    "relayout_phasing": (
        "개편 대안의 공사 순서를 짠다. 공사는 빈 매장에서만 시작하고, 옮기는 브랜드는 새 매장 공사가 끝날 때까지 기존 매장에서 "
        "영업한다고 본다. 서로 자리를 바꾸는 순환은 한 브랜드를 임시 매장(계획에 안 쓰는 공실)으로 옮기거나 휴점해서 푼다. "
        "crews(동시 공사 수, 기본 2), fitout_weeks(매장당 공사 주, 기본 브리프 값), temporary_sales_ratio(임시 매장 매출 비율, 기본 0.6)를 "
        "받아 주차별 단계(waits_for: 먼저 끝나야 하는 단계), 총 기간, 휴점 손실(lost_krw), 모두 휴점 가정 손실(naive_lost_krw), "
        "입점 지연 기회 손실(entry_delay_krw), 입점 개장 주차를 반환하고 프로젝트에 저장한다. alt_id를 생략하면 선택한 대안을 쓴다."
    ),
    "upload_list_jobs": (
        "사람이 올린 도면의 처리 작업을 확인한다. job_id가 없으면 최근 작업 목록(개정, 상태, 올린 층, 단계 요약)을, 있으면 그 작업의 "
        "단계별 상태, 파싱 이슈 수, 레이어 검토 큐, 변경 요약, 계약 정보 대조, 개편안 대조, 오류를 반환한다. 파일은 올릴 수 없다."
    ),
    "revision_check_metadata": (
        "실제 개정의 도면을 적용일에 유효한 계약 정보(leases)와 대조한다. 계약 매장이 도면에 없음, 도면 브랜드와 계약 브랜드 불일치, "
        "계약 면적과 도면 면적의 3% 넘는 차이, 계약 없는 매장을 유형별 건수와 함께 반환한다. floors로 층을 좁힐 수 있다."
    ),
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
