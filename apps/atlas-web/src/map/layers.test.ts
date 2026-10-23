import { describe, expect, it, vi } from 'vitest'
import { FLOW_MARKER_COLORS, normalizeFlow } from '../lib/flow'
import { hexToRgba } from '../lib/colors'
import { buildFloorModel } from '../lib/geometry'
import { normalizeJourney } from '../lib/journey'
import { normalizeRoute } from '../lib/route'
import type { Feature } from '../types/api'
import { FLOW_CASING_PX, FLOW_MARKER_MAX_PX, FLOW_MARKER_MIN_PX, buildLayers, flowMarkerLayers, flowPathLayers, routeLayers, routeMarkerFontPx, routeMarkerRadiusPx, type LayerInput } from './layers'
import { EMPTY_FLOW_RENDER, EMPTY_JOURNEY_RENDER, EMPTY_ROUTE_RENDER, prepareFlow, prepareJourney, prepareRoute, resolveEdges, resolveHighlights } from './prepare'

// deck.gl needs WebGL only to draw: stand-ins that keep their props are enough to check the layer stack.
vi.mock('@deck.gl/layers', () => {
  class FakeLayer {
    id: string
    props: Record<string, unknown>
    constructor(props: Record<string, unknown>) {
      this.id = String(props.id)
      this.props = props
    }
  }
  return { PolygonLayer: FakeLayer, PathLayer: FakeLayer, ScatterplotLayer: FakeLayer, TextLayer: FakeLayer }
})

type Props = Record<string, unknown>
const ids = (layers: unknown[]) => (layers as { id: string }[]).map((l) => l.id)
const propsById = (layers: unknown[]): Record<string, Props> =>
  Object.fromEntries((layers as { id: string; props: Props }[]).map((l) => [l.id, l.props]))
const data = (props: Props) => props.data as Props[]
const call = (props: Props, accessor: string, d: unknown) => (props[accessor] as (d: unknown) => unknown)(d)

const unitFeature = (code: string, x0: number, x1: number): Feature => ({
  type: 'Feature',
  id: `U-${code}`,
  geometry: { type: 'Polygon', coordinates: [[[x0, 0], [x1, 0], [x1, 12], [x0, 12], [x0, 0]]] },
  properties: { kind: 'unit', unit_code: code, category: 'CAFE', brand_name: `Brand ${code}` },
})
const model = buildFloorModel({ type: 'FeatureCollection', features: [unitFeature('1F-A01', 0, 20), unitFeature('1F-A02', 40, 60)] })

const route = normalizeRoute({
  origin: { ref: 'ENT-1', kind: 'entrance', label: 'ENT-1 정문', floor: '1F', unit_code: null, x: 30, y: 0.4 },
  destination: { ref: '3F-C05', kind: 'unit', label: 'Cafe Halune(3F-C05)', floor: '3F', unit_code: '3F-C05', x: 60, y: 50 },
  segments: [
    { floor: '1F', points: [[30, 0.4], [30, 15], [51, 40]] },
    { floor: '3F', points: [[51, 40], [60, 50]] },
  ],
  transitions: [{ from_floor: '1F', to_floor: '3F', via: 'ESC-A', floors: 2, height_m: 10 }],
  walk_m: 85,
})

const journey = normalizeJourney({
  stops: [
    { order: 1, time: '11:32', unit_code: '1F-A01', floor: '1F', x: 10, y: 12 },
    { order: 2, time: '11:58', unit_code: '1F-A02', floor: '1F', x: 50, y: 12 },
  ],
  segments: [{ floor: '1F', points: [[10, 12], [10, 15], [50, 15], [50, 12]], from_order: 1, to_order: 2 }],
})

function input(over: Partial<LayerInput> = {}): LayerInput {
  return {
    model,
    compareModel: null,
    removed: [],
    changeMarks: null,
    style: { categoryColors: new Map(), heatValues: null, changeMarks: null },
    styleKey: 'test',
    layers: { fixtures: false, walkways: true, labels: true },
    highlights: [],
    selection: [],
    edges: [],
    journey: EMPTY_JOURNEY_RENDER,
    route: EMPTY_ROUTE_RENDER,
    flow: EMPTY_FLOW_RENDER,
    zoom: 2,
    ...over,
  }
}

describe('route layers', () => {
  it('adds nothing without a route on this floor', () => {
    expect(routeLayers(EMPTY_ROUTE_RENDER, 2)).toEqual([])
    expect(ids(buildLayers(input())).some((id) => id.startsWith('route-'))).toBe(false)
  })

  it('stacks the route above highlights and edges and below the journey', () => {
    const order = ids(
      buildLayers(
        input({
          highlights: resolveHighlights([{ unit_code: '1F-A01' }], model.units),
          edges: resolveEdges([{ from: '1F-A01', to: '1F-A02', weight: 1, label: 'lift 1.0' }], '1F', model.units, []),
          journey: prepareJourney(journey, '1F'),
          route: prepareRoute(route, '1F'),
        }),
      ),
    )
    expect(order.filter((id) => id.startsWith('route-'))).toEqual([
      'route-casing',
      'route-path',
      'route-destination-halo',
      'route-places',
      'route-place-text',
      'route-place-labels',
      'route-notes',
    ])
    expect(order.indexOf('route-casing')).toBeGreaterThan(order.indexOf('highlights'))
    expect(order.indexOf('route-casing')).toBeGreaterThan(order.indexOf('edge-labels'))
    expect(order.indexOf('route-notes')).toBeLessThan(order.indexOf('journey-casing'))
  })

  it('draws a cased blue line, a green start marker with its name and the floor-change note', () => {
    const start = propsById(routeLayers(prepareRoute(route, '1F'), 1))
    expect(start['route-casing']).toMatchObject({ getColor: [255, 255, 255, 235], getWidth: 9, widthUnits: 'pixels' })
    expect(start['route-path']).toMatchObject({ getColor: [30, 136, 229, 255], getWidth: 5 })
    expect(data(start['route-path']).map((d) => d.path)).toEqual([[[30, 0.4], [30, 15], [51, 40]]])
    const marker = data(start['route-places'])[0]
    expect(marker).toMatchObject({ role: 'origin', text: '출발', position: [30, 0.4] })
    expect(call(start['route-places'], 'getFillColor', marker)).toEqual([46, 125, 50, 255])
    expect(start['route-places']).toMatchObject({ radiusUnits: 'common', radiusMinPixels: 13, radiusMaxPixels: 18, pickable: true })
    expect(data(start['route-destination-halo'])).toEqual([])
    expect(data(start['route-place-text']).map((d) => [d.text, d.size])).toEqual([['출발', 10]])
    expect(call(start['route-place-labels'], 'getText', marker)).toBe('ENT-1 정문')
    expect(call(start['route-place-labels'], 'getPixelOffset', marker)).toEqual([13 + 5, 0])
    expect(data(start['route-notes']).map((d) => d.text)).toEqual(['→ 3층 (ESC-A)'])
  })

  it('rings the end marker and keeps its name clear of the ring', () => {
    const end = propsById(routeLayers(prepareRoute(route, '3F'), 4))
    const marker = data(end['route-places'])[0]
    expect(marker).toMatchObject({ role: 'destination', text: '도착' })
    expect(call(end['route-places'], 'getFillColor', marker)).toEqual([21, 101, 192, 255])
    expect(data(end['route-destination-halo'])).toEqual([marker])
    expect(end['route-destination-halo'].getRadius).toBe(18 + 6)
    expect(call(end['route-place-labels'], 'getPixelOffset', marker)).toEqual([18 + 6 + 5, 0])
    expect(data(end['route-notes']).map((d) => d.text)).toEqual(['1층에서 올라옴'])
  })

  it('keeps markers legible at any zoom', () => {
    expect(routeMarkerRadiusPx(-1)).toBe(13)
    expect(routeMarkerRadiusPx(3)).toBeCloseTo(14.4)
    expect(routeMarkerRadiusPx(6)).toBe(18)
    expect(routeMarkerFontPx(13)).toBe(10)
    expect(routeMarkerFontPx(15)).toBe(11)
    expect(routeMarkerFontPx(18)).toBe(12)
  })
})

describe('flow layers', () => {
  const flow = normalizeFlow({
    mode: 'popup',
    floors: {
      '1F': {
        segments: [
          { points: [[0, 15], [20, 15]], weight: 0.25, value: 300 },
          { points: [[20, 15], [60, 15]], weight: 1, value: 1200 },
          { points: [[20, 13], [20, 20]], weight: 0.5, color: '#c62828' },
        ],
      },
    },
    markers: [
      { floor: '1F', x: 30, y: 15, rank: 1, kind: 'spot', label: '1', title: '후보 1 · 1F 남측 통로' },
      { floor: '1F', x: 50, y: 6, rank: 2, kind: 'unit', unit_code: '1F-A02' },
    ],
  })!

  it('adds nothing without a flow on this floor', () => {
    expect(flowPathLayers(EMPTY_FLOW_RENDER)).toEqual([])
    expect(flowMarkerLayers(EMPTY_FLOW_RENDER)).toEqual([])
    expect(ids(buildLayers(input())).some((id) => id.startsWith('flow-'))).toBe(false)
  })

  it('lays the lines on the walkways under highlights and labels, and the markers above the edges', () => {
    const order = ids(
      buildLayers(
        input({
          highlights: resolveHighlights([{ unit_code: '1F-A01' }], model.units),
          edges: resolveEdges([{ from: '1F-A01', to: '1F-A02', weight: 1, label: 'lift 1.0' }], '1F', model.units, []),
          flow: prepareFlow(flow, '1F'),
        }),
      ),
    )
    expect(order.filter((id) => id.startsWith('flow-'))).toEqual(['flow-casing', 'flow-paths', 'flow-markers', 'flow-marker-text'])
    expect(order.indexOf('flow-casing')).toBeGreaterThan(order.indexOf('openings'))
    expect(order.indexOf('flow-paths')).toBeLessThan(order.indexOf('highlights'))
    expect(order.indexOf('flow-paths')).toBeLessThan(order.indexOf('unit-labels'))
    expect(order.indexOf('flow-markers')).toBeGreaterThan(order.indexOf('edge-labels'))
  })

  it('draws 2-10 px lines colored by weight (or the backend color) and numbered markers colored by kind', () => {
    const layers = propsById([...flowPathLayers(prepareFlow(flow, '1F')), ...flowMarkerLayers(prepareFlow(flow, '1F'))])
    const paths = data(layers['flow-paths'])
    expect(paths.map((d) => call(layers['flow-paths'], 'getWidth', d))).toEqual([4, 6, 10])
    expect(paths.map((d) => call(layers['flow-casing'], 'getWidth', d))).toEqual([4 + FLOW_CASING_PX, 6 + FLOW_CASING_PX, 10 + FLOW_CASING_PX])
    expect(call(layers['flow-paths'], 'getColor', paths[1])).toEqual(hexToRgba('#c62828', 240))
    expect(layers['flow-paths']).toMatchObject({ widthUnits: 'pixels', pickable: true })
    const markers = data(layers['flow-markers'])
    expect(markers.map((d) => call(layers['flow-markers'], 'getFillColor', d))).toEqual([hexToRgba(FLOW_MARKER_COLORS.unit), hexToRgba(FLOW_MARKER_COLORS.spot)])
    expect(layers['flow-markers']).toMatchObject({ radiusMinPixels: FLOW_MARKER_MIN_PX, radiusMaxPixels: FLOW_MARKER_MAX_PX, pickable: true })
    expect(data(layers['flow-marker-text']).map((d) => d.text)).toEqual(['2', '1'])
  })
})