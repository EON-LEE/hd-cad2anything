"""Transparent what-if scoring of brand-to-unit assignments. Every output is an estimate (추정치).

Expected sales of brand ``b`` in unit ``u`` (Huff-like attractiveness)::

    S(b, u) = B_b * A_u ** beta_c * (W_u + 1) ** -gamma * (1 + bonus_u)

- ``B_b``: brand base strength. For brands with observed sales it is backed out of the sales at the
  brand's reference unit, so the observed layout reproduces observed sales. Other brands get the
  category's mean base scaled by their relative ``attractiveness`` (a prior, wider range).
- ``beta_c``: category space elasticity: catalog ``space_beta`` prior (sd ``prior_sd``) blended with the
  within-category cross-section slope by precision (normal-normal posterior mean,
  ``w = prior_sd**2 / (prior_sd**2 + se**2)``), so noisy slopes from a few units barely move the prior.
- ``gamma``: exposure elasticity w.r.t. walking distance to the nearest escalator, from a pooled OLS of
  log(sales) on category intercepts, log(area) and log(walk + 1). It is biased when brand strength is
  correlated with location (e.g. strong brands placed near escalators), which is a key caveat.
- ``bonus_u = min(cap, lambda * sum over adjacent occupied units of max(lift(b, b_v) - 1, 0))`` with
  cross-shopping lift from ``analytics.affinity.cross_shopping``; ``lambda`` is an assumption, not fitted.

Ranges: brands at their reference unit have no range (observed). Moved brands get ``exp(+/- h)`` with
``h = sqrt(resid_std**2 + (d_log_area * se_beta)**2 + (d_log_walk * se_gamma)**2)``; prior-based brands
use ``h = sqrt(2) * resid_std``. Totals combine unit ranges in quadrature (independent errors).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.analytics.kpi import ols
from cad2anything.analytics.prep import as_list, category_name, floor_sort_key, to_bool
from cad2anything.catalog import CATEGORIES

ESTIMATE_LABEL = "추정치"
DEFAULT_AFFINITY_LAMBDA = 0.02
DEFAULT_AFFINITY_CAP = 0.15
DEFAULT_GAMMA = 0.3
PRIOR_BETA_SD = 0.1
MIN_BETA_SE = 0.01
GAMMA_BOUNDS = (0.0, 1.5)
BETA_BOUNDS = (0.0, 1.0)
SCORING_CAVEATS = (
    "기대 매출은 단면 데이터로 추정한 투명 점수 모델의 추정치이며 실제 매출을 보장하지 않습니다.",
    "노출·면적 탄력성은 관측 데이터의 연관성에서 추정했으며 인과 효과가 아닙니다. 강한 브랜드가 좋은 "
    "위치에 몰려 있으면 노출 탄력성이 과대 추정될 수 있습니다.",
    "인접 친화도 보너스의 강도(lambda)는 가정값이고, 교차구매 lift는 현재 배치의 영향을 이미 포함합니다.",
)


def format_krw(value: float) -> str:
    """Korean money format: ``1.23억원``, ``4,560만원`` or ``7,800원`` (sign kept)."""
    if value is None or not np.isfinite(value):
        return "-"
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1e8:
        return f"{sign}{amount / 1e8:,.2f}억원"
    if amount >= 1e4:
        return f"{sign}{amount / 1e4:,.0f}만원"
    return f"{sign}{amount:,.0f}원"


def format_krw_delta(value: float) -> str:
    return ("+" if value >= 0 else "") + format_krw(value)


def normalize_assignment(assignment: Mapping[str, Any]) -> dict[str, str | None]:
    """Unit -> brand mapping with missing values (None, NaN, NA, empty string) as None."""
    result: dict[str, str | None] = {}
    for unit_code, brand_id in assignment.items():
        missing = brand_id is None or (not isinstance(brand_id, str) and bool(pd.isna(brand_id)))
        result[str(unit_code)] = None if missing or not str(brand_id).strip() else str(brand_id)
    return result


def assignment_from_features(unit_features: pd.DataFrame) -> dict[str, str | None]:
    """Current layout (unit -> brand, vacant units None) from a ``unit_features`` table."""
    vacant = (
        to_bool(unit_features["vacant"], unit_features.index)
        if "vacant" in unit_features
        else pd.Series(False, index=unit_features.index)
    )
    brands = unit_features["brand_id"].where(~vacant)
    return normalize_assignment(dict(zip(unit_features["unit_code"], brands, strict=True)))


@dataclass(frozen=True)
class BrandInfo:
    brand_id: str
    brand_name: str
    category: str | None
    anchor: bool = False
    target_area_min_m2: float = math.nan
    target_area_max_m2: float = math.nan
    preferred_floors: tuple[str, ...] = ()
    adjacency_conflicts: tuple[str, ...] = ()
    attractiveness: float = 1.0


def brand_info(brands: pd.DataFrame | Mapping[str, BrandInfo]) -> dict[str, BrandInfo]:
    """Normalize the brands table (``;``-joined or list fields) into ``BrandInfo`` records."""
    if isinstance(brands, Mapping):
        return dict(brands)
    table = brands.drop_duplicates("brand_id")
    anchors = (
        to_bool(table["anchor"], table.index) if "anchor" in table else pd.Series(False, index=table.index)
    )
    info: dict[str, BrandInfo] = {}
    for idx, row in table.iterrows():
        raw_attr = pd.to_numeric(row.get("attractiveness"), errors="coerce")
        info[str(row["brand_id"])] = BrandInfo(
            brand_id=str(row["brand_id"]),
            brand_name=str(row.get("brand_name") or row["brand_id"]),
            category=row.get("category") if isinstance(row.get("category"), str) else None,
            anchor=bool(anchors.loc[idx]),
            target_area_min_m2=float(pd.to_numeric(row.get("target_area_min_m2"), errors="coerce")),
            target_area_max_m2=float(pd.to_numeric(row.get("target_area_max_m2"), errors="coerce")),
            preferred_floors=tuple(as_list(row.get("preferred_floors"))),
            adjacency_conflicts=tuple(as_list(row.get("adjacency_conflicts"))),
            attractiveness=float(raw_attr) if pd.notna(raw_attr) and raw_attr > 0 else 1.0,
        )
    return info


@dataclass(frozen=True)
class UnitInfo:
    unit_code: str
    floor: str
    area_m2: float
    walk_m: float
    walk_imputed: bool


def unit_info(unit_features: pd.DataFrame) -> dict[str, UnitInfo]:
    """Unit geometry facts; missing walking distances use the floor median (then the store median)."""
    feats = unit_features.drop_duplicates("unit_code").copy()
    feats["area_m2"] = pd.to_numeric(feats["area_m2"], errors="coerce")
    feats = feats.loc[feats["area_m2"] > 0]
    walk = pd.to_numeric(feats["walk_to_escalator_m"], errors="coerce")
    floor_median = walk.groupby(feats["floor"]).transform("median")
    store_median = float(walk.median()) if walk.notna().any() else 0.0
    filled = walk.fillna(floor_median).fillna(store_median)
    return {
        str(row.unit_code): UnitInfo(
            unit_code=str(row.unit_code),
            floor=str(row.floor),
            area_m2=float(row.area_m2),
            walk_m=float(filled.loc[idx]),
            walk_imputed=bool(pd.isna(walk.loc[idx])),
        )
        for idx, row in zip(feats.index, feats.itertuples(index=False), strict=True)
    }


@dataclass
class BrandReference:
    unit_code: str
    sales: float
    area_m2: float
    walk_m: float


@dataclass
class ScoringModel:
    """Fitted parameters of the transparent scoring model (all 추정치)."""

    gamma: float
    gamma_se: float
    gamma_data: float
    beta: dict[str, float]
    beta_se: dict[str, float]
    beta_prior: dict[str, float]
    beta_data: dict[str, float]
    beta_weight: dict[str, float]
    brand_log_base: dict[str, float]
    brand_reference: dict[str, BrandReference]
    category_log_base: dict[str, float]
    category_attractiveness: dict[str, float]
    reference_assignment: dict[str, str | None]
    resid_std: float
    r2: float
    n_obs: int
    affinity_lambda: float = DEFAULT_AFFINITY_LAMBDA
    affinity_cap: float = DEFAULT_AFFINITY_CAP
    notes: list[str] = field(default_factory=list)
    label: str = ESTIMATE_LABEL

    def beta_for(self, category: str | None) -> float:
        if category in self.beta:
            return self.beta[category]
        if category in CATEGORIES:
            return CATEGORIES[category].space_beta
        return float(np.mean([c.space_beta for c in CATEGORIES.values()]))

    def beta_se_for(self, category: str | None) -> float:
        return self.beta_se.get(category, PRIOR_BETA_SD) if category else PRIOR_BETA_SD

    def prior_log_base(self, brand: BrandInfo) -> float:
        """Category mean base scaled by the brand's attractiveness relative to its category."""
        values = list(self.category_log_base.values())
        base = self.category_log_base.get(brand.category, float(np.mean(values)) if values else 0.0)
        mean_attr = self.category_attractiveness.get(brand.category, 1.0)
        return base + math.log(brand.attractiveness / mean_attr) if mean_attr > 0 else base

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["brand_reference"] = {k: asdict(v) for k, v in self.brand_reference.items()}
        return data


def fit_scoring_model(
    productivity: pd.DataFrame,
    unit_features: pd.DataFrame,
    brands: pd.DataFrame,
    prior_sd: float = PRIOR_BETA_SD,
    affinity_lambda: float = DEFAULT_AFFINITY_LAMBDA,
    affinity_cap: float = DEFAULT_AFFINITY_CAP,
) -> ScoringModel:
    """Fit brand bases, category space elasticities and the exposure elasticity from unit productivity.

    ``productivity`` is the unit-level output of ``space_productivity`` (``unit_code``, ``brand_id``,
    ``sales``). One observation per brand (its highest-sales unit) enters the cross-section. ``prior_sd``
    is the assumed uncertainty of the catalog ``space_beta`` prior; ``affinity_lambda``/``affinity_cap``
    are assumptions stored with the parameters.
    """
    infos = brand_info(brands)
    units = unit_info(unit_features)
    notes: list[str] = []
    df = productivity[["unit_code", "brand_id", "sales"]].dropna(subset=["brand_id"]).copy()
    df["unit_code"] = df["unit_code"].astype(str)
    df["brand_id"] = df["brand_id"].astype(str)
    df["category"] = df["brand_id"].map(lambda b: infos[b].category if b in infos else None)
    if "category" in productivity:
        df["category"] = df["category"].fillna(productivity.loc[df.index, "category"])
    df["area_m2"] = df["unit_code"].map(lambda u: units[u].area_m2 if u in units else np.nan)
    df["walk_m"] = df["unit_code"].map(lambda u: units[u].walk_m if u in units else np.nan)
    valid = (df["sales"] > 0) & (df["area_m2"] > 0) & df["walk_m"].notna() & df["category"].notna()
    if (~valid).any():
        notes.append(
            f"매출·면적·거리 정보가 없는 {int((~valid).sum())}개 브랜드-매장 행은 추정에서 제외했습니다."
        )
    sample = (
        df.loc[valid]
        .sort_values(["brand_id", "sales", "unit_code"], ascending=[True, False, True], kind="mergesort")
        .drop_duplicates("brand_id")
        .reset_index(drop=True)
    )
    y = np.log(sample["sales"].to_numpy(dtype=float))
    la = np.log(sample["area_m2"].to_numpy(dtype=float))
    lw = np.log1p(sample["walk_m"].to_numpy(dtype=float))
    categories = sorted(sample["category"].unique())
    gamma_data, gamma_se, resid_std, r2 = math.nan, math.nan, math.nan, math.nan
    if len(sample) >= len(categories) + 4:
        dummies = [(sample["category"] == c).to_numpy(dtype=float) for c in categories]
        fit = ols(np.column_stack([*dummies, la, lw]), y, [*categories, "log_area", "log_walk"])
        gamma_data, gamma_se = -fit.coefficient("log_walk"), fit.std_error("log_walk")
        resid_std, r2 = fit.resid_std, fit.r2
    else:
        notes.append(f"표본이 작아 노출 탄력성에 가정값 {DEFAULT_GAMMA}를 사용했습니다.")
    gamma = float(np.clip(gamma_data, *GAMMA_BOUNDS)) if np.isfinite(gamma_data) else DEFAULT_GAMMA
    if np.isfinite(gamma_data) and gamma != gamma_data:
        notes.append(f"노출 탄력성 추정값 {gamma_data:.3f}을 허용 범위 {GAMMA_BOUNDS}로 제한했습니다.")
    if not np.isfinite(gamma_se):
        gamma_se = 0.5 * max(gamma, 0.1)
    if not np.isfinite(resid_std) or resid_std <= 0:
        resid_std = 0.3
        notes.append("잔차 표준편차를 추정할 수 없어 가정값 0.3(로그)을 사용했습니다.")

    known = sorted({i.category for i in infos.values() if i.category} | set(categories))
    beta, beta_se, beta_prior, beta_data, beta_weight = {}, {}, {}, {}, {}
    prior_mean = float(np.mean([c.space_beta for c in CATEGORIES.values()]))
    for category in known:
        prior = CATEGORIES[category].space_beta if category in CATEGORIES else prior_mean
        mask = (sample["category"] == category).to_numpy()
        n_c = int(mask.sum())
        beta_prior[category] = prior
        beta_data[category] = math.nan
        weight, posterior_se = 0.0, prior_sd
        if n_c >= 3 and float(np.std(la[mask])) > 0.05:
            fit_c = ols(
                np.column_stack([np.ones(n_c), la[mask]]),
                y[mask] + gamma * lw[mask],
                ["intercept", "log_area"],
            )
            beta_data[category] = float(np.clip(fit_c.coefficient("log_area"), -0.5, 1.5))
            se_data = max(fit_c.std_error("log_area"), MIN_BETA_SE)
            weight = prior_sd**2 / (prior_sd**2 + se_data**2)
            posterior_se = math.sqrt(weight) * se_data
        blended = weight * beta_data[category] + (1 - weight) * prior if weight > 0 else prior
        beta[category] = float(np.clip(blended, *BETA_BOUNDS))
        beta_weight[category] = weight
        beta_se[category] = posterior_se

    log_base: dict[str, float] = {}
    references: dict[str, BrandReference] = {}
    by_category: dict[str, list[float]] = defaultdict(list)
    for row, y_i, la_i, lw_i in zip(sample.itertuples(index=False), y, la, lw, strict=True):
        log_base[row.brand_id] = float(y_i - beta[row.category] * la_i + gamma * lw_i)
        references[row.brand_id] = BrandReference(
            row.unit_code, float(row.sales), float(row.area_m2), float(row.walk_m)
        )
        by_category[row.category].append(log_base[row.brand_id])
    category_log_base = {c: float(np.mean(v)) for c, v in sorted(by_category.items())}
    attractiveness: dict[str, list[float]] = defaultdict(list)
    for info in infos.values():
        attractiveness[info.category].append(info.attractiveness)
    category_attr = {c: float(np.mean(v)) for c, v in attractiveness.items() if c}

    reference_assignment: dict[str, str | None] = dict.fromkeys(units)
    for unit_code, brand_id in zip(df["unit_code"], df["brand_id"], strict=True):
        if brand_id in references:
            if references[brand_id].unit_code == unit_code:
                reference_assignment[unit_code] = brand_id
        elif reference_assignment.get(unit_code) is None:
            reference_assignment[unit_code] = brand_id
    return ScoringModel(
        gamma=gamma,
        gamma_se=float(gamma_se),
        gamma_data=float(gamma_data),
        beta=beta,
        beta_se=beta_se,
        beta_prior=beta_prior,
        beta_data=beta_data,
        beta_weight=beta_weight,
        brand_log_base=log_base,
        brand_reference=references,
        category_log_base=category_log_base,
        category_attractiveness=category_attr,
        reference_assignment=reference_assignment,
        resid_std=float(resid_std),
        r2=float(r2),
        n_obs=len(sample),
        affinity_lambda=affinity_lambda,
        affinity_cap=affinity_cap,
        notes=notes,
    )


# --- scoring -----------------------------------------------------------------------------------------


@dataclass
class AssignmentScore:
    """Per-unit expected sales with ranges and the totals (추정치)."""

    units: pd.DataFrame
    total: float
    total_low: float
    total_high: float
    label: str = ESTIMATE_LABEL
    caveats: list[str] = field(default_factory=lambda: list(SCORING_CAVEATS))

    def by_floor(self) -> pd.DataFrame:
        """Expected sales, ranges and sales per occupied m2 by floor."""
        units = self.units.assign(
            _dev_low=(self.units["expected_sales"] - self.units["low"]) ** 2,
            _dev_high=(self.units["high"] - self.units["expected_sales"]) ** 2,
            _occupied_area=self.units["area_m2"].where(self.units["brand_id"].notna(), 0.0),
        )
        table = units.groupby("floor").agg(
            expected_sales=("expected_sales", "sum"),
            dev_low=("_dev_low", "sum"),
            dev_high=("_dev_high", "sum"),
            occupied_area_m2=("_occupied_area", "sum"),
        )
        table["low"] = table["expected_sales"] - np.sqrt(table.pop("dev_low"))
        table["high"] = table["expected_sales"] + np.sqrt(table.pop("dev_high"))
        table["sales_per_m2"] = table["expected_sales"] / table["occupied_area_m2"].where(
            table["occupied_area_m2"] > 0
        )
        table = table.reset_index()
        return table.sort_values("floor", key=lambda s: s.map(floor_sort_key)).reset_index(drop=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "total": self.total,
            "total_low": self.total_low,
            "total_high": self.total_high,
            "units": self.units.to_dict(orient="records"),
            "caveats": self.caveats,
        }


def _lift_table(affinity_pairs: pd.DataFrame | None) -> tuple[str | None, dict[tuple[str, str], float]]:
    if affinity_pairs is None or not len(affinity_pairs):
        return None, {}
    level = affinity_pairs.attrs.get("level")
    if "level" in affinity_pairs and affinity_pairs["level"].notna().any():
        level = str(affinity_pairs["level"].dropna().iloc[0])
    level = level or "brand"
    lifts: dict[tuple[str, str], float] = {}
    for a, b, lift in zip(
        affinity_pairs["item_a"], affinity_pairs["item_b"], affinity_pairs["lift"], strict=True
    ):
        if pd.notna(lift):
            lifts[tuple(sorted((str(a), str(b))))] = float(lift)
    return level, lifts


class AssignmentScorer:
    """Precomputed scorer for one parameter set; shared by ``score_assignment`` and the layout engine."""

    def __init__(
        self,
        unit_features: pd.DataFrame,
        brands: pd.DataFrame | Mapping[str, BrandInfo],
        model: ScoringModel,
        affinity_pairs: pd.DataFrame | None = None,
        adjacency_pairs: pd.DataFrame | None = None,
    ) -> None:
        self.model = model
        self.brands = brand_info(brands)
        self.units = unit_info(unit_features)
        self.neighbors: dict[str, tuple[str, ...]] = {}
        if adjacency_pairs is not None and len(adjacency_pairs):
            links: dict[str, set[str]] = defaultdict(set)
            for a, b in zip(
                adjacency_pairs["unit_a"].astype(str), adjacency_pairs["unit_b"].astype(str), strict=True
            ):
                links[a].add(b)
                links[b].add(a)
            self.neighbors = {k: tuple(sorted(v)) for k, v in links.items()}
        self.lift_level, self._lifts = _lift_table(affinity_pairs)
        self.has_affinity = bool(self._lifts) and bool(self.neighbors)
        self._log_base: dict[str, float] = {}
        for brand_id, info in self.brands.items():
            if brand_id in model.brand_reference:
                ref = model.brand_reference[brand_id]
                ref_bonus = self.bonus(brand_id, ref.unit_code, model.reference_assignment)
                self._log_base[brand_id] = model.brand_log_base[brand_id] - math.log1p(ref_bonus)
            else:
                self._log_base[brand_id] = model.prior_log_base(info)

    def category_of(self, brand_id: str | None) -> str | None:
        return self.brands[brand_id].category if brand_id in self.brands else None

    def excess_lift(self, brand_a: str, brand_b: str) -> float:
        """``max(lift - 1, 0)`` between two brands (brand- or category-level pairs; 0 when unknown)."""
        if not self._lifts or brand_a == brand_b:
            return 0.0
        if self.lift_level == "category":
            key_a, key_b = self.category_of(brand_a), self.category_of(brand_b)
            if key_a is None or key_b is None or key_a == key_b:
                return 0.0
        else:
            key_a, key_b = brand_a, brand_b
        return max(self._lifts.get(tuple(sorted((key_a, key_b))), 1.0) - 1.0, 0.0)

    def bonus(self, brand_id: str, unit_code: str, assignment: Mapping[str, str | None]) -> float:
        if not self._lifts:
            return 0.0
        total = sum(
            self.excess_lift(brand_id, other)
            for other in (assignment.get(v) for v in self.neighbors.get(unit_code, ()))
            if other is not None
        )
        return min(self.model.affinity_cap, self.model.affinity_lambda * total)

    def log_base(self, brand_id: str) -> float:
        return self._log_base[brand_id]

    def location_terms(self, brand_id: str, unit_code: str) -> tuple[float, float, float]:
        """(log base, area term ``beta*ln A``, exposure term ``-gamma*ln(W+1)``)."""
        unit = self.units[unit_code]
        beta = self.model.beta_for(self.category_of(brand_id))
        return (
            self._log_base[brand_id],
            beta * math.log(unit.area_m2),
            -self.model.gamma * math.log1p(unit.walk_m),
        )

    def location_value(self, brand_id: str, unit_code: str) -> float:
        """Expected sales without the affinity multiplier."""
        return math.exp(sum(self.location_terms(brand_id, unit_code)))

    def half_width(self, brand_id: str, unit_code: str) -> float:
        """Log half-width of the range (0 at the brand's observed reference unit)."""
        ref = self.model.brand_reference.get(brand_id)
        if ref is None:
            return math.sqrt(2.0) * self.model.resid_std
        if ref.unit_code == unit_code:
            return 0.0
        unit = self.units[unit_code]
        se_beta = self.model.beta_se_for(self.category_of(brand_id))
        d_area = math.log(unit.area_m2) - math.log(ref.area_m2)
        d_walk = math.log1p(unit.walk_m) - math.log1p(ref.walk_m)
        return math.sqrt(
            self.model.resid_std**2 + (d_area * se_beta) ** 2 + (d_walk * self.model.gamma_se) ** 2
        )

    def score(self, assignment: Mapping[str, str | None]) -> AssignmentScore:
        assignment = normalize_assignment(assignment)
        rows: list[dict[str, Any]] = []
        skipped: list[str] = []
        for unit_code in sorted(
            assignment, key=lambda u: (floor_sort_key(self.units[u].floor if u in self.units else ""), u)
        ):
            if unit_code not in self.units:
                skipped.append(unit_code)
                continue
            unit = self.units[unit_code]
            brand_id = assignment[unit_code]
            row: dict[str, Any] = {
                "unit_code": unit_code,
                "floor": unit.floor,
                "brand_id": None,
                "brand_name": None,
                "category": None,
                "area_m2": unit.area_m2,
                "walk_to_escalator_m": unit.walk_m,
                "expected_sales": 0.0,
                "low": 0.0,
                "high": 0.0,
                "log_base": math.nan,
                "area_effect": math.nan,
                "exposure_effect": math.nan,
                "affinity_bonus": 0.0,
                "affinity_effect": 0.0,
                "half_width_log": 0.0,
                "observed_reference": False,
                "moved_from_reference": False,
            }
            if brand_id is not None and brand_id in self.brands:
                info = self.brands[brand_id]
                base, area_term, exposure_term = self.location_terms(brand_id, unit_code)
                bonus = self.bonus(brand_id, unit_code, assignment)
                expected = math.exp(base + area_term + exposure_term) * (1.0 + bonus)
                half = self.half_width(brand_id, unit_code)
                ref = self.model.brand_reference.get(brand_id)
                row.update(
                    {
                        "brand_id": brand_id,
                        "brand_name": info.brand_name,
                        "category": info.category,
                        "expected_sales": expected,
                        "low": expected * math.exp(-half),
                        "high": expected * math.exp(half),
                        "log_base": base,
                        "area_effect": area_term,
                        "exposure_effect": exposure_term,
                        "affinity_bonus": bonus,
                        "affinity_effect": math.log1p(bonus),
                        "half_width_log": half,
                        "observed_reference": ref is not None,
                        "moved_from_reference": ref is not None and ref.unit_code != unit_code,
                    }
                )
            elif brand_id is not None:
                skipped.append(f"{unit_code}:{brand_id}")
            rows.append(row)
        units = pd.DataFrame(rows)
        total = float(units["expected_sales"].sum()) if len(units) else 0.0
        low = (
            total - math.sqrt(float(((units["expected_sales"] - units["low"]) ** 2).sum()))
            if len(units)
            else 0.0
        )
        high = (
            total + math.sqrt(float(((units["high"] - units["expected_sales"]) ** 2).sum()))
            if len(units)
            else 0.0
        )
        caveats = list(SCORING_CAVEATS)
        if skipped:
            caveats.append(f"형상 또는 브랜드 정보가 없어 점수에서 제외한 항목: {', '.join(skipped[:10])}")
        if not self.has_affinity:
            caveats.append("교차구매 lift 또는 인접 정보가 없어 인접 친화도 보너스를 0으로 두었습니다.")
        return AssignmentScore(units=units, total=total, total_low=low, total_high=high, caveats=caveats)


def score_assignment(
    assignment: Mapping[str, str | None],
    unit_features: pd.DataFrame,
    brands: pd.DataFrame | Mapping[str, BrandInfo],
    model_params: ScoringModel,
    affinity_pairs: pd.DataFrame | None = None,
    adjacency_pairs: pd.DataFrame | None = None,
) -> AssignmentScore:
    """Expected sales per unit (``None`` = vacant) with ranges, totals and log contributions (추정치).

    ``adjacency_pairs`` (from ``spatial.adjacency_pairs``) is needed for the affinity bonus; without it
    (or without ``affinity_pairs``) the bonus is 0.
    """
    return AssignmentScorer(unit_features, brands, model_params, affinity_pairs, adjacency_pairs).score(
        assignment
    )


def category_label(code: str | None) -> str:
    return category_name(code) if code else "-"
