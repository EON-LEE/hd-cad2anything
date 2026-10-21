import { describe, expect, it } from 'vitest'
import { CHANGE_COLORS, NO_DATA_COLOR, VACANT_COLOR, hexToRgba, heatmapColor, tint } from '../lib/colors'
import { buildFloorModel, type UnitInfo } from '../lib/geometry'
import { normalizeJourney } from '../lib/journey'
import type { FeatureCollection } from '../types/api'
import {
  EMPTY_JOURNEY_RENDER,
  changeMarksForFloor,
  crossFloorTargets,
  fitUnitLabel,
  layoutFloorBadges,
  prepareJourney,
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
