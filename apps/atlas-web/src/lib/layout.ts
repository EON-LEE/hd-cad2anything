// Normalization of layout alternatives. Accepts the contract shape
// ({id, kpi_delta: {sales_krw, low, high}, constraints: [{name, ok}]}) and the layout engine's
// native shape ({alt_id, delta_krw, objective_low/high, constraint_checks: [{constraint, satisfied, detail}]}).
import type { LayoutAlternative } from '../types/api'
import { formatKrw } from './format'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
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

  return {
    id: String(raw.id ?? raw.alt_id ?? `ALT-${index + 1}`),
    moves,
    objective_before: before,
    objective_after: after,
    kpi_delta: { sales_krw: mid, low: Math.min(low, mid), high: Math.max(high, mid) },
    constraints,
    explanations,
    ...(typeof raw.rev_id === 'string' && raw.rev_id ? { rev_id: raw.rev_id } : {}),
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
