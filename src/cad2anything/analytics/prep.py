"""Transaction enrichment shared by every analytics module.

``enrich_transactions`` maps each transaction to the unit and brand of its POS terminal at the time of
the transaction, nets returns, and attaches category, floor, zone and commission rate. Conventions:

- Validity windows: ``valid_from`` is inclusive; a date-only ``valid_to`` includes that whole day, a
  timestamp ``valid_to`` is exclusive; empty ``valid_to`` means open-ended. When windows overlap, the most
  recent ``valid_from`` wins.
- Returns: a row is a return when ``is_return`` is true or ``amount_krw`` is negative. Its net amount is
  ``-abs(amount_krw)`` regardless of the sign convention. Returns that reference a known sale through
  ``ref_txn_id`` are attributed to that sale's unit and brand (the sale may predate a relocation).
- Sampling weight: ``weight`` (default 1.0 when the column is missing or empty) scales a sampled receipt to
  the full-scale store. Sales use ``net_sales_krw = net_amount_krw * weight`` and transaction counts use
  ``sum(weight)``; customer-based ratios (cross-shopping, floor flows) stay unweighted.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.catalog import CATEGORIES
from cad2anything.spatial.features import floor_ordinal

PYEONG_M2 = 3.3058
TIME_DIMS = ("hour", "weekday", "date", "month")
WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")
_OPEN_START = pd.Timestamp("1900-01-01")
_TRUE_STRINGS = {"true", "t", "1", "y", "yes"}

ENRICHED_COLUMNS = [
    "txn_id",
    "ts",
    "date",
    "hour",
    "weekday",
    "month",
    "pos_id",
    "customer_id",
    "amount_krw",
    "net_amount_krw",
    "weight",
    "net_sales_krw",
    "items",
    "net_items",
    "is_return",
    "ref_txn_id",
    "unit_code",
    "brand_id",
    "brand_name",
    "category",
    "floor",
    "zone_code",
    "commission_rate",
    "commission_source",
    "mapped",
    "return_attributed",
]
_INPUTS = {
    "txn_id",
    "ts",
    "pos_id",
    "customer_id",
    "amount_krw",
    "weight",
    "items",
    "ref_txn_id",
    "is_return",
}
_DERIVED = set(ENRICHED_COLUMNS) - _INPUTS


def to_bool(values: Any, index: pd.Index | None = None) -> pd.Series:
    """Booleans from bool/int/str columns (``"True"``, ``"1"``, ``"Y"`` ...); missing values are False."""
    if not isinstance(values, pd.Series):
        return pd.Series(bool(values), index=index, dtype=bool)
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(False).astype(bool)
    if pd.api.types.is_numeric_dtype(values):
        return values.fillna(0).astype(float) != 0
    return values.map(lambda v: str(v).strip().lower() in _TRUE_STRINGS if pd.notna(v) else False).astype(
        bool
    )


def to_datetime(values: Any) -> pd.Series:
    """Naive ``datetime64[ns]`` series (unparseable values become NaT)."""
    series = pd.to_datetime(pd.Series(values), errors="coerce")
    if getattr(series.dt, "tz", None) is not None:
        series = series.dt.tz_localize(None)
    return series.astype("datetime64[ns]")


def end_exclusive(values: pd.Series) -> pd.Series:
    """Exclusive end of validity: date-only values are extended to the end of that day."""
    ends = to_datetime(values)
    date_only = ends.notna() & (ends == ends.dt.normalize())
    return ends + pd.to_timedelta(date_only.astype(int), unit="D")


def period_bounds(start: Any = None, end: Any = None) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """``[start, end)`` timestamps for an inclusive date range (a date-only ``end`` covers that day)."""
    lo = pd.Timestamp(start) if start is not None else None
    hi = pd.Timestamp(end) if end is not None else None
    if hi is not None and hi == hi.normalize():
        hi = hi + pd.Timedelta(days=1)
    return lo, hi


def period_mask(ts: pd.Series, start: Any = None, end: Any = None) -> pd.Series:
    """Rows inside ``[start, end]`` (inclusive dates); None leaves that side open."""
    lo, hi = period_bounds(start, end)
    mask = pd.Series(True, index=ts.index)
    if lo is not None:
        mask &= ts >= lo
    if hi is not None:
        mask &= ts < hi
    return mask


def month_label(ts: pd.Series) -> pd.Series:
    """``YYYY-MM`` labels (vectorized; much faster than ``strftime`` on large tables)."""
    key = ts.dt.year * 100 + ts.dt.month
    labels = {int(k): f"{int(k) // 100:04d}-{int(k) % 100:02d}" for k in key.dropna().unique()}
    return key.map(labels)


def floor_from_unit_code(unit_code: Any) -> str | None:
    """``3F`` from ``3F-N05`` (None when the code has no floor prefix)."""
    if not isinstance(unit_code, str) or "-" not in unit_code:
        return None
    return unit_code.split("-", 1)[0]


def floor_sort_key(code: Any) -> tuple[float, str]:
    """Sort key placing floors bottom-up (``B1`` before ``1F``)."""
    return (floor_ordinal(str(code)), str(code))


def category_name(code: Any) -> str:
    """Korean category name from the catalog (the code itself when unknown)."""
    return CATEGORIES[code].name_ko if code in CATEGORIES else str(code)


def catalog_commission(category: Any) -> float:
    """Midpoint of the catalog commission range for a category (NaN when unknown)."""
    if category in CATEGORIES:
        lo, hi = CATEGORIES[category].commission_range
        return (lo + hi) / 2
    return math.nan


def as_list(value: Any) -> list[str]:
    """``;``-joined strings, lists or missing values as a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(";") if part.strip()]
    if isinstance(value, Collection):
        return [str(v) for v in value if v is not None and str(v).strip()]
    if isinstance(value, float) and math.isnan(value):
        return []
    return [str(value)]


def id_key(values: pd.Series) -> pd.Series:
    """String keys for ID columns so that ``123``, ``123.0`` and ``"123"`` match."""
    if pd.api.types.is_float_dtype(values):
        try:
            return values.astype("Int64").astype("string")
        except (TypeError, ValueError):
            pass
    return values.astype("string")


def segment_customers(customers: pd.DataFrame | None, segment: Mapping[str, Any] | None) -> set[str] | None:
    """Customer IDs (``pseudo_id``) matching a segment filter such as ``{"age_band": "40s", "tier": [...]}``.

    Returns None when no segment is given (no filtering). Scalars match by equality, collections by
    membership.
    """
    if not segment:
        return None
    if customers is None:
        raise ValueError("a segment filter needs the customers table")
    table = customers
    mask = pd.Series(True, index=table.index)
    for key, wanted in segment.items():
        if key not in table.columns:
            raise ValueError(f"unknown segment field: {key}")
        if isinstance(wanted, Collection) and not isinstance(wanted, str):
            mask &= table[key].isin(list(wanted))
        else:
            mask &= table[key] == wanted
    return set(id_key(table.loc[mask, "pseudo_id"]).dropna().tolist())


def purchases(
    enriched: pd.DataFrame,
    customers: pd.DataFrame | None = None,
    segment: Mapping[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
) -> pd.DataFrame:
    """Non-return, mapped transactions of identified customers in the period and segment."""
    mask = (
        ~enriched["is_return"].astype(bool)
        & enriched["customer_id"].notna()
        & enriched["mapped"].astype(bool)
    )
    mask &= period_mask(enriched["ts"], start, end)
    rows = enriched.loc[mask].copy()
    rows["customer_key"] = id_key(rows["customer_id"])
    members = segment_customers(customers, segment)
    if members is not None:
        rows = rows.loc[rows["customer_key"].isin(members)]
    return rows


def _asof_validity(
    left: pd.DataFrame, right: pd.DataFrame, by: list[str], columns: list[str], prefix: str
) -> pd.DataFrame:
    """Attach ``columns`` of the ``right`` row valid at ``left.ts`` (matched on ``by``)."""
    right = right.copy()
    right["_from"] = to_datetime(right["valid_from"]).fillna(_OPEN_START)
    right["_to"] = end_exclusive(right["valid_to"]) if "valid_to" in right else pd.NaT
    right = right[[*by, "_from", "_to", *columns]].rename(columns={c: f"{prefix}{c}" for c in columns})
    right = right.sort_values(["_from", *by], kind="mergesort")
    left = left.sort_values(["ts", "_order"], kind="mergesort")
    merged = pd.merge_asof(left, right, left_on="ts", right_on="_from", by=by, direction="backward")
    valid = merged["_from"].notna() & (merged["_to"].isna() | (merged["ts"] < merged["_to"]))
    for column in columns:
        merged.loc[~valid, f"{prefix}{column}"] = None
    return merged.drop(columns=["_from", "_to"])


def enrich_transactions(
    transactions: pd.DataFrame,
    pos_terminals: pd.DataFrame,
    tenancies: pd.DataFrame | None = None,
    brands: pd.DataFrame | None = None,
    unit_features: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Map transactions to unit/brand/category/floor via POS validity and net the returns.

    Output columns: see ``ENRICHED_COLUMNS`` (extra input columns such as ``payment`` are kept at the end).
    ``net_amount_krw`` is the sampled receipt amount, negative for returns; ``net_sales_krw`` scales it by
    ``weight``. ``commission_rate`` comes from the tenancy valid at the transaction time
    (``commission_source="tenancy"``) or, failing that, the catalog midpoint for the category (``"catalog"``).
    """
    tx = transactions.drop(columns=[c for c in _DERIVED if c in transactions.columns]).copy()
    tx["ts"] = to_datetime(tx["ts"])
    if tx["ts"].isna().any():
        raise ValueError("transactions.ts contains unparseable timestamps")
    tx["_order"] = np.arange(len(tx))
    tx["pos_id"] = tx["pos_id"].astype("string")
    for column in ("customer_id", "ref_txn_id", "items"):
        if column not in tx:
            tx[column] = None
    pos = pos_terminals.copy()
    pos["pos_id"] = pos["pos_id"].astype("string")
    tx = _asof_validity(tx, pos, ["pos_id"], ["brand_id", "unit_code"], "")
    tx["brand_id"] = tx["brand_id"].astype("string")
    tx["unit_code"] = tx["unit_code"].astype("string")

    amount = pd.to_numeric(tx["amount_krw"], errors="coerce").fillna(0.0).astype(float)
    is_return = to_bool(tx["is_return"], tx.index) if "is_return" in tx else pd.Series(False, index=tx.index)
    is_return = is_return | (amount < 0)
    tx["is_return"] = is_return
    tx["net_amount_krw"] = np.where(is_return, -amount.abs(), amount)
    weight = (
        pd.to_numeric(tx["weight"], errors="coerce") if "weight" in tx else pd.Series(np.nan, index=tx.index)
    )
    tx["weight"] = weight.fillna(1.0).astype(float)
    tx["net_sales_krw"] = tx["net_amount_krw"] * tx["weight"]
    items = pd.to_numeric(tx["items"], errors="coerce")
    tx["net_items"] = np.where(is_return, -items.abs(), items)

    tx["return_attributed"] = False
    tx["_origin_order"] = np.nan
    candidates = is_return & tx["ref_txn_id"].notna()
    if candidates.any():
        keys = id_key(tx["txn_id"])
        sales = tx.loc[~is_return & tx["unit_code"].notna()].assign(_key=keys)
        origin = sales.drop_duplicates("_key").set_index("_key")[["brand_id", "unit_code", "_order"]]
        refs = id_key(tx.loc[candidates, "ref_txn_id"])
        refs = refs[refs.isin(origin.index)]
        if len(refs):
            hit = refs.index
            matched = origin.loc[refs.to_numpy()]
            tx.loc[hit, "brand_id"] = matched["brand_id"].to_numpy()
            tx.loc[hit, "unit_code"] = matched["unit_code"].to_numpy()
            tx.loc[hit, "_origin_order"] = matched["_order"].to_numpy(dtype=float)
            tx.loc[hit, "return_attributed"] = True

    features = None
    if unit_features is not None and len(unit_features):
        cols = [c for c in ("unit_code", "floor", "zone_code", "category") if c in unit_features]
        features = unit_features[cols].drop_duplicates("unit_code").copy()
        features["unit_code"] = features["unit_code"].astype("string")
    if brands is not None and len(brands):
        brand_table = brands[[c for c in ("brand_id", "brand_name", "category") if c in brands]].copy()
        brand_table["brand_id"] = brand_table["brand_id"].astype("string")
        tx = tx.merge(brand_table.drop_duplicates("brand_id"), on="brand_id", how="left")
    for column in ("brand_name", "category"):
        if column not in tx:
            tx[column] = None
    if features is not None:
        renamed = features.rename(columns={"floor": "_floor", "zone_code": "_zone", "category": "_category"})
        tx = tx.merge(renamed, on="unit_code", how="left")
    for column in ("_floor", "_zone", "_category"):
        if column not in tx:
            tx[column] = None
    tx["category"] = tx["category"].where(tx["category"].notna(), tx["_category"])
    prefix = {code: floor_from_unit_code(code) for code in tx["unit_code"].dropna().unique()}
    tx["floor"] = tx["_floor"].where(tx["_floor"].notna(), tx["unit_code"].map(prefix))
    tx["zone_code"] = tx["_zone"]
    tx = tx.drop(columns=["_floor", "_zone", "_category"])

    tx["commission_rate"] = np.nan
    tx["commission_source"] = None
    mapped = tx["unit_code"].notna() & tx["brand_id"].notna()
    if tenancies is not None and len(tenancies) and mapped.any():
        ten = tenancies.copy()
        ten["brand_id"] = ten["brand_id"].astype("string")
        ten["unit_code"] = ten["unit_code"].astype("string")
        part = _asof_validity(
            tx.loc[mapped, ["_order", "ts", "brand_id", "unit_code"]],
            ten,
            ["brand_id", "unit_code"],
            ["commission_rate"],
            "_t_",
        ).set_index("_order")["_t_commission_rate"]
        rates = tx["_order"].map(pd.to_numeric(part, errors="coerce"))
        tx["commission_rate"] = rates
        tx.loc[rates.notna(), "commission_source"] = "tenancy"
    attributed = tx["_origin_order"].notna()
    if attributed.any():
        by_order = tx.set_index("_order")
        origin_order = tx.loc[attributed, "_origin_order"].astype(int)
        tx.loc[attributed, "commission_rate"] = origin_order.map(by_order["commission_rate"]).to_numpy()
        tx.loc[attributed, "commission_source"] = origin_order.map(by_order["commission_source"]).to_numpy()
    catalog_rates = pd.to_numeric(tx["category"].map(catalog_commission, na_action="ignore"), errors="coerce")
    fallback = tx["commission_rate"].isna() & catalog_rates.notna()
    tx["commission_rate"] = tx["commission_rate"].where(~fallback, catalog_rates).astype(float)
    tx.loc[fallback, "commission_source"] = "catalog"

    tx["mapped"] = tx["unit_code"].notna()
    tx["date"] = tx["ts"].dt.normalize()
    tx["hour"] = tx["ts"].dt.hour
    tx["weekday"] = tx["ts"].dt.dayofweek
    tx["month"] = month_label(tx["ts"])
    tx = tx.sort_values(["ts", "_order"], kind="mergesort").reset_index(drop=True)
    extras = [c for c in tx.columns if c not in ENRICHED_COLUMNS and not c.startswith("_")]
    return tx[[*ENRICHED_COLUMNS, *extras]]
