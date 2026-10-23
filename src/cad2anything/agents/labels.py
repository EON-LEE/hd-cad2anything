"""Korean, user-facing labels for tool calls shown in the Atlas chat ("4층 공간 생산성 계산")."""

from __future__ import annotations

import re
from typing import Any

TOOL_LABELS = {
    "spatial_list_revisions": "개정 목록 확인",
    "spatial_floor_summary": "층 정보 확인",
    "spatial_find_units": "매장 찾기",
    "spatial_unit_details": "매장 상세 확인",
    "spatial_nearby_units": "주변 매장 찾기",
    "revision_diff": "도면 변경 내역 확인",
    "revision_process": "도면 분석",
    "revision_create_hypothetical": "가상 배치 만들기",
    "registry_list_generators": "산출물 종류 확인",
    "registry_impact": "다시 만들 산출물 확인",
    "registry_run": "산출물 만들기",
    "registry_validate": "산출물 검증",
    "render_adhoc_table": "표 만들기",
    "analytics_productivity": "공간 생산성 계산",
    "analytics_drivers": "효율 저하 원인 분석",
    "analytics_cross_shopping": "교차구매 분석",
    "analytics_floor_transitions": "층간 이동 흐름 분석",
    "analytics_adjacency_effect": "인접 효과 검증",
    "analytics_before_after": "개편 전후 비교",
    "analytics_time_patterns": "시간대별 매출 분석",
    "analytics_customer_journey": "고객 동선 찾기",
    "spatial_query": "공간 조건 조회",
    "spatial_route": "경로 계산",
    "spatial_describe_floor": "층 구조 확인",
    "layout_generate_alternatives": "재배치 대안 계산",
    "whatif_score_moves": "이동 효과 추정",
    "relayout_list_briefs": "개편 계획 확인",
    "relayout_diagnose": "개편 진단",
    "relayout_plan": "개편 대안 계산",
    "relayout_select": "개편안 결정 기록",
    "relayout_check_drawing": "개편안과 도면 대조",
    "relayout_report": "개편 제안서 만들기",
    "relayout_entry_options": "입점 위치 추천",
    "relayout_unit_options": "빈 자리 채울 브랜드 추천",
    "relayout_phasing": "공사 순서 계산",
    "analytics_flow_map": "통로 유동 추정",
    "spatial_popup_candidates": "팝업 위치 추천",
    "spatial_egress_check": "통로 폭·피난 거리 점검",
    "analytics_store_compare": "다점포 비교",
    "analytics_relayout_review": "지난 개편 성과 추적",
    "upload_list_jobs": "업로드 작업 확인",
    "revision_check_metadata": "도면과 계약 정보 대조",
    "ui_set_floor": "층 이동",
    "ui_show_heatmap": "도면 색칠",
    "ui_highlight_units": "매장 강조",
    "ui_draw_edges": "관계선 표시",
    "ui_focus": "화면 확대",
    "ui_compare": "개정 비교 표시",
    "ui_show_panel": "패널 열기",
    "ui_show_journey": "도면에 고객 동선 그리기",
    "ui_show_route": "도면에 경로 그리기",
    "ui_show_flow": "도면에 유동 표시",
    "ui_clear": "화면 정리",
    "consult_specialist": "전문 에이전트에게 확인",
    "web_search": "웹 검색",
    "code_interpreter": "계산 실행",
}
SPECIALIST_LABELS = {
    "cad_change_interpreter": "도면 변경 해석 에이전트",
    "space_productivity_analyst": "공간 생산성 분석 에이전트",
    "layout_planner": "레이아웃 설계 에이전트",
    "generator": "산출물 생성 에이전트",
    "qa_reviewer": "검증 에이전트",
    "researcher": "시장 조사 에이전트",
}
METRIC_NAMES = {
    "sales_per_m2": "㎡당 매출",
    "sales_krw": "매출",
    "aov_krw": "객단가",
    "gmrof": "GMROF",
    "walk_to_escalator_m": "에스컬레이터 접근성",
    "low_efficiency": "저효율 매장",
}
PANEL_LABELS = {
    "changes": "변경 내역 열기",
    "artifact": "산출물 열기",
    "layout": "재배치 대안 열기",
    "journey": "고객 동선 목록 열기",
    "uploads": "업로드 화면 열기",
    "none": "패널 닫기",
}
AGE_NAMES = {"20s": "20대", "30s": "30대", "40s": "40대", "50s": "50대", "60s+": "60대 이상"}


def floor_ko(code: Any) -> str:
    """ "B1" -> "지하 1층", "3F" -> "3층"; other values unchanged."""
    text = str(code or "").strip().upper()
    if match := re.fullmatch(r"B(\d+)F?", text):
        return f"지하 {match.group(1)}층"
    if match := re.fullmatch(r"(\d+)F", text):
        return f"{match.group(1)}층"
    return str(code or "")


def _segment_ko(segment: Any) -> str:
    if not isinstance(segment, dict):
        return ""
    tier = segment.get("tier")
    tiers = "·".join(tier) if isinstance(tier, list) else (tier or "")
    gender = {"F": "여성", "M": "남성"}.get(str(segment.get("gender")), "")
    parts = [AGE_NAMES.get(str(segment.get("age_band")), ""), gender, str(tiers)]
    return " ".join(part for part in parts if part)


def _with_floor(base: str, args: dict[str, Any]) -> str:
    floor = args.get("floor")
    return f"{floor_ko(floor)} {base}" if floor else base


def tool_label(name: str | None, args: dict[str, Any] | None = None) -> str:
    """Short Korean description of a tool call for the chat's progress line."""
    name = name or ""
    args = args if isinstance(args, dict) else {}
    base = TOOL_LABELS.get(name, "작업 중")
    if name == "spatial_floor_summary" and args.get("floor"):
        return f"{floor_ko(args['floor'])} 정보 확인"
    if name == "spatial_unit_details" and args.get("unit_code"):
        return f"{args['unit_code']} 매장 확인"
    if name == "spatial_nearby_units" and args.get("unit_code"):
        return f"{args['unit_code']} 주변 매장 찾기"
    if name == "spatial_describe_floor" and args.get("floor"):
        return f"{floor_ko(args['floor'])} 구조 확인"
    if name == "spatial_query":
        filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
        return _with_floor(base, filters) if isinstance(filters.get("floor"), str) else base
    if name in ("spatial_route", "ui_show_route") and args.get("origin"):
        target = args.get("destination") or " ".join(
            str(v) for v in (floor_ko(args.get("destination_floor")), args.get("destination_category")) if v
        )
        verb = "경로 계산" if name == "spatial_route" else "경로 그리기"
        return f"{verb}: {args['origin']} → {target}" if target else base
    if name in ("analytics_productivity", "analytics_drivers", "spatial_find_units"):
        return _with_floor(base, args)
    if name in ("analytics_cross_shopping", "analytics_customer_journey", "analytics_floor_transitions"):
        segment = _segment_ko(args.get("segment"))
        return f"{segment} {base}" if segment else base
    if name == "layout_generate_alternatives":
        floors = args.get("floors") or []
        return f"{', '.join(floor_ko(f) for f in floors)} {base}" if floors else base
    if name == "relayout_entry_options" and (args.get("brand") or args.get("category")):
        return f"{args.get('brand') or args.get('category')} {base}"
    if name == "relayout_unit_options" and args.get("unit_code"):
        return f"{args['unit_code']} 자리에 넣을 브랜드 추천"
    if name == "relayout_check_drawing" and args.get("rev"):
        return f"{args['rev']} 도면과 개편안 대조"
    if name.startswith("relayout_") and args.get("brief_id"):
        return f"{args['brief_id']} {base}"
    if name in ("analytics_flow_map", "spatial_popup_candidates", "spatial_egress_check"):
        return _with_floor(base, args)
    if name == "ui_show_flow":
        mode = {"traffic": "유동", "popup": "팝업 후보", "egress": "통로 폭 점검"}.get(str(args.get("mode")))
        label = f"도면에 {mode} 표시" if mode else base
        return f"{floor_ko(args['floor'])} {label}" if args.get("floor") else label
    if name == "analytics_relayout_review" and args.get("rev"):
        return f"{args['rev']} {base}"
    if name == "revision_check_metadata" and args.get("rev"):
        return f"{args['rev']} {base}"
    if name == "revision_diff" and args.get("rev_from") and args.get("rev_to"):
        return f"{args['rev_from']}→{args['rev_to']} {base}"
    if name == "ui_set_floor" and args.get("floor"):
        return f"{floor_ko(args['floor'])}으로 이동"
    if name == "ui_show_heatmap":
        metric = METRIC_NAMES.get(str(args.get("metric")), "지표")
        return (
            f"{floor_ko(args['floor'])} 도면에 '{metric}' 색칠"
            if args.get("floor")
            else f"도면에 '{metric}' 색칠"
        )
    if name == "ui_highlight_units" and isinstance(args.get("unit_codes"), list):
        return f"매장 {len(args['unit_codes'])}곳 강조"
    if name == "ui_draw_edges" and isinstance(args.get("edges"), list):
        return f"관계선 {len(args['edges'])}개 표시"
    if name == "ui_compare" and args.get("rev_from") and args.get("rev_to"):
        return f"{args['rev_from']}→{args['rev_to']} 변경 표시"
    if name == "ui_show_panel":
        panel = str(args.get("type"))
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        if panel == "table":
            title = payload.get("title")
            return f"표 열기: {title}" if title else "표 열기"
        if panel == "layout" and payload.get("brief_id"):
            return "시즌 개편 화면 열기"
        return PANEL_LABELS.get(panel, base)
    if name == "consult_specialist":
        return f"{SPECIALIST_LABELS.get(str(args.get('agent')), '전문 에이전트')}에게 확인"
    if name == "web_search" and args.get("query"):
        return f"웹 검색: {args['query']}"
    return base
