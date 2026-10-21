"""Customer-day purchase sequences ("journeys") from enriched transactions.

A journey lists where and when one identified customer paid on one day. It is a purchase sequence, not a
tracked walking path; routes between stores are estimated separately (``cad2anything.spatial.routes``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date as Date
from typing import Any

import pandas as pd

from cad2anything.analytics.prep import category_name, purchases, segment_customers

JOURNEY_CAVEAT = (
    "결제 기록의 순서입니다. 실제로 걸어간 경로는 알 수 없고, 매장 사이 경로는 통로망 최단 경로로 추정했습니다. "
    "고객 ID는 가명입니다."
)
AGE_LABELS = {"20s": "20대", "30s": "30대", "40s": "40대", "50s": "50대", "60s+": "60대 이상"}
GENDER_LABELS = {"F": "여성", "M": "남성"}
TIER_LABELS = {"VVIP": "VVIP", "VIP": "VIP", "GOLD": "골드", "MEMBER": "일반 회원"}
WEEKDAYS_KO = "월화수목금토일"
MIN_STOPS = 3
MAX_STOPS = 12
CANDIDATE_COLUMNS = ["customer_id", "date", "stops", "floors", "total_krw"]


def segment_label(customer: Mapping[str, Any] | None) -> str:
    """Korean description such as "40대 여성 VIP" (unknown parts are skipped)."""
    if not customer:
        return "고객"
    parts = [
        AGE_LABELS.get(str(customer.get("age_band")), ""),
        GENDER_LABELS.get(str(customer.get("gender")), ""),
        TIER_LABELS.get(str(customer.get("tier")), ""),
    ]
    return " ".join(part for part in parts if part) or "고객"


def weekday_ko(day: Date) -> str:
    return WEEKDAYS_KO[day.weekday()]


def purchase_rows(enriched: pd.DataFrame) -> pd.DataFrame:
    """Identified, mapped, non-return purchases with ``customer_key`` and a ``day`` column."""
    rows = purchases(enriched)
    return rows.assign(day=rows["ts"].dt.date)


def filter_rows(
    rows: pd.DataFrame,
    customers: pd.DataFrame | None = None,
    segment: Mapping[str, Any] | None = None,
    start: Date | None = None,
    end: Date | None = None,
) -> pd.DataFrame:
    """Rows of ``purchase_rows`` inside an inclusive date range and a customer segment."""
    mask = pd.Series(True, index=rows.index)
    if start is not None:
        mask &= rows["day"] >= start
    if end is not None:
        mask &= rows["day"] <= end
    members = segment_customers(customers, segment)
    if members is not None:
        mask &= rows["customer_key"].isin(members)
    return rows.loc[mask]


def journey_candidates(
    rows: pd.DataFrame, min_stops: int = MIN_STOPS, max_stops: int = MAX_STOPS
) -> pd.DataFrame:
    """Customer-days ranked for display: more floors, then more stops, then higher spend (deterministic)."""
    if rows.empty:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)
    days = (
        rows.groupby(["customer_key", "day"], sort=False)
        .agg(stops=("unit_code", "size"), floors=("floor", "nunique"), total_krw=("amount_krw", "sum"))
        .reset_index()
        .rename(columns={"customer_key": "customer_id", "day": "date"})
    )
    days = days[(days["stops"] >= min_stops) & (days["stops"] <= max_stops)]
    return days.sort_values(
        ["floors", "stops", "total_krw", "customer_id", "date"],
        ascending=[False, False, False, True, True],
        kind="mergesort",
    ).reset_index(drop=True)[CANDIDATE_COLUMNS]


def journey_stops(rows: pd.DataFrame, customer_id: str, day: Date) -> tuple[list[dict[str, Any]], int]:
    """Ordered stops of one customer-day and the minutes between the first and last purchase.

    Consecutive purchases in the same unit are merged into one stop (amounts and receipts added up).
    """
    selected = rows.loc[(rows["customer_key"] == str(customer_id)) & (rows["day"] == day)]
    selected = selected.sort_values("ts", kind="mergesort")
    stops: list[dict[str, Any]] = []
    times: list[pd.Timestamp] = []
    for row in selected.itertuples(index=False):
        if stops and stops[-1]["unit_code"] == row.unit_code:
            stops[-1]["amount_krw"] += int(round(float(row.amount_krw)))
            stops[-1]["receipts"] += 1
            continue
        category = None if pd.isna(row.category) else str(row.category)
        gap = None if not times else int(round((row.ts - times[-1]).total_seconds() / 60))
        stops.append(
            {
                "order": len(stops) + 1,
                "time": row.ts.strftime("%H:%M"),
                "unit_code": str(row.unit_code),
                "floor": None if pd.isna(row.floor) else str(row.floor),
                "brand_id": None if pd.isna(row.brand_id) else str(row.brand_id),
                "brand_name": None if pd.isna(row.brand_name) else str(row.brand_name),
                "category": category,
                "category_name": category_name(category) if category else "",
                "amount_krw": int(round(float(row.amount_krw))),
                "receipts": 1,
                "gap_min": gap,
            }
        )
        times.append(row.ts)
    duration = int(round((times[-1] - times[0]).total_seconds() / 60)) if times else 0
    return stops, duration
