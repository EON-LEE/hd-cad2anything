"""Adjacency effects and before/after comparisons, with explicit non-causal caveats.

Both functions describe associations in observational data. Neighbor categories and relocations are
not randomly assigned: stronger brands get better spots and planners place anchors near escalators, so
naive differences mix the effect of interest with exposure and brand strength.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.analytics.kpi import ESTIMATE_LABEL, ols, weighted_sales
from cad2anything.analytics.prep import category_name, to_bool, to_datetime

SMD_THRESHOLD = 0.5
SHRINK_THRESHOLD = 0.5
MIN_EFFECT_LOG = 0.05
ADJACENCY_CAVEAT = (
    "인접 효과는 관측 데이터의 연관성 추정치이며 인과 효과가 아닙니다. 인접 배치는 무작위가 아니므로 "
    "노출도·브랜드력·층 특성 같은 요인이 함께 작용할 수 있습니다. 통제 변수를 넣어도 관측되지 않은 "
    "교란 요인은 남습니다."
)
BEFORE_AFTER_CAVEAT = (
    "개편 전후 비교는 계절성, 판촉, 동시 개편, 브랜드 교체 같은 요인을 통제하지 않은 기술 통계입니다. "
    "비교군 대비 차이도 추정치이며 인과 효과로 해석하면 안 됩니다."
)


def _pct(log_value: float) -> float:
    return (math.exp(log_value) - 1.0) * 100.0 if np.isfinite(log_value) else math.nan


@dataclass
class AdjacencyEffect:
    """Naive vs exposure-controlled association between a neighbor category and sales per m2."""

    neighbor_categories: list[str]
    n_treated: int
    n_control: int
    naive_log_diff: float
    naive_uplift_pct: float
    controlled_log_coef: float
    controlled_std_err: float
    controlled_uplift_pct: float
    controlled_ci_pct: tuple[float, float]
    exposure_smd: float
    confounding_suspected: bool
    reasons: list[str]
    controls: list[str]
    caveat: str
    table: pd.DataFrame
    label: str = ESTIMATE_LABEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "neighbor_categories": self.neighbor_categories,
            "n_treated": self.n_treated,
            "n_control": self.n_control,
            "naive_log_diff": self.naive_log_diff,
            "naive_uplift_pct": self.naive_uplift_pct,
            "controlled_log_coef": self.controlled_log_coef,
            "controlled_std_err": self.controlled_std_err,
            "controlled_uplift_pct": self.controlled_uplift_pct,
            "controlled_ci_pct": list(self.controlled_ci_pct),
            "exposure_smd": self.exposure_smd,
            "confounding_suspected": self.confounding_suspected,
            "reasons": self.reasons,
            "controls": self.controls,
            "caveat": self.caveat,
        }


def _category_predicate(
    flag: str | Collection[str] | Callable[[str], bool],
) -> tuple[Callable[[Any], bool], list]:
    if callable(flag):
        return (lambda c: isinstance(c, str) and bool(flag(c))), []
    wanted = {flag} if isinstance(flag, str) else set(flag)
    return (lambda c: c in wanted), sorted(wanted)


def adjacency_effect(
    productivity: pd.DataFrame,
    adjacency_pairs: pd.DataFrame,
    unit_features: pd.DataFrame,
    neighbor_category_flag: str | Collection[str] | Callable[[str], bool],
    exclude_flag_category: bool = True,
    min_shared_m: float = 0.0,
) -> AdjacencyEffect:
    """Is having an occupied neighbor of the flagged category associated with higher sales per m2?

    Treated units share a boundary with an occupied unit whose category is flagged (units of the flagged
    category itself are excluded by default). The naive estimate is the difference in mean
    log(sales per m2); the controlled estimate is the treatment coefficient of an OLS that adds
    log(walk_to_escalator_m + 1), frontage_m, log(area_m2) and floor/category dummies when the sample
    allows. ``confounding_suspected`` is True when exposure is imbalanced (|SMD| >= 0.5), when controls
    remove at least half of a non-trivial naive effect, or when the sign flips.
    """
    is_flagged, flag_list = _category_predicate(neighbor_category_flag)
    feats = unit_features.drop_duplicates("unit_code").set_index("unit_code")
    vacant = to_bool(feats["vacant"], feats.index) if "vacant" in feats else feats["brand_id"].isna()
    occupied_category = feats.loc[~vacant, "category"]
    pairs = adjacency_pairs
    if min_shared_m > 0 and "shared_length_m" in pairs:
        pairs = pairs.loc[pairs["shared_length_m"] >= min_shared_m]
    treated: set[str] = set()
    for a, b in zip(pairs["unit_a"].astype(str), pairs["unit_b"].astype(str), strict=True):
        if is_flagged(occupied_category.get(b)):
            treated.add(a)
        if is_flagged(occupied_category.get(a)):
            treated.add(b)
    data = productivity[["unit_code", "sales_per_m2", "floor", "category"]].copy()
    data["unit_code"] = data["unit_code"].astype(str)
    columns = ["walk_to_escalator_m", "frontage_m", "area_m2"]
    data = data.join(feats[columns], on="unit_code")
    valid = (data["sales_per_m2"] > 0) & data[columns].notna().all(axis=1) & (data["area_m2"] > 0)
    if exclude_flag_category:
        valid &= ~data["category"].map(is_flagged).astype(bool)
    sample = data.loc[valid].reset_index(drop=True)
    sample["treated"] = sample["unit_code"].isin(treated)
    sample["log_sales_per_m2"] = np.log(sample["sales_per_m2"].astype(float))
    sample["log_walk"] = np.log1p(sample["walk_to_escalator_m"].astype(float))
    n_treated, n_control = int(sample["treated"].sum()), int((~sample["treated"]).sum())
    names = flag_list or ["(사용자 정의 조건)"]
    if n_treated < 2 or n_control < 2:
        return AdjacencyEffect(
            neighbor_categories=names,
            n_treated=n_treated,
            n_control=n_control,
            naive_log_diff=math.nan,
            naive_uplift_pct=math.nan,
            controlled_log_coef=math.nan,
            controlled_std_err=math.nan,
            controlled_uplift_pct=math.nan,
            controlled_ci_pct=(math.nan, math.nan),
            exposure_smd=math.nan,
            confounding_suspected=False,
            reasons=["처리군 또는 비교군 표본이 2개 미만이라 추정하지 않았습니다."],
            controls=[],
            caveat=ADJACENCY_CAVEAT,
            table=sample,
        )
    y = sample["log_sales_per_m2"].to_numpy()
    t = sample["treated"].to_numpy(dtype=float)
    naive = float(y[t == 1].mean() - y[t == 0].mean())
    base = {
        "log_walk": sample["log_walk"].to_numpy(),
        "frontage_m": sample["frontage_m"].to_numpy(dtype=float),
        "log_area": np.log(sample["area_m2"].to_numpy(dtype=float)),
    }
    dummy_sets: list[dict[str, np.ndarray]] = []
    for column, prefix in (("floor", "floor"), ("category", "cat")):
        levels = sorted(sample[column].dropna().astype(str).unique())
        values = sample[column].astype(str)
        dummy_sets.append({f"{prefix}:{lv}": (values == lv).to_numpy(dtype=float) for lv in levels[1:]})
    n = len(sample)
    while dummy_sets and n - (2 + len(base) + sum(len(d) for d in dummy_sets)) < 3:
        dummy_sets.pop()
    dummies = {k: v for d in dummy_sets for k, v in d.items()}
    terms = ["intercept", "treated", *base, *dummies]
    x = np.column_stack([np.ones(n), t, *base.values(), *dummies.values()])
    fit = ols(x, y, terms)
    coef, se = fit.coefficient("treated"), fit.std_error("treated")
    lw_t, lw_c = base["log_walk"][t == 1], base["log_walk"][t == 0]
    pooled = math.sqrt((lw_t.var(ddof=1) + lw_c.var(ddof=1)) / 2) if n_treated > 1 and n_control > 1 else 0.0
    smd = float((lw_t.mean() - lw_c.mean()) / pooled) if pooled > 0 else 0.0
    reasons: list[str] = []
    if abs(smd) >= SMD_THRESHOLD:
        closer = "가깝습니다" if smd < 0 else "멉니다"
        reasons.append(
            f"처리군이 비교군보다 에스컬레이터에 뚜렷하게 {closer}(보행거리 로그의 표준화 차이 {smd:+.2f}). "
            "노출도가 교란 요인일 수 있습니다."
        )
    if abs(naive) >= MIN_EFFECT_LOG and abs(naive - coef) >= SHRINK_THRESHOLD * abs(naive):
        reasons.append(
            f"노출도 등을 통제하면 효과가 단순 비교 {_pct(naive):+.1f}%에서 {_pct(coef):+.1f}%로 크게 달라집니다."
        )
    if abs(naive) >= MIN_EFFECT_LOG and np.sign(naive) != np.sign(coef):
        reasons.append("통제 후 효과의 방향이 단순 비교와 반대입니다.")
    flagged_ko = ", ".join(category_name(c) for c in flag_list) or "지정 조건"
    caveat = (
        f"[{ESTIMATE_LABEL}] {flagged_ko} 인접 매장의 면적당 매출 차이: 단순 비교 {_pct(naive):+.1f}%, "
        f"노출도 통제 후 {_pct(coef):+.1f}%(95% 구간 {_pct(coef - 1.96 * se):+.1f}%~{_pct(coef + 1.96 * se):+.1f}%). "
        + ("교란 가능성이 있습니다. " + " ".join(reasons) + " " if reasons else "")
        + ADJACENCY_CAVEAT
    )
    return AdjacencyEffect(
        neighbor_categories=names,
        n_treated=n_treated,
        n_control=n_control,
        naive_log_diff=naive,
        naive_uplift_pct=_pct(naive),
        controlled_log_coef=coef,
        controlled_std_err=se,
        controlled_uplift_pct=_pct(coef),
        controlled_ci_pct=(_pct(coef - 1.96 * se), _pct(coef + 1.96 * se)),
        exposure_smd=smd,
        confounding_suspected=bool(reasons),
        reasons=reasons,
        controls=terms[2:],
        caveat=caveat,
        table=sample,
    )


@dataclass
class BeforeAfter:
    """Daily weighted sales before vs after a change for selected units (or brands) and a comparison group."""

    change_date: str
    window_days: int
    key: str
    pre_days: int
    post_days: int
    units: pd.DataFrame
    treated_change_pct: float
    control_change_pct: float
    difference_pct: float
    caveat: str = BEFORE_AFTER_CAVEAT
    notes: list[str] = field(default_factory=list)
    label: str = ESTIMATE_LABEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "change_date": self.change_date,
            "window_days": self.window_days,
            "key": self.key,
            "pre_days": self.pre_days,
            "post_days": self.post_days,
            "units": self.units.to_dict(orient="records"),
            "treated_change_pct": self.treated_change_pct,
            "control_change_pct": self.control_change_pct,
            "difference_pct": self.difference_pct,
            "caveat": self.caveat,
            "notes": self.notes,
        }


def _change_pct(pre_daily: float, post_daily: float) -> float:
    return (post_daily / pre_daily - 1.0) * 100.0 if pre_daily > 0 else math.nan


def before_after(
    enriched: pd.DataFrame,
    change_date: Any,
    unit_codes: Iterable[str],
    window_days: int = 90,
    key: str = "unit_code",
    control: str = "same_floor",
) -> BeforeAfter:
    """Compare daily weighted net sales in ``window_days`` before and after ``change_date``.

    ``unit_codes`` are values of ``key`` (``unit_code`` or ``brand_id``). The comparison group is every
    other key on the treated floors (``control="same_floor"``) or in the store (``"all"``). Day counts are
    clipped to the data range so partial windows are averaged correctly.
    """
    if key not in ("unit_code", "brand_id"):
        raise ValueError("key must be unit_code or brand_id")
    change = pd.Timestamp(change_date).normalize()
    window = pd.Timedelta(days=int(window_days))
    ts = to_datetime(enriched["ts"])
    data_start, data_end = ts.min().normalize(), ts.max().normalize() + pd.Timedelta(days=1)

    def overlap_days(lo: pd.Timestamp, hi: pd.Timestamp) -> int:
        return max(0, (min(hi, data_end) - max(lo, data_start)).days)

    pre_days, post_days = overlap_days(change - window, change), overlap_days(change, change + window)
    mask = (ts >= change - window) & (ts < change + window) & enriched[key].notna()
    tx = enriched.loc[mask]
    frame = pd.DataFrame(
        {
            "id": tx[key].astype(str),
            "floor": tx["floor"],
            "period": np.where(ts[mask] < change, "pre", "post"),
            "sales": weighted_sales(tx),
        }
    )
    treated = {str(code) for code in unit_codes}
    table = frame.pivot_table(index="id", columns="period", values="sales", aggfunc="sum", fill_value=0.0)
    table = table.reindex(columns=["pre", "post"], fill_value=0.0).reindex(sorted(set(table.index) | treated))
    table = table.fillna(0.0)
    table.columns = ["pre_sales", "post_sales"]
    table["pre_daily"] = table["pre_sales"] / pre_days if pre_days else math.nan
    table["post_daily"] = table["post_sales"] / post_days if post_days else math.nan
    table["change_pct"] = [
        _change_pct(a, b) for a, b in zip(table["pre_daily"], table["post_daily"], strict=True)
    ]
    table["treated"] = table.index.isin(treated)
    floors = set(frame.loc[frame["id"].isin(treated), "floor"].dropna())
    floor_of = frame.dropna(subset=["floor"]).groupby("id")["floor"].agg(lambda s: set(s))
    if control == "same_floor":
        in_scope = [bool(floor_of.get(i, set()) & floors) for i in table.index]
    elif control == "all":
        in_scope = [True] * len(table)
    else:
        raise ValueError("control must be same_floor or all")
    table["control"] = ~table["treated"] & np.array(in_scope, dtype=bool)
    units = table.reset_index().rename(columns={"index": key, "id": key})

    def group_change(selector: pd.Series) -> float:
        part = table.loc[selector]
        if not len(part) or not pre_days or not post_days:
            return math.nan
        return _change_pct(part["pre_sales"].sum() / pre_days, part["post_sales"].sum() / post_days)

    treated_change = group_change(table["treated"])
    control_change = group_change(table["control"])
    notes = []
    if pre_days < window_days or post_days < window_days:
        notes.append(f"데이터 기간이 짧아 전후 일수를 {pre_days}일/{post_days}일로 맞췄습니다.")
    if not table["control"].any():
        notes.append("비교군이 없어 차이를 계산하지 않았습니다.")
    caveat = (
        f"[{ESTIMATE_LABEL}] {change.date()} 기준 전후 {window_days}일 일평균 매출 변화: 대상 {treated_change:+.1f}%, "
        f"비교군 {control_change:+.1f}%. {BEFORE_AFTER_CAVEAT}"
    )
    return BeforeAfter(
        change_date=str(change.date()),
        window_days=int(window_days),
        key=key,
        pre_days=pre_days,
        post_days=post_days,
        units=units,
        treated_change_pct=treated_change,
        control_change_pct=control_change,
        difference_pct=treated_change - control_change,
        caveat=caveat,
        notes=notes,
    )
