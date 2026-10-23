import { describe, expect, it } from 'vitest'
import { CHANGE_COLORS, NO_DATA_COLOR, VACANT_COLOR, hexToRgba, heatmapColor, tint } from '../lib/colors'
import { FLOW_MARKER_COLORS, flowColor, normalizeFlow } from '../lib/flow'
import { buildFloorModel, type UnitInfo } from '../lib/geometry'
import { normalizeJourney } from '../lib/journey'
import { normalizeRoute } from '../lib/route'
import type { FeatureCollection } from '../types/api'
import {
  EMPTY_FLOW_RENDER,
  EMPTY_JOURNEY_RENDER,
  EMPTY_ROUTE_RENDER,
  changeMarksForFloor,
  crossFloorTargets,
  fitUnitLabel,
  layoutFloorBadges,
  prepareFlow,
  prepareJourney,
  prepareRoute,
  removedUnits,
  resolveEdges,
  resolveHighlights,
  unitFillColor,
  type UnitStyleContext,
} from './prepare'

const rect = (x0: number, y0: number, x1: number, y1: number) => [
  [x0, y0],
  [x1, y0],
  [x1, y1],
  [x0, y1],
  [x0, y0],
]

function floor(units: [string, number, number, number, number, Record<string, unknown>?][]): FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: units.map(([code, x0, y0, x1, y1, props]) => ({
      type: 'Feature',
      id: `U-${code}`,
      geometry: { type: 'Polygon', coordinates: [rect(x0, y0, x1, y1)] },
      properties: { kind: 'unit', unit_code: code, category: 'CAFE', vacant: false, brand_name: `Brand ${code}`, ...props },
    })),
  }
}

const model = buildFloorModel(
  floor([
    ['3F-A01', 0, 0, 20, 12],
    ['3F-A02', 20, 0, 40, 12, { vacant: true, brand_name: null }],
    ['3F-A03', 40, 0, 60, 12, { category: 'LUXURY' }],
  ]),
)
const unit = (code: string) => model.units.get(code) as UnitInfo
const floors = [
  { code: '1F', ordinal: 0, name: '명품·화장품' },
  { code: '3F', ordinal: 2, name: '여성 컨템포러리' },
  { code: '5F', ordinal: 4, name: '남성' },
]
const ctx = (over: Partial<UnitStyleContext> = {}): UnitStyleContext => ({
  categoryColors: new Map([
    ['CAFE', hexToRgba('#aabbcc')],
    ['LUXURY', hexToRgba('#112233')],
  ]),
  heatValues: null,
  changeMarks: null,
  ...over,
})

describe('unit fill colors', () => {
  it('uses category colors and a vacant color by default', () => {
    expect(unitFillColor(unit('3F-A01'), ctx())).toEqual(hexToRgba('#aabbcc'))
    expect(unitFillColor(unit('3F-A02'), ctx())).toEqual(VACANT_COLOR)
  })

  it('uses the heatmap scale (and no-data color) when heatmap values are present', () => {
    const style = ctx({ heatValues: { '3F-A01': 0.25 } })
    expect(unitFillColor(unit('3F-A01'), style)).toEqual(heatmapColor(0.25))
    expect(unitFillColor(unit('3F-A03'), style)).toEqual(NO_DATA_COLOR)
  })

  it('colors changed units by change type and fades unchanged ones in compare mode', () => {
    const style = ctx({ changeMarks: new Map([['3F-A01', 'resize']]) })
    expect(unitFillColor(unit('3F-A01'), style)).toEqual(hexToRgba(CHANGE_COLORS.resize, 235))
    expect(unitFillColor(unit('3F-A03'), style)).toEqual(tint(hexToRgba('#112233'), 0.6))
  })
})

describe('change marks', () => {
  it('maps before/after codes on the floor and keeps the higher-priority type', () => {
    const marks = changeMarksForFloor(
      [
        { change_type: 'resize', floor: '3F', unit_codes_before: ['3F-A01'], unit_codes_after: ['3F-A01'], summary: '' },
        { change_type: 'move', floor: '3F', unit_codes_before: ['3F-A01'], unit_codes_after: ['3F-A02'], summary: '' },
        { change_type: 'add', floor: '1F', unit_codes_before: [], unit_codes_after: ['1F-X01'], summary: '' },
        { change_type: 'remove', floor: '3F', unit_codes_before: ['3F-Z09'], unit_codes_after: [], summary: '' },
      ],
      '3F',
    )
    expect(Object.fromEntries(marks)).toEqual({ '3F-A01': 'move', '3F-A02': 'move', '3F-Z09': 'remove' })
  })

  it('finds units that exist only in the comparison revision', () => {
    const previous = buildFloorModel(floor([['3F-A01', 0, 0, 20, 12], ['3F-Z09', 60, 0, 80, 12]]))
    expect(removedUnits(model, previous).map((u) => u.code)).toEqual(['3F-Z09'])
    expect(removedUnits(model, null)).toEqual([])
  })
})

describe('highlights, labels, edges and floor badges', () => {
  it('resolves highlights on the current floor only, with default colors', () => {
    const resolved = resolveHighlights([{ unit_code: '3F-A01' }, { unit_code: '1F-B01', color: '#00ff00' }], model.units)
    expect(resolved).toHaveLength(1)
    expect(resolved[0].color).toEqual(hexToRgba('#e53935'))
  })

  it('fits labels to the on-screen unit size', () => {
    expect(fitUnitLabel(unit('3F-A01'), 4)).toBe('Brand 3F-A01\n3F-A01') // 320 px wide, 192 px tall
    expect(fitUnitLabel(unit('3F-A02'), 1.5)).toBe('(공실)') // too short for two lines
    expect(fitUnitLabel(unit('3F-A01'), -1)).toBeNull()
  })

  it('lists other floors linked from this floor, top floor first', () => {
    const edges = [
      { from: '3F-A01', to: '1F-C03' },
      { from: '5F-N01', to: '3F-A03' },
      { from: '1F-C03', to: '5F-N01' }, // both ends elsewhere: not a badge
      { from: '3F-A01', to: '3F-A03' },
    ]
    expect(crossFloorTargets(edges, '3F', model.units, floors)).toEqual(['5F', '1F'])
  })

  it('anchors cross-floor edges to badges on the right canvas edge', () => {
    const view = { target: [30, 6, 0] as [number, number, number], zoom: 3 }
    const badges = layoutFloorBadges(['5F', '1F', '9F'], floors, view, 1000, 600)
    expect(badges.map((b) => b.screen[0])).toEqual([960, 960, 960])
    expect(badges[0].screen[1]).toBeLessThan(badges[1].screen[1])
    expect(badges[2].known).toBe(false)
    // World position matches the screen position: x = 30 + (960 - 500) / 8.
    expect(badges[0].world[0]).toBeCloseTo(87.5)

    const edges = resolveEdges(
      [
        { from: '3F-A01', to: '3F-A03', weight: 2.4, label: 'lift 2.4' },
        { from: '3F-A01', to: '1F-C03', weight: 1 },
        { from: '1F-C03', to: '5F-N01' },
        { from: '3F-A01', to: '3F-NOPE' },
      ],
      '3F',
      model.units,
      badges,
    )
    expect(edges).toHaveLength(2)
    expect(edges[0]).toMatchObject({ crossFloor: null, label: 'lift 2.4', source: unit('3F-A01').centroid })
    expect(edges[0].width).toBeGreaterThan(edges[1].width)
    expect(edges[1]).toMatchObject({ crossFloor: '1F', target: badges[1].world, label: '1.0' })
  })
})

describe('customer journey render data', () => {
  // 1F -> 3F by ESC-A, two stops on 3F, back to 1F by ESC-A; one 3F stop has no coordinates.
  const journey = normalizeJourney({
    stops: [
      { order: 1, time: '11:32', unit_code: '1F-C03', floor: '1F', x: 90, y: 37 },
      { order: 2, time: '12:41', unit_code: '3F-C05', floor: '3F', x: 60, y: 50 },
      { order: 3, time: '13:20', unit_code: '3F-S03', floor: '3F', x: null, y: null },
      { order: 4, time: '14:37', unit_code: '1F-E01', floor: '1F', x: 108, y: 27.5 },
    ],
    segments: [
      { floor: '1F', points: [[90, 37], [90, 40], [49.5, 40], [51, 40]], from_order: 1, to_order: 2 },
      { floor: '3F', points: [[51, 40], [49.5, 40], [49.5, 48.5], [60, 50]], from_order: 1, to_order: 2 },
      { floor: '3F', points: [[60, 50]], from_order: 2, to_order: 3 },
      { floor: '3F', points: [[70.5, 15], [70.5, 31.5], [49.5, 31.5], [51, 40]], from_order: 3, to_order: 4 },
      { floor: '1F', points: [[51, 40], [49.5, 31.5], [105, 27.5], [108, 27.5]], from_order: 3, to_order: 4 },
    ],
    transitions: [
      { from_order: 1, to_order: 2, from_floor: '1F', to_floor: '3F', via: 'ESC-A' },
      { from_order: 3, to_order: 4, from_floor: '3F', to_floor: '1F', via: 'ESC-A' },
    ],
  })

  it('keeps only the current floor: paths with 2+ points and stops with coordinates', () => {
    const f1 = prepareJourney(journey, '1F')
    expect(f1.paths.map((p) => p.key)).toEqual(['seg-0', 'seg-4'])
    expect(f1.stops.map((s) => [s.order, s.position, s.time])).toEqual([
      [1, [90, 37], '11:32'],
      [4, [108, 27.5], '14:37'],
    ])
    const f3 = prepareJourney(journey, '3F')
    // The one-point segment (no walkway path) is not drawn; the stop without coordinates is skipped.
    expect(f3.paths.map((p) => p.key)).toEqual(['seg-1', 'seg-3'])
    expect(f3.stops.map((s) => s.order)).toEqual([2])
    expect(prepareJourney(journey, '5F')).toEqual({ paths: [], stops: [], notes: [] })
    expect(prepareJourney(null, '1F')).toBe(EMPTY_JOURNEY_RENDER)
  })

  it('labels where the path leaves for and arrives from another floor', () => {
    expect(prepareJourney(journey, '1F').notes).toEqual([
      { key: 'depart-0', position: [51, 40], text: '→ 3층 (ESC-A)', kind: 'depart', stack: 0 },
      { key: 'arrive-1', position: [51, 40], text: '3층에서 내려옴', kind: 'arrive', stack: 1 },
    ])
    expect(prepareJourney(journey, '3F').notes).toEqual([
      { key: 'arrive-0', position: [51, 40], text: '1층에서 올라옴', kind: 'arrive', stack: 0 },
      { key: 'depart-1', position: [51, 40], text: '→ 1층 (ESC-A)', kind: 'depart', stack: 1 },
    ])
  })

  it('falls back to the stop position when a floor change has no segment', () => {
    const sparse = normalizeJourney({
      stops: [
        { order: 1, time: '10:00', unit_code: 'B1-C05', floor: 'B1', x: 52.4, y: 31 },
        { order: 2, time: '10:30', unit_code: '3F-C01', floor: '3F', x: 30, y: 37 },
      ],
    })
    expect(prepareJourney(sparse, 'B1').notes).toEqual([{ key: 'depart-0', position: [52.4, 31], text: '→ 3층', kind: 'depart', stack: 0 }])
    expect(prepareJourney(sparse, '3F', floors).notes.map((n) => [n.text, n.position])).toEqual([['지하 1층에서 올라옴', [30, 37]]])
  })
})

describe('route render data', () => {
  // The contract example: ENT-1 on 1F to 8F-C05 by ESC-A.
  const route = normalizeRoute({
    title: 'ENT-1 정문 → Orlix Coffee(8F-C05)',
    origin: { ref: 'ENT-1', kind: 'entrance', label: 'ENT-1 정문', floor: '1F', unit_code: null, x: 60, y: 0.4 },
    destination: { ref: '8F-C05', kind: 'unit', label: 'Orlix Coffee(8F-C05)', floor: '8F', unit_code: '8F-C05', x: 41, y: 52 },
    segments: [
      { floor: '1F', points: [[60, 0.4], [60, 30], [52, 38]] },
      { floor: '8F', points: [[52, 38], [41, 52]] },
    ],
    transitions: [{ from_floor: '1F', to_floor: '8F', via: 'ESC-A', floors: 7, height_m: 35 }],
    walk_m: 85,
  })

  it('keeps the current floor only: its walking line and the start or end marker there', () => {
    const f1 = prepareRoute(route, '1F')
    expect(f1.paths).toEqual([{ key: 'seg-0', path: [[60, 0.4], [60, 30], [52, 38]] }])
    expect(f1.places.map((p) => [p.role, p.text, p.label, p.position])).toEqual([['origin', '출발', 'ENT-1 정문', [60, 0.4]]])
    const f8 = prepareRoute(route, '8F')
    expect(f8.paths.map((p) => p.key)).toEqual(['seg-1'])
    expect(f8.places.map((p) => [p.role, p.text, p.label, p.position])).toEqual([['destination', '도착', 'Orlix Coffee(8F-C05)', [41, 52]]])
    expect(f8.places[0].place.unit_code).toBe('8F-C05')
    expect(prepareRoute(route, '5F')).toEqual({ paths: [], places: [], notes: [] })
    expect(prepareRoute(null, '1F')).toBe(EMPTY_ROUTE_RENDER)
  })

  it('labels where the walk leaves for and arrives from another floor', () => {
    expect(prepareRoute(route, '1F').notes).toEqual([{ key: 'depart-0', position: [52, 38], text: '→ 8층 (ESC-A)', kind: 'depart', stack: 0 }])
    expect(prepareRoute(route, '8F').notes).toEqual([{ key: 'arrive-0', position: [52, 38], text: '1층에서 올라옴', kind: 'arrive', stack: 0 }])
    // Ordinals from /api/meta decide up or down.
    const reversed = [
      { code: '1F', ordinal: 9, name: '' },
      { code: '8F', ordinal: 2, name: '' },
    ]
    expect(prepareRoute(route, '8F', reversed).notes[0].text).toBe('1층에서 내려옴')
  })

  it('skips one-point lines and places without coordinates', () => {
    const same = normalizeRoute({
      origin: { ref: '3F:ESC-B', kind: 'escalator', label: 'ESC-B 에스컬레이터', x: 69, y: 40 },
      destination: { ref: '3F-C05', kind: 'unit', label: 'Cafe Halune(3F-C05)', unit_code: '3F-C05', x: 60, y: null },
      segments: [{ floor: '3F', points: [[69, 40]] }],
      transitions: [],
    })
    const f3 = prepareRoute(same, '3F')
    expect(f3.paths).toEqual([])
    expect(f3.places.map((p) => p.role)).toEqual(['origin'])
    expect(f3.notes).toEqual([])
  })

  it('puts a floor-change note at the place when its floor has no line', () => {
    const sparse = normalizeRoute({
      origin: { ref: '1F:ESC-A', kind: 'escalator', label: 'ESC-A 에스컬레이터', floor: '1F', x: 51, y: 40 },
      destination: { ref: '3F-C05', kind: 'unit', label: 'Cafe Halune(3F-C05)', floor: '3F', x: 60, y: 50 },
      segments: [{ floor: '3F', points: [[51, 40], [49.5, 40], [49.5, 48.5], [60, 48.5], [60, 50]] }],
      transitions: [{ from_floor: '1F', to_floor: '3F', via: 'ESC-A' }],
    })
    expect(prepareRoute(sparse, '1F').notes.map((n) => [n.text, n.position])).toEqual([['→ 3층 (ESC-A)', [51, 40]]])
    expect(prepareRoute(sparse, '3F').notes.map((n) => [n.text, n.position])).toEqual([['1층에서 올라옴', [51, 40]]])
  })

  it('pairs each floor change with the lines around it on a route over three floors', () => {
    const twoChanges = normalizeRoute({
      origin: { ref: '1F-C01', kind: 'unit', floor: '1F', x: 30, y: 37 },
      destination: { ref: '5F-C01', kind: 'unit', floor: '5F', x: 30, y: 37 },
      segments: [
        { floor: '1F', points: [[30, 37], [51, 40]] },
        { floor: '3F', points: [[51, 40], [6, 12]] },
        { floor: '5F', points: [[6, 12], [30, 37]] },
      ],
      transitions: [
        { from_floor: '1F', to_floor: '3F', via: 'ESC-A' },
        { from_floor: '3F', to_floor: '5F', via: 'EV-1' },
      ],
    })
    const f3 = prepareRoute(twoChanges, '3F', floors)
    expect(f3.notes).toEqual([
      { key: 'arrive-0', position: [51, 40], text: '1층에서 올라옴', kind: 'arrive', stack: 0 },
      { key: 'depart-1', position: [6, 12], text: '→ 5층 (EV-1)', kind: 'depart', stack: 0 },
    ])
    expect(f3.places).toEqual([])
    expect(f3.paths.map((p) => p.key)).toEqual(['seg-1'])
  })
})

describe('flow render data', () => {
  const flow = normalizeFlow({
    mode: 'popup',
    floors: {
      '3F': {
        max_value: 1500,
        segments: [
          { points: [[12, 15], [40, 15]], weight: 0.82, value: 1234, label: '3F-C05·3F-C06 앞 통로' },
          { points: [[40, 15], [40, 40]], value: 300 },
          { points: [[0, 0], [5, 0]], weight: 0.6, color: '#c62828', label: '아트리움 남측 · 폭 3m' },
          { points: [[1, 1]], weight: 1 },
        ],
      },
      '1F': { segments: [{ points: [[0, 0], [9, 0]], weight: 1 }] },
    },
    markers: [
      { floor: '3F', x: 60, y: 31.5, rank: 2, kind: 'unit', unit_code: '3F-P01' },
      { floor: '3F', x: 20, y: 15, rank: 1, kind: 'spot', label: '1', title: '후보 1' },
      { floor: '1F', x: 5, y: 5, rank: 3, kind: 'spot' },
      { floor: '3F', x: 9, y: 9, kind: 'warning' },
    ],
  })!

  it('keeps the current floor only: its segments by weight and its markers', () => {
    const f3 = prepareFlow(flow, '3F')
    // Lighter lines first so heavy ones stay on top; the one-point segment is not drawn.
    expect(f3.paths.map((p) => [p.key, p.width])).toEqual([
      ['seg-1', 2 + 8 * 0.2],
      ['seg-2', 2 + 8 * 0.6],
      ['seg-0', 2 + 8 * 0.82],
    ])
    expect(f3.paths.find((p) => p.key === 'seg-0')!.path).toEqual([[12, 15], [40, 15]])
    // Color by weight unless the backend chose one.
    expect(f3.paths.find((p) => p.key === 'seg-0')!.color).toEqual(flowColor(0.82))
    expect(f3.paths.find((p) => p.key === 'seg-2')!.color).toEqual(hexToRgba('#c62828', 240))
    // Markers on this floor, the top rank drawn last; the text is the label, the rank or "!".
    expect(f3.markers.map((m) => [m.text, m.marker.kind])).toEqual([
      ['!', 'warning'],
      ['2', 'unit'],
      ['1', 'spot'],
    ])
    expect(f3.markers.map((m) => m.color)).toEqual([hexToRgba(FLOW_MARKER_COLORS.warning), hexToRgba(FLOW_MARKER_COLORS.unit), hexToRgba(FLOW_MARKER_COLORS.spot)])
    expect(f3.markers.at(-1)!.position).toEqual([20, 15])
    const f1 = prepareFlow(flow, '1F')
    expect(f1.paths.map((p) => p.width)).toEqual([10])
    expect(f1.markers.map((m) => m.text)).toEqual(['3'])
    expect(prepareFlow(flow, '5F')).toEqual({ paths: [], markers: [] })
    expect(prepareFlow(null, '3F')).toBe(EMPTY_FLOW_RENDER)
  })
})