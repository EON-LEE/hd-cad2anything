"""Floor-to-floor purchase flows from same-day customer purchase sequences (unweighted).

Transitions link consecutive purchases (sorted by timestamp) of one customer on one day. The fountain
indicator is the share of base-floor (B1) purchasers who also buy on an upper floor that day; the shower
indicator is the share of top-floor purchasers who buy on a lower floor after their first top-floor
purchase that day. These are purchase-record proxies for movement, not observed foot traffic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.analytics.prep import floor_sort_key, purchases
from cad2anything.spatial.features import floor_ordinal

FLOW_CAVEAT = (
    "층간 이동은 같은 날 같은 고객의 구매 순서로 추정한 값이며 실제 동선 관측이 아닙니다. "
    "고객 단위 비율이므로 표본 가중치(weight)는 적용하지 않았습니다."
)


@dataclass
class FloorFlows:
    """Floor transition counts/probabilities and fountain/shower indicators."""

    floors: list[str]
    counts: pd.DataFrame
    probabilities: pd.DataFrame
    base_floor: str | None
    top_floor: str | None
    fountain_share: float
    fountain_customer_days: int
    shower_share: float
    shower_customer_days: int
    n_customer_days: int
    n_transitions: int
    notes: list[str] = field(default_factory=list)
    caveat: str = FLOW_CAVEAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "floors": self.floors,
            "counts": self.counts.to_dict(),
            "probabilities": self.probabilities.to_dict(),
            "base_floor": self.base_floor,
            "top_floor": self.top_floor,
            "fountain_share": self.fountain_share,
            "fountain_customer_days": self.fountain_customer_days,
            "shower_share": self.shower_share,
            "shower_customer_days": self.shower_customer_days,
            "n_customer_days": self.n_customer_days,
            "n_transitions": self.n_transitions,
            "notes": self.notes,
            "caveat": self.caveat,
        }


def floor_transitions(
    enriched: pd.DataFrame,
    customers: pd.DataFrame | None = None,
    segment: Mapping[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
    include_same_floor: bool = True,
    base_floor: str = "B1",
    top_floor: str | None = None,
) -> FloorFlows:
    """Transition matrix between floors (rows: from, columns: to; rows with data sum to 1).

    ``base_floor`` defaults to B1 (the lowest floor when B1 has no purchases); ``top_floor`` defaults to
    the highest floor with purchases.
    """
    rows = purchases(enriched, customers, segment, start, end)
    rows = rows.loc[rows["floor"].notna()].copy()
    rows["floor"] = rows["floor"].astype(str)
    floors = sorted(rows["floor"].unique(), key=floor_sort_key)
    empty = pd.DataFrame(0, index=pd.Index(floors, name="from"), columns=pd.Index(floors, name="to"))
    if rows.empty:
        return FloorFlows(floors, empty, empty.astype(float), None, None, np.nan, 0, np.nan, 0, 0, 0)
    rows["_day"] = rows["ts"].dt.normalize()
    rows["_seq"] = np.arange(len(rows))
    rows = rows.sort_values(["customer_key", "_day", "ts", "_seq"], kind="mergesort")
    keys = ["customer_key", "_day"]
    rows["_next"] = rows.groupby(keys)["floor"].shift(-1)
    moves = rows.loc[rows["_next"].notna(), ["floor", "_next"]]
    if not include_same_floor:
        moves = moves.loc[moves["floor"] != moves["_next"]]
    counts = pd.crosstab(moves["floor"], moves["_next"]) if len(moves) else empty.copy()
    counts = counts.reindex(index=floors, columns=floors, fill_value=0).astype(int)
    counts.index.name, counts.columns.name = "from", "to"
    totals = counts.sum(axis=1)
    probabilities = counts.div(totals.where(totals > 0), axis=0)

    ordinal = {code: floor_ordinal(code) for code in floors}
    rows["_ord"] = rows["floor"].map(ordinal)
    base = base_floor if base_floor in ordinal else floors[0]
    top = top_floor if top_floor in ordinal else floors[-1]
    rows["_is_base"] = rows["floor"] == base
    rows["_above_base"] = rows["_ord"] > ordinal[base]
    per_day = rows.groupby(keys)[["_is_base", "_above_base"]].any()
    base_days = per_day.loc[per_day["_is_base"]]
    fountain = float(base_days["_above_base"].mean()) if len(base_days) else np.nan
    first_top = rows.loc[rows["floor"] == top].groupby(keys)["ts"].min().rename("_first_top")
    rows = rows.join(first_top, on=keys)
    top_rows = rows.loc[rows["_first_top"].notna()]
    lower_after = (top_rows["_ord"] < ordinal[top]) & (top_rows["ts"] > top_rows["_first_top"])
    shower_days = lower_after.groupby([top_rows["customer_key"], top_rows["_day"]]).any()
    shower = float(shower_days.mean()) if len(shower_days) else np.nan
    notes = [
        f"분수 효과: {base} 구매 고객-일 중 같은 날 상층 구매 비율 {fountain:.1%}"
        if len(base_days)
        else f"분수 효과: {base} 구매 기록이 없습니다.",
        f"샤워 효과: {top} 구매 고객-일 중 이후 하층 구매 비율 {shower:.1%}"
        if len(shower_days)
        else f"샤워 효과: {top} 구매 기록이 없습니다.",
    ]
    if (totals == 0).any():
        empty_rows = ", ".join(totals.index[totals == 0])
        notes.append(f"다음 층은 이후 구매가 없어 전이 확률이 비어 있습니다: {empty_rows}")
    return FloorFlows(
        floors=floors,
        counts=counts,
        probabilities=probabilities,
        base_floor=base,
        top_floor=top,
        fountain_share=fountain,
        fountain_customer_days=len(base_days),
        shower_share=shower,
        shower_customer_days=len(shower_days),
        n_customer_days=len(per_day),
        n_transitions=int(counts.to_numpy().sum()),
        notes=notes,
    )
