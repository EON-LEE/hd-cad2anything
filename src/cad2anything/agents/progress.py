"""Korean narration of an analysis for the chat's "분석 과정" panel.

For every tool call the chat shows how the step computes its answer (``tool_method``) and, once it is done,
what it found (``tool_result``). Both are deterministic: methods describe the code behind each tool, and
results are read from the tool output. The agent's own plan sentences come from the runtime as notes.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Any

from cad2anything.agents.labels import SPECIALIST_LABELS, floor_ko

TOOL_METHODS: dict[str, str] = {
    "spatial_list_revisions": "도면 개정 목록과 적용일을 확인해요",
    "spatial_floor_summary": "층의 매장 수·면적·카테고리 구성을 집계해요",
    "spatial_find_units": "조건에 맞는 매장을 도면 속성으로 찾아요",
    "spatial_unit_details": "인접 매장·통로 접면·에스컬레이터까지 보행거리를 도면에서 읽어요",
    "spatial_nearby_units": "매장 경계에서 반경 안의 매장을 거리순으로 찾아요",
    "spatial_query": "도면에서 계산한 매장 속성으로 거르고 묶어 집계해요",
    "spatial_route": "1m 간격 통로망에서 최단 경로를 찾아 걷는 거리를 재요",
    "spatial_describe_floor": "수직동선·구역·주요 매장의 위치를 도면 기준 방위로 정리해요",
    "spatial_popup_candidates": "추정 유동·승강장 가시성·남는 통로 폭으로 팝업 자리에 점수를 매겨요",
    "spatial_egress_check": "통로 폭과 계단·출입구까지 걷는 거리를 도면으로 재요",
    "revision_diff": "두 개정을 매장 단위로 비교해 이동·교체·면적 변경을 찾아요",
    "revision_process": "올린 도면을 읽어 부모 개정과 비교하고 산출물을 다시 만들어요",
    "revision_create_hypothetical": "브랜드 이동안으로 가상 도면을 만들어요",
    "registry_list_generators": "등록된 산출물 생성기를 확인해요",
    "registry_impact": "바뀐 도면 때문에 다시 만들 산출물과 승인 방식을 찾아요",
    "registry_run": "생성기로 산출물 초안을 만들어요",
    "registry_validate": "산출물 검증 결과를 확인해요",
    "render_adhoc_table": "도면 데이터로 즉석 표 초안을 만들어요",
    "analytics_productivity": "결제 실적을 연 매출로 환산해 ㎡당 매출을 구하고 층 하위 10%를 저효율로 봐요",
    "analytics_drivers": "㎡당 매출을 노출·통로 접면·면적·카테고리로 회귀 분해해요",
    "analytics_cross_shopping": "같은 고객이 같은 날 함께 산 비율이 평소의 몇 배인지 계산해요",
    "analytics_floor_transitions": "같은 날 결제 순서로 층 사이 이동 비율을 계산해요",
    "analytics_adjacency_effect": "근처 매장 효과를 에스컬레이터 노출을 통제한 비교로 다시 봐요",
    "analytics_before_after": "변경일 전후 같은 기간 매출을 같은 층 비교군과 비교해요",
    "analytics_time_patterns": "시간대·요일별 매출과 비중을 집계해요",
    "analytics_customer_journey": "가명 고객 한 명의 하루 결제 순서를 찾아요",
    "analytics_flow_map": "같은 날 이어진 결제를 통로망 최단 경로로 이어 통로별로 모아요",
    "analytics_store_compare": "우리 점포 실적과 다른 점포 실적 자료를 같은 기준으로 비교해요",
    "analytics_relayout_review": "바뀐 매장의 전후 매출을 같은 층의 바뀌지 않은 매장과 비교해요",
    "layout_generate_alternatives": "제약을 지키며 기대 매출이 가장 커지는 배치를 정수계획으로 찾아요",
    "whatif_score_moves": "이동안의 기대 매출 변화를 점수 모델로 추정해요",
    "relayout_list_briefs": "사람이 올린 개편 브리프와 진행 상태를 확인해요",
    "relayout_diagnose": "시즌 지수·계약 만료·저효율로 개편 후보를 진단해요",
    "relayout_plan": "기대 매출에서 이전 공사 손실을 뺀 순효과가 가장 큰 배치를 정수계획으로 찾아요",
    "relayout_select": "사람이 고른 대안과 결정자를 기록해요",
    "relayout_check_drawing": "선택한 대안과 올린 도면을 매장별로 대조해요",
    "relayout_report": "진단·대안·공사 순서를 제안서로 묶어요",
    "relayout_entry_options": "빈 매장마다 신규 브랜드의 기대 매출을 추정해 비교해요",
    "relayout_unit_options": "이 자리에 카테고리별 평균 브랜드를 넣었을 때 기대 매출을 비교해요",
    "relayout_phasing": "빈 매장부터 공사하고 공사팀 수에 맞춰 순서와 영업 손실을 계산해요",
    "upload_list_jobs": "올린 도면의 처리 단계와 결과를 확인해요",
    "revision_check_metadata": "도면과 계약 정보를 매장별로 대조해요",
    "ui_set_floor": "도면을 해당 층으로 옮겨요",
    "ui_show_heatmap": "지표 값으로 매장을 색칠해요",
    "ui_highlight_units": "근거가 되는 매장을 강조해요",
    "ui_draw_edges": "매장·카테고리 사이 관계를 선으로 그려요",
    "ui_focus": "해당 매장으로 화면을 확대해요",
    "ui_compare": "두 개정의 차이를 도면에 표시해요",
    "ui_show_panel": "결과를 패널에 열어요",
    "ui_show_journey": "결제 순서를 통로망 최단 경로로 이어 도면에 그려요",
    "ui_show_route": "최단 경로를 도면에 그려요",
    "ui_show_flow": "통로 위에 집계 결과를 그려요",
    "ui_clear": "화면 표시를 지워요",
    "consult_specialist": "전문 에이전트에게 여러 단계의 분석을 맡겨요",
    "web_search": "공개 웹 자료를 검색해요",
    "code_interpreter": "계산 코드를 실행해 숫자를 확인해요",
}
SPECIALIST_METHODS: dict[str, str] = {
    "cad_change_interpreter": "도면 변경 해석 에이전트가 바뀐 점과 다시 만들 산출물을 정리해요",
    "space_productivity_analyst": "공간 생산성 분석 에이전트가 여러 지표로 원인을 깊게 분석해요",
    "layout_planner": "레이아웃 설계 에이전트가 배치 대안을 만들고 비교해요",
    "generator": "산출물 생성 에이전트가 표·문서 초안을 만들어요",
    "qa_reviewer": "검증 에이전트가 결과의 오류와 누락을 점검해요",
    "researcher": "시장 조사 에이전트가 공개 자료를 찾아 출처와 함께 요약해요",
}
METRIC_NAMES = {
    "sales_per_m2": "㎡당 매출",
    "sales_krw": "매출",
    "area_m2": "면적",
    "expected_sales_krw": "기대 매출",
    "aov_krw": "객단가",
    "walk_to_escalator_m": "에스컬레이터까지 거리",
}
MAX_RESULT_CHARS = 90


def tool_method(name: str | None, args: dict[str, Any] | None = None) -> str | None:
    """How a tool call computes its answer (one short Korean sentence), or None for unknown tools."""
    name = name or ""
    if name == "consult_specialist" and isinstance(args, dict):
        method = SPECIALIST_METHODS.get(str(args.get("agent")))
        if method:
            return method
    return TOOL_METHODS.get(name)


# --- results ------------------------------------------------------------------------------------------


def _krw(value: Any) -> str | None:
    """ "6,648만 원", "1.5억 원" (None for missing values)."""
    number = _num(value)
    if number is None:
        return None
    sign = "-" if number < 0 else ""
    amount = abs(number)
    if amount >= 1e8:
        return f"{sign}{amount / 1e8:,.1f}억 원"
    if amount >= 1e4:
        return f"{sign}{amount / 1e4:,.0f}만 원"
    return f"{sign}{amount:,.0f}원"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value: Any, signed: bool = True) -> str | None:
    number = _num(value)
    if number is None:
        return None
    return f"{number:+.1f}%" if signed else f"{number:.1f}%"


def _count(value: Any) -> int | None:
    if isinstance(value, list | dict):
        return len(value)
    number = _num(value)
    return int(number) if number is not None else None


def _first(value: Any) -> dict[str, Any]:
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}


def _sentence(text: Any) -> str | None:
    """The first sentence of a Korean summary (up to the first period followed by a space), shortened."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text.split())
    match = re.search(r"[.!?]\s", text)
    first = text[: match.start()] if match else text.rstrip(".")
    return first if len(first) <= MAX_RESULT_CHARS else first[: MAX_RESULT_CHARS - 1].rstrip() + "…"


def _join(*parts: str | None) -> str | None:
    kept = [p for p in parts if p]
    return " · ".join(kept) if kept else None


def _productivity(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    rows = _count(summary.get("rows", data.get("rows")))
    low = _count(summary.get("low_efficiency", data.get("low_efficiency")))
    if data.get("level", "unit") != "unit":
        return f"{rows}개 묶음의 ㎡당 매출 계산" if rows else None
    lowest = min(
        (r for r in data.get("low_efficiency") or [] if isinstance(r, dict) and _num(r.get("sales_per_m2"))),
        key=lambda r: float(r["sales_per_m2"]),
        default=None,
    )
    first = (
        f"가장 낮은 곳 {lowest.get('brand_name') or lowest.get('unit_code')}({lowest.get('unit_code')})"
        if lowest
        else None
    )
    reasons = summary.get("low_efficiency_by_reason")
    structural = _count(reasons.get("카테고리 특성")) if isinstance(reasons, dict) else None
    weak = _count(summary.get("category_weak"))
    count = f"매장 {rows}곳 중 저효율 {low}곳" if rows is not None and low is not None else None
    if count and structural:
        count += f"(카테고리 특성 {structural}곳)"
    return _join(count, f"같은 업종보다 부진 {weak}곳" if weak is not None else None, first)


def _drivers(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    r2 = _num(data.get("r2"))
    counts = data.get("driver_counts") if isinstance(data.get("driver_counts"), dict) else {}
    main = max(counts.items(), key=lambda kv: kv[1])[0] if counts else None
    units = _count(data.get("low_efficiency_units"))
    reason = f"저효율 {units}곳의 주요 요인: {main}" if main and units else None
    return _join(f"설명력 R² {r2:.2f}" if r2 is not None else None, reason)


def _cross(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    top = _first(data.get("partners") or data.get("pairs"))
    lift = _num(top.get("lift"))
    partner = top.get("partner_name") or top.get("partner")
    if not partner or lift is None:
        return None
    focal = data.get("focal_name") or data.get("focal")
    head = f"{focal}과 함께 사는 1위" if focal else "함께 사는 1위"
    return f"{head}: {partner}(평소의 {lift:.1f}배)"


def _transitions(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    fountain = data.get("fountain") if isinstance(data.get("fountain"), dict) else {}
    shower = data.get("shower") if isinstance(data.get("shower"), dict) else {}
    f, s = _num(fountain.get("share")), _num(shower.get("share"))
    return _join(
        f"분수효과 {f:.0%}" if f is not None else None, f"샤워효과 {s:.0%}" if s is not None else None
    )


def _adjacency(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    naive, controlled = _pct(data.get("naive_uplift_pct")), _pct(data.get("controlled_uplift_pct"))
    if not (naive and controlled):
        return None
    suspect = "공통 원인(노출) 의심" if data.get("confounding_suspected") else None
    return _join(f"단순 비교 {naive} → 노출 통제 후 {controlled}", suspect)


def _before_after(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    treated, control = _pct(data.get("treated_change_pct")), _pct(data.get("control_change_pct"))
    if not treated:
        return None
    return _join(f"대상 매장 {treated}", f"같은 층 비교군 {control}" if control else None)


def _time(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    rows = [r for r in data.get("rows") or [] if isinstance(r, dict)]
    by = (data.get("by") or ["hour"])[0] if isinstance(data.get("by"), list) else "hour"
    key = "share_in_group" if any("share_in_group" in r for r in rows) else "share"
    top = max(rows, key=lambda r: _num(r.get(key)) or 0.0, default=None)
    if not top or by not in top:
        return None
    unit = {"hour": "시", "weekday": "", "month": "월"}.get(by, "")
    share = _num(top.get(key))
    return f"비중이 가장 큰 때: {top[by]}{unit}" + (f"({share:.0%})" if share is not None else "")


def _journey(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    stops = _count(summary.get("stops", data.get("stops")))
    floors = summary.get("floors") or []
    where = "→".join(floor_ko(f) for f in floors[:4]) if floors else None
    return _join(f"결제 {stops}건" if stops else None, where)


def _query(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    matched = _count(data.get("matched", totals.get("count")))
    extra = None
    for key, value in totals.items():
        if key == "count" or _num(value) is None:
            continue
        op, _, field = key.partition("_")
        name = METRIC_NAMES.get(field) or METRIC_NAMES.get(key)
        if not name:
            continue
        label = {"mean": "평균", "sum": "합계", "median": "중앙값", "min": "최소", "max": "최대"}.get(op, "")
        if "sales" in field or "krw" in field:
            text = _krw(value)
        elif "area" in field:
            text = f"{float(value):,.0f}㎡"
        elif field.endswith("_m"):
            text = f"{float(value):,.0f}m"
        else:
            text = f"{float(value):,.1f}"
        extra = f"{label} {name} {text}".strip()
        break
    groups = _count(data.get("groups"))
    return _join(
        f"조건에 맞는 매장 {matched}곳" if matched is not None else None,
        f"{groups}개 묶음" if groups else None,
        extra,
    )


def _route(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    walk = _num(data.get("walk_m"))
    if walk is None:
        return None
    hop = _first(data.get("transitions"))
    floors = _count(hop.get("floors"))
    return _join(f"걷는 거리 약 {walk:,.0f}m", f"{floors}개 층 이동" if floors else None)


def _optimal(data: dict[str, Any]) -> str | None:
    search = data.get("search")
    return "ALT-1 최적해 확인" if isinstance(search, dict) and search.get("status") == "optimal" else None


def _plan(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    alternatives = [a for a in data.get("alternatives") or [] if isinstance(a, dict)]
    scored = [a for a in alternatives if _num((a.get("kpi") or {}).get("net_krw")) is not None]
    if not scored:
        return f"대안 {len(alternatives)}개" if alternatives else None
    best = max(scored, key=lambda a: float(a["kpi"]["net_krw"]))
    return _join(
        f"대안 {len(alternatives)}개 · 순효과 최고 {best.get('alt_id')}(연 {_krw(best['kpi']['net_krw'])})",
        _optimal(data),
    )


def _layout(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    alternatives = [a for a in data.get("alternatives") or [] if isinstance(a, dict)]
    return _join(f"대안 {len(alternatives)}개", _optimal(data)) if alternatives else None


def _phasing(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    weeks = _num(data.get("total_weeks"))
    return _join(
        f"총 {weeks:g}주" if weeks is not None else None,
        f"예상 영업 손실 {_krw(data.get('lost_krw'))}" if _num(data.get("lost_krw")) is not None else None,
    )


def _diagnose(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    candidates, vacancies = _count(data.get("candidates")), _count(data.get("vacancies"))
    return _join(
        f"개편 후보 {candidates}곳" if candidates is not None else None,
        f"공실 {vacancies}곳" if vacancies else None,
    )


def _entry(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    top = _first(data.get("options"))
    if not top:
        return None
    where = top.get("unit_code") or top.get("category_name")
    value = _krw(top.get("expected_sales_krw"))
    total = _count(data.get("total_candidates"))
    return _join(f"후보 {total}곳" if total else None, f"1순위 {where}" + (f"(연 {value})" if value else ""))


def _unit_options(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    top = _first(data.get("options"))
    if not top:
        return None
    value = _krw(top.get("expected_sales_krw"))
    return f"1순위 {top.get('category_name')}" + (f"(연 {value})" if value else "")


def _check_drawing(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    pct = _pct(data.get("conformance_pct"), signed=False)
    return f"계획 대비 일치율 {pct}" if pct else None


def _flow_map(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    top = _first(data.get("top_corridors"))
    per_day = _num(top.get("per_day"))
    if not top.get("label"):
        return None
    return f"가장 많이 지나는 곳: {top['label']}" + (f"(하루 약 {per_day:,.0f}명)" if per_day else "")


def _popup(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    candidates = [c for c in data.get("candidates") or [] if isinstance(c, dict)]
    if not candidates:
        return None
    top = candidates[0]
    where = top.get("near") or top.get("unit_code")
    per_day = _num(top.get("passers_per_day"))
    return f"후보 {len(candidates)}곳 · 1순위 {where}" + (f"(하루 약 {per_day:,.0f}명)" if per_day else "")


def _egress(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    narrow, over = _count(totals.get("narrow_stretches")), _count(totals.get("units_over_limit"))
    if narrow is None and over is None:
        return None
    return f"좁은 통로 {narrow or 0}곳 · 피난 거리 초과 매장 {over or 0}곳"


def _review(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    total = data.get("total") if isinstance(data.get("total"), dict) else {}
    change, versus = _pct(total.get("change_pct")), _pct(total.get("vs_control_pct"))
    if not change:
        return None
    return _join(f"바뀐 매장 {change}", f"비교군 대비 {versus}" if versus else None)


def _metadata(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    if not counts:
        return _sentence(data.get("summary"))
    names = {"area_mismatch": "면적 불일치", "brand_mismatch": "브랜드 불일치", "missing_unit": "도면에 없음"}
    return " · ".join(f"{names.get(k, k)} {v}건" for k, v in counts.items())


def _diff(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    total = _count(data.get("total"))
    return f"변경 {total}건" if total is not None else None


def _impact(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    plan = _count(data.get("plan"))
    return f"다시 만들 산출물 {plan}개" if plan is not None else None


def _floor_summary(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    units, vacant = _count(data.get("units")), _count(data.get("vacant_units"))
    return _join(
        f"매장 {units}개" if units is not None else None, f"공실 {vacant}개" if vacant is not None else None
    )


def _find(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    count = _count(data.get("count", data.get("units")))
    return f"매장 {count}곳" if count is not None else None


def _unit_details(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    walk = _num(data.get("walk_to_escalator_m"))
    name = data.get("brand_name") or data.get("unit_code")
    return _join(name, f"에스컬레이터까지 {walk:.0f}m" if walk is not None else None)


def _nearby(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    units, radius = _count(data.get("units")), _num(data.get("radius_m"))
    if units is None:
        return None
    return f"반경 {radius:g}m 안 매장 {units}곳" if radius is not None else f"주변 매장 {units}곳"


def _briefs(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    briefs = [b for b in data.get("briefs") or [] if isinstance(b, dict)]
    if not briefs:
        return "등록된 브리프 없음"
    return ", ".join(str(b.get("brief_id")) for b in briefs[:3]) + f" · {len(briefs)}개"


def _jobs(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    if data.get("status_ko") or data.get("status"):
        return f"작업 상태: {data.get('status_ko') or data.get('status')}"
    jobs = [j for j in data.get("jobs") or [] if isinstance(j, dict)]
    if not jobs:
        return "올린 작업 없음"
    return f"작업 {len(jobs)}개 · 최근 {jobs[0].get('rev_id')} {jobs[0].get('status_ko') or jobs[0].get('status')}"


def _ui_flow(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    mode = data.get("mode")
    if mode == "egress":
        narrow, over = _count(summary.get("narrow_stretches")), _count(summary.get("units_over_limit"))
        return f"좁은 통로 {narrow or 0}곳 · 피난 거리 초과 매장 {over or 0}곳"
    if mode == "popup":
        markers = [m for m in data.get("markers") or [] if isinstance(m, dict)]
        if not markers:
            return None
        top = markers[0]
        where = re.sub(r"^후보 \d+ · ", "", str(top.get("title") or top.get("unit_code") or ""))
        per_day = _num(top.get("per_day"))
        return f"후보 {len(markers)}곳 · 1순위 {where}" + (f"(하루 약 {per_day:,.0f}명)" if per_day else "")
    return _flow_map({"top_corridors": summary.get("top_corridors")}, args)


def _ui_panel(data: dict[str, Any], args: dict[str, Any]) -> str | None:
    rows = _count(data.get("rows"))
    return f"표 {rows}행" if data.get("panel") == "table" and rows else None


RESULTS: dict[str, Callable[[dict[str, Any], dict[str, Any]], str | None]] = {
    "analytics_productivity": _productivity,
    "analytics_drivers": _drivers,
    "analytics_cross_shopping": _cross,
    "analytics_floor_transitions": _transitions,
    "analytics_adjacency_effect": _adjacency,
    "analytics_before_after": _before_after,
    "analytics_time_patterns": _time,
    "analytics_customer_journey": _journey,
    "analytics_flow_map": _flow_map,
    "analytics_relayout_review": _review,
    "spatial_query": _query,
    "spatial_route": _route,
    "spatial_floor_summary": _floor_summary,
    "spatial_find_units": _find,
    "spatial_unit_details": _unit_details,
    "spatial_nearby_units": _nearby,
    "spatial_popup_candidates": _popup,
    "spatial_egress_check": _egress,
    "revision_diff": _diff,
    "registry_impact": _impact,
    "revision_check_metadata": _metadata,
    "layout_generate_alternatives": _layout,
    "relayout_list_briefs": _briefs,
    "relayout_diagnose": _diagnose,
    "relayout_plan": _plan,
    "relayout_entry_options": _entry,
    "relayout_unit_options": _unit_options,
    "relayout_check_drawing": _check_drawing,
    "relayout_phasing": _phasing,
    "upload_list_jobs": _jobs,
    "ui_show_route": _route,
    "ui_show_journey": _journey,
    "ui_show_flow": _ui_flow,
    "ui_show_panel": _ui_panel,
}
# UI actions whose label already says everything ("4층으로 이동", "매장 5곳 강조").
SILENT = {
    "ui_set_floor",
    "ui_show_heatmap",
    "ui_highlight_units",
    "ui_draw_edges",
    "ui_focus",
    "ui_compare",
    "ui_clear",
}


def tool_result(name: str | None, args: dict[str, Any] | None, output: Any) -> str | None:
    """What a finished tool call found, in one short Korean line (None when there is nothing to say).

    ``output`` is the tool's JSON text (or an already parsed dict). Failed calls (``ok: false``) say that
    the agent retries; other outputs use a per-tool reading, then the first sentence of a Korean ``summary``.
    """
    name = name or ""
    args = args if isinstance(args, dict) else {}
    data = output
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            data = None
    if not isinstance(data, dict):
        return None
    if data.get("ok") is False:
        return "조건을 고쳐 다시 시도해요"
    if name in SILENT:
        return None
    reader = RESULTS.get(name)
    try:
        text = reader(data, args) if reader else None
    except (TypeError, ValueError, KeyError, AttributeError):  # an unexpected output shape is not fatal
        text = None
    if not text:
        text = _sentence(data.get("summary"))
    if not text:
        return None
    return text if len(text) <= MAX_RESULT_CHARS else text[: MAX_RESULT_CHARS - 1].rstrip() + "…"


def specialist_result(agent_key: str, ok: bool) -> str:
    who = SPECIALIST_LABELS.get(agent_key, "전문 에이전트")
    return f"{who}의 분석을 받았어요" if ok else f"{who}의 답을 받지 못해 직접 정리해요"
