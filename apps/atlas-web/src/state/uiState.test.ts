import { describe, expect, it } from 'vitest'
import { bottomPercentText, revisionMarkers } from '../lib/format'
import type { Meta, RevisionInfo, UiState } from '../types/api'
import { createInitialUiState, panelPayloadString, payloadString, pickDefaultRevision, revisionHasSales, visibleOverlays } from './uiState'

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

describe('revisionHasSales', () => {
  const withData = (revisions: RevisionInfo[], data_through?: string): Meta => ({ ...meta(revisions), ...(data_through ? { data_through } : {}) })

  it('says no for hypothetical revisions and for actual ones the server marks without sales', () => {
    const m = withData([
      rev('R2', '2025-09-01', { has_sales: true }),
      rev('R3', '2026-10-01', { has_sales: false }),
      rev('FW2026-A1', null, { kind: 'hypothetical' }),
      rev('H1', null, { kind: 'hypothetical', has_sales: true }),
    ])
    expect(revisionHasSales(m, 'R2')).toBe(true)
    expect(revisionHasSales(m, 'R3')).toBe(false)
    expect(revisionHasSales(m, 'FW2026-A1')).toBe(false)
    // Hypothetical wins over a `has_sales` the server should not send.
    expect(revisionHasSales(m, 'H1')).toBe(false)
  })

  it('infers it from the effective date and the last transaction date when has_sales is absent (older backends)', () => {
    const m = withData([rev('R2', '2025-09-01'), rev('R3', '2026-10-01'), rev('R4', '2026-09-30'), rev('R5', null)], '2026-09-30')
    expect(revisionHasSales(m, 'R2')).toBe(true)
    expect(revisionHasSales(m, 'R3')).toBe(false)
    expect(revisionHasSales(m, 'R4')).toBe(true)
    // Without a date, or without data_through, nothing says it has none.
    expect(revisionHasSales(m, 'R5')).toBe(true)
    expect(revisionHasSales(withData([rev('R3', '2026-10-01')]), 'R3')).toBe(true)
  })

  it('knows what-if ids that are not in the list yet, and leaves other unknown ids undecided', () => {
    const m = withData([rev('R2', '2025-09-01', { has_sales: true })])
    expect(revisionHasSales(m, 'H7')).toBe(false)
    expect(revisionHasSales(m, 'FW2026-A2')).toBe(false)
    expect(revisionHasSales(m, 'R9')).toBeNull()
    expect(revisionHasSales(null, 'R2')).toBeNull()
    expect(revisionHasSales(undefined, 'H1')).toBe(false)
  })
})

describe('visibleOverlays', () => {
  it('lists the overlays on screen in the names the agent uses, and nothing for a clean screen', () => {
    const clean = createInitialUiState(null, { rev: 'R2', floor: '3F' })
    expect(visibleOverlays(clean)).toEqual([])
    const busy = {
      ...clean,
      compareRev: 'R1',
      heatmap: { metric: 'sales_per_m2', label: '평효율', values: { '3F-C01': 0.5 } },
      highlights: [{ unit_code: '3F-C01' }],
      edges: [{ from: '3F-C01', to: '3F-C02' }],
    }
    expect(visibleOverlays(busy)).toEqual(['heatmap', 'highlights', 'edges', 'compare'])
    expect(visibleOverlays({ ...clean, flow: { mode: 'traffic' } as UiState['flow'] })).toEqual(['flow'])
    expect(visibleOverlays({ ...clean, route: { title: 'r' } as UiState['route'], journey: { title: 'j' } as UiState['journey'] })).toEqual(['route', 'journey'])
  })
})

describe('payloadString', () => {
  it('reads a non-empty string field of a payload', () => {
    expect(payloadString({ brief_id: 'FW2026' }, 'brief_id')).toBe('FW2026')
    expect(payloadString({ brief_id: '  ' }, 'brief_id')).toBeNull()
    expect(payloadString({ brief_id: 3 }, 'brief_id')).toBeNull()
    expect(payloadString({}, 'brief_id')).toBeNull()
    expect(payloadString(null, 'brief_id')).toBeNull()
    expect(panelPayloadString({ type: 'uploads', payload: { job_id: 'J1' } }, 'uploads', 'job_id')).toBe('J1')
    expect(panelPayloadString({ type: 'uploads', payload: { job_id: 'J1' } }, 'layout', 'job_id')).toBeNull()
  })
})
