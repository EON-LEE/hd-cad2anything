import { describe, expect, it } from 'vitest'
import type { FeatureCollection } from '../types/api'
import { buildFloorModel, curvedPath, fitViewState, fitViewStateBeside, floorOfUnitCode, polygonCentroid, screenToWorld, worldToScreen } from './geometry'

const square = (x0: number, y0: number, x1: number, y1: number) => [
  [x0, y0],
  [x1, y0],
  [x1, y1],
  [x0, y1],
  [x0, y0],
]

const fc: FeatureCollection = {
  type: 'FeatureCollection',
  features: [
    { type: 'Feature', id: 'X-3F', geometry: { type: 'Polygon', coordinates: [square(0, 0, 120, 80)] }, properties: { kind: 'exterior' } },
    {
      type: 'Feature',
      id: 'U-3F-N05',
      geometry: { type: 'Polygon', coordinates: [square(10, 68, 20, 80)] },
      properties: { kind: 'unit', unit_code: '3F-N05', brand_name: 'Studio Velune', category: 'WOMEN_CONTEMPORARY', vacant: false },
    },
    {
      type: 'Feature',
      id: 'U-3F-C01',
      geometry: { type: 'MultiPolygon', coordinates: [[square(0, 0, 2, 2)], [square(10, 10, 20, 20)]] },
      properties: { kind: 'unit', unit_code: '3F-C01', vacant: true },
    },
    { type: 'Feature', id: 'O-1', geometry: { type: 'LineString', coordinates: [[56, 0], [64, 0]] }, properties: { kind: 'opening' } },
    { type: 'Feature', id: 'P-1', geometry: { type: 'Point', coordinates: [1, 1] }, properties: { kind: 'unknown' } },
    { type: 'Feature', id: 'N-1', geometry: null, properties: { kind: 'unit' } },
  ],
}

describe('buildFloorModel', () => {
  const model = buildFloorModel(fc)

  it('groups polygons by kind and keeps lines', () => {
    expect(model.polygons.exterior).toHaveLength(1)
    expect(model.polygons.unit).toHaveLength(3) // two parts for the multipolygon
    expect(model.lines.map((l) => l.id)).toEqual(['O-1'])
    expect(model.bbox).toEqual([0, 0, 120, 80])
  })

  it('indexes units by unit_code with bbox, centroid of the largest part and total area', () => {
    const n05 = model.units.get('3F-N05')!
    expect(n05.bbox).toEqual([10, 68, 20, 80])
    expect(n05.centroid[0]).toBeCloseTo(15)
    expect(n05.centroid[1]).toBeCloseTo(74)
    expect(n05.area).toBeCloseTo(120)
    const c01 = model.units.get('3F-C01')!
    expect(c01.parts).toHaveLength(2)
    expect(c01.centroid[0]).toBeCloseTo(15)
    expect(c01.area).toBeCloseTo(104)
  })

  it('tolerates empty input', () => {
    expect(buildFloorModel(undefined).units.size).toBe(0)
    expect(buildFloorModel(undefined).bbox).toBeNull()
  })
})

describe('geometry helpers', () => {
  it('computes polygon centroids', () => {
    const c = polygonCentroid([square(0, 0, 4, 2) as [number, number][]])
    expect(c[0]).toBeCloseTo(2)
    expect(c[1]).toBeCloseTo(1)
  })

  it('reads the floor from a unit code', () => {
    expect(floorOfUnitCode('3F-N05')).toBe('3F')
    expect(floorOfUnitCode('B1-C03')).toBe('B1')
    expect(floorOfUnitCode('6F-E01A')).toBe('6F')
    expect(floorOfUnitCode('nonsense')).toBeNull()
  })

  it('fits a bounding box into the viewport', () => {
    const view = fitViewState([0, 0, 120, 80], 1000, 700, { padding: 20 })
    expect(view.target).toEqual([60, 40, 0])
    expect(2 ** view.zoom * 120).toBeLessThanOrEqual(960 + 1e-6)
    expect(2 ** view.zoom * 80).toBeLessThanOrEqual(660 + 1e-6)
    expect(Math.max(2 ** view.zoom * 120 - 960, 2 ** view.zoom * 80 - 660)).toBeCloseTo(0, 6)
  })

  it('round-trips between world and screen coordinates (y axis up)', () => {
    const view = { target: [60, 40, 0] as [number, number, number], zoom: 3 }
    const screen = worldToScreen(view, 1000, 700, [70, 50])
    expect(screen).toEqual([500 + 80, 350 - 80])
    expect(screenToWorld(view, 1000, 700, screen)).toEqual([70, 50])
  })

  it('fits beside a floating card on the left', () => {
    const view = fitViewStateBeside([0, 0, 120, 80], 1000, 700, 354, { padding: 20 })
    // The floor fills the 646 px right of the card and is centered there.
    expect(2 ** view.zoom * 120).toBeCloseTo(646 - 40, 6)
    const [left] = worldToScreen(view, 1000, 700, [0, 40])
    const [right] = worldToScreen(view, 1000, 700, [120, 40])
    expect((left + right) / 2).toBeCloseTo(354 + 646 / 2, 6)
    expect(left).toBeCloseTo(354 + 20, 6)
    expect(fitViewStateBeside([0, 0, 120, 80], 1000, 700, 0)).toEqual(fitViewState([0, 0, 120, 80], 1000, 700))
  })

  it('builds curved edge paths through a bent midpoint', () => {
    const { path, mid } = curvedPath([0, 0], [10, 0], 0.2, 10)
    expect(path[0]).toEqual([0, 0])
    expect(path.at(-1)).toEqual([10, 0])
    expect(mid[0]).toBeCloseTo(5)
    expect(mid[1]).toBeCloseTo(1)
  })
})
