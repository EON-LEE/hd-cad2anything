import { describe, expect, it } from 'vitest'
import { CHANGE_COLORS, NO_DATA_COLOR, VACANT_COLOR, hexToRgba, heatmapColor, tint } from '../lib/colors'
import { buildFloorModel, type UnitInfo } from '../lib/geometry'
import type { FeatureCollection } from '../types/api'
import {
  changeMarksForFloor,
  crossFloorTargets,
  fitUnitLabel,
  layoutFloorBadges,
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
