import { describe, expect, it } from 'vitest'
import type { Meta, RevisionInfo } from '../types/api'
import { DEFAULT_REVIEW_WINDOW, REVIEW_WINDOW_OPTIONS, canReviewRelayout, normalizeRelayoutReview, normalizeStoreComparison } from './benchmark'

const rev = (rev_id: string, extra: Partial<RevisionInfo> = {}): RevisionInfo => ({ rev_id, kind: 'actual', parent_rev_id: null, effective_date: null, ...extra })

const meta: Meta = {
  facility: '',
  floors: [],
  categories: [],
  revisions: [
    rev('R0', { effective_date: '2024-10-01', has_sales: true }),
    rev('R1', { parent_rev_id: 'R0', effective_date: '2025-03-01', has_sales: true }),
    rev('R2', { parent_rev_id: 'R1', effective_date: '2025-09-01', has_sales: true }),
    // Uploaded for the coming season: no sales after its effective date yet.
    rev('R3', { parent_rev_id: 'R2', effective_date: '2026-10-01', has_sales: false, model_source: 'uploaded' }),
    rev('H1', { kind: 'hypothetical', parent_rev_id: 'R2' }),
    // Older backends do not say whether there are sales.
    rev('R9', { parent_rev_id: 'R2', effective_date: '2025-12-01' }),
  ],
}

describe('relayout review report', () => {
  it('is offered for actual revisions with a parent and sales after their effective date', () => {
    expect(['R0', 'R1', 'R2', 'R3', 'H1', 'R9', 'NOPE'].filter((r) => canReviewRelayout(meta, r))).toEqual(['R1', 'R2'])
    expect(canReviewRelayout(null, 'R2')).toBe(false)
    expect(canReviewRelayout(meta, null)).toBe(false)
    expect(REVIEW_WINDOW_OPTIONS).toEqual([30, 90])
    expect(DEFAULT_REVIEW_WINDOW).toBe(90)
  })

  it('normalizes the review and the store benchmark defensively', () => {
    expect(normalizeRelayoutReview({ summary: 'no rev' })).toBeNull()
    const review = normalizeRelayoutReview({ rev: 'R2', window: 'none', items: [{ unit_code: '1F-S04', change_pct: '37.9' }, 'bad'] })!
    expect(review.window).toEqual({ before: null, after: null, days: null })
    expect(review.items).toEqual([
      {
        change_type: '',
        unit_code: '1F-S04',
        floor: '1F',
        brand_name: null,
        before_daily_krw: null,
        after_daily_krw: null,
        change_pct: 37.9,
        control_pct: null,
        vs_control_pct: null,
      },
    ])
    expect(normalizeStoreComparison({ stores: [] })).toBeNull()
    const stores = normalizeStoreComparison({ rows: [{ label: '3F', values: 'none' }], notes: ['a', 3], stores: [{ store_id: 'ST-E' }] })!
    expect(stores).toMatchObject({ level: 'store', metric: '', notes: ['a', '3'], period: null, caveat: null })
    expect(stores.rows[0]).toEqual({ key: '3F', label: '3F', flagship: null, peer_median: null, rank: null, n_stores: null, gap_pct: null, values: {} })
    expect(stores.stores[0]).toEqual({ store_id: 'ST-E', store_name: 'ST-E', region: null, format: null, flagship: false })
  })
})
