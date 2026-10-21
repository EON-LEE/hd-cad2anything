import { describe, expect, it } from 'vitest'
import { formatObjective, normalizeAlternative, normalizeAlternatives } from './layout'

describe('normalizeAlternative', () => {
  it('keeps the contract shape as is', () => {
    const alt = {
      id: 'ALT-3F-A',
      moves: [{ brand_id: 'BR1', brand_name: 'Quorelle', from_unit: '3F-E01', to_unit: '3F-C06' }],
      objective_before: 0.62,
      objective_after: 0.71,
      kpi_delta: { sales_krw: 280_000_000, low: 110_000_000, high: 420_000_000 },
      constraints: [{ name: '카테고리 존 연속성', ok: true }],
      explanations: ['설명'],
    }
    expect(normalizeAlternative(alt, 0)).toEqual(alt)
  })

  it('maps the layout engine shape (alt_id, delta_krw, objective range, constraint_checks)', () => {
    const alt = normalizeAlternative(
      {
        alt_id: 'A2',
        summary: '2건 이동',
        objective_before: 10_000_000_000,
        objective_after: 10_300_000_000,
        objective_low: 10_100_000_000,
        objective_high: 10_450_000_000,
        delta_krw: 300_000_000,
        moves: [{ brand_id: 'BR9', brand_name: 'Atelier Noa', category: 'ACCESSORIES', from_unit: '3F-N03', to_unit: '3F-C06', delta: 1 }],
        constraint_checks: [
          { constraint: '앵커 고정', rule: 'r', checked: 3, violations: 0, satisfied: true, detail: '위반 없음' },
          { constraint: '이동 수', rule: 'r', checked: 1, violations: 1, satisfied: false, detail: '7건 이동' },
        ],
        rev_id: 'H2',
      },
      1,
    )
    expect(alt).toEqual({
      id: 'A2',
      moves: [{ brand_id: 'BR9', brand_name: 'Atelier Noa', from_unit: '3F-N03', to_unit: '3F-C06' }],
      objective_before: 10_000_000_000,
      objective_after: 10_300_000_000,
      kpi_delta: { sales_krw: 300_000_000, low: 100_000_000, high: 450_000_000 },
      constraints: [
        { name: '앵커 고정', ok: true, detail: '위반 없음' },
        { name: '이동 수', ok: false, detail: '7건 이동' },
      ],
      explanations: ['2건 이동'],
      rev_id: 'H2',
    })
  })

  it('drops malformed entries and invalid moves', () => {
    const list = normalizeAlternatives([null, 'x', { moves: [{ brand_id: 'B' }], objective_before: 1, objective_after: 2 }])
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ id: 'ALT-3', moves: [], kpi_delta: { sales_krw: 1, low: 1, high: 1 } })
    expect(normalizeAlternatives(undefined)).toEqual([])
  })
})

describe('formatObjective', () => {
  it('formats scores and KRW amounts', () => {
    expect(formatObjective(0.62)).toBe('0.62')
    expect(formatObjective(0.09, true)).toBe('+0.09')
    expect(formatObjective(300_000_000, true)).toBe('+3.0억 원')
  })
})
