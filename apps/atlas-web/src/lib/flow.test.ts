import { describe, expect, it } from 'vitest'
import { createInitialUiState, normalizeUiState } from '../state/uiState'
import {
  FLOW_STOPS,
  FLOW_WIDTH_MAX_PX,
  FLOW_WIDTH_MIN_PX,
  flowColor,
  flowDataFloors,
  flowFloorStats,
  flowFloorText,
  flowLegendInfo,
  flowMarkerName,
  flowMarkerText,
  flowMarkerTitle,
  flowModeLabel,
  flowSummaryChips,
  flowValueText,
  flowWidthPx,
  normalizeFlow,
  sortedFlowMarkers,
} from './flow'
import { hexToRgba } from './colors'

/** The example of docs/specs/atlas-api.md §3. */
const contractExample = {
  mode: 'traffic',
  title: '40대 여성 VIP 고객의 추정 유동(주말 10~14시)',
  rev: 'R2',
  query: { segment: { age_band: '40s', gender: 'F', tier: 'VIP' }, day_type: 'weekend', hours: [10, 14], floor: null },
  floors: {
    '3F': {
      segments: [{ points: [[12.0, 15.0], [40.0, 15.0]], weight: 0.82, value: 1234, color: null, label: '3F-C05·3F-C06 앞 통로' }],
      max_value: 1500,
      total: 23456,
    },
  },
  markers: [{ floor: '3F', x: 60.0, y: 31.5, rank: 1, kind: 'spot', label: '1', title: '후보 1 · 3F 에스컬레이터 A 앞 통로', unit_code: null }],
  legend: { low: '적음', high: '많음', unit: '명(추정)', note: '결제 순서를 통로망 최단 경로로 이어 모은 값' },
  summary: { customer_days: 1234, trips: 4567, floors: ['3F', '4F'] },
  caveat: '결제 순서를 통로망 최단 경로로 이은 추정이에요. 실제로 걸은 길이 아니에요.',
}

describe('normalizeFlow', () => {
  it('keeps a contract-shaped flow as is', () => {
    expect(normalizeFlow(contractExample)).toEqual(contractExample)
  })

  it('drops bad segments and markers and fixes weights, colors and numbers', () => {
    const flow = normalizeFlow({
      mode: 'traffic',
      floors: {
        '1F': {
          max_value: '200',
          segments: [
            { points: [[0, 0], [10, 0]], value: 100 },
            { points: [[0, 0], ['x', 1], { x: 5, y: 5 }], weight: 1.7, color: 'red' },
            { points: [[0, 0]], weight: 0.5 },
            { points: 'none', weight: 0.5 },
            { points: [[0, 0], [1, 1]], weight: -2, color: '#E53935', label: '  ' },
            'bad',
          ],
        },
        '3F': 'none',
      },
      markers: [
        { x: 1, y: 2, rank: '2', unit_code: '1F-C03' },
        { floor: '1F', x: 'n/a', y: 2 },
        { x: 1, y: 2 },
        { floor: '1F', x: 3, y: 4, kind: 'warning' },
      ],
      summary: 'none',
    })!
    expect(flow.floors['1F'].segments).toEqual([
      // A missing weight is derived from value / max_value.
      { points: [[0, 0], [10, 0]], weight: 0.5, value: 100, color: null, label: null },
      // Weights are clamped to 0..1; colors other than hex are left to the weight scale.
      { points: [[0, 0], [5, 5]], weight: 1, value: null, color: null, label: null },
      { points: [[0, 0], [1, 1]], weight: 0, value: null, color: '#E53935', label: null },
    ])
    expect(flow.floors['1F']).toMatchObject({ max_value: 200, total: null })
    expect(flow.floors['3F']).toBeUndefined()
    // The floor of a marker comes from its unit code; a marker without a floor or a position is dropped.
    expect(flow.markers).toEqual([
      { x: 1, y: 2, rank: 2, unit_code: '1F-C03', floor: '1F', kind: 'spot', label: '2', title: null },
      { floor: '1F', x: 3, y: 4, kind: 'warning', rank: null, label: null, title: null, unit_code: null },
    ])
    expect(flow).toMatchObject({ title: '추정 유동', rev: null, query: null, legend: null, caveat: null })
    // Without summary floors, the floors with something to draw.
    expect(flow.summary).toEqual({ customer_days: null, trips: null, floors: ['1F'] })
  })

  it('accepts floors as a list and keeps an egress result without findings', () => {
    const listed = normalizeFlow({ mode: 'popup', floors: [{ floor: '3F', segments: [{ points: [[0, 0], [1, 0]], weight: 0.3 }] }, { segments: [] }] })!
    expect(Object.keys(listed.floors)).toEqual(['3F'])
    const empty = normalizeFlow({ mode: 'egress', floors: {}, markers: [] })!
    expect(empty).toMatchObject({ mode: 'egress', title: '통로 폭·피난 점검', markers: [] })
    expect(flowSummaryChips(empty)).toEqual(['좁은 통로 0곳', '피난 거리 초과 0곳'])
    // Egress values are widths: no weight is derived from them.
    const egress = normalizeFlow({ mode: 'egress', floors: { '3F': { max_value: 6, segments: [{ points: [[0, 0], [1, 0]], value: 3 }] } } })!
    expect(egress.floors['3F'].segments[0].weight).toBeNull()
  })

  it('returns null for values that are not a flow', () => {
    expect(normalizeFlow(null)).toBeNull()
    expect(normalizeFlow('flow')).toBeNull()
    expect(normalizeFlow([contractExample])).toBeNull()
    expect(normalizeFlow({ title: '제목만' })).toBeNull()
  })

  it('is part of the UI state: default null, normalized when patched, invalid values dropped', () => {
    const ui = createInitialUiState(null, { floor: '3F' })
    expect(ui.flow).toBeNull()
    expect(Object.prototype.hasOwnProperty.call(ui, 'flow')).toBe(true)
    const next = normalizeUiState({ ...ui, flow: contractExample }, ui)
    expect(next.flow?.floors['3F'].segments).toHaveLength(1)
    // Untouched flows keep their identity across unrelated patches.
    expect(normalizeUiState({ ...next, floor: '1F' }, next).flow).toBe(next.flow)
    expect(normalizeUiState({ ...next, flow: { title: 'x' } }, next).flow).toBeNull()
  })
})

describe('flow helpers', () => {
  const flow = normalizeFlow({
    ...contractExample,
    mode: 'popup',
    floors: {
      ...contractExample.floors,
      '4F': { segments: [{ points: [[0, 0], [5, 0]], weight: 0.2, value: 300 }, { points: [[5, 0], [9, 0]], weight: 0.9, value: 1300 }] },
    },
    markers: [
      { floor: '4F', x: 1, y: 1, rank: 3, kind: 'unit', unit_code: '4F-P01' },
      ...contractExample.markers,
      { floor: '4F', x: 2, y: 2, rank: 2, kind: 'spot', label: '2', title: '후보 2 · 4F 아트리움' },
      { floor: '5F', x: 2, y: 2, kind: 'warning' },
    ],
  })!

  it('lists floors with something to draw and what is there', () => {
    expect(Object.fromEntries(flowFloorStats(flow))).toEqual({
      '3F': { segments: 1, markers: 1 },
      '4F': { segments: 2, markers: 2 },
      '5F': { segments: 0, markers: 1 },
    })
    expect(flowDataFloors(flow)).toEqual(['3F', '4F', '5F'])
    expect(flowFloorStats(null).size).toBe(0)
    expect(flowFloorText('popup', { segments: 2, markers: 2 })).toBe('팝업 후보 2곳')
    expect(flowFloorText('traffic', { segments: 12, markers: 0 })).toBe('추정 유동 12구간')
    expect(flowFloorText('egress', { segments: 3, markers: 2 })).toBe('통로 폭·피난 점검: 좁은 통로 3곳 · 피난 거리 초과 2곳')
    expect(flowFloorText('egress', { segments: 0, markers: 1 })).toBe('통로 폭·피난 점검: 피난 거리 초과 1곳')
  })

  it('orders markers by rank and names them', () => {
    expect(sortedFlowMarkers(flow).map((m) => m.rank)).toEqual([1, 2, 3, null])
    expect(sortedFlowMarkers(flow).map(flowMarkerText)).toEqual(['1', '2', '3', '!'])
    expect(flowMarkerTitle(flow.markers[0])).toBe('공실·팝업 매장 4F-P01')
    expect(flowMarkerTitle(flow.markers[1])).toBe('후보 1 · 3F 에스컬레이터 A 앞 통로')
    expect(flowMarkerTitle({ title: null, kind: 'mystery', unit_code: null })).toBe('표시 지점')
    // The number is not repeated when the title already starts with it; floor codes are not numbers.
    expect(sortedFlowMarkers(flow).map(flowMarkerName)).toEqual([
      '후보 1 · 3F 에스컬레이터 A 앞 통로',
      '후보 2 · 4F 아트리움',
      '3. 공실·팝업 매장 4F-P01',
      '피난 거리 초과 지점',
    ])
    const named = (label: string, title: string) => flowMarkerName({ label, rank: null, kind: 'spot', title, unit_code: null })
    expect(named('1', '1F 서측 횡단 통로')).toBe('1. 1F 서측 횡단 통로')
    expect(named('1', 'B1 주차장 입구')).toBe('1. B1 주차장 입구')
    expect(named('1', '후보 12 · 3F')).toBe('1. 후보 12 · 3F')
    expect(named('1', '1위 3F 아트리움')).toBe('1위 3F 아트리움')
    expect(named('2', '#2 ESC-A 앞')).toBe('#2 ESC-A 앞')
  })

  it('builds the summary chips, value text and legend of each mode', () => {
    expect(flowModeLabel('popup')).toBe('팝업 후보')
    expect(flowModeLabel('crowd')).toBe('통로 집계')
    expect(flowSummaryChips(flow)).toEqual(['고객-일 1,234', '이동 4,567건', '2개 층', '후보 4곳'])
    expect(flowSummaryChips(normalizeFlow(contractExample)!)).toEqual(['고객-일 1,234', '이동 4,567건', '2개 층'])
    expect(flowValueText(flow, 1234)).toBe('1,234명(추정)')
    expect(flowValueText(flow, null)).toBe('')
    expect(flowValueText({ mode: 'egress', legend: null }, 2.4)).toBe('폭 2.4m')
    expect(flowValueText({ mode: 'egress', legend: null }, 3)).toBe('폭 3m')
    expect(flowLegendInfo(flow)).toEqual({ gradient: true, lines: [], markerKinds: ['spot', 'unit', 'warning'] })

    const egress = normalizeFlow({
      mode: 'egress',
      query: { min_width_m: 2.4, max_travel_m: 50 },
      floors: { '3F': { segments: [{ points: [[0, 0], [5, 0]], value: 1.8, color: '#C62828' }, { points: [[0, 1], [5, 1]], value: 2, color: '#c62828' }] } },
      markers: [{ floor: '3F', x: 1, y: 1, rank: 1, kind: 'warning', title: '3F-C05 앞 · 계단까지 66m' }],
    })!
    expect(flowSummaryChips(egress)).toEqual(['좁은 통로 2곳', '피난 거리 초과 1곳', '기준 폭 2.4m', '기준 거리 50m'])
    expect(flowLegendInfo(egress)).toEqual({ gradient: false, lines: [{ color: '#c62828', label: '기준보다 좁은 통로' }], markerKinds: ['warning'] })
  })

  it('colors and widens lines by weight: light and thin to dark and thick', () => {
    expect(flowColor(0)).toEqual(hexToRgba(FLOW_STOPS[0], 240))
    expect(flowColor(1)).toEqual(hexToRgba(FLOW_STOPS.at(-1)!, 240))
    expect(flowColor(5)).toEqual(flowColor(1))
    expect(flowColor(null)).toEqual(flowColor(0.5))
    const light = flowColor(0.1)
    const dark = flowColor(0.9)
    expect(light[0] + light[1] + light[2]).toBeGreaterThan(dark[0] + dark[1] + dark[2])
    expect(flowWidthPx(0)).toBe(FLOW_WIDTH_MIN_PX)
    expect(flowWidthPx(1)).toBe(FLOW_WIDTH_MAX_PX)
    expect([2, 10]).toEqual([FLOW_WIDTH_MIN_PX, FLOW_WIDTH_MAX_PX])
    expect(flowWidthPx(0.5)).toBe(6)
    expect(flowWidthPx(null)).toBe(6)
    expect(flowWidthPx(-1)).toBe(2)
  })
})
