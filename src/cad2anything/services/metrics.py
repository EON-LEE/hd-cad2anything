"""Unit-level metrics for heatmaps and the productivity API.

Uses ``cad2anything.analytics`` when it is installed; otherwise a transparent fallback joins
transactions to units through POS validity windows for the revision period.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from cad2anything.catalog import CATEGORIES
from cad2anything.model import EntityKind, SpatialModel
from cad2anything.services.tools import ToolError, Toolkit

PYEONG_M2 = 3.3058
METRICS = ("sales_per_m2", "sales_krw", "aov_krw", "gmrof", "walk_to_escalator_m", "low_efficiency")


def revision_window(toolkit: Toolkit, rev: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    if not toolkit.ws.has_table("tenancies"):
        return None
    tenancies = toolkit.ws.table("tenancies")
    rows = tenancies[tenancies["rev_id"] == rev]
    if rows.empty:
        return None
    return pd.to_datetime(rows["valid_from"]).min(), pd.to_datetime(rows["valid_to"]).max()


def _escalator_distance(model: SpatialModel) -> dict[str, float]:
    distances: dict[str, float] = {}
    for unit in model.units():
        escalators = [
            v.geometry
            for v in model.by_kind(EntityKind.VERTICAL_LINK, unit.floor)
            if v.attributes.get("link_type") == "escalator"
        ]
        if escalators:
            distances[unit.attributes["unit_code"]] = min(unit.geometry.distance(e) for e in escalators)
    return distances


def productivity_table(toolkit: Toolkit, rev: str) -> pd.DataFrame:
    """Per-unit sales, transactions, AOV, sales density, and commission-based GMROF for a revision."""
    model = toolkit._model(rev)
    units = pd.DataFrame(
        [
            {
                "unit_code": u.attributes.get("unit_code"),
                "floor": u.floor,
                "brand_id": u.attributes.get("brand_id"),
                "brand_name": u.attributes.get("brand_name"),
                "category": u.attributes.get("category"),
                "vacant": bool(u.attributes.get("vacant")),
                "area_m2": u.geometry.area,
            }
            for u in model.units()
        ]
    )
    window = revision_window(toolkit, rev)
    needed = ("pos_terminals", "transactions", "tenancies")
    if window is None or not all(toolkit.ws.has_table(name) for name in needed):
        raise ToolError(f"{rev}의 실적을 계산할 거래 데이터가 없습니다(가상 개정이거나 데이터 미적재).")
    start, end = window
    pos = toolkit.ws.table("pos_terminals").copy()
    pos["valid_from"] = pd.to_datetime(pos["valid_from"])
    pos["valid_to"] = pd.to_datetime(pos["valid_to"]).fillna(pd.Timestamp.max)
    txns = toolkit.ws.table("transactions")
    ts = pd.to_datetime(txns["ts"])
    mask = (ts >= start) & (ts < end + pd.Timedelta(days=1))
    window_txns = txns.loc[
        mask, ["pos_id", "amount_krw"] + (["weight"] if "weight" in txns.columns else [])
    ].copy()
    window_txns["ts"] = ts[mask]
    if "weight" not in window_txns.columns:
        window_txns["weight"] = 1.0
    joined = window_txns.merge(pos[["pos_id", "unit_code", "valid_from", "valid_to"]], on="pos_id")
    joined = joined[
        (joined["ts"] >= joined["valid_from"]) & (joined["ts"] < joined["valid_to"] + pd.Timedelta(days=1))
    ]
    joined["sales"] = joined["amount_krw"] * joined["weight"]
    joined["count"] = np.where(joined["amount_krw"] > 0, joined["weight"], 0.0)
    sales = joined.groupby("unit_code").agg(sales_krw=("sales", "sum"), txns=("count", "sum")).reset_index()
    tenancies = toolkit.ws.table("tenancies")
    commission = tenancies[tenancies["rev_id"] == rev][["unit_code", "commission_rate"]]
    table = units.merge(sales, on="unit_code", how="left").merge(commission, on="unit_code", how="left")
    table[["sales_krw", "txns"]] = table[["sales_krw", "txns"]].fillna(0.0)
    days = max((end - start).days + 1, 1)
    annual = table["sales_krw"] * 365.0 / days
    table["aov_krw"] = np.where(table["txns"] > 0, table["sales_krw"] / table["txns"].clip(lower=1), np.nan)
    table["sales_per_m2"] = annual / table["area_m2"]
    table["sales_per_pyeong"] = table["sales_per_m2"] * PYEONG_M2
    table["gmrof"] = annual * table["commission_rate"].fillna(0.0) / table["area_m2"]
    distances = _escalator_distance(model)
    table["walk_to_escalator_m"] = table["unit_code"].map(distances)
    features = toolkit.unit_features(rev)
    if features is not None and "walk_to_escalator_m" in features.columns:
        walk = features.set_index("unit_code")["walk_to_escalator_m"]
        table["walk_to_escalator_m"] = table["unit_code"].map(walk).fillna(table["walk_to_escalator_m"])
    occupied = ~table["vacant"]
    table["percentile"] = np.nan
    table.loc[occupied, "percentile"] = table[occupied].groupby("floor")["sales_per_m2"].rank(pct=True)
    table["low_efficiency"] = occupied & (table["percentile"] <= 0.1)
    table["category_name"] = table["category"].map(lambda c: CATEGORIES[c].name_ko if c in CATEGORIES else "")
    table.attrs["window"] = [start.date().isoformat(), end.date().isoformat()]
    return table


def unit_metric(toolkit: Toolkit, metric: str, rev: str, floor: str | None) -> dict[str, float]:
    if metric not in METRICS:
        raise ToolError(f"지원하지 않는 지표입니다: {metric}")
    if metric == "walk_to_escalator_m":
        model = toolkit._model(rev)
        distances = _escalator_distance(model)
        features = toolkit.unit_features(rev)
        if features is not None and "walk_to_escalator_m" in features.columns:
            distances.update(features.set_index("unit_code")["walk_to_escalator_m"].dropna().to_dict())
        floor_units = {u.attributes.get("unit_code") for u in model.units(floor)}
        return {k: float(v) for k, v in distances.items() if k in floor_units}
    table = productivity_table(toolkit, rev)
    if floor:
        table = table[table["floor"] == floor]
    table = table[~table["vacant"]]
    values = table.set_index("unit_code")[metric].astype(float)
    return {k: float(v) for k, v in values.items() if pd.notna(v)}


def productivity_rows(
    toolkit: Toolkit, rev: str, floor: str | None, top: int | None = None
) -> dict[str, Any]:
    table = productivity_table(toolkit, rev)
    if floor:
        table = table[table["floor"] == floor]
    table = table.sort_values("sales_per_m2", ascending=False)
    rows = [
        {
            "unit_code": r.unit_code,
            "floor": r.floor,
            "brand_name": r.brand_name,
            "category": r.category,
            "category_name": r.category_name,
            "area_m2": round(float(r.area_m2), 1),
            "sales_krw": round(float(r.sales_krw)),
            "aov_krw": None if pd.isna(r.aov_krw) else round(float(r.aov_krw)),
            "sales_per_m2": round(float(r.sales_per_m2)),
            "sales_per_pyeong": round(float(r.sales_per_pyeong)),
            "gmrof": round(float(r.gmrof)),
            "walk_to_escalator_m": None
            if pd.isna(r.walk_to_escalator_m)
            else round(float(r.walk_to_escalator_m), 1),
            "percentile": None if pd.isna(r.percentile) else round(float(r.percentile), 3),
            "low_efficiency": bool(r.low_efficiency),
            "vacant": bool(r.vacant),
        }
        for r in table.itertuples(index=False)
    ]
    occupied = [row["sales_per_m2"] for row in rows if not row["vacant"]]
    return {
        "rev": rev,
        "floor": floor,
        "window": table.attrs.get("window"),
        "metric": {
            "name": "sales_per_m2",
            "label": "연환산 ㎡당 매출(원)",
            "min": min(occupied) if occupied else 0,
            "max": max(occupied) if occupied else 0,
        },
        "rows": rows[:top] if top else rows,
        "note": "합성 데이터, 거래 가중치로 전체 규모 환산. 공용면적 배분 전 전용면적 기준.",
    }
