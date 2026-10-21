import json

import pytest

from cad2anything.agents.ui_actions import UiActionHandler
from cad2anything.services import metrics
from cad2anything.services.analytics_tools import MOVE_FIELDS, parse_segment
from cad2anything.services.tools import ToolError, Toolkit


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> Toolkit:
    return Toolkit(populate(tmp_path_factory.mktemp("analytics-ws")), use_postgis=False)


def test_parse_segment_accepts_korean_shorthands():
    parsed = parse_segment("age_band:40대;tier:vip|VVIP;gender:여성")
    assert parsed == {"age_band": "40s", "tier": ["VIP", "VVIP"], "gender": "F"}
    assert parse_segment({"age_band": "60대 이상"}) == {"age_band": "60s+"}
    assert parse_segment(None) is None
    with pytest.raises(ToolError):
        parse_segment({"income": "high"})


def test_revision_periods_follow_effective_dates(toolkit):
    assert toolkit.revision_period("R0") == ("2024-10-01", "2025-02-28", 151)
    assert toolkit.revision_period("R2")[:2] == ("2025-09-01", "2026-09-30")
    with pytest.raises(ToolError, match="가상 개정"):
        toolkit.revision_period("H1")


def test_productivity_flags_the_planted_low_efficiency_band(toolkit):
    result = toolkit.analytics_productivity("R2", floor="4층", top=5)
    assert result["floor"] == "4F" and len(result["rows"]) == 5 and result["truncated"]
    low = {row["unit_code"] for row in result["low_efficiency"]}
    assert any(code.startswith("4F-E0") for code in low)
    assert all(row["driver"] for row in result["low_efficiency"])
    assert len(toolkit.analytics_productivity("R2", level="floor")["rows"]) == 9


def test_drivers_report_effects_per_low_unit(toolkit):
    result = toolkit.analytics_drivers("R2", floor="4F")
    assert 0 < result["r2"] <= 1 and result["label"] == "추정치"
    unit = result["low_efficiency_units"][0]
    assert len(unit["effects_pct"]) == 5 and unit["explanation"]


def test_cross_shopping_recovers_the_planted_segment_lift(toolkit):
    segment = {"age_band": "40s", "tier": ["VIP", "VVIP"], "gender": "F"}
    focal = toolkit.analytics_cross_shopping(
        level="category", focal="화장품", segment=segment, min_customers=3
    )
    lifts = {p["partner"]: p["lift"] for p in focal["partners"]}
    assert focal["focal"] == "COSMETICS" and focal["focal_name"] == "화장품"
    assert max(lifts.get("LUXURY", 0), lifts.get("GLOBAL_FASHION", 0)) > 1.5
    pairs = toolkit.analytics_cross_shopping(level="category", top=3)
    assert len(pairs["pairs"]) == 3 and pairs["caveat"]


def test_floor_transitions_and_time_patterns(toolkit):
    flows = toolkit.analytics_floor_transitions()
    assert flows["floors"][0] == "B1" and 0 < flows["fountain"]["share"] <= 1
    first_row = [value for value in flows["matrix"][0] if value is not None]
    assert abs(sum(first_row) - 1) < 1e-3
    hours = toolkit.analytics_time_patterns(by="hour", category="푸드코트")
    assert {row["hour"] for row in hours["rows"]} <= set(range(24))
    with pytest.raises(ToolError):
        toolkit.analytics_time_patterns(by="season")


def test_adjacency_effect_supports_near_and_wall_neighborhoods(toolkit):
    near = toolkit.analytics_adjacency_effect("R2", "아동")
    assert near["neighborhood"].startswith("경계 사이 8m") and near["n_treated"] > 0
    wall = toolkit.analytics_adjacency_effect("R2", "CAFE", neighborhood="wall")
    assert wall["neighborhood"] == "벽 공유"
    with pytest.raises(ToolError):
        toolkit.analytics_adjacency_effect("R2", "없는 카테고리")


def test_before_after_suggests_the_brand_key_for_new_tenants(toolkit):
    by_unit = toolkit.analytics_before_after("2025-03-01", ["3F-C05"])
    assert "hint" in by_unit and by_unit["n_control"] > 0
    moved = toolkit.ws.model("R1").unit_by_code("3F-C05").attributes["brand_id"]
    by_brand = toolkit.analytics_before_after("2025-03-01", [moved], key="brand_id")
    assert by_brand["treated"][0]["pre_sales"] > 0 and by_brand["treated_change_pct"] is not None


def test_layout_alternatives_and_whatif_estimates(toolkit):
    result = toolkit.layout_generate_alternatives("R2", floors=["4F"], n_alternatives=2, save=False)
    assert result["alternatives"] and all(alt["all_constraints_satisfied"] for alt in result["alternatives"])
    assert result["label"] == "추정치" and result["caveats"]
    move = result["alternatives"][0]["moves"][0]
    assert set(move) == set(MOVE_FIELDS) and move["to_unit"].startswith("4F-")
    whatif = toolkit.whatif_score_moves("R2", [{"brand_id": move["brand_id"], "to_unit": move["to_unit"]}])
    low, high = whatif["delta_range_krw"]
    assert low <= whatif["delta_krw"] <= high and whatif["units"]
    with pytest.raises(ToolError):
        toolkit.whatif_score_moves("R2", [{"brand_id": "NO-SUCH-BRAND", "to_unit": "4F-E01"}])


def test_heatmaps_cover_actual_and_hypothetical_revisions(toolkit):
    actual = metrics.unit_metric(toolkit, "sales_per_m2", "R2", "4F")
    assert actual and all(value > 0 for value in actual.values())
    assert metrics.unit_metric(toolkit, "sales_per_m2", "H1", "3F")
    assert set(metrics.unit_metric(toolkit, "low_efficiency", "R2", "4F").values()) <= {0.0, 1.0}
    handler = UiActionHandler(heatmap_provider=lambda m, r, f: metrics.unit_metric(toolkit, m, r, f))
    result = handler.handle("ui_show_heatmap", {"metric": "aov_krw", "rev": "H1", "floor": "3F"}, {})
    assert json.loads(result.output)["ok"] is False and not result.patches
