"""Constraint-aware layout alternatives: constraint filter -> greedy -> simulated annealing (seeded).

The objective is the total expected sales (추정치) of the units in scope, computed with
``whatif.AssignmentScorer`` so that engine numbers and ``score_assignment`` agree. Neighborhoods are
"move a brand into a vacant unit" and "swap two brands". Brands may only go to units that pass the
constraint filter (anchors fixed, movable filter, area tolerance, preferred floors, cross-floor rule) or
back to their original unit; new adjacencies between conflicting categories and more than ``max_moves``
relocated brands are rejected. Later alternatives are penalized for repeating earlier relocations.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Collection, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.analytics.prep import floor_sort_key
from cad2anything.layout.whatif import (
    ESTIMATE_LABEL,
    SCORING_CAVEATS,
    AssignmentScore,
    AssignmentScorer,
    BrandInfo,
    ScoringModel,
    brand_info,
    category_label,
    format_krw,
    format_krw_delta,
    normalize_assignment,
)

CONSTRAINT_COLUMNS = ["constraint", "rule", "checked", "violations", "satisfied", "detail"]
ENGINE_CAVEATS = (
    *SCORING_CAVEATS,
    "대안은 제약 조건을 만족하는 배치 중 점수 모델 기준으로 탐색한 결과이며, 실행 전 현장 검토가 필요합니다.",
)


@dataclass
class LayoutConstraints:
    """Search scope and rules. ``area_tolerance`` widens the brand target area range by +/- that share."""

    floors: Collection[str] | None = None
    movable_brands: Collection[str] | None = None
    movable_categories: Collection[str] | None = None
    keep_anchors: bool = True
    area_tolerance: float = 0.25
    require_preferred_floor: bool = True
    forbid_conflicts: bool = True
    extra_conflicts: Collection[tuple[str, str]] = ()
    max_moves: int = 6
    allow_cross_floor: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.floors, str):
            self.floors = (self.floors,)
        if isinstance(self.movable_brands, str):
            self.movable_brands = (self.movable_brands,)
        if isinstance(self.movable_categories, str):
            self.movable_categories = (self.movable_categories,)
        if self.floors is not None:
            self.floors = tuple(sorted(set(self.floors), key=floor_sort_key))
        if self.movable_brands is not None:
            self.movable_brands = frozenset(self.movable_brands)
        if self.movable_categories is not None:
            self.movable_categories = frozenset(self.movable_categories)
        self.extra_conflicts = tuple(tuple(sorted(pair)) for pair in self.extra_conflicts)
        if self.area_tolerance < 0 or self.max_moves < 0:
            raise ValueError("area_tolerance and max_moves must be non-negative")

    def in_scope(self, floor: str | None) -> bool:
        return self.floors is None or floor in self.floors

    def passes_filter(self, info: BrandInfo) -> bool:
        if self.movable_brands is not None and info.brand_id not in self.movable_brands:
            return False
        return self.movable_categories is None or info.category in self.movable_categories

    def is_movable(self, info: BrandInfo) -> bool:
        return not (self.keep_anchors and info.anchor) and self.passes_filter(info)

    def area_band(self, info: BrandInfo) -> tuple[float, float]:
        lo = (
            info.target_area_min_m2 * (1 - self.area_tolerance)
            if np.isfinite(info.target_area_min_m2)
            else 0.0
        )
        hi = (
            info.target_area_max_m2 * (1 + self.area_tolerance)
            if np.isfinite(info.target_area_max_m2)
            else math.inf
        )
        return lo, hi

    def area_ok(self, info: BrandInfo, area_m2: float) -> bool:
        lo, hi = self.area_band(info)
        return lo - 1e-9 <= area_m2 <= hi + 1e-9

    def floor_ok(self, info: BrandInfo, floor: str) -> bool:
        return not self.require_preferred_floor or not info.preferred_floors or floor in info.preferred_floors

    def conflicting(self, a: BrandInfo, b: BrandInfo) -> bool:
        ca, cb = a.category, b.category
        if ca is None or cb is None:
            return False
        return (
            cb in a.adjacency_conflicts
            or ca in b.adjacency_conflicts
            or tuple(sorted((ca, cb))) in self.extra_conflicts
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "floors": list(self.floors) if self.floors is not None else None,
            "movable_brands": sorted(self.movable_brands) if self.movable_brands is not None else None,
            "movable_categories": sorted(self.movable_categories)
            if self.movable_categories is not None
            else None,
            "keep_anchors": self.keep_anchors,
            "area_tolerance": self.area_tolerance,
            "require_preferred_floor": self.require_preferred_floor,
            "forbid_conflicts": self.forbid_conflicts,
            "extra_conflicts": [list(p) for p in self.extra_conflicts],
            "max_moves": self.max_moves,
            "allow_cross_floor": self.allow_cross_floor,
        }


@dataclass
class Move:
    """One brand relocation with its estimated effect split into exposure, area and affinity (KRW)."""

    brand_id: str
    brand_name: str
    category: str | None
    from_unit: str
    to_unit: str
    from_floor: str
    to_floor: str
    expected_before: float
    expected_after: float
    delta: float
    delta_low: float
    delta_high: float
    exposure_krw: float
    area_krw: float
    affinity_krw: float
    walk_before_m: float
    walk_after_m: float
    area_before_m2: float
    area_after_m2: float
    explanation: str


@dataclass
class LayoutAlternative:
    """A constraint-satisfying layout alternative with estimated KPI deltas (추정치)."""

    alt_id: str
    seed: int
    assignment: dict[str, str | None]
    moves: list[Move]
    objective_before: float
    objective_after: float
    objective_low: float
    objective_high: float
    spillover_krw: float
    kpi_deltas: pd.DataFrame
    constraint_checks: pd.DataFrame
    summary: str
    explanations: list[str]
    label: str = ESTIMATE_LABEL
    caveats: list[str] = field(default_factory=lambda: list(ENGINE_CAVEATS))

    @property
    def delta(self) -> float:
        return self.objective_after - self.objective_before

    @property
    def all_constraints_satisfied(self) -> bool:
        return bool(self.constraint_checks["satisfied"].all())

    def moves_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(m) for m in self.moves], columns=list(Move.__dataclass_fields__))

    def to_dict(self) -> dict[str, Any]:
        return {
            "alt_id": self.alt_id,
            "label": self.label,
            "seed": self.seed,
            "summary": self.summary,
            "objective_before": self.objective_before,
            "objective_after": self.objective_after,
            "objective_low": self.objective_low,
            "objective_high": self.objective_high,
            "spillover_krw": self.spillover_krw,
            "moves": [asdict(m) for m in self.moves],
            "assignment": self.assignment,
            "kpi_deltas": self.kpi_deltas.to_dict(orient="records"),
            "constraint_checks": self.constraint_checks.to_dict(orient="records"),
            "explanations": self.explanations,
            "caveats": self.caveats,
        }


# --- independent constraint check -------------------------------------------------------------------


def check_constraints(
    base_assignment: Mapping[str, str | None],
    assignment: Mapping[str, str | None],
    unit_features: pd.DataFrame,
    brands: pd.DataFrame | Mapping[str, BrandInfo],
    adjacency_pairs: pd.DataFrame | None,
    constraints: LayoutConstraints,
) -> pd.DataFrame:
    """Verify an assignment against the constraints; one row per rule with violations listed in ``detail``.

    Rules on relocated brands only; existing placements are grandfathered (an adjacency conflict counts
    only when at least one of the two units changed brand).
    """
    infos = brand_info(brands)
    base_assignment = normalize_assignment(base_assignment)
    assignment = normalize_assignment(assignment)
    feats = unit_features.drop_duplicates("unit_code").set_index("unit_code")
    floor_of = feats["floor"].astype(str).to_dict()
    area_of = pd.to_numeric(feats["area_m2"], errors="coerce").to_dict()
    base_pos = {b: u for u, b in base_assignment.items() if b is not None}
    counts = Counter(b for b in assignment.values() if b is not None)
    new_pos = {b: u for u, b in assignment.items() if b is not None}
    moved = sorted(b for b, u in new_pos.items() if base_pos.get(b) != u)
    changed_units = sorted(
        u for u in set(base_assignment) | set(assignment) if base_assignment.get(u) != assignment.get(u)
    )
    rows: list[dict[str, Any]] = []

    def add(name: str, rule: str, checked: int, violations: list[str]) -> None:
        rows.append(
            {
                "constraint": name,
                "rule": rule,
                "checked": checked,
                "violations": len(violations),
                "satisfied": not violations,
                "detail": "; ".join(violations) if violations else "위반 없음",
            }
        )

    duplicated = [f"{b} 중복 배치" for b, n in sorted(counts.items()) if n > 1]
    dropped = [f"{b} 누락" for b in sorted(base_pos) if b not in new_pos]
    add("브랜드 유일성", "모든 기존 브랜드는 정확히 한 매장에 배치", len(base_pos), duplicated + dropped)
    add(
        "변경 범위",
        f"변경 매장은 대상 층({', '.join(constraints.floors) if constraints.floors else '전체'})에만 있어야 함",
        len(changed_units),
        [f"{u}({floor_of.get(u, '?')})" for u in changed_units if not constraints.in_scope(floor_of.get(u))],
    )
    anchors = [b for b in moved if b in infos and infos[b].anchor]
    add(
        "앵커 고정",
        "앵커 브랜드는 이동하지 않음" if constraints.keep_anchors else "앵커 이동 허용",
        len(moved),
        anchors if constraints.keep_anchors else [],
    )
    add(
        "이동 대상 필터",
        "이동한 브랜드는 이동 허용 브랜드·카테고리에 속함",
        len(moved),
        [b for b in moved if b not in infos or not constraints.passes_filter(infos[b])],
    )
    area_violations = []
    floor_violations = []
    cross_violations = []
    for b in moved:
        info, unit = infos.get(b), new_pos[b]
        if info is None:
            continue
        lo, hi = constraints.area_band(info)
        area = area_of.get(unit, math.nan)
        if not constraints.area_ok(info, area):
            area_violations.append(f"{b}→{unit} {area:.0f}㎡(허용 {lo:.0f}~{hi:.0f}㎡)")
        if not constraints.floor_ok(info, floor_of.get(unit, "")):
            floor_violations.append(
                f"{b}→{unit}({floor_of.get(unit, '?')}) 선호 층 {'/'.join(info.preferred_floors)}"
            )
        if not constraints.allow_cross_floor and floor_of.get(unit) != floor_of.get(
            base_pos.get(b, ""), None
        ):
            cross_violations.append(f"{b} {base_pos.get(b)}→{unit}")
    add(
        "면적 적합",
        f"목표 면적 범위 ±{constraints.area_tolerance:.0%} 안의 매장으로만 이동",
        len(moved),
        area_violations,
    )
    add(
        "선호 층",
        "선호 층이 있는 브랜드는 선호 층으로만 이동"
        if constraints.require_preferred_floor
        else "선호 층 미적용",
        len(moved),
        floor_violations,
    )
    add(
        "층간 이동",
        "층간 이동 허용" if constraints.allow_cross_floor else "같은 층 안에서만 이동",
        len(moved),
        cross_violations,
    )
    conflicts: list[str] = []
    checked_pairs = 0
    if adjacency_pairs is not None and len(adjacency_pairs):
        for a, b in zip(
            adjacency_pairs["unit_a"].astype(str), adjacency_pairs["unit_b"].astype(str), strict=True
        ):
            brand_a, brand_b = assignment.get(a), assignment.get(b)
            if brand_a is None or brand_b is None or brand_a not in infos or brand_b not in infos:
                continue
            if base_assignment.get(a) == brand_a and base_assignment.get(b) == brand_b:
                continue
            checked_pairs += 1
            if constraints.forbid_conflicts and constraints.conflicting(infos[brand_a], infos[brand_b]):
                conflicts.append(
                    f"{a}({category_label(infos[brand_a].category)})-{b}({category_label(infos[brand_b].category)})"
                )
    add("인접 충돌 금지", "변경된 인접 쌍에 상충 카테고리가 없어야 함", checked_pairs, conflicts)
    add(
        "최대 이동 수",
        f"이동 브랜드 수 ≤ {constraints.max_moves}",
        len(moved),
        [f"{len(moved)}건 이동"] if len(moved) > constraints.max_moves else [],
    )
    return pd.DataFrame(rows, columns=CONSTRAINT_COLUMNS)


# --- search ------------------------------------------------------------------------------------------


@dataclass
class _State:
    state: list[int]
    pos: list[int]
    n_moved: int = 0
    raw: float = 0.0
    pen: float = 0.0

    def copy(self) -> _State:
        return _State(list(self.state), list(self.pos), self.n_moved, self.raw, self.pen)


Change = list[tuple[int, int]]


class _Search:
    """Index-based search state over the units in scope (brands fixed outside the filter)."""

    def __init__(
        self,
        scorer: AssignmentScorer,
        base_assignment: Mapping[str, str | None],
        constraints: LayoutConstraints,
    ):
        self.scorer = scorer
        self.constraints = constraints
        codes = [u for u, info in scorer.units.items() if constraints.in_scope(info.floor)]
        self.codes = sorted(codes, key=lambda u: (floor_sort_key(scorer.units[u].floor), u))
        self.index = {u: i for i, u in enumerate(self.codes)}
        self.nbrs = [
            tuple(self.index[v] for v in scorer.neighbors.get(u, ()) if v in self.index) for u in self.codes
        ]
        self.bids = sorted({b for u in self.codes if (b := base_assignment.get(u)) is not None})
        bindex = {b: i for i, b in enumerate(self.bids)}
        self.base = [bindex[b] if (b := base_assignment.get(u)) is not None else -1 for u in self.codes]
        self.base_pos = [0] * len(self.bids)
        for u, b in enumerate(self.base):
            if b >= 0:
                self.base_pos[b] = u
        self.info = [scorer.brands.get(b) for b in self.bids]
        self.movable = [info is not None and constraints.is_movable(info) for info in self.info]
        self.dest: list[list[int]] = []
        self.dest_all: list[list[int]] = []
        self.value: list[dict[int, float]] = []
        for b, info in enumerate(self.info):
            home = self.base_pos[b]
            dest = (
                [u for u in range(len(self.codes)) if u != home and self._destination_ok(b, u)]
                if self.movable[b]
                else []
            )
            self.dest.append(dest)
            self.dest_all.append(sorted([*dest, home]))
            if info is None:
                self.value.append({home: 0.0})
            else:
                self.value.append(
                    {u: scorer.location_value(self.bids[b], self.codes[u]) for u in self.dest_all[b]}
                )
        self.dest_set = [set(d) for d in self.dest]
        self.movers = [b for b in range(len(self.bids)) if self.movable[b] and self.dest[b]]
        self.mover_set = set(self.movers)
        self.lam = scorer.model.affinity_lambda if scorer.has_affinity else 0.0
        self.cap = scorer.model.affinity_cap
        self._excess: dict[tuple[int, int], float] = {}
        self._conflict: dict[tuple[int, int], bool] = {}
        self.tol = 1e-9

    def _destination_ok(self, b: int, u: int) -> bool:
        info, unit = self.info[b], self.scorer.units[self.codes[u]]
        if info is None:
            return False
        if (
            not self.constraints.allow_cross_floor
            and unit.floor != self.scorer.units[self.codes[self.base_pos[b]]].floor
        ):
            return False
        return self.constraints.area_ok(info, unit.area_m2) and self.constraints.floor_ok(info, unit.floor)

    def excess(self, a: int, b: int) -> float:
        key = (a, b) if a < b else (b, a)
        if key not in self._excess:
            known = self.info[a] is not None and self.info[b] is not None
            self._excess[key] = self.scorer.excess_lift(self.bids[a], self.bids[b]) if known else 0.0
        return self._excess[key]

    def conflict(self, a: int, b: int) -> bool:
        key = (a, b) if a < b else (b, a)
        if key not in self._conflict:
            ia, ib = self.info[a], self.info[b]
            self._conflict[key] = ia is not None and ib is not None and self.constraints.conflicting(ia, ib)
        return self._conflict[key]

    def unit_value(self, state: list[int], u: int) -> float:
        b = state[u]
        if b < 0:
            return 0.0
        value = self.value[b][u]
        if self.lam > 0:
            total = sum(self.excess(b, state[v]) for v in self.nbrs[u] if state[v] >= 0)
            value *= 1.0 + min(self.cap, self.lam * total)
        return value

    def initial(self) -> _State:
        return _State(list(self.base), list(self.base_pos))

    def changes_for(self, st: _State, b: int, v: int) -> Change | None:
        u = st.pos[b]
        if v == u:
            return None
        other = st.state[v]
        if other < 0:
            return [(u, -1), (v, b)]
        if not self.movable[other] or (u != self.base_pos[other] and u not in self.dest_set[other]):
            return None
        return [(u, other), (v, b)]

    def candidates(self, st: _State) -> Iterator[Change]:
        """Every valid-looking move/swap once (a swap between two movers is generated by the lower index)."""
        for b in self.movers:
            for v in self.dest_all[b]:
                other = st.state[v]
                if 0 <= other < b and other in self.mover_set:
                    continue
                change = self.changes_for(st, b, v)
                if change is not None:
                    yield change

    def evaluate(
        self, st: _State, change: Change, mu: float, placed: Mapping[tuple[int, int], int]
    ) -> tuple[float, float, int] | None:
        """(raw gain, penalized gain, change in moved-brand count), or None when a rule is violated."""
        new_pos = {b: u for u, b in change if b >= 0}
        moved_delta = sum(
            int(u != self.base_pos[b]) - int(st.pos[b] != self.base_pos[b]) for b, u in new_pos.items()
        )
        if st.n_moved + moved_delta > self.constraints.max_moves:
            return None
        for b, u in new_pos.items():
            if u != self.base_pos[b] and u not in self.dest_set[b]:
                return None
        affected = {u for u, _ in change}
        for u, _ in change:
            affected.update(self.nbrs[u])
        old = sum(self.unit_value(st.state, a) for a in affected)
        saved = [(u, st.state[u]) for u, _ in change]
        for u, b in change:
            st.state[u] = b
        ok = True
        if self.constraints.forbid_conflicts:
            for u, b in change:
                if b < 0:
                    continue
                for v in self.nbrs[u]:
                    other = st.state[v]
                    if (
                        other >= 0
                        and self.conflict(b, other)
                        and not (self.base[u] == b and self.base[v] == other)
                    ):
                        ok = False
                        break
                if not ok:
                    break
        new = sum(self.unit_value(st.state, a) for a in affected) if ok else 0.0
        for u, b in saved:
            st.state[u] = b
        if not ok:
            return None
        raw = new - old
        penalty = 0.0
        if mu > 0:
            for b, u in new_pos.items():
                if u != self.base_pos[b]:
                    penalty += placed.get((b, u), 0)
                if st.pos[b] != self.base_pos[b]:
                    penalty -= placed.get((b, st.pos[b]), 0)
        return raw, raw - mu * penalty, moved_delta

    @staticmethod
    def apply(st: _State, change: Change, result: tuple[float, float, int]) -> None:
        for u, b in change:
            st.state[u] = b
            if b >= 0:
                st.pos[b] = u
        st.raw += result[0]
        st.pen += result[1]
        st.n_moved += result[2]

    def greedy(self, st: _State, mu: float, placed: Mapping[tuple[int, int], int]) -> None:
        while True:
            best: tuple[tuple[float, float, int], Change] | None = None
            for change in self.candidates(st):
                result = self.evaluate(st, change, mu, placed)
                if result is not None and result[1] > (best[0][1] if best else self.tol):
                    best = (result, change)
            if best is None:
                return
            self.apply(st, best[1], best[0])

    def anneal(
        self,
        st: _State,
        rng: np.random.Generator,
        iters: int,
        mu: float,
        placed: Mapping[tuple[int, int], int],
    ) -> _State:
        if not self.movers or iters <= 0:
            return st

        def propose() -> Change | None:
            b = self.movers[int(rng.integers(len(self.movers)))]
            options = self.dest_all[b]
            return self.changes_for(st, b, options[int(rng.integers(len(options)))])

        samples = []
        for _ in range(min(100, iters)):
            change = propose()
            result = self.evaluate(st, change, mu, placed) if change is not None else None
            if result is not None and abs(result[1]) > self.tol:
                samples.append(abs(result[1]))
        t0 = float(np.median(samples)) if samples else 1.0
        best = st.copy()
        for step in range(iters):
            temperature = t0 * (1.0 - step / iters) + t0 * 1e-3
            change = propose()
            if change is None:
                continue
            result = self.evaluate(st, change, mu, placed)
            if result is None:
                continue
            if result[1] >= 0 or rng.random() < math.exp(result[1] / temperature):
                self.apply(st, change, result)
                if st.pen > best.pen + self.tol:
                    best = st.copy()
        return best

    def run(
        self, seed: int, alt: int, attempt: int, iters: int, mu: float, placed: Mapping[tuple[int, int], int]
    ) -> _State:
        rng = np.random.default_rng([seed, alt, attempt])
        st = self.initial()
        self.greedy(st, mu, placed)
        st = self.anneal(st, rng, iters, mu, placed)
        self.greedy(st, mu, placed)
        return st

    def placements(self, st: _State) -> frozenset[tuple[int, int]]:
        return frozenset((b, u) for b, u in enumerate(st.pos) if u != self.base_pos[b])

    def assignment(self, st: _State) -> dict[str, str | None]:
        return {
            code: (self.bids[b] if b >= 0 else None) for code, b in zip(self.codes, st.state, strict=True)
        }


# --- outputs -----------------------------------------------------------------------------------------


def _kpi_deltas(before: AssignmentScore, after: AssignmentScore) -> pd.DataFrame:
    fb = before.by_floor().set_index("floor")
    fa = after.by_floor().set_index("floor")
    rows = []
    for floor in sorted(set(fb.index) | set(fa.index), key=floor_sort_key):
        b, a = fb.loc[floor], fa.loc[floor]
        rows.append(
            {
                "floor": floor,
                "expected_before": b["expected_sales"],
                "expected_after": a["expected_sales"],
                "delta": a["expected_sales"] - b["expected_sales"],
                "delta_low": a["low"] - b["expected_sales"],
                "delta_high": a["high"] - b["expected_sales"],
                "delta_pct": (a["expected_sales"] / b["expected_sales"] - 1) * 100
                if b["expected_sales"]
                else math.nan,
                "occupied_area_before_m2": b["occupied_area_m2"],
                "occupied_area_after_m2": a["occupied_area_m2"],
                "sales_per_m2_before": b["sales_per_m2"],
                "sales_per_m2_after": a["sales_per_m2"],
            }
        )
    area_before = float(fb["occupied_area_m2"].sum())
    area_after = float(fa["occupied_area_m2"].sum())
    rows.append(
        {
            "floor": "합계",
            "expected_before": before.total,
            "expected_after": after.total,
            "delta": after.total - before.total,
            "delta_low": after.total_low - before.total,
            "delta_high": after.total_high - before.total,
            "delta_pct": (after.total / before.total - 1) * 100 if before.total else math.nan,
            "occupied_area_before_m2": area_before,
            "occupied_area_after_m2": area_after,
            "sales_per_m2_before": before.total / area_before if area_before else math.nan,
            "sales_per_m2_after": after.total / area_after if area_after else math.nan,
        }
    )
    table = pd.DataFrame(rows)
    table["label"] = ESTIMATE_LABEL
    return table


def _move_detail(
    brand_id: str, from_unit: str, to_unit: str, before: pd.DataFrame, after: pd.DataFrame
) -> Move:
    b, a = before.loc[from_unit], after.loc[to_unit]
    expected_before, expected_after = float(b["expected_sales"]), float(a["expected_sales"])
    delta = expected_after - expected_before
    logs = {
        "exposure": float(a["exposure_effect"] - b["exposure_effect"]),
        "area": float(a["area_effect"] - b["area_effect"]),
        "affinity": float(a["affinity_effect"] - b["affinity_effect"]),
    }
    total_log = sum(logs.values())
    shares = {k: (v / total_log if abs(total_log) > 1e-12 else 0.0) for k, v in logs.items()}
    krw = {k: delta * s for k, s in shares.items()}
    pct = {k: (math.exp(v) - 1) * 100 for k, v in logs.items()}
    name, category = str(a["brand_name"]), a["category"]
    affinity = (
        f", 인접 친화도: {pct['affinity']:+.1f}% ({format_krw_delta(krw['affinity'])})"
        if abs(logs["affinity"]) > 1e-12
        else ""
    )
    explanation = (
        f"{name}({category_label(category)}) {from_unit}→{to_unit}: 기대 매출 {format_krw(expected_before)} → "
        f"{format_krw(expected_after)} ({format_krw_delta(delta)}, 범위 {format_krw_delta(float(a['low']) - expected_before)}"
        f"~{format_krw_delta(float(a['high']) - expected_before)}, {ESTIMATE_LABEL}). "
        f"노출: 에스컬레이터 보행거리 {b['walk_to_escalator_m']:.0f}m→{a['walk_to_escalator_m']:.0f}m "
        f"({pct['exposure']:+.1f}%, {format_krw_delta(krw['exposure'])}), "
        f"면적: {b['area_m2']:.0f}㎡→{a['area_m2']:.0f}㎡ ({pct['area']:+.1f}%, {format_krw_delta(krw['area'])})"
        f"{affinity}."
    )
    return Move(
        brand_id=brand_id,
        brand_name=name,
        category=category,
        from_unit=from_unit,
        to_unit=to_unit,
        from_floor=str(b["floor"]),
        to_floor=str(a["floor"]),
        expected_before=expected_before,
        expected_after=expected_after,
        delta=delta,
        delta_low=float(a["low"]) - expected_before,
        delta_high=float(a["high"]) - expected_before,
        exposure_krw=krw["exposure"],
        area_krw=krw["area"],
        affinity_krw=krw["affinity"],
        walk_before_m=float(b["walk_to_escalator_m"]),
        walk_after_m=float(a["walk_to_escalator_m"]),
        area_before_m2=float(b["area_m2"]),
        area_after_m2=float(a["area_m2"]),
        explanation=explanation,
    )


def generate_alternatives(
    base_assignment: Mapping[str, str | None],
    unit_features: pd.DataFrame,
    brands: pd.DataFrame | Mapping[str, BrandInfo],
    adjacency_pairs: pd.DataFrame | None,
    model_params: ScoringModel,
    constraints: LayoutConstraints | None = None,
    n_alternatives: int = 3,
    seed: int = 7,
    max_iter: int = 3000,
    affinity_pairs: pd.DataFrame | None = None,
    diversity_weight: float = 0.5,
) -> list[LayoutAlternative]:
    """Up to ``n_alternatives`` distinct, constraint-satisfying layouts that raise expected sales.

    Deterministic for a given ``seed``. Each alternative runs greedy improvement, ``max_iter`` simulated
    annealing steps and a final greedy polish (a heuristic: the best layout found, not a proven optimum).
    Later alternatives pay ``diversity_weight * (average gain per move of the first alternative)`` for
    every relocation already used by an earlier alternative. Alternatives without a strict improvement
    are not returned, so the list may be shorter (or empty).
    """
    constraints = constraints or LayoutConstraints()
    base_assignment = normalize_assignment(base_assignment)
    infos = brand_info(brands)
    scorer = AssignmentScorer(unit_features, infos, model_params, affinity_pairs, adjacency_pairs)
    search = _Search(scorer, base_assignment, constraints)
    scope_base = {code: base_assignment.get(code) for code in search.codes}
    before = scorer.score(scope_base)
    search.tol = 1e-9 * max(1.0, abs(before.total))
    before_units = before.units.set_index("unit_code")
    placed: Counter[tuple[int, int]] = Counter()
    seen: list[frozenset[tuple[int, int]]] = []
    alternatives: list[LayoutAlternative] = []
    mu = 0.0
    for k in range(n_alternatives):
        accepted: _State | None = None
        for attempt in range(3):
            st = search.run(seed, k, attempt, max_iter, mu, placed)
            moves = search.placements(st)
            if not moves or st.raw <= search.tol:
                break
            if moves in seen:
                mu = max(2.0 * mu, search.tol * 10)
                continue
            accepted = st
            break
        if accepted is None:
            break
        scope_after = search.assignment(accepted)
        after = scorer.score(scope_after)
        if after.total <= before.total + search.tol:
            break
        full = {**dict(base_assignment), **scope_after}
        after_units = after.units.set_index("unit_code")
        base_pos = {b: u for u, b in scope_base.items() if b is not None}
        moved = sorted(
            (b for u, b in scope_after.items() if b is not None and base_pos.get(b) != u),
            key=lambda b: (floor_sort_key(scorer.units[base_pos[b]].floor), base_pos[b]),
        )
        new_pos = {b: u for u, b in scope_after.items() if b is not None}
        move_list = [_move_detail(b, base_pos[b], new_pos[b], before_units, after_units) for b in moved]
        delta = after.total - before.total
        spillover = delta - sum(m.delta for m in move_list)
        alt_id = f"ALT-{len(alternatives) + 1}"
        summary = (
            f"[{ESTIMATE_LABEL}] {alt_id}: 브랜드 {len(move_list)}건 이동, 대상 범위 기대 매출 "
            f"{format_krw(before.total)} → {format_krw(after.total)} ({format_krw_delta(delta)}, "
            f"{delta / before.total * 100 if before.total else math.nan:+.1f}%, 범위 "
            f"{format_krw_delta(after.total_low - before.total)}~{format_krw_delta(after.total_high - before.total)})."
        )
        explanations = [m.explanation for m in move_list]
        if abs(spillover) > search.tol:
            explanations.append(
                f"이웃 매장 인접 친화도 변화에 따른 파급 효과: {format_krw_delta(spillover)} ({ESTIMATE_LABEL})."
            )
        alternatives.append(
            LayoutAlternative(
                alt_id=alt_id,
                seed=seed,
                assignment=full,
                moves=move_list,
                objective_before=before.total,
                objective_after=after.total,
                objective_low=after.total_low,
                objective_high=after.total_high,
                spillover_krw=spillover,
                kpi_deltas=_kpi_deltas(before, after),
                constraint_checks=check_constraints(
                    base_assignment, full, unit_features, infos, adjacency_pairs, constraints
                ),
                summary=summary,
                explanations=explanations,
            )
        )
        placements = search.placements(accepted)
        seen.append(placements)
        placed.update(placements)
        if len(alternatives) == 1:
            mu = diversity_weight * delta / max(1, len(move_list))
    return alternatives
