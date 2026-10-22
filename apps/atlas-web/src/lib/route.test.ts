import { describe, expect, it } from 'vitest'
import { createInitialUiState, normalizeUiState } from '../state/uiState'
import {
  floorOfRef,
  normalizeRoute,
  placeKindLabel,
  routeFloorRoles,
  routeFloors,
  routeLegendText,
  routeSummaryChips,
  routeTransitionText,
  transitionFloorCount,
} from './route'

/** The example of docs/specs/atlas-api.md §3. */
const contractExample = {
  title: 'ENT-1 정문 → Orlix Coffee(8F-C05)',
  rev: 'R2',
  via_type: 'escalator',
  origin: { ref: 'ENT-1', kind: 'entrance', label: 'ENT-1 정문', floor: '1F', unit_code: null, x: 60.0, y: 0.4 },
  destination: { ref: '8F-C05', kind: 'unit', label: 'Orlix Coffee(8F-C05)', floor: '8F', unit_code: '8F-C05', x: 41.0, y: 52.0 },
  segments: [
    { floor: '1F', points: [[60.0, 0.4], [60.0, 30.0], [52.0, 38.0]] },
    { floor: '8F', points: [[52.0, 38.0], [41.0, 52.0]] },
  ],
  transitions: [{ from_floor: '1F', to_floor: '8F', via: 'ESC-A', floors: 7, height_m: 35.0 }],
  walk_m: 85,
  caveat: '통로망 최단 경로로 계산한 걷는 거리예요. 에스컬레이터를 타는 구간과 층마다 갈아타는 거리는 넣지 않았어요.',
}

describe('normalizeRoute', () => {
  it('keeps a contract-shaped route as is', () => {
    expect(normalizeRoute(contractExample)).toEqual(contractExample)
  })

  it('tolerates missing and invalid fields', () => {
    const route = normalizeRoute({
      origin: { ref: '1F:ESC-A', kind: 'escalator', x: '52', y: 38 },
      destination: { unit_code: '3F-C05', x: 60, y: 'n/a' },
      segments: [
        { floor: '1F', points: [[52, 38], ['x', 1], { x: 51, y: 40 }] },
        { points: [[0, 0], [1, 1]] },
        { floor: '3F', points: 'none' },
        { floor: '3F', points: [['a', 'b']] },
        'bad',
      ],
      transitions: 'none',
      walk_m: '40',
    })!
    // Floors from the floor-qualified reference and from the unit code; labels fall back to the reference.
    expect(route.origin).toEqual({ ref: '1F:ESC-A', kind: 'escalator', label: '1F:ESC-A', floor: '1F', unit_code: null, x: 52, y: 38 })
    expect(route.destination).toEqual({ ref: '3F-C05', kind: 'unit', label: '3F-C05', floor: '3F', unit_code: '3F-C05', x: 60, y: null })
    expect(route.title).toBe('1F:ESC-A → 3F-C05')
    expect(route.segments).toEqual([{ floor: '1F', points: [[52, 38], [51, 40]] }])
    // No usable transition between two floors: one is derived with the link unknown.
    expect(route.transitions).toEqual([{ from_floor: '1F', to_floor: '3F', via: null, floors: null, height_m: null }])
    expect(route).toMatchObject({ rev: null, via_type: null, walk_m: 40, caveat: null })
  })

  it('cleans transitions: numbers from strings, same-floor entries dropped', () => {
    const route = normalizeRoute({
      ...contractExample,
      transitions: [
        { from_floor: '1F', to_floor: '1F', via: 'ESC-A' },
        { from_floor: '1F', to_floor: '8F', via: ' ', floors: '7', height_m: 'tall' },
        { to_floor: '8F' },
      ],
    })!
    expect(route.transitions).toEqual([{ from_floor: '1F', to_floor: '8F', via: null, floors: 7, height_m: null }])
    const sameFloor = normalizeRoute({
      ...contractExample,
      destination: { ...contractExample.destination, floor: '1F' },
      segments: [contractExample.segments[0]],
      transitions: [],
    })!
    expect(sameFloor.transitions).toEqual([])
  })

  it('returns null unless both places have a floor', () => {
    expect(normalizeRoute(null)).toBeNull()
    expect(normalizeRoute('route')).toBeNull()
    expect(normalizeRoute([contractExample])).toBeNull()
    expect(normalizeRoute({ ...contractExample, origin: null })).toBeNull()
    expect(normalizeRoute({ origin: contractExample.origin })).toBeNull()
    // "ENT-1" says nothing about the floor.
    expect(normalizeRoute({ ...contractExample, destination: { ref: 'ENT-1', label: '정문' } })).toBeNull()
  })

  it('is part of the UI state: default null, normalized when patched, invalid values dropped', () => {
    const ui = createInitialUiState(null, { floor: '3F' })
    expect(ui.route).toBeNull()
    expect(Object.prototype.hasOwnProperty.call(ui, 'route')).toBe(true)
    const next = normalizeUiState({ ...ui, route: contractExample }, ui)
    expect(next.route?.segments).toHaveLength(2)
    // Untouched routes keep their identity across unrelated patches.
    expect(normalizeUiState({ ...next, floor: '1F' }, next).route).toBe(next.route)
    expect(normalizeUiState({ ...next, route: { origin: 'ENT-1' } }, next).route).toBeNull()
    const withoutRoute: Record<string, unknown> = { ...next }
    delete withoutRoute.route
    expect(normalizeUiState(withoutRoute, next).route).toBeNull()
  })
})

describe('route helpers', () => {
  const route = normalizeRoute(contractExample)!

  it('lists the floors on the way and their roles', () => {
    expect(routeFloors(route)).toEqual(['1F', '8F'])
    expect(Object.fromEntries(routeFloorRoles(route))).toEqual({ '1F': 'origin', '8F': 'destination' })
    const sameFloor = normalizeRoute({
      ...contractExample,
      destination: { ...contractExample.destination, floor: '1F' },
      segments: [contractExample.segments[0]],
      transitions: [],
    })!
    expect(Object.fromEntries(routeFloorRoles(sameFloor))).toEqual({ '1F': 'both' })
    const twoChanges = normalizeRoute({
      ...contractExample,
      transitions: [
        { from_floor: '1F', to_floor: '3F', via: 'ESC-A' },
        { from_floor: '3F', to_floor: '8F', via: 'EV-1' },
      ],
    })!
    expect(routeFloors(twoChanges)).toEqual(['1F', '3F', '8F'])
    expect(routeFloorRoles(twoChanges).get('3F')).toBe('pass')
    expect(routeFloors(null)).toEqual([])
    expect(routeFloorRoles(undefined).size).toBe(0)
  })

  it('builds the summary chips and the legend line', () => {
    expect(routeSummaryChips(route)).toEqual(['걷는 거리 85m', '에스컬레이터 A · 7개 층 이동 · 높이 35m'])
    expect(routeLegendText(route)).toBe('통로망 최단 경로 · 걷는 거리 85m')
    const unknown = normalizeRoute({ ...contractExample, walk_m: null, transitions: null, via_type: 'elevator' })!
    expect(routeSummaryChips(unknown)).toEqual(['엘리베이터 · 7개 층 이동'])
    expect(routeLegendText(unknown)).toBe('통로망 최단 경로')
  })

  it('counts floors from `floors`, else from the floor ordinals', () => {
    const t = { from_floor: '1F', to_floor: '3F', via: null, floors: null, height_m: null }
    expect(transitionFloorCount(t)).toBe(2)
    expect(transitionFloorCount({ ...t, floors: -4 })).toBe(4)
    // Ordinals from /api/meta take precedence over the code.
    expect(transitionFloorCount(t, [{ code: '1F', ordinal: 0, name: '' }, { code: '3F', ordinal: 1, name: '' }])).toBe(1)
    expect(routeTransitionText({ ...t, from_floor: 'B1', to_floor: '1F', via: 'ST-2' }, 'escalator')).toBe('계단 2 · 1개 층 이동')
    expect(routeTransitionText({ ...t, from_floor: 'M', to_floor: 'RF' }, null)).toBe('옥상으로 이동')
  })

  it('reads places', () => {
    expect(floorOfRef('1F:ESC-A')).toBe('1F')
    expect(floorOfRef('b1:RESTROOM')).toBe('B1')
    expect(floorOfRef('8F-C05')).toBe('8F')
    expect(floorOfRef('ENT-1')).toBeNull()
    expect(floorOfRef(null)).toBeNull()
    expect(placeKindLabel('stairs')).toBe('계단')
    expect(placeKindLabel('entrance')).toBe('출입구')
    expect(placeKindLabel('portal')).toBe('')
  })
})
