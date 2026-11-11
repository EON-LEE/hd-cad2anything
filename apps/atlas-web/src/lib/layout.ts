// Normalization of layout alternatives. Accepts the contract shape
// ({id, kpi_delta: {sales_krw, low, high}, constraints: [{name, ok}]}) and the layout engine's
// native shape ({alt_id, delta_krw, objective_low/high, constraint_checks: [{constraint, satisfied, detail}]}).
import type { LayoutAlternative, SearchStatus } from '../types/api'
import { formatKrw, formatNumber } from './format'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

const SEARCH_STATUSES: readonly SearchStatus[] = ['optimal', 'optimal_diverse', 'feasible', 'heuristic']

export function normalizeSearchStatus(value: unknown): SearchStatus | null {
  return SEARCH_STATUSES.find((status) => status === value) ?? null
}

/** Rounding unit (KRW) the exact search assumes when the backend does not say. */
export const DEFAULT_OBJECTIVE_ROUNDING_KRW = 100

export const DIVERSE_OPTIMAL_NOTE = '앞선 대안과 다르게 만드는 조건 안에서 최적'

/**
 * What the search status of an alternative lets the screen say. Only `optimal` of the first alternative claims the
 * best answer of the model (within its rounding and constraints); a later alternative is optimal only under the
 * condition to differ from the earlier ones; the other states claim nothing. Older backends send `optimal` for
 * every alternative the solver proved: a later one reads like `optimal_diverse`. Null: nothing to say.
 */
export function optimalityNote(status: unknown, options: { first: boolean; roundingKrw?: number | null }): string | null {
  switch (normalizeSearchStatus(status)) {
    case 'optimal':
      if (!options.first) return DIVERSE_OPTIMAL_NOTE
      return `${formatNumber(options.roundingKrw && options.roundingKrw > 0 ? options.roundingKrw : DEFAULT_OBJECTIVE_ROUNDING_KRW)}원 단위로 반올림한 모델·제약 범위에서 최적해 확인`
    case 'optimal_diverse':
      return DIVERSE_OPTIMAL_NOTE
    case 'feasible':
      return '실행 가능한 해 · 최적성 미확인'
    case 'heuristic':
      return '휴리스틱 탐색 · 최적성 미확인'
    default:
      return null
  }
}

/**
 * The solver's remaining gap to its bound, when there is one. The bound allows for the rounding of the objective (and
 * the diversity penalty), so even a proven optimum can show a small gap.
 */
export function optimalityGapNote(gapKrw: number | null | undefined): string | null {
  return gapKrw !== null && gapKrw !== undefined && Number.isFinite(gapKrw) && gapKrw > 0
    ? `상한과의 차이 최대 ${formatKrw(gapKrw)}(반올림 허용 오차 포함)`
    : null
}

export function normalizeAlternative(raw: unknown, index: number): LayoutAlternative | null {
  if (!isRecord(raw)) return null
  const moves = (Array.isArray(raw.moves) ? raw.moves : [])
    .filter(isRecord)
    .map((m) => ({
      brand_id: String(m.brand_id ?? ''),
      brand_name: String(m.brand_name ?? m.brand_id ?? ''),
      from_unit: String(m.from_unit ?? ''),
      to_unit: String(m.to_unit ?? ''),
    }))
    .filter((m) => m.from_unit && m.to_unit)

  const before = num(raw.objective_before) ?? 0
  const after = num(raw.objective_after) ?? before
  const kpi = isRecord(raw.kpi_delta) ? raw.kpi_delta : {}
  const mid = num(kpi.sales_krw) ?? num(raw.delta_krw) ?? after - before
  const objectiveLow = num(raw.objective_low)
  const objectiveHigh = num(raw.objective_high)
  const low = num(kpi.low) ?? (objectiveLow !== null ? objectiveLow - before : mid)
  const high = num(kpi.high) ?? (objectiveHigh !== null ? objectiveHigh - before : mid)

  const checks = Array.isArray(raw.constraints) ? raw.constraints : Array.isArray(raw.constraint_checks) ? raw.constraint_checks : []
  const constraints = checks.filter(isRecord).map((c) => ({
    name: String(c.name ?? c.constraint ?? c.rule ?? '제약'),
    ok: Boolean(c.ok ?? c.satisfied),
    ...(typeof c.detail === 'string' && c.detail ? { detail: c.detail } : {}),
  }))

  const explanations = Array.isArray(raw.explanations) ? raw.explanations.map(String) : []
  if (explanations.length === 0 && typeof raw.summary === 'string' && raw.summary) explanations.push(raw.summary)

  const status = normalizeSearchStatus(raw.search_status)
  const rounding = num(raw.objective_rounding_krw)
  const gap = num(raw.gap_krw)
  const bound = num(raw.bound_krw)
  return {
    id: String(raw.id ?? raw.alt_id ?? `ALT-${index + 1}`),
    moves,
    objective_before: before,
    objective_after: after,
    kpi_delta: { sales_krw: mid, low: Math.min(low, mid), high: Math.max(high, mid) },
    constraints,
    explanations,
    ...(typeof raw.rev_id === 'string' && raw.rev_id ? { rev_id: raw.rev_id } : {}),
    ...(status ? { search_status: status } : {}),
    ...(rounding !== null ? { objective_rounding_krw: rounding } : {}),
    ...(gap !== null ? { gap_krw: gap } : {}),
    ...(bound !== null ? { bound_krw: bound } : {}),
  }
}

export function normalizeAlternatives(list: unknown): LayoutAlternative[] {
  return (Array.isArray(list) ? list : []).map(normalizeAlternative).filter((a): a is LayoutAlternative => a !== null)
}

/** Objective values may be scores (0..1) or expected sales in KRW. */
export function formatObjective(value: number, signed = false): string {
  if (Math.abs(value) >= 1000) return formatKrw(value, signed)
  const text = value.toFixed(2)
  return signed && value > 0 ? `+${text}` : text
}
