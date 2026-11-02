"""Exact layout search: the engine's objective solved as an integer program with OR-Tools CP-SAT.

The model mirrors ``engine._Search`` term for term, so its optimum is directly comparable with the heuristic:

    max  sum_{b,u} v[b,u] * x[b,u] + sum_{b,u} bonus[b,u]  -  sum_b cost[b] * moved[b]
         -  mu * sum_{b,u} placed[b,u] * x[b,u]

- ``x[b,u]`` = 1 when brand ``b`` takes unit ``u`` (only the units the constraint filter allows, or its own unit).
- ``v[b,u]`` is the weighted expected sales without the adjacency bonus. ``bonus[b,u]`` is the capped affinity
  bonus in KRW, ``v * min(cap, lambda * sum of excess lift with the neighbors' brands)``, linearized with
  ``bonus <= v * cap * x`` and ``bonus <= v * lambda * sum(excess * x_neighbor)``; maximization pushes it to the
  smaller of the two.
- Every brand takes exactly one unit, every unit holds at most one brand, at most ``max_moves`` non-free
  brands leave their unit, and conflicting categories may not become neighbors (pairs already adjacent in the
  base layout stay allowed).
- ``exclude`` lists relocation sets of earlier alternatives; a no-good cut removes each exact set, so every
  solve returns a layout that differs from them.

Coefficients are integers in units of ``UNIT_KRW`` (CP-SAT is an integer solver). The rounding changes any
layout's objective by at most a few units per brand; that allowance is added to the reported bound, so
"optimal" means no feasible layout is better by more than the allowance (well under 1,000,000 KRW for a
store). A single worker with the full LP relaxation (``linearization_level`` 2) proves the store-scale
problems in about a second and is deterministic; the limit is CP-SAT's deterministic time, so results do not
depend on machine speed.

``solve`` returns None when OR-Tools is not installed or no layout was found; the caller then keeps the
heuristic result.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cad2anything.layout.engine import _Search

DEFAULT_TIME_LIMIT_S = 10.0
ENV_SWITCH = "ATLAS_LAYOUT_EXACT"
ENV_SECONDS = "ATLAS_LAYOUT_EXACT_SECONDS"
UNIT_KRW = 100.0
"""Coefficient unit. Whole KRW makes the LP relaxation slow; 100 KRW keeps it fast and the allowance small."""


@dataclass
class ExactResult:
    state: list[int]
    """Brand index per scope unit (-1 = vacant), as in ``_Search``."""
    optimal: bool
    """True when the solver proved that no feasible layout is better (beyond the rounding allowance)."""
    bound: float
    """Upper bound of the objective in KRW (absolute, including the penalty term and the allowance)."""
    seconds: float


def enabled() -> bool:
    """Exact solving is on unless ``ATLAS_LAYOUT_EXACT`` is 0/false/off/no."""
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in ("0", "false", "off", "no")


def time_limit() -> float:
    """Deterministic-time limit per solve from ``ATLAS_LAYOUT_EXACT_SECONDS`` (default 10)."""
    try:
        return max(1.0, float(os.environ.get(ENV_SECONDS, DEFAULT_TIME_LIMIT_S)))
    except ValueError:
        return DEFAULT_TIME_LIMIT_S


def solve(
    search: _Search,
    mu: float = 0.0,
    placed: Mapping[tuple[int, int], int] | None = None,
    hint: Sequence[int] | None = None,
    exclude: Collection[frozenset[tuple[int, int]]] = (),
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
) -> ExactResult | None:
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None
    placed = placed or {}
    model = cp_model.CpModel()

    x: dict[tuple[int, int], Any] = {}
    for b in range(len(search.bids)):
        units = search.dest_all[b] if search.movable[b] else [search.base_pos[b]]
        for u in units:
            x[b, u] = model.NewBoolVar(f"x_{b}_{u}")
    by_brand: dict[int, list[int]] = {}
    by_unit: dict[int, list[int]] = {}
    for b, u in x:
        by_brand.setdefault(b, []).append(u)
        by_unit.setdefault(u, []).append(b)
    for b in sorted(by_brand):
        model.AddExactlyOne([x[b, u] for u in by_brand[b]])
    for u in sorted(by_unit):
        model.AddAtMostOne([x[b, u] for b in by_unit[u]])

    homes = {b: search.base_pos[b] for b in by_brand}
    counted = [b for b in sorted(by_brand) if not search.free[b] and (b, homes[b]) in x]
    model.Add(sum(x[b, homes[b]] for b in counted) >= len(counted) - search.constraints.max_moves)

    away = [key for key in sorted(x) if key[1] != homes[key[0]]]
    for relocations in exclude:
        inside = [x[key] for key in sorted(relocations) if key in x]
        if len(inside) < len(relocations):
            continue  # that set is infeasible here anyway
        outside = [x[key] for key in away if key not in relocations]
        model.Add(sum(inside) - sum(outside) <= len(inside) - 1)

    if search.constraints.forbid_conflicts:
        for u in range(len(search.codes)):
            for v in search.nbrs[u]:
                if v <= u:
                    continue
                for b in by_unit.get(u, []):
                    for other in by_unit.get(v, []):
                        if b == other or not search.conflict(b, other):
                            continue
                        if search.base[u] == b and search.base[v] == other:
                            continue
                        model.AddBoolOr([x[b, u].Not(), x[other, v].Not()])

    terms = []
    widest = 0
    unit = UNIT_KRW
    for (b, u), var in sorted(x.items()):
        value = search.value[b].get(u, 0.0) / unit
        if value:
            terms.append(round(value) * var)
            top = math.floor(value * search.cap) if search.lam > 0 else 0
            support = (
                [
                    (math.floor(value * search.lam * search.excess(b, other)), x[other, v])
                    for v in search.nbrs[u]
                    for other in by_unit.get(v, [])
                    if other != b and search.excess(b, other) > 0
                ]
                if top > 0
                else []
            )
            support = [(coef, var_) for coef, var_ in support if coef > 0]
            if support:
                bonus = model.NewIntVar(0, top, f"bonus_{b}_{u}")
                model.Add(bonus <= top * var)
                model.Add(bonus <= sum(coef * other for coef, other in support))
                terms.append(bonus)
                widest = max(widest, len(search.nbrs[u]))
        if u != homes[b]:
            penalty = round(mu * placed.get((b, u), 0) / unit)
            if penalty:
                terms.append(-penalty * var)
    for b in sorted(by_brand):
        if search.cost[b] > 0 and (b, homes[b]) in x:
            terms.append(-round(search.cost[b] / unit) * (1 - x[b, homes[b]]))
    model.Maximize(sum(terms))

    if hint is not None:
        for b, u in sorted(x):
            model.AddHint(x[b, u], hint[u] == b)

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.linearization_level = 2
    solver.parameters.max_deterministic_time = time_limit_s
    solver.parameters.max_time_in_seconds = 3 * time_limit_s
    started = time.perf_counter()
    status = solver.Solve(model)
    seconds = time.perf_counter() - started
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    state = [-1] * len(search.codes)
    for (b, u), var in x.items():
        if solver.BooleanValue(var):
            state[u] = b
    # Per brand: value (0.5), cost and penalty (0.5 each) rounded; the bonus floors (1 + one per neighbor).
    allowance = (len(by_brand) * (2.5 + widest) + 1.0) * unit
    return ExactResult(
        state, status == cp_model.OPTIMAL, solver.BestObjectiveBound() * unit + allowance, seconds
    )
