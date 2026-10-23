// Store benchmark (GET /api/analytics/stores) and past relayout review (GET /api/analytics/relayout-review,
// GET /api/reports/relayout-review/{rev}): normalization and the rule for offering the review report.
import type { DatePeriod, Meta, RelayoutReview, RelayoutReviewItem, StoreComparison, StoreComparisonRow } from '../types/api'
import { bool, isRecord, num, optText, records, text, texts } from './coerce'
import { floorOfUnitCode } from './geometry'

/** Windows (days before and after the effective date) the review report can compare. */
export const REVIEW_WINDOW_OPTIONS: readonly number[] = [30, 90]
export const DEFAULT_REVIEW_WINDOW = 90

/**
 * The review report compares sales before and after a revision took effect, so it is offered for an
 * actual revision that replaced a parent and has sales in its own period (`has_sales: true`), e.g. R1, R2.
 * The first revision, hypothetical ones and drawings uploaded for a coming season are left out.
 */
export function canReviewRelayout(meta: Meta | null | undefined, rev: string | null | undefined): boolean {
  const revision = rev ? meta?.revisions.find((r) => r.rev_id === rev) : undefined
  return !!revision && revision.kind === 'actual' && !!revision.parent_rev_id && revision.has_sales === true
}

function period(value: unknown): DatePeriod | null {
  if (!isRecord(value)) return null
  const start = optText(value.start)
  const end = optText(value.end)
  if (!start || !end) return null
  const days = num(value.days)
  return days === null ? { start, end } : { start, end, days }
}

/** Contract notes may arrive as a list or a single string. */
function noteList(value: unknown): string[] {
  const single = optText(value)
  return single && !Array.isArray(value) ? [single] : texts(value)
}

export function normalizeStoreComparison(raw: unknown): StoreComparison | null {
  if (!isRecord(raw) || !Array.isArray(raw.rows)) return null
  const rows: StoreComparisonRow[] = records(raw.rows).map((r) => {
    const values: Record<string, number | null> = {}
    if (isRecord(r.values)) for (const [store, value] of Object.entries(r.values)) values[store] = num(value)
    return {
      key: text(r.key, text(r.label)),
      label: text(r.label, text(r.key)),
      flagship: num(r.flagship),
      peer_median: num(r.peer_median),
      rank: num(r.rank),
      n_stores: num(r.n_stores),
      gap_pct: num(r.gap_pct),
      values,
    }
  })
  return {
    level: text(raw.level, 'store'),
    metric: text(raw.metric),
    metric_label: text(raw.metric_label, text(raw.metric)),
    period: period(raw.period),
    stores: records(raw.stores)
      .filter((s) => optText(s.store_id))
      .map((s) => ({
        store_id: text(s.store_id),
        store_name: text(s.store_name, text(s.store_id)),
        region: optText(s.region),
        format: optText(s.format),
        flagship: bool(s.flagship) ?? false,
      })),
    rows,
    notes: noteList(raw.notes),
    caveat: optText(raw.caveat),
  }
}

export function normalizeRelayoutReview(raw: unknown): RelayoutReview | null {
  if (!isRecord(raw) || !optText(raw.rev)) return null
  const window = isRecord(raw.window) ? raw.window : {}
  const items: RelayoutReviewItem[] = records(raw.items).map((i) => {
    const unitCode = text(i.unit_code)
    return {
      change_type: text(i.change_type),
      unit_code: unitCode,
      floor: optText(i.floor) ?? floorOfUnitCode(unitCode),
      brand_name: optText(i.brand_name),
      before_daily_krw: num(i.before_daily_krw),
      after_daily_krw: num(i.after_daily_krw),
      change_pct: num(i.change_pct),
      control_pct: num(i.control_pct),
      vs_control_pct: num(i.vs_control_pct),
    }
  })
  return {
    rev: text(raw.rev),
    parent_rev: optText(raw.parent_rev),
    effective_date: optText(raw.effective_date),
    window: { before: period(window.before), after: period(window.after), days: num(window.days) },
    items,
    by_type: records(raw.by_type),
    by_floor: records(raw.by_floor),
    summary: text(raw.summary),
    caveat: optText(raw.caveat),
  }
}
