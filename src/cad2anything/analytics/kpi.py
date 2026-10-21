"""Space productivity KPIs, driver decomposition and time patterns.

Sales and transaction counts are weight-aware: ``sales = sum(net_amount_krw * weight)`` and
``txns = sum(weight)`` over non-return receipts, so a 1/50 sample with ``weight = 50`` reports full-scale
figures. Regression outputs are correlational estimates (추정치), never causal effects.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.analytics.prep import (
    PYEONG_M2,
    TIME_DIMS,
    WEEKDAY_KO,
    catalog_commission,
    category_name,
    end_exclusive,
    floor_from_unit_code,
    floor_sort_key,
    month_label,
    period_bounds,
    period_mask,
    to_bool,
    to_datetime,
)

LEVELS = ("unit", "zone", "floor", "category")
LOW_SHARE = 0.10
ESTIMATE_LABEL = "추정치"
CAUSAL_CAVEAT = "관측 데이터의 상관관계에 기반한 추정치이며 인과 효과로 해석하면 안 됩니다."
UNZONED = "UNZONED"
_SUM_COLUMNS = ["sales", "gross_sales", "returns", "txns", "commission_revenue"]
_METRIC_COLUMNS = [
    "area_m2",
    "sales",
    "gross_sales",
    "returns",
    "txns",
    "aov",
    "sales_per_m2",
    "sales_per_pyeong",
    "commission_rate",
    "commission_revenue",
    "gmrof",
]


# --- OLS ---------------------------------------------------------------------------------------------


@dataclass
class OLSResult:
    """Ordinary least squares fit (numpy ``lstsq``; standard errors from the pseudo-inverse)."""

    terms: list[str]
    coef: np.ndarray
    std_err: np.ndarray
    fitted: np.ndarray
    resid: np.ndarray
    r2: float
    adj_r2: float
    n_obs: int
    dof: int
    cov: np.ndarray

    def coefficient(self, term: str) -> float:
        return float(self.coef[self.terms.index(term)])

    def std_error(self, term: str) -> float:
        return float(self.std_err[self.terms.index(term)])

    @property
    def resid_std(self) -> float:
        return math.sqrt(float(self.resid @ self.resid) / self.dof) if self.n_obs else math.nan

    def table(self, labels: dict[str, str] | None = None) -> pd.DataFrame:
        labels = labels or {}
        with np.errstate(divide="ignore", invalid="ignore"):
            t_stat = np.where(self.std_err > 0, self.coef / self.std_err, np.nan)
        return pd.DataFrame(
            {
                "term": self.terms,
                "label_ko": [labels.get(term, term) for term in self.terms],
                "coef": self.coef,
                "std_err": self.std_err,
                "t_stat": t_stat,
            }
        )


def ols(x: np.ndarray, y: np.ndarray, terms: Sequence[str]) -> OLSResult:
    """Least squares of ``y`` on the columns of ``x`` (include an intercept column explicitly)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(y)
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    fitted = x @ coef
    resid = y - fitted
    rank = int(np.linalg.matrix_rank(x)) if n else 0
    dof = max(n - rank, 1)
    ssr = float(resid @ resid)
    sst = float(((y - y.mean()) ** 2).sum()) if n else 0.0
    cov = ssr / dof * np.linalg.pinv(x.T @ x)
    std_err = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    r2 = 1.0 - ssr / sst if sst > 0 else math.nan
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / dof if sst > 0 and n > rank else math.nan
    return OLSResult(list(terms), coef, std_err, fitted, resid, r2, adj_r2, n, dof, cov)


# --- productivity ------------------------------------------------------------------------------------


def _weights(frame: pd.DataFrame) -> pd.Series:
    if "weight" in frame:
        return pd.to_numeric(frame["weight"], errors="coerce").fillna(1.0).astype(float)
    return pd.Series(1.0, index=frame.index)


def weighted_sales(frame: pd.DataFrame) -> pd.Series:
    """Full-scale net sales per row (``net_sales_krw`` or ``net_amount_krw * weight``)."""
    if "net_sales_krw" in frame:
        return pd.to_numeric(frame["net_sales_krw"], errors="coerce").fillna(0.0).astype(float)
    return pd.to_numeric(frame["net_amount_krw"], errors="coerce").fillna(0.0) * _weights(frame)


def _sales_columns(tx: pd.DataFrame) -> pd.DataFrame:
    """Per-row weighted sales, gross sales, returns and transaction weight."""
    is_return = to_bool(tx["is_return"], tx.index)
    sales = weighted_sales(tx)
    return pd.DataFrame(
        {
            "sales": sales,
            "gross_sales": sales.where(~is_return, 0.0),
            "returns": (-sales).where(is_return, 0.0),
            "txns": _weights(tx).where(~is_return, 0.0),
        },
        index=tx.index,
    )


def _tenancy_rates(tenancies: pd.DataFrame | None, start: Any, end: Any) -> dict[str, float]:
    """Commission rate per unit from the tenancy overlapping the period the most (latest start on ties)."""
    if tenancies is None or not len(tenancies) or "commission_rate" not in tenancies:
        return {}
    ten = tenancies.copy()
    ten["_from"] = to_datetime(ten["valid_from"]).fillna(pd.Timestamp("1900-01-01"))
    ten["_to"] = end_exclusive(ten["valid_to"]).fillna(pd.Timestamp("2200-01-01"))
    lo, hi = period_bounds(start, end)
    lo = lo if lo is not None else pd.Timestamp("1900-01-01")
    hi = hi if hi is not None else pd.Timestamp("2200-01-01")
    ten["_overlap"] = (
        (ten["_to"].clip(upper=hi) - ten["_from"].clip(lower=lo)).dt.total_seconds().clip(lower=0)
    )
    ten["commission_rate"] = pd.to_numeric(ten["commission_rate"], errors="coerce")
    ten = ten.dropna(subset=["commission_rate"]).sort_values(
        ["unit_code", "_overlap", "_from"], ascending=[True, False, False], kind="mergesort"
    )
    best = ten.drop_duplicates("unit_code")
    return {
        str(code): float(rate) for code, rate in zip(best["unit_code"], best["commission_rate"], strict=True)
    }


def _feature_table(unit_features: pd.DataFrame) -> pd.DataFrame:
    feats = unit_features.drop_duplicates("unit_code").copy()
    for column in ("floor", "zone_code", "category", "brand_id", "brand_name"):
        if column not in feats:
            feats[column] = None
    feats["vacant"] = to_bool(feats["vacant"], feats.index) if "vacant" in feats else feats["brand_id"].isna()
    feats["area_m2"] = pd.to_numeric(feats.get("area_m2"), errors="coerce")
    feats["unit_code"] = feats["unit_code"].astype(str)
    return feats[
        ["unit_code", "floor", "zone_code", "category", "brand_id", "brand_name", "vacant", "area_m2"]
    ]


def _unit_table(
    enriched: pd.DataFrame, unit_features: pd.DataFrame, tenancies: pd.DataFrame | None, start: Any, end: Any
) -> pd.DataFrame:
    feats = _feature_table(unit_features)
    mask = period_mask(enriched["ts"], start, end) & enriched["unit_code"].notna()
    tx = enriched.loc[mask]
    frame = _sales_columns(tx)
    frame["unit_code"] = tx["unit_code"].astype(str)
    frame["brand_id"] = tx["brand_id"]
    frame["brand_name"] = tx.get("brand_name")
    frame["category"] = tx["category"]
    rates = pd.to_numeric(tx["commission_rate"], errors="coerce") if "commission_rate" in tx else np.nan
    frame["rate"] = rates
    fallback = _tenancy_rates(tenancies, start, end)
    frame["rate"] = frame["rate"].fillna(frame["unit_code"].map(fallback))
    catalog_rates = pd.to_numeric(
        frame["category"].map(catalog_commission, na_action="ignore"), errors="coerce"
    )
    frame["rate"] = pd.to_numeric(frame["rate"], errors="coerce").fillna(catalog_rates)
    frame["commission_revenue"] = frame["sales"] * frame["rate"].fillna(0.0)
    agg = frame.groupby("unit_code")[_SUM_COLUMNS].sum()
    dominant = (
        frame.dropna(subset=["brand_id"])
        .groupby(["unit_code", "brand_id"], as_index=False)
        .agg(gross=("gross_sales", "sum"), brand_name=("brand_name", "first"), category=("category", "first"))
        .sort_values(["unit_code", "gross", "brand_id"], ascending=[True, False, True], kind="mergesort")
        .drop_duplicates("unit_code")
        .set_index("unit_code")
    )
    codes = set(feats.loc[~feats["vacant"], "unit_code"]) | set(agg.index)
    table = pd.DataFrame({"unit_code": sorted(codes)})
    table = table.merge(feats, on="unit_code", how="left").merge(agg, on="unit_code", how="left")
    table[_SUM_COLUMNS] = table[_SUM_COLUMNS].fillna(0.0)
    for column in ("brand_id", "brand_name", "category"):
        observed = table["unit_code"].map(dominant[column]) if len(dominant) else None
        if observed is not None:
            table[column] = observed.where(observed.notna(), table[column])
    table["floor"] = table["floor"].where(
        table["floor"].notna(), table["unit_code"].map(floor_from_unit_code)
    )
    table["zone_code"] = table["zone_code"].fillna(UNZONED)
    return table


def _derive(table: pd.DataFrame) -> pd.DataFrame:
    area = table["area_m2"].where(table["area_m2"] > 0)
    sales = table["sales"]
    table["aov"] = table["gross_sales"] / table["txns"].where(table["txns"] > 0)
    table["sales_per_m2"] = sales / area
    table["sales_per_pyeong"] = sales / (area / PYEONG_M2)
    table["commission_rate"] = table["commission_revenue"] / sales.where(sales != 0)
    table["gmrof"] = table["commission_revenue"] / area
    return table


def _percentile(values: pd.Series) -> pd.Series:
    return values.rank(method="average", pct=True) * 100.0


def bottom_share_flags(
    table: pd.DataFrame,
    value: str,
    group_cols: Sequence[str],
    key_cols: Sequence[str],
    share: float = LOW_SHARE,
) -> pd.Series:
    """True for the ``ceil(share * n)`` lowest ``value`` rows of each group with at least two valid rows
    (ties broken by ``key_cols``)."""
    flags = pd.Series(False, index=table.index)
    groups = table.groupby(list(group_cols), dropna=False) if group_cols else [(None, table)]
    for _, part in groups:
        valid = part.loc[part[value].notna()]
        if len(valid) < 2:
            continue
        count = max(1, math.ceil(share * len(valid) - 1e-9))
        lowest = valid.sort_values([value, *key_cols], kind="mergesort").index[:count]
        flags.loc[lowest] = True
    return flags


def space_productivity(
    enriched: pd.DataFrame,
    unit_features: pd.DataFrame,
    tenancies: pd.DataFrame | None = None,
    start: Any = None,
    end: Any = None,
    level: str = "unit",
    low_share: float = LOW_SHARE,
) -> pd.DataFrame:
    """Sales productivity per unit, zone, floor or category for ``[start, end]`` (inclusive dates).

    Columns: keys, ``area_m2`` (occupied), ``sales`` (net, weighted KRW), ``gross_sales``, ``returns``,
    ``txns`` (weighted receipts), ``aov`` (gross sales per receipt), ``sales_per_m2``,
    ``sales_per_pyeong`` (1평 = 3.3058㎡), ``commission_rate`` (effective), ``commission_revenue``
    (sales x commission rate), ``gmrof`` (commission revenue per m2), ``percentile`` (store-wide rank of
    sales per m2, 0-100), ``floor_percentile`` (unit/zone) and ``low_efficiency`` (the ceil(10% x n)
    lowest sales per m2 within each floor for unit/zone, across all groups for floor/category; groups with
    fewer than two rows are never flagged).

    Units come from ``unit_features`` (occupied units, even without sales) plus any unit with sales in the
    period. Unit brand/category follow the brand with the highest gross sales in the period.
    """
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    units = _derive(_unit_table(enriched, unit_features, tenancies, start, end))
    if level == "unit":
        table = units
        keys = ["unit_code"]
        group = ["floor"]
    else:
        by = {"zone": ["floor", "zone_code"], "floor": ["floor"], "category": ["category"]}[level]
        spec = {column: (column, "sum") for column in _SUM_COLUMNS}
        spec["area_m2"] = ("area_m2", lambda s: s.sum(min_count=1))
        spec["n_units"] = ("unit_code", "count")
        if level == "zone":
            spec["category"] = ("category", lambda s: s.mode().iat[0] if s.notna().any() else None)
        table = _derive(units.groupby(by, dropna=False).agg(**spec).reset_index())
        keys = by
        group = ["floor"] if level == "zone" else []
    table["percentile"] = _percentile(table["sales_per_m2"])
    if level in ("unit", "zone"):
        table["floor_percentile"] = table.groupby("floor", dropna=False)["sales_per_m2"].transform(
            _percentile
        )
    table["low_efficiency"] = bottom_share_flags(table, "sales_per_m2", group, keys, low_share)
    if "category" in table:
        table["category_name"] = table["category"].map(
            lambda c: category_name(c) if isinstance(c, str) else None
        )
    sort_cols = {"unit": ["unit_code"], "zone": ["zone_code"], "floor": [], "category": ["category"]}[level]
    if "floor" in table:
        table["_floor_order"] = table["floor"].map(lambda f: floor_sort_key(f)[0])
        sort_cols = ["_floor_order", "floor", *sort_cols]
    table = table.sort_values(sort_cols, kind="mergesort")
    ordered = {
        "unit": ["unit_code", "floor", "zone_code", "category", "category_name", "brand_id", "brand_name"],
        "zone": ["floor", "zone_code", "category", "category_name", "n_units"],
        "floor": ["floor", "n_units"],
        "category": ["category", "category_name", "n_units"],
    }[level]
    rank_cols = ["percentile", "floor_percentile"] if level in ("unit", "zone") else ["percentile"]
    result = table[[*ordered, *_METRIC_COLUMNS, *rank_cols, "low_efficiency"]].reset_index(drop=True)
    lo, hi = period_bounds(start, end)
    result.attrs["level"] = level
    result.attrs["start"] = str(lo) if lo is not None else None
    result.attrs["end_exclusive"] = str(hi) if hi is not None else None
    return result


# --- driver decomposition ----------------------------------------------------------------------------

DRIVER_LABELS = {
    "intercept": "절편",
    "log_walk_escalator": "에스컬레이터 보행거리(로그)",
    "frontage_m": "통로 접면 길이(m)",
    "log_area": "매장 면적(로그)",
}
_CONTRIBUTION_LABELS = {
    "exposure": "에스컬레이터 보행거리",
    "frontage": "통로 접면 길이",
    "area": "매장 면적",
    "category": "카테고리 특성",
    "residual": "공간 요인으로 설명되지 않는 부분(브랜드력·운영 등)",
}


@dataclass
class DriverDecomposition:
    """OLS of log(sales per m2) on exposure, frontage, area and category dummies (추정치)."""

    coefficients: pd.DataFrame
    r2: float
    adj_r2: float
    n_obs: int
    contributions: pd.DataFrame
    explanations: dict[str, str]
    reference_category: str | None
    caveat: str = CAUSAL_CAVEAT
    notes: list[str] = field(default_factory=list)
    label: str = ESTIMATE_LABEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "r2": self.r2,
            "adj_r2": self.adj_r2,
            "n_obs": self.n_obs,
            "reference_category": self.reference_category,
            "coefficients": self.coefficients.to_dict(orient="records"),
            "contributions": self.contributions.to_dict(orient="records"),
            "explanations": self.explanations,
            "caveat": self.caveat,
            "notes": self.notes,
        }


def _driver_detail(component: str, row: pd.Series, means: dict[str, float]) -> str:
    if component == "exposure":
        return f"보행거리 {row['walk_to_escalator_m']:.0f}m, 표본 평균 {means['walk']:.0f}m"
    if component == "frontage":
        return f"접면 {row['frontage_m']:.1f}m, 표본 평균 {means['frontage']:.1f}m"
    if component == "area":
        return f"면적 {row['area_m2']:.0f}㎡, 표본 평균 {means['area']:.0f}㎡"
    if component == "category":
        return f"{category_name(row['category'])} 카테고리"
    return "관측된 공간 변수 밖의 요인"


def driver_decomposition(
    productivity: pd.DataFrame, unit_features: pd.DataFrame | None = None, low_flag: str = "low_efficiency"
) -> DriverDecomposition:
    """Explain unit sales per m2 with exposure, frontage, area and category (unit-level productivity).

    Model: ``log(sales_per_m2) ~ 1 + log(walk_to_escalator_m + 1) + frontage_m + log(area_m2) +
    category dummies`` (reference = most frequent category). Contributions are ``coef * (x - mean(x))`` in
    log points, so drivers, category and residual add up to ``log(y) - mean(log(y))``. For each low
    performer the most negative contribution is explained in Korean.
    """
    data = productivity.copy()
    needed = ["walk_to_escalator_m", "frontage_m", "area_m2"]
    if unit_features is not None:
        extra = [c for c in needed if c not in data or data[c].isna().all()]
        if extra:
            data = data.drop(columns=[c for c in extra if c in data]).merge(
                unit_features[["unit_code", *extra]].drop_duplicates("unit_code"), on="unit_code", how="left"
            )
    missing = [c for c in [*needed, "sales_per_m2", "category"] if c not in data]
    if missing:
        raise ValueError(f"driver_decomposition needs columns: {missing}")
    notes: list[str] = []
    valid = (
        (data["sales_per_m2"] > 0)
        & data["walk_to_escalator_m"].notna()
        & data["frontage_m"].notna()
        & (data["area_m2"] > 0)
    )
    if (~valid).any():
        notes.append(f"매출·면적·거리 값이 없는 {int((~valid).sum())}개 매장은 회귀에서 제외했습니다.")
    sample = data.loc[valid].reset_index(drop=True)
    y = np.log(sample["sales_per_m2"].to_numpy(dtype=float))
    numeric = {
        "log_walk_escalator": np.log1p(sample["walk_to_escalator_m"].to_numpy(dtype=float)),
        "frontage_m": sample["frontage_m"].to_numpy(dtype=float),
        "log_area": np.log(sample["area_m2"].to_numpy(dtype=float)),
    }
    categories = sample["category"].fillna("UNKNOWN").astype(str)
    counts = categories.value_counts()
    reference = sorted(counts.index, key=lambda c: (-counts[c], c))[0] if len(counts) else None
    dummy_names = [c for c in sorted(counts.index) if c != reference]
    n_params = 1 + len(numeric) + len(dummy_names)
    if len(sample) - n_params < 2 and dummy_names:
        notes.append("표본이 작아 카테고리 더미를 제외했습니다.")
        dummy_names = []
    if len(sample) < len(numeric) + 2:
        raise ValueError("driver_decomposition needs more units than parameters")
    dummies = {f"cat:{c}": (categories == c).to_numpy(dtype=float) for c in dummy_names}
    terms = ["intercept", *numeric, *dummies]
    x = np.column_stack([np.ones(len(sample)), *numeric.values(), *dummies.values()])
    fit = ols(x, y, terms)
    labels = {**DRIVER_LABELS, **{f"cat:{c}": f"카테고리: {category_name(c)}" for c in dummy_names}}
    contrib = pd.DataFrame(
        {
            "unit_code": sample["unit_code"],
            "floor": sample.get("floor"),
            "category_code": categories,
            "log_sales_per_m2": y,
            "predicted": fit.fitted,
        }
    )
    for name, key in (("exposure", "log_walk_escalator"), ("frontage", "frontage_m"), ("area", "log_area")):
        values = numeric[key]
        contrib[name] = fit.coefficient(key) * (values - values.mean())
    category_part = np.zeros(len(sample))
    for term, values in dummies.items():
        category_part += fit.coefficient(term) * (values - values.mean())
    contrib["category"] = category_part
    contrib["residual"] = fit.resid
    components = list(_CONTRIBUTION_LABELS)
    contrib["main_driver"] = contrib[components].idxmin(axis=1)
    contrib["main_driver_ko"] = contrib["main_driver"].map(_CONTRIBUTION_LABELS)
    contrib["main_driver_effect_pct"] = [
        (math.exp(row[row["main_driver"]]) - 1.0) * 100.0 for _, row in contrib.iterrows()
    ]
    flags = (
        to_bool(sample[low_flag], sample.index)
        if low_flag in sample
        else pd.Series(False, index=sample.index)
    )
    contrib["low_efficiency"] = flags.to_numpy()
    means = {
        "walk": float(sample["walk_to_escalator_m"].mean()),
        "frontage": float(sample["frontage_m"].mean()),
        "area": float(sample["area_m2"].mean()),
    }
    explanations: dict[str, str] = {}
    texts: list[str] = []
    for i, row in contrib.iterrows():
        if not row["low_efficiency"]:
            texts.append("")
            continue
        source = sample.loc[i]
        component = str(row["main_driver"])
        text = (
            f"{row['unit_code']}({source.get('floor', '')}·{category_name(row['category_code'])}): 면적당 매출이 "
            f"{source['sales_per_m2']:,.0f}원/㎡로 층 하위 10%입니다. 가장 큰 하락 요인은 "
            f"{_CONTRIBUTION_LABELS[component]}({_driver_detail(component, source, means)})이며, 표본 평균 대비 "
            f"영향은 약 {row['main_driver_effect_pct']:+.0f}%로 추정됩니다({ESTIMATE_LABEL}). {CAUSAL_CAVEAT}"
        )
        explanations[str(row["unit_code"])] = text
        texts.append(text)
    contrib["explanation"] = texts
    if fit.n_obs < 3 * len(terms):
        notes.append("관측치가 계수 수에 비해 적어 계수가 불안정할 수 있습니다.")
    return DriverDecomposition(
        coefficients=fit.table(labels),
        r2=fit.r2,
        adj_r2=fit.adj_r2,
        n_obs=fit.n_obs,
        contributions=contrib,
        explanations=explanations,
        reference_category=reference,
        notes=notes,
    )


# --- time patterns -----------------------------------------------------------------------------------


def time_patterns(
    enriched: pd.DataFrame,
    by: Sequence[str] = ("hour", "weekday", "category"),
    start: Any = None,
    end: Any = None,
) -> pd.DataFrame:
    """Weighted sales and receipts by time dimensions (``hour``, ``weekday``, ``date``, ``month``) and any
    enriched column (``category``, ``floor``, ``brand_id`` ...).

    ``share`` is the share of total net sales; ``share_in_group`` normalizes within the non-time
    dimensions (e.g. the hourly profile of each category sums to 1).
    """
    by = list(by)
    if not by:
        raise ValueError("time_patterns needs at least one dimension")
    tx = enriched.loc[period_mask(enriched["ts"], start, end)].copy()
    ts = to_datetime(tx["ts"])
    derived = {
        "hour": lambda: ts.dt.hour,
        "weekday": lambda: ts.dt.dayofweek,
        "date": lambda: ts.dt.normalize(),
        "month": lambda: month_label(ts),
    }
    for dim in by:
        if dim in derived:
            if dim not in tx:
                tx[dim] = derived[dim]()
        elif dim not in tx:
            raise ValueError(f"unknown dimension: {dim}")
    frame = _sales_columns(tx)
    frame[by] = tx[by]
    out = frame.groupby(by, dropna=False)[["sales", "gross_sales", "txns"]].sum().reset_index()
    out["aov"] = out["gross_sales"] / out["txns"].where(out["txns"] > 0)
    total = out["sales"].sum()
    out["share"] = out["sales"] / total if total else np.nan
    groups = [d for d in by if d not in TIME_DIMS]
    if groups and len(groups) < len(by):
        group_total = out.groupby(groups, dropna=False)["sales"].transform("sum")
        out["share_in_group"] = out["sales"] / group_total.where(group_total != 0)
    else:
        out["share_in_group"] = out["share"]
    if "weekday" in by:
        out["weekday_name"] = out["weekday"].map(lambda d: WEEKDAY_KO[int(d)] if pd.notna(d) else None)
    if "category" in by:
        out["category_name"] = out["category"].map(lambda c: category_name(c) if isinstance(c, str) else None)
    return out.sort_values(by, kind="mergesort").reset_index(drop=True)
