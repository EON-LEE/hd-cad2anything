import { describe, expect, it } from 'vitest'
import { bottomPercentText, revisionMarkers } from '../lib/format'
import type { Meta, RevisionInfo } from '../types/api'
import { createInitialUiState, pickDefaultRevision } from './uiState'

const meta = (revisions: RevisionInfo[]): Meta => ({ facility: '', revisions, floors: [{ code: '3F', ordinal: 2, name: '' }], categories: [] })

const rev = (rev_id: string, effective_date: string | null, extra: Partial<RevisionInfo> = {}): RevisionInfo => ({
  rev_id,
  kind: 'actual',
  parent_rev_id: null,
  effective_date,
  ...extra,
})

describe('pickDefaultRevision', () => {
  it('picks the latest actual revision that has sales', () => {
    const revisions = [
      rev('R0', '2024-10-01', { has_sales: true }),
      rev('R2', '2025-09-01', { has_sales: true }),
      rev('R1', '2025-03-01', { has_sales: true }),
      // Drawn for next season: after data_through, so no sales yet.
      rev('R3', '2026-10-01', { has_sales: false, model_source: 'uploaded' }),
      rev('FW2026-A1', null, { kind: 'hypothetical', has_sales: false }),
    ]
    expect(pickDefaultRevision(meta(revisions))).toBe('R2')
    expect(createInitialUiState(meta(revisions)).rev).toBe('R2')
  })

  it('treats a missing has_sales as having sales (older backends)', () => {
    expect(pickDefaultRevision(meta([rev('R1', '2025-03-01'), rev('R2', '2025-09-01'), rev('H1', null, { kind: 'hypothetical' })]))).toBe('R2')
    expect(pickDefaultRevision(meta([rev('R2', '2025-09-01'), rev('R3', '2026-10-01', { has_sales: false }), rev('R4', '2027-03-01')]))).toBe('R4')
  })

  it('falls back to the latest actual revision, then to the last revision', () => {
    expect(pickDefaultRevision(meta([rev('R2', '2025-09-01', { has_sales: false }), rev('R3', '2026-10-01', { has_sales: false })]))).toBe('R3')
    expect(pickDefaultRevision(meta([rev('H1', null, { kind: 'hypothetical' }), rev('H2', null, { kind: 'hypothetical' })]))).toBe('H2')
    expect(pickDefaultRevision(null)).toBe('R0')
  })
})

describe('revisionMarkers', () => {
  it('marks uploaded revisions and actual revisions without sales (not hypothetical ones)', () => {
    expect(revisionMarkers(rev('R3', '2026-10-01', { has_sales: false, model_source: 'uploaded' }))).toEqual(['업로드', '실적 없음'])
    expect(revisionMarkers(rev('R2', '2025-09-01', { has_sales: true, model_source: 'parsed' }))).toEqual([])
    expect(revisionMarkers(rev('R1', '2025-03-01'))).toEqual([])
    // Hypothetical revisions are labelled 가상 already.
    expect(revisionMarkers(rev('FW2026-A1', null, { kind: 'hypothetical', has_sales: false, model_source: 'hypothetical' }))).toEqual([])
    // `source` is a free-text tool description, never a marker.
    expect(revisionMarkers({ ...rev('R2', '2025-09-01'), source: 'uploaded' } as RevisionInfo)).toEqual([])
  })
})

describe('bottomPercentText', () => {
  it('reads an efficiency percentile (low = inefficient) as 하위 n%', () => {
    expect([0, 0.4, 1, 12.6, 100, 140].map(bottomPercentText)).toEqual(['하위 1%', '하위 1%', '하위 1%', '하위 13%', '하위 100%', '하위 100%'])
    expect([null, undefined, Number.NaN].map(bottomPercentText)).toEqual(['–', '–', '–'])
  })
})
