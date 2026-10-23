import { describe, expect, it } from 'vitest'
import type { Meta, PhasingStep, RelayoutAlternative } from '../types/api'
import {
  brandDisplay,
  checkRevisionOptions,
  defaultCheckRevision,
  defaultExpandedAlternative,
  firstChangedFloor,
  groupRelayoutChanges,
  isBaselineAlternative,
  normalizePhasing,
  normalizePlan,
  normalizeProject,
  pastDiagnosis,
  phasingCycleLoop,
  phasingCycleText,
  phasingDefaults,
  phasingStepRemark,
  phasingWeekText,
  relayoutPreview,
  seasonExtremes,
  seasonText,
  weeksText,
} from './relayout'

const plan = normalizePlan({
  alternatives: [
    { alt_id: 'ALT-0', rev_id: 'FW2026-A0', kpi: { net_krw: 0, moves: 0 }, changes: [] },
    {
      alt_id: 'ALT-1',
      rev_id: 'FW2026-A1',
      summary: '아웃도어를 아트리움 가까이',
      kpi: { net_krw: 150_000_000, vs_current_objective_krw: '230000000' },
      changes: [
        { type: 'exit', brand_id: 'B1', brand_name: 'Quorelle', from_unit: '4F-E01' },
        { type: 'move', brand_id: 'B2', brand_name: 'Lumivane', from_unit: '4F-C06', to_unit: '4F-E01' },
        { type: 'entry', brand_id: 'B3', brand_name: 'Norvane Outdoor', to_unit: '6F-C06' },
        { type: 'swap', brand_id: 'B4', brand_name: '기타' },
      ],
      constraints: [{ constraint: '앵커 고정', satisfied: true }],
      explanations: ['F/W 가중치'],
    },
  ],
})!
const [baseline, alt] = plan.alternatives

describe('relayout normalization', () => {
  it('reads plans with the baseline alternative, explanations and the extra KPI', () => {
    expect(isBaselineAlternative(baseline)).toBe(true)
    expect(isBaselineAlternative(alt)).toBe(false)
    expect(alt.kpi).toMatchObject({ net_krw: 150_000_000, vs_current_objective_krw: 230_000_000, moves: 1, move_cost_krw: null })
    expect(alt.constraints).toEqual([{ name: '앵커 고정', ok: true }])
    expect(alt.explanations).toEqual(['F/W 가중치'])
    expect(alt.changes[0]).toMatchObject({ type: 'exit', from_floor: '4F', to_unit: null, to_floor: null })
    expect(plan).toMatchObject({ unplaced: [], caveats: [], current: { sales_krw: null } })
    expect(normalizePlan('x')).toBeNull()
  })

  it('reads a project with the extra brief, diagnosis and check fields', () => {
    const project = normalizeProject({
      brief: {
        brief_id: 'FW2026',
        season: 'FW',
        season_label: 'F/W(9~2월)',
        objective: 'commission',
        objective_label: '수수료 수익',
        area_tolerance: 0.15,
        entries: [{ brand_id: 'NB01', brand_name: 'Norvane Outdoor', target_area_m2: 200, new_brand: true, attractiveness: 1.22 }],
      },
      status: 'checked',
      diagnosis: {
        period: { start: '2025-10-01', end: '2026-09-30', days: 365 },
        candidate_total: 12,
        entries: [{ brand_id: 'NB01', feasible_unit_codes: ['5F-W03', '6F-C02'], season_factor: 1.32, note: '선호 6F' }],
      },
      check: {
        rev: 'R3',
        items: [{ type: 'move', type_label: '이전', status: 'match', status_label: '일치', from_unit: '4F-C06' }],
        unexpected: [
          { unit_code: '4F-N02', before_brand: 'BR0333', before_brand_name: 'Dovrelle', after_brand: 'BR0166', after_brand_name: 'Talvessa' },
          { unit_code: '4F-N03', before_brand: 'BR0334', after_brand: null },
        ],
      },
    })
    expect(project.brief).toMatchObject({ title: 'FW2026', season_label: 'F/W(9~2월)', objective_label: '수수료 수익', area_tolerance: 0.15, exits: [] })
    expect(project.brief.entries[0]).toMatchObject({ target_area_m2: [200, 200], new_brand: true, attractiveness: 1.22 })
    expect(project.diagnosis).toMatchObject({ period: { start: '2025-10-01', end: '2026-09-30', days: 365 }, candidate_total: 12, candidates: [] })
    expect(project.diagnosis?.entries[0]).toMatchObject({ feasible_units: 2, feasible_unit_codes: ['5F-W03', '6F-C02'], season_factor: 1.32, note: '선호 6F' })
    expect(project.check?.items[0]).toEqual({
      type: 'move',
      type_label: '이전',
      brand_name: '',
      from_unit: '4F-C06',
      planned_unit: null,
      actual_unit: null,
      status: 'match',
      status_label: '일치',
    })
    // Unplanned changes carry brand IDs and display names; the UI shows the name, else the ID.
    const [named, unnamed] = project.check!.unexpected
    expect(named).toEqual({ unit_code: '4F-N02', before_brand: 'BR0333', after_brand: 'BR0166', before_brand_name: 'Dovrelle', after_brand_name: 'Talvessa' })
    expect([brandDisplay(named.before_brand_name, named.before_brand), brandDisplay(named.after_brand_name, named.after_brand)]).toEqual(['Dovrelle', 'Talvessa'])
    expect([brandDisplay(unnamed.before_brand_name, unnamed.before_brand), brandDisplay(unnamed.after_brand_name, unnamed.after_brand)]).toEqual(['BR0334', '(공실)'])
    expect(project).toMatchObject({ plan: null, selected_alt_id: null, report_url: null, phasing: null })
    expect(() => normalizeProject({ status: 'draft' })).toThrow()
  })

  it('reads a stored construction order with steps, entries, cycles and the entry delay', () => {
    const project = normalizeProject({
      brief: { brief_id: 'FW2026', downtime_weeks: 3 },
      phasing: {
        alt_id: 'ALT-2',
        params: { crews: '3', fitout_weeks: 2.5, use_temporary: false },
        steps: [
          { start_week: 2, end_week: 1, action: 'entry', unit_code: '6F-S02B', brand_name: 'Norvane Outdoor', waits_for: [1] },
          { step: 1, action: 'close', unit_code: '4F-C06', brand_id: 'BR0101', closed_weeks: 4, lost_krw: '66481534' },
          'bad',
        ],
        lost_krw: 66_481_534,
        naive_lost_krw: 616_032_748,
        entry_delay_krw: 147_000_000,
        entries: [{ brand_name: 'Norvane Outdoor', unit_code: '6F-S02B', open_week: 8, earliest_week: 6 }],
        cycles: [{ brands: ['Novier & Co', 'Kaelova Intimates'], resolved_by: 'close', weeks: 4 }],
      },
    })
    const phasing = project.phasing!
    expect(phasing.params).toEqual({ crews: 3, fitout_weeks: 2.5, temporary_sales_ratio: null, use_temporary: false })
    // Missing numbers: the step from the order, the end no earlier than the start, the total from the last end.
    expect(phasing.steps.map((s) => [s.step, s.start_week, s.end_week, s.floor, s.brand_name])).toEqual([
      [1, 2, 2, '6F', 'Norvane Outdoor'],
      [1, 0, 0, '4F', 'BR0101'],
    ])
    expect(phasing.total_weeks).toBe(2)
    expect(phasing.saved_krw).toBe(616_032_748 - 66_481_534)
    expect(phasing.entry_delay_krw).toBe(147_000_000)
    expect(phasing.entries[0]).toEqual({ brand_id: null, brand_name: 'Norvane Outdoor', unit_code: '6F-S02B', open_week: 8, earliest_week: 6 })
    expect(phasing.cycles[0]).toEqual({ brands: ['Novier & Co', 'Kaelova Intimates'], units: [], breaker: null, resolved_by: 'close', unit_code: null, weeks: 4 })
    expect(normalizePhasing('none')).toBeNull()
  })
})

describe('relayout display helpers', () => {
  it('groups changes as 입점, 퇴점, 이전 and then the rest', () => {
    expect(groupRelayoutChanges(alt.changes).map((g) => `${g.label}:${g.items.length}`)).toEqual(['입점:1', '퇴점:1', '이전:1', 'swap:1'])
    expect(firstChangedFloor(alt)).toBe('4F')
    expect(firstChangedFloor(baseline)).toBeNull()
  })

  it('previews an alternative against the base revision on its first changed floor', () => {
    const view = relayoutPreview(alt, 'R2', ['4F', '5F', '6F'])
    expect(view).toMatchObject({ rev: 'FW2026-A1', compareRev: 'R2', floor: '4F', codes: ['4F-E01', '4F-C06', '6F-C06'] })
    // 4F-E01 is left by Quorelle and taken by Lumivane: the arrival wins.
    expect(view.highlights.find((h) => h.unit_code === '4F-E01')?.reason).toBe('ALT-1: Lumivane 이전 후')
    expect(view.highlights.find((h) => h.unit_code === '6F-C06')?.reason).toBe('ALT-1: Norvane Outdoor 입점')
    expect(view.edges).toEqual([expect.objectContaining({ from: '4F-C06', to: '4F-E01', label: 'Lumivane' })])
    expect(relayoutPreview(alt, 'R2', ['1F', '3F']).floor).toBeNull()
    const noRev: RelayoutAlternative = { ...alt, rev_id: null }
    expect(relayoutPreview(noRev, 'R2').rev).toBeNull()
  })

  it('defaults the drawing check to the newest uploaded child of the base revision', () => {
    const meta = {
      facility: '',
      floors: [],
      categories: [],
      revisions: [
        { rev_id: 'R1', kind: 'actual', parent_rev_id: 'R0', effective_date: '2025-03-01' },
        { rev_id: 'R2', kind: 'actual', parent_rev_id: 'R1', effective_date: '2025-09-01' },
        { rev_id: 'R3', kind: 'actual', parent_rev_id: 'R2', effective_date: '2026-10-01', model_source: 'uploaded' },
        { rev_id: 'R3B', kind: 'actual', parent_rev_id: 'R2', effective_date: '2026-10-02', model_source: 'uploaded' },
        { rev_id: 'R4', kind: 'actual', parent_rev_id: 'R1', effective_date: '2027-01-01', model_source: 'uploaded' },
        { rev_id: 'FW2026-A1', kind: 'hypothetical', parent_rev_id: 'R2', effective_date: null },
      ],
    } satisfies Meta
    expect(checkRevisionOptions(meta, 'R2').map((r) => r.rev_id)).toEqual(['R4', 'R3B', 'R3', 'R1'])
    expect(defaultCheckRevision(meta, 'R2')).toBe('R3B')
    expect(defaultCheckRevision({ ...meta, revisions: meta.revisions.filter((r) => r.rev_id !== 'R3B' && r.rev_id !== 'R3') }, 'R2')).toBe('R4')
    expect(defaultCheckRevision({ ...meta, revisions: [meta.revisions[1]] }, 'R2')).toBeNull()
  })

  it('opens the decided card first, else the best non-baseline alternative by net effect', () => {
    const three = normalizePlan({
      alternatives: [
        { alt_id: 'ALT-0', rev_id: 'FW2026-A0', kpi: { net_krw: 900_000_000 } },
        { alt_id: 'ALT-1', rev_id: 'FW2026-A1', kpi: { net_krw: 120_000_000 } },
        { alt_id: 'ALT-2', rev_id: 'FW2026-A2', kpi: { net_krw: 180_000_000 } },
        { alt_id: 'ALT-3', rev_id: 'FW2026-A3', kpi: { net_krw: 180_000_000 } },
      ],
    })
    // ALT-0 is never the default (its KPI is the reference); ties keep the first one.
    expect(defaultExpandedAlternative(three, null)).toBe('ALT-2')
    expect(defaultExpandedAlternative(three, 'ALT-1')).toBe('ALT-1')
    expect(defaultExpandedAlternative(three, 'ALT-9')).toBe('ALT-2')
    expect(defaultExpandedAlternative(normalizePlan({ alternatives: [{ alt_id: 'ALT-0' }] }), null)).toBe('ALT-0')
    expect(defaultExpandedAlternative(normalizePlan({ alternatives: [] }), null)).toBeNull()
    expect(defaultExpandedAlternative(null, 'ALT-1')).toBeNull()
  })

  it('folds the diagnosis away once alternatives exist', () => {
    expect(['draft', 'diagnosed', 'planned', 'selected', 'checked', 'invalid'].map(pastDiagnosis)).toEqual([false, false, true, true, true, false])
  })

  it('describes construction steps, cycles and the form defaults', () => {
    const step = (over: Partial<PhasingStep>): PhasingStep => ({
      step: 1,
      start_week: 0,
      end_week: 2,
      action: 'move',
      action_label: '이전 공사',
      unit_code: '4F-E01',
      floor: '4F',
      brand_id: 'B1',
      brand_name: 'Lumivane',
      from_unit: '4F-C06',
      waits_for: [],
      closed_weeks: 0,
      temporary_unit: null,
      lost_krw: 0,
      note: null,
      ...over,
    })
    expect(phasingWeekText(step({}))).toBe('0~2주')
    expect(phasingWeekText(step({ action: 'exit', start_week: 0, end_week: 0 }))).toBe('0주')
    expect(phasingWeekText(step({ start_week: 1.5, end_week: 4 }))).toBe('1.5~4주')
    expect([weeksText(10), weeksText(2.5), weeksText(null)]).toEqual(['10주', '2.5주', '–'])
    expect(phasingStepRemark(step({ waits_for: [2] }))).toBe('4F-C06에서 · 2번 작업 뒤')
    expect(phasingStepRemark(step({ action: 'entry', from_unit: null, start_week: 2, end_week: 4 }))).toBe('4주차 개점')
    expect(phasingStepRemark(step({ action: 'close', unit_code: '4F-C06', closed_weeks: 4, lost_krw: 66_481_534 }))).toBe('4F-C06 휴점 4주 · 손실 6,648만 원')
    expect(phasingStepRemark(step({ action: 'temporary', unit_code: '4F-E02', temporary_unit: '4F-E02' }))).toBe('4F-C06에서 임시 이전')
    expect(phasingStepRemark(step({ action: 'exit', from_unit: '4F-E02' }))).toBe('')

    expect(phasingCycleLoop(['A', 'B'])).toBe('A ↔ B')
    expect(phasingCycleLoop(['A', 'B', 'C'])).toBe('A → B → C → A')
    const close = { brands: ['Novier & Co', 'Kaelova Intimates'], units: ['4F-C06', '4F-C20'], breaker: 'Novier & Co', resolved_by: 'close', unit_code: null, weeks: 4 }
    expect(phasingCycleText(close)).toBe('서로 자리를 바꾸는 순환(Novier & Co ↔ Kaelova Intimates)은 Novier & Co 매장이 4주 동안 휴점해서 풉니다.')
    expect(phasingCycleText({ ...close, resolved_by: 'temporary', unit_code: '4F-E02' })).toBe(
      '서로 자리를 바꾸는 순환(Novier & Co ↔ Kaelova Intimates)은 Novier & Co 매장을 4주 동안 임시 매장(4F-E02)으로 옮겨 풉니다.',
    )

    const brief = normalizeProject({ brief: { brief_id: 'FW2026', downtime_weeks: 3 } }).brief
    expect(phasingDefaults({ brief, phasing: null })).toEqual({ crews: 2, fitoutWeeks: 3, useTemporary: true })
    expect(phasingDefaults({ brief: { ...brief, downtime_weeks: 0 }, phasing: null }).fitoutWeeks).toBe(1)
    expect(phasingDefaults({ brief: { ...brief, downtime_weeks: null }, phasing: null }).fitoutWeeks).toBe(2)
    const stored = normalizePhasing({ params: { crews: 9, fitout_weeks: 4, use_temporary: false } })
    expect(phasingDefaults({ brief, phasing: stored })).toEqual({ crews: 6, fitoutWeeks: 4, useTemporary: false })
  })

  it('names seasons and picks strong and weak categories', () => {
    expect(seasonText('FW', 2026)).toBe('2026 F/W')
    expect(seasonText('ss', null)).toBe('S/S')
    const { strong, weak } = seasonExtremes([
      { category: 'A', category_name: 'A', factor: 1.3 },
      { category: 'B', category_name: 'B', factor: 0.8 },
      { category: 'C', category_name: 'C', factor: 1.0 },
      { category: 'D', category_name: 'D', factor: 0.9 },
    ])
    expect(strong.map((f) => f.category)).toEqual(['A'])
    expect(weak.map((f) => f.category)).toEqual(['B', 'D'])
  })
})
