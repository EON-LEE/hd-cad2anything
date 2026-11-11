"""Korean narration of analysis steps (method and result lines in the chat's analysis panel)."""

import json
import re
from pathlib import Path

import pytest

from cad2anything.agents import SPECIALISTS
from cad2anything.agents.deploy import load_ui_tools
from cad2anything.agents.labels import SPECIALIST_LABELS
from cad2anything.agents.progress import (
    SPECIALIST_METHODS,
    TOOL_METHODS,
    parse_tool_output,
    specialist_result,
    tool_method,
    tool_result,
)
from cad2anything.agents.runtime import _output_ok
from cad2anything.services.mcp_server import TOOL_DESCRIPTIONS

MOCK_PROGRESS = Path(__file__).resolve().parents[1] / "apps" / "atlas-web" / "src" / "mock" / "progress.ts"


def _ts_record(source: str, name: str) -> dict[str, str]:
    body = source.split(f"export const {name}")[1].split("}")[0]
    return dict(re.findall(r"^\s*([a-z_]+): '([^']*)',?$", body, re.MULTILINE))


def test_mock_progress_text_matches_the_backend():
    """apps/atlas-web/src/mock/progress.ts copies the backend sentences so mock mode reads like the backend."""
    source = MOCK_PROGRESS.read_text(encoding="utf-8")
    assert _ts_record(source, "TOOL_METHODS") == TOOL_METHODS
    assert _ts_record(source, "SPECIALIST_METHODS") == SPECIALIST_METHODS
    assert _ts_record(source, "SPECIALIST_LABELS") == SPECIALIST_LABELS
    assert "`${who}의 분석을 받았어요`" in source and "`${who}의 답을 받지 못해 직접 정리해요`" in source
    assert specialist_result("researcher", True).endswith("의 분석을 받았어요")


def test_every_tool_and_specialist_has_a_method_sentence():
    ui_tools = [tool["name"] for tool in load_ui_tools()]
    for name in [*TOOL_DESCRIPTIONS, *ui_tools, "web_search", "code_interpreter"]:
        method = tool_method(name, {})
        assert method and method.endswith("요"), name
        assert "_" not in method, name  # no tool or field names in user-facing text
    assert set(SPECIALIST_METHODS) == set(SPECIALISTS)
    assert tool_method("consult_specialist", {"agent": "layout_planner"}).startswith("레이아웃 설계 에이전트")
    assert tool_method("unknown_tool") is None


@pytest.mark.parametrize(
    ("name", "output", "expected"),
    [
        (
            "analytics_productivity",
            {
                "level": "unit",
                "summary": {"rows": 50, "low_efficiency": 5},
                "low_efficiency": [
                    {"unit_code": "4F-E05", "brand_name": "Ravandre Nuit", "sales_per_m2": 4.0e6},
                    {"unit_code": "4F-C22", "brand_name": "Nyssio Intimates", "sales_per_m2": 7.0e6},
                ],
            },
            "매장 50곳 중 저효율 5곳 · 가장 낮은 곳 Ravandre Nuit(4F-E05)",
        ),
        (
            "analytics_productivity",
            {
                "level": "unit",
                "summary": {
                    "rows": 50,
                    "low_efficiency": 5,
                    "low_efficiency_by_reason": {"카테고리 특성": 4, "같은 카테고리 대비 부진": 1},
                    "category_weak": 5,
                },
                "low_efficiency": [
                    {"unit_code": "4F-E05", "brand_name": "Ravandre Nuit", "sales_per_m2": 4.0e6}
                ],
            },
            "매장 50곳 중 저효율 5곳(카테고리 특성 4곳) · 같은 업종보다 부진 5곳 · 가장 낮은 곳 Ravandre Nuit(4F-E05)",
        ),
        (
            "analytics_drivers",
            {
                "r2": 0.9239,
                "driver_counts": {"카테고리 특성": 4, "노출": 1},
                "low_efficiency_units": [{}] * 5,
            },
            "설명력 R² 0.92 · 저효율 5곳의 주요 요인: 카테고리 특성",
        ),
        (
            "analytics_cross_shopping",
            {"focal_name": "화장품", "partners": [{"partner_name": "해외패션", "lift": 3.04}]},
            "화장품과 함께 사는 1위: 해외패션(평소의 3.0배)",
        ),
        (
            "analytics_adjacency_effect",
            {"naive_uplift_pct": 18.8, "controlled_uplift_pct": 3.3, "confounding_suspected": True},
            "단순 비교 +18.8% → 노출 통제 후 +3.3% · 공통 원인(노출) 의심",
        ),
        (
            "spatial_query",
            {"matched": 23, "totals": {"count": 23, "mean_sales_per_m2": 25_660_000}},
            "조건에 맞는 매장 23곳 · 평균 ㎡당 매출 2,566만 원",
        ),
        (
            "spatial_route",
            {"walk_m": 55, "transitions": [{"floors": 7, "via": "ESC-A"}]},
            "걷는 거리 약 55m · 7개 층 이동",
        ),
        (
            "relayout_phasing",
            {"total_weeks": 10, "lost_krw": 66_481_534},
            "총 10주 · 예상 영업 손실 6,648만 원",
        ),
        (
            "relayout_plan",
            {
                "alternatives": [
                    {"alt_id": "ALT-0", "kpi": {"net_krw": 0}},
                    {"alt_id": "ALT-2", "kpi": {"net_krw": 7.3e8}},
                ]
            },
            "대안 2개 · 순효과 최고 ALT-2(7.3억 원)",
        ),
        (
            "relayout_plan",
            {
                "alternatives": [
                    {"alt_id": "ALT-0", "kpi": {"net_krw": 0}},
                    {"alt_id": "ALT-1", "kpi": {"net_krw": 7.36e8}},
                ],
                "search": {"status": "optimal"},
            },
            "대안 2개 · 순효과 최고 ALT-1(7.4억 원) · ALT-1: 100원 단위로 반올림한 모델·제약 범위에서 최적",
        ),
        (
            "relayout_plan",
            {
                "alternatives": [
                    {"alt_id": "ALT-0", "kpi": {"net_krw": 0}, "search_status": None},
                    {
                        "alt_id": "ALT-1",
                        "kpi": {"net_krw": 7.36e8},
                        "search_status": "optimal",
                        "objective_rounding_krw": 100,
                    },
                    {"alt_id": "ALT-2", "kpi": {"net_krw": 6.1e8}, "search_status": "optimal_diverse"},
                ],
                "search": {"status": "optimal", "objective_rounding_krw": 100},
            },
            "대안 3개 · 순효과 최고 ALT-1(7.4억 원) · ALT-1: 100원 단위로 반올림한 모델·제약 범위에서 최적",
        ),
        (
            "relayout_plan",
            {
                "alternatives": [
                    {"alt_id": "ALT-1", "kpi": {"net_krw": 7.36e8}, "search_status": "feasible"},
                    {"alt_id": "ALT-2", "kpi": {"net_krw": 6.1e8}, "search_status": "optimal_diverse"},
                ],
                "search": {"status": "optimal"},
            },
            "대안 2개 · 순효과 최고 ALT-1(7.4억 원) · ALT-2: 앞 대안과 다르게 만드는 조건에서 최적",
        ),
        (
            "relayout_plan",
            {
                "reused": True,
                "alternatives": [
                    {"alt_id": "ALT-1", "kpi": {"net_krw": 7.36e8}, "search_status": "heuristic"}
                ],
                "search": {"status": "heuristic"},
            },
            "저장된 대안 재사용 · 대안 1개 · 순효과 최고 ALT-1(7.4억 원)",
        ),
        (
            "relayout_plan",
            {
                "alternatives": [
                    {
                        "alt_id": "ALT-1",
                        "kpi": {"net_krw": 7.2e8, "annual_uplift_krw": 7.4e8, "one_off_loss_krw": 2.0e7},
                    }
                ]
            },
            "대안 1개 · 순효과 최고 ALT-1(연간 증가 7.4억 원, 1회성 손실 2,000만 원)",
        ),
        (
            "layout_generate_alternatives",
            {"alternatives": [{"alt_id": "ALT-1"}, {"alt_id": "ALT-2"}], "search": {"status": "feasible"}},
            "대안 2개",
        ),
        (
            "layout_generate_alternatives",
            {"alternatives": [{"alt_id": "ALT-1"}], "search": {"status": "optimal"}},
            "대안 1개 · ALT-1: 100원 단위로 반올림한 모델·제약 범위에서 최적",
        ),
        (
            "relayout_phasing",
            {"alt_id": "ALT-2", "basis": "best_net_fallback", "total_weeks": 10, "lost_krw": 66_481_534},
            "선택안이 없어 ALT-2 기준 · 총 10주 · 예상 영업 손실 6,648만 원",
        ),
        (
            "relayout_phasing",
            {"alt_id": "ALT-2", "basis": "selected", "total_weeks": 10},
            "총 10주",
        ),
        (
            "spatial_route",
            {"walk_m": 40, "straight": True, "transitions": []},
            "직선 추정 약 40m",
        ),
        (
            "ui_show_route",
            {"walk_m": None, "straight": True, "transitions": [{"floors": 2}]},
            "걷는 거리 알 수 없음 · 2개 층 이동",
        ),
        (
            "data_provenance",
            {
                "ok": True,
                "default_dataset": "synthetic_demo",
                "domains": [
                    {"domain": "transactions", "source": "synthetic_demo"},
                    {"domain": "leases", "source": "uploaded"},
                ],
            },
            "기본 데이터는 가상 합성 데모 · 올린 자료가 있는 영역 1개",
        ),
        (
            "ui_show_heatmap",
            {"ok": True, "rev": "R0", "rev_switched": True, "estimated": True},
            "R0 개정으로 전환 · 실적이 없어 모델 추정치로 색칠",
        ),
        (
            "spatial_egress_check",
            {"totals": {"narrow_stretches": 2, "units_over_limit": 15}},
            "좁은 통로 2곳 · 피난 거리 초과 매장 15곳",
        ),
        (
            "analytics_relayout_review",
            {"total": {"change_pct": -5.1, "vs_control_pct": -19.9}},
            "바뀐 매장 -5.1% · 비교군 대비 -19.9%",
        ),
        (
            "revision_check_metadata",
            {"counts": {"area_mismatch": 3, "brand_mismatch": 1}},
            "면적 불일치 3건 · 브랜드 불일치 1건",
        ),
        (
            "ui_show_flow",
            {
                "mode": "popup",
                "markers": [
                    {"title": "후보 1 · 1F 에스컬레이터 B 옆 통로", "per_day": 1803.2},
                    {"title": "후보 2"},
                ],
            },
            "후보 2곳 · 1순위 1F 에스컬레이터 B 옆 통로(하루 약 1,803회 통행)",
        ),
        (
            "analytics_store_compare",
            {"summary": "2025-10~2026-09 ㎡당 매출 기준으로 9개 층 중 8개가 높아요. 가장 앞선 곳은 2F예요."},
            "2025-10~2026-09 ㎡당 매출 기준으로 9개 층 중 8개가 높아요",
        ),
    ],
)
def test_results_read_the_tool_output(name, output, expected):
    assert tool_result(name, {}, json.dumps(output, ensure_ascii=False)) == expected


def test_results_for_failures_ui_actions_and_odd_outputs():
    assert tool_result("spatial_query", {}, '{"ok": false, "error": "x"}') == "조건을 고쳐 다시 시도해요"
    decision = {"ok": False, "code": "decision_exists", "error": "이미 결정한 대안이 있어요."}
    assert tool_result("relayout_plan", {}, decision) == "기존 결정이 있어 사용자 확인이 필요해요"
    internal = {"ok": False, "code": "internal_error", "error": "도구 내부 오류(KeyError)가 났어요."}
    assert tool_result("spatial_query", {}, internal) == "도구 내부 오류로 이 단계를 하지 못했어요"
    assert tool_result("ui_show_heatmap", {}, {"ok": True, "rev": "R2", "estimated": False}) is None
    assert tool_result("ui_set_floor", {"floor": "4F"}, '{"ok": true, "floor": "4F"}') is None
    assert tool_result("ui_show_panel", {}, {"ok": True, "panel": "table", "rows": 9}) == "표 9행"
    assert tool_result("analytics_productivity", {}, "not json") is None
    assert tool_result("analytics_productivity", {}, {"low_efficiency": "broken"}) is None
    assert tool_result("analytics_productivity", {}, None) is None
    long = tool_result("unknown_tool", {}, {"summary": "가" * 200})
    assert long.endswith("…") and len(long) <= 90
    assert (
        specialist_result("space_productivity_analyst", True) == "공간 생산성 분석 에이전트의 분석을 받았어요"
    )
    assert "받지 못해" in specialist_result("researcher", False)


def test_results_read_hosted_mcp_output_with_a_structured_copy():
    # Foundry appends MCP structured content to the text content as a second JSON document.
    data = {"rev": "R2", "rows": [], "summary": "4층 매장 50곳을 집계했습니다. 다음 문장."}
    joined = json.dumps(data, ensure_ascii=False, indent=2) + "\n" + json.dumps({"structuredResponse": data})
    assert tool_result("unknown_tool", {}, joined) == "4층 매장 50곳을 집계했습니다"
    failed = '{"ok": false, "error": "x"}\n{"structuredResponse": {"ok": false, "error": "x"}}'
    assert tool_result("spatial_query", {}, failed) == "조건을 고쳐 다시 시도해요"
    assert parse_tool_output(json.dumps({"structuredResponse": {"ok": True}})) == {"ok": True}
    assert parse_tool_output("  [1, 2] trailing") == [1, 2]
    assert parse_tool_output("not json") is None
    assert not _output_ok(failed)
    assert _output_ok("plain text output")
