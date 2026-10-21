import { describe, expect, it } from 'vitest'
import { createInitialUiState, normalizeUiState } from '../state/uiState'
import { floorLabel, formatManwon, formatMinutes, formatWalk, withEuro } from './format'
import {
  arrivalLabel,
  departureLabel,
  gapText,
  journeyStopCounts,
  journeySubtitle,
  journeySummaryChips,
  normalizeJourney,
  segmentParam,
  transitionText,
  viaLabel,
} from './journey'

/** The example of docs/specs/atlas-api.md §3, completed to four stops on three floors. */
const contractExample = {
  title: '40대 여성 VIP 고객의 쇼핑 동선',
  customer: { id: 'C004211', segment: '40대 여성 VIP' },
  date: '2025-11-08',
  weekday: '토',
  rev: 'R2',
  query: { segment: { age_band: '40s', gender: 'F', tier: 'VIP' }, rank: 1 },
  stops: [
    { order: 2, time: '11:55', unit_code: '1F-C03', floor: '1F', brand_name: 'Vaelora', category: 'COSMETICS', category_name: '화장품', amount_krw: 186000, gap_min: 23, x: 90, y: 37 },
    { order: 1, time: '11:32', unit_code: 'B1-C05', floor: 'B1', brand_name: 'Delino', category: 'FOOD_DELI', category_name: '델리', amount_krw: 24000, gap_min: null, x: 52.4, y: 31.0 },
    { order: 3, time: '13:10', unit_code: '3F-S03', floor: '3F', brand_name: 'Kaelith', category: 'WOMEN_CONTEMPORARY', category_name: '여성 컨템포러리', amount_krw: 290000, gap_min: 75, x: 70.5, y: 12 },
    { order: 4, time: '14:37', unit_code: '3F-C05', floor: '3F', brand_name: 'Cafe Halune', category: 'CAFE', category_name: '카페', amount_krw: 12000, gap_min: 87, x: 60, y: 50 },
  ],
  segments: [
    { floor: 'B1', points: [[52.4, 31.0], [58.0, 31.0]], from_order: 1, to_order: 2 },
    { floor: '1F', points: [[58.0, 31.0], [90, 40], [90, 37]], from_order: 1, to_order: 2 },
  ],
  transitions: [{ from_order: 1, to_order: 2, from_floor: 'B1', to_floor: '1F', via: 'ESC-A' }],
  summary: { stops: 4, floors: ['B1', '1F', '3F'], total_krw: 512000, duration_min: 185, walk_m: 640 },
  caveat: '결제 기록의 순서입니다.',
}

describe('normalizeJourney', () => {
  it('keeps a contract-shaped journey and sorts stops by order', () => {
    const journey = normalizeJourney(contractExample)!
    expect(journey.title).toBe('40대 여성 VIP 고객의 쇼핑 동선')
    expect(journey.stops.map((s) => s.order)).toEqual([1, 2, 3, 4])
    expect(journey.stops[0]).toMatchObject({ unit_code: 'B1-C05', floor: 'B1', amount_krw: 24000, gap_min: null, x: 52.4, y: 31 })
    expect(journey.segments).toHaveLength(2)
    expect(journey.transitions).toEqual([{ from_order: 1, to_order: 2, from_floor: 'B1', to_floor: '1F', via: 'ESC-A' }])
    expect(journey.summary).toEqual({ stops: 4, floors: ['B1', '1F', '3F'], total_krw: 512000, duration_min: 185, walk_m: 640 })
    expect(journey.query).toEqual({ segment: { age_band: '40s', gender: 'F', tier: 'VIP' }, rank: 1 })
  })

  it('tolerates missing and invalid fields', () => {
    const journey = normalizeJourney({
      stops: [
        'bad',
        { unit_code: '3F-C01', time: '10:00', amount_krw: '30000', x: 'n/a', y: 4 },
        { order: 5, floor: '1F', time: '11:15', amount_krw: 6500, x: 1, y: 2 },
        { brand_name: 'no unit and no floor' },
      ],
      segments: [
        { floor: '3F', points: [[0, 0], ['x', 1], [5, 5], { x: 6, y: 7 }] },
        { points: [[0, 0], [1, 1]] },
        { floor: '1F', points: 'none' },
      ],
      query: { rank: -3 },
      customer: 'C1',
      summary: null,
    })!
    expect(journey.title).toBe('고객 쇼핑 동선')
    expect(journey.customer).toBeNull()
    expect(journey.caveat).toBeNull()
    // Index-based order for the stop without `order`, floor from the unit code, invalid x -> null.
    expect(journey.stops.map((s) => [s.order, s.floor, s.unit_code])).toEqual([
      [2, '3F', '3F-C01'],
      [5, '1F', ''],
    ])
    expect(journey.stops[0]).toMatchObject({ x: null, y: 4, amount_krw: 30000, gap_min: null, brand_name: null })
    expect(journey.segments).toEqual([{ floor: '3F', points: [[0, 0], [5, 5], [6, 7]], from_order: null, to_order: null }])
    // No `transitions` array: derived from consecutive stops on different floors.
    expect(journey.transitions).toEqual([{ from_order: 2, to_order: 5, from_floor: '3F', to_floor: '1F', via: null }])
    expect(journey.summary).toEqual({ stops: 2, floors: ['3F', '1F'], total_krw: 36500, duration_min: 75, walk_m: null })
    expect(journey.query).toEqual({ segment: null, rank: 1 })
    expect(normalizeJourney({ ...contractExample, query: { rank: 2, rev: ' R1 ' } })!.query).toEqual({ segment: null, rank: 2, rev: 'R1' })
    expect(normalizeJourney({ ...contractExample, query: { rank: 2, rev: {} } })!.query).toEqual({ segment: null, rank: 2 })
  })

  it('returns null for non-objects and journeys without usable stops', () => {
    expect(normalizeJourney(null)).toBeNull()
    expect(normalizeJourney('journey')).toBeNull()
    expect(normalizeJourney([contractExample])).toBeNull()
    expect(normalizeJourney({ title: 'x', stops: [] })).toBeNull()
    expect(normalizeJourney({ title: 'x', stops: [{ brand_name: 'nowhere' }] })).toBeNull()
  })

  it('is part of the UI state: default null, normalized when patched, invalid values dropped', () => {
    const ui = createInitialUiState(null, { floor: '3F' })
    expect(ui.journey).toBeNull()
    expect(Object.prototype.hasOwnProperty.call(ui, 'journey')).toBe(true)
    const next = normalizeUiState({ ...ui, journey: contractExample, panel: { type: 'journey', payload: {} } }, ui)
    expect(next.journey?.stops).toHaveLength(4)
    expect(next.panel).toEqual({ type: 'journey', payload: {} })
    // Untouched journeys keep their identity across unrelated patches.
    expect(normalizeUiState({ ...next, floor: '1F' }, next).journey).toBe(next.journey)
    expect(normalizeUiState({ ...next, journey: { stops: 'oops' } }, next).journey).toBeNull()
    const withoutJourney: Record<string, unknown> = { ...next }
    delete withoutJourney.journey
    expect(normalizeUiState(withoutJourney, next).journey).toBeNull()
  })
})

describe('journey helpers', () => {
  it('encodes the segment filter like the cross-shopping endpoint', () => {
    expect(segmentParam({ age_band: '40s', gender: 'F', tier: ['VIP', 'VVIP'] })).toBe('age_band:40s;gender:F;tier:VIP|VVIP')
    expect(segmentParam({ age_band: '40s', gender: null, tier: [] })).toBe('age_band:40s')
    expect(segmentParam(' tier:VIP ')).toBe('tier:VIP')
    expect(segmentParam(null)).toBe('')
  })

  it('counts stops per floor', () => {
    expect(Object.fromEntries(journeyStopCounts(normalizeJourney(contractExample)))).toEqual({ B1: 1, '1F': 1, '3F': 2 })
    expect(journeyStopCounts(null).size).toBe(0)
  })

  it('formats floors, money, durations and distances in Korean', () => {
    expect(floorLabel('B1')).toBe('지하 1층')
    expect(floorLabel('3F')).toBe('3층')
    expect(floorLabel('10F')).toBe('10층')
    expect(floorLabel('M2')).toBe('M2')
    expect(withEuro('3층')).toBe('3층으로')
    expect(withEuro('서울')).toBe('서울로')
    expect(withEuro('카페')).toBe('카페로')
    expect(formatManwon(24000)).toBe('2.4만원')
    expect(formatManwon(30000)).toBe('3만원')
    expect(formatManwon(512000)).toBe('51만원')
    expect(formatManwon(12_500_000)).toBe('1,250만원')
    expect(formatManwon(123_000_000)).toBe('1.2억원')
    expect(formatManwon(6500)).toBe('6,500원')
    expect(formatManwon(null)).toBe('–')
    expect(formatMinutes(185)).toBe('3시간 5분')
    expect(formatMinutes(45)).toBe('45분')
    expect(formatMinutes(120)).toBe('2시간')
    expect(formatWalk(640)).toBe('640m')
    expect(formatWalk(1250)).toBe('1.3km')
    expect(gapText(23)).toBe('23분 후')
    expect(gapText(null)).toBe('')
  })

  it('describes floor changes for the card and the map', () => {
    const up = { from_order: 1, to_order: 2, from_floor: '1F', to_floor: '3F', via: 'ESC-A' }
    const down = { from_order: 3, to_order: 4, from_floor: '3F', to_floor: 'B1', via: null }
    expect(transitionText(up)).toBe('↑ 3층으로 이동 (에스컬레이터 A)')
    expect(transitionText(down)).toBe('↓ 지하 1층으로 이동')
    expect(departureLabel(up)).toBe('→ 3층 (ESC-A)')
    expect(departureLabel(down)).toBe('→ 지하 1층')
    expect(arrivalLabel(up)).toBe('1층에서 올라옴')
    expect(arrivalLabel(down)).toBe('3층에서 내려옴')
    // Ordinals from /api/meta take precedence over the code.
    const floors = [
      { code: '3F', ordinal: 0, name: '' },
      { code: '1F', ordinal: 5, name: '' },
    ]
    expect(arrivalLabel(up, floors)).toBe('1층에서 내려옴')
    expect(viaLabel('EV-2')).toBe('엘리베이터 2')
    expect(viaLabel('ST-1')).toBe('계단 1')
    expect(viaLabel('LINK-9')).toBe('LINK-9')
  })

  it('builds the card summary chips and subtitle', () => {
    const journey = normalizeJourney(contractExample)!
    expect(journeySummaryChips(journey)).toEqual(['결제 4회', '3개 층', '총 51만원', '3시간 5분', '추정 이동 640m'])
    expect(journeySubtitle(journey)).toBe('40대 여성 VIP · 2025-11-08(토) · 고객 C004211')
    const minimal = normalizeJourney({ stops: [{ unit_code: '3F-C01', time: '10:00' }] })!
    expect(journeySummaryChips(minimal)).toEqual(['결제 1회', '1개 층'])
  })
})
