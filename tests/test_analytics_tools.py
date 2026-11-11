import json
from datetime import date

import pandas as pd
import pytest

from cad2anything.agents.ui_actions import UiActionHandler
from cad2anything.services import metrics
from cad2anything.services.analytics_tools import MOVE_FIELDS, parse_segment
from cad2anything.services.tools import ToolError, Toolkit
from cad2anything.workspace import Workspace


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


def test_productivity_separates_category_structure_from_weak_stores(toolkit):
    result = toolkit.analytics_productivity("R2", floor="4F", top=500)
    rows = {row["unit_code"]: row for row in result["rows"]}
    by_reason = result["summary"]["low_efficiency_by_reason"]
    assert sum(by_reason.values()) == result["summary"]["low_efficiency"] == len(result["low_efficiency"])
    for row in result["low_efficiency"]:
        weak = row["category_index"] < 0.7
        assert row["low_reason"] == ("같은 카테고리 대비 부진" if weak else "카테고리 특성")
    indexes = [row["category_index"] for row in result["category_weak"]]
    assert indexes == sorted(indexes) and all(value < 0.7 for value in indexes)
    assert result["summary"]["category_weak"] == len(result["category_weak"])
    assert all(
        rows[row["unit_code"]]["category_index"] == row["category_index"] for row in result["category_weak"]
    )
    mix = result["category_mix"]
    rates = [m["sales_per_m2"] for m in mix]
    assert rates == sorted(rates) and sum(m["n_units"] for m in mix) <= len(rows)
    assert sum(m["low_efficiency"] for m in mix) == len(result["low_efficiency"])
    assert "category_index" in result["reading_guide"]
    floor_level = toolkit.analytics_productivity("R2", level="floor")
    assert "category_mix" not in floor_level and "category_index" not in floor_level["rows"][0]


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
    weekday = toolkit.analytics_time_patterns(by=["weekday", "hour"], category="F&B")
    assert weekday["rows"] and weekday["category"] == "F&B"
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


def test_unit_metric_values_flag_model_estimates(toolkit):
    observed, estimated = metrics.unit_metric_values(toolkit, "sales_per_m2", "R2", "4F")
    assert observed and estimated is False

    modelled, estimated = metrics.unit_metric_values(toolkit, "sales_per_m2", "H1", "3F")
    assert modelled and estimated is True
    assert metrics.unit_metric(toolkit, "sales_per_m2", "H1", "3F") == modelled

    walks, estimated = metrics.unit_metric_values(toolkit, "walk_to_escalator_m", "H1", "3F")
    assert walks and estimated is False
    with pytest.raises(ToolError, match="기대 매출 추정치"):
        metrics.unit_metric_values(toolkit, "aov_krw", "H1", "3F")


def test_productivity_reports_how_the_period_was_annualized(toolkit):
    result = toolkit.analytics_productivity("R1", level="floor")
    block = result["annualization"]

    assert block["method"] == "calendar_days" and block["seasonality_adjusted"] is False
    assert block["index_source"] == "observed_transactions"
    assert block["index_through"] == result["period"]["end"]
    assert block["factor"] == pytest.approx(365.0 / result["period"]["days"], abs=1e-3)
    assert set(block["monthly_index"]) == {str(m) for m in range(1, 13)}
    assert block["seasonal_factor"] > 0 and 0.5 < block["seasonal_ratio"] < 2.0
    assert result["summary"]["sales_krw_seasonally_adjusted"] == pytest.approx(
        result["summary"]["sales_krw"] * block["seasonal_ratio"], rel=1e-3
    )
    assert "seasonality_adjusted=false" in result["note"]

    # R1 covers the low season, so annualizing on calendar days alone understates the year.
    assert block["seasonal_ratio"] > 1.01
    other = toolkit.analytics_productivity("R2", level="floor")["annualization"]
    assert other["seasonal_ratio"] != pytest.approx(block["seasonal_ratio"], rel=1e-3)


def test_monthly_index_uses_only_sales_up_to_the_period_end(toolkit):
    early = toolkit._monthly_index(date(2025, 2, 28))
    late = toolkit._monthly_index(toolkit.data_through())

    assert set(early) == set(range(1, 13)) and early != late
    # August is never observed before the cut-off, so it stays neutral instead of showing its trough.
    assert early[8] == pytest.approx(1.0, abs=0.01) and late[8] < 0.95
    assert late[5] > 1.05 and late[2] < 0.95  # observed peak and trough
    assert toolkit._season_profile(date(2025, 2, 28))[1] < 12
    assert toolkit._season_profile(toolkit.data_through())[1] == 12


def test_as_of_keeps_later_transactions_out_of_the_layout_fit(tmp_path_factory, populate):
    cutoff = "2025-06-30"
    full = Toolkit(populate(tmp_path_factory.mktemp("asof-full")), use_postgis=False)
    root = populate(tmp_path_factory.mktemp("asof-cut")).root
    path = Workspace(root).table_path("transactions")
    frame = pd.read_csv(path)
    frame[pd.to_datetime(frame["ts"]) < pd.Timestamp(cutoff) + pd.Timedelta(days=1)].to_csv(path, index=False)
    truncated = Toolkit(Workspace(root), use_postgis=False)

    inputs = full._layout_inputs("R2", as_of=cutoff)
    assert inputs["as_of"] == cutoff and inputs["period"]["end"] == cutoff
    assert inputs["fit_rev"] == "R1"  # R2 only takes effect after the cut-off

    expected = full.expected_unit_sales("R2", as_of=cutoff).set_index("unit_code")["expected_sales"]
    same = truncated.expected_unit_sales("R2", as_of=cutoff).set_index("unit_code")["expected_sales"]
    assert expected.to_dict() == pytest.approx(same.to_dict())

    # Without a cut-off the full workspace sees the later transactions and fits on R2 instead.
    default = full._layout_inputs("R2")
    start, end, days = full.revision_period("R2")
    assert default["as_of"] is None and default["fit_rev"] == "R2"
    assert default["period"] == {"start": start, "end": end, "days": days}
    assert full.revision_period("R1", as_of=cutoff) == ("2025-03-01", cutoff, 122)
    with pytest.raises(ToolError, match="아직 거래 실적이 없습니다"):
        full.revision_period("R2", as_of=cutoff)  # R2 only takes effect after the cut-off
