"""Unit metrics for heatmaps and the productivity API, computed by the analytics toolkit.

Actual revisions use annualized sales from transactions. Hypothetical revisions, and actual revisions that
took effect after the last transaction day (for example an uploaded next-season drawing), have no
transactions, so their sales metrics are the layout scoring model's expected sales (estimates).
``unit_metric_values`` reports which of the two a caller got; ``unit_metric`` keeps the plain mapping.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from cad2anything.services.tools import ToolError, Toolkit

METRICS = ("sales_per_m2", "sales_krw", "aov_krw", "gmrof", "walk_to_escalator_m", "low_efficiency")
EXPECTED_METRICS = {"sales_per_m2": "expected_sales_per_m2", "sales_krw": "expected_sales"}
ACTUAL_COLUMNS = {"sales_krw": "sales", "aov_krw": "aov"}
ESTIMATE_NOTE = "이 개정은 거래 실적이 없어 레이아웃 모형의 기대 매출(추정치)을 보여 줍니다. 실제 실적과 다를 수 있습니다."


def _values(frame: pd.DataFrame, column: str) -> dict[str, float]:
    values = frame.set_index("unit_code")[column]
    return {str(k): float(v) for k, v in values.items() if pd.notna(v)}


def unit_metric_values(
    toolkit: Toolkit, metric: str, rev: str, floor: str | None
) -> tuple[dict[str, float], bool]:
    """Unit values of a metric and whether they are model estimates instead of observed results."""
    if metric not in METRICS:
        raise ToolError(f"지원하지 않는 지표입니다: {metric}. 사용 가능: {', '.join(METRICS)}")
    if metric == "walk_to_escalator_m":
        features = toolkit.unit_features(rev)
        if features is None:
            raise ToolError("공간 특징을 계산할 수 없습니다.")
        return _values(features[features["floor"] == floor] if floor else features, metric), False
    if not toolkit._has_sales(rev):
        if metric not in EXPECTED_METRICS:
            raise ToolError(
                "가상 개정이나 아직 실적이 없는 개정은 sales_per_m2와 sales_krw(기대 매출 추정치)만 표시할 수 있습니다."
            )
        expected = toolkit.expected_unit_sales(rev)
        expected = expected[expected["brand_id"].notna()]
        values = _values(
            expected[expected["floor"] == floor] if floor else expected, EXPECTED_METRICS[metric]
        )
        return values, True
    frame = toolkit.productivity_frame(rev, "unit")
    frame = frame[frame["floor"] == floor] if floor else frame
    column = ACTUAL_COLUMNS.get(metric, metric)
    if metric == "low_efficiency":
        frame = frame.assign(low_efficiency=frame["low_efficiency"].astype(float))
    return _values(frame, column), False


def unit_metric(toolkit: Toolkit, metric: str, rev: str, floor: str | None) -> dict[str, float]:
    """Unit values of a metric (use ``unit_metric_values`` to learn whether they are estimates)."""
    return unit_metric_values(toolkit, metric, rev, floor)[0]


def productivity_rows(
    toolkit: Toolkit, rev: str, floor: str | None, top: int | None = None, level: str = "unit"
) -> dict[str, Any]:
    result = toolkit.analytics_productivity(rev, floor=floor, level=level, top=top or 500)
    for row in result["rows"]:
        row.setdefault("driver", None)
    return result
