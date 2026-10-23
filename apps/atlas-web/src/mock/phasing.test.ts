import { describe, expect, it } from 'vitest'
import type { RelayoutChange } from '../types/api'
import { planConstruction, type ConstructionInput } from './phasing'

const change = (type: string, brand: string, from: string | null, to: string | null, delta = 0): RelayoutChange => ({
  type,
  brand_id: brand,
  brand_name: `Brand ${brand}`,
  category_name: null,
  from_unit: from,
  to_unit: to,
  from_floor: from ? from.split('-')[0] : null,
  to_floor: to ? to.split('-')[0] : null,
  delta_krw: delta,
  explanation: null,
})

// BA (1M/week) and BB (2M/week) swap units; BX leaves 4F-E02; 4F-V01 is vacant.
const swap: ConstructionInput = {
  altId: 'ALT-2',
  changes: [change('exit', 'BX', '4F-E02', null), change('move', 'BA', '4F-C06', '4F-C20'), change('move', 'BB', '4F-C20', '4F-C06')],
  occupancy: new Map([
    ['4F-C06', 'BA'],
    ['4F-C20', 'BB'],
    ['4F-E02', 'BX'],
  ]),
  areas: new Map([
    ['4F-C06', 120],
    ['4F-C20', 150],
    ['4F-E02', 118],
    ['4F-V01', 200],
  ]),
  annualSales: new Map([
    ['4F-C06', 52_000_000],
    ['4F-C20', 104_000_000],
  ]),
  baseRev: 'R2',
  crews: 2,
  weeks: 2,
  ratio: 0.6,
  useTemporary: true,
}

describe('mock construction order', () => {
  it('breaks a swap with the brand of the lowest weekly sales, in a temporary unit of a similar size', () => {
    const plan = planConstruction(swap)
    expect(plan.steps.map((s) => `${s.step}:${s.start_week}-${s.end_week}:${s.action}:${s.unit_code}:${s.brand_id}:${s.waits_for.join(',')}`)).toEqual([
      '1:0-0:exit:4F-E02:BX:',
      // The exiting unit (closest in area) serves as the temporary unit until BA's new unit is ready.
      '2:0-4:temporary:4F-E02:BA:',
      '3:0-2:move:4F-C06:BB:',
      '4:2-4:move:4F-C20:BA:3',
    ])
    expect(plan.steps[1]).toMatchObject({ action_label: '임시 매장 영업', from_unit: '4F-C06', temporary_unit: '4F-E02', closed_weeks: 0 })
    expect(plan.steps[2].note).toBe('Brand BA 임시 매장 이전 뒤 바로 시작해요. 기존 매장 4F-C20에서 영업하다가 공사가 끝나면 옮겨요.')
    expect(plan.steps[3].note).toBe('Brand BB 이전 공사가 끝난 뒤 시작해요. 임시 매장 4F-E02에서 영업하다가 공사가 끝나면 옮겨요.')
    expect(plan.cycles).toEqual([
      { brands: ['Brand BA', 'Brand BB'], units: ['4F-C06', '4F-C20'], breaker: 'Brand BA', resolved_by: 'temporary', unit_code: '4F-E02', weeks: 4 },
    ])
    // 4 weeks at 40% less of 1M per week, against 2 weeks of closure for both movers.
    expect(plan).toMatchObject({ total_weeks: 4, lost_krw: 1_600_000, naive_lost_krw: 6_000_000, saved_krw: 4_400_000, entry_delay_krw: 0 })
    expect(plan.summary).toContain('서로 자리를 바꾸는 순환(Brand BA ↔ Brand BB)은 Brand BA 매장을 4주 동안 임시 매장(4F-E02)으로 옮겨서 풀어요.')
    expect(plan.caveats).toContain('임시 매장 매출은 평소의 60%로 봤어요. 임시 매장 공사 비용은 넣지 않았어요.')
  })

  it('closes the brand instead when temporary units are off or none is free', () => {
    const closed = planConstruction({ ...swap, useTemporary: false })
    expect(closed.steps[1]).toMatchObject({ action: 'close', action_label: '임시 휴점', unit_code: '4F-C06', closed_weeks: 4, temporary_unit: null, lost_krw: 4_000_000 })
    expect(closed.cycles[0]).toMatchObject({ resolved_by: 'close', unit_code: null, weeks: 4 })
    expect(closed.summary).toContain('쓸 수 있는 빈 매장이 없어 Brand BA 매장이 4주 동안 휴점해야 해요.')
    const noSpace = planConstruction({ ...swap, changes: swap.changes.slice(1), areas: new Map([...swap.areas].filter(([unit]) => unit !== '4F-V01' && unit !== '4F-E02')) })
    expect(noSpace.steps[0]).toMatchObject({ action: 'close', lost_krw: 4_000_000 })
  })

  it('counts the sales new brands lose when too few crews delay their opening', () => {
    const plan = planConstruction({
      ...swap,
      changes: [change('entry', 'E1', null, '4F-V01', 52_000_000), change('entry', 'E2', null, '4F-V02', 104_000_000)],
      occupancy: new Map(),
      areas: new Map([
        ['4F-V01', 100],
        ['4F-V02', 100],
      ]),
      crews: 1,
    })
    // The brand with more sales opens first; the other one waits two weeks for the crew.
    expect(plan.entries).toEqual([
      { brand_id: 'E2', brand_name: 'Brand E2', unit_code: '4F-V02', open_week: 2, earliest_week: 2 },
      { brand_id: 'E1', brand_name: 'Brand E1', unit_code: '4F-V01', open_week: 4, earliest_week: 2 },
    ])
    expect(plan).toMatchObject({ total_weeks: 4, lost_krw: 0, naive_lost_krw: 0, entry_delay_krw: 2_000_000 })
    expect(plan.summary).toContain('공사팀이 모자라 신규 입점이 늦어지는 기회 손실은 200만 원이에요.')
    expect(planConstruction({ ...swap, changes: [] }).summary).toBe('ALT-2에는 공사할 매장이 없어요.')
  })
})
