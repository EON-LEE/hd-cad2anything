// Geometry helpers for floor plans in local meter coordinates (origin at the building's SW corner).
import type { Feature, FeatureCollection, FeatureProps, Position } from '../types/api'

export type Point2 = [number, number]
/** [minX, minY, maxX, maxY] */
export type BBox = [number, number, number, number]

export interface PolygonItem {
  id: string
  featureId: string
  kind: string
  rings: Point2[][]
  props: FeatureProps
}

export interface LineItem {
  id: string
  featureId: string
  kind: string
  path: Point2[]
  props: FeatureProps
}

export interface UnitInfo {
  code: string
  featureId: string
  props: FeatureProps
  parts: PolygonItem[]
  bbox: BBox
  centroid: Point2
  area: number
}

export type PolygonGroup = 'exterior' | 'walkway' | 'unit' | 'vertical_link' | 'amenity' | 'fixture' | 'zone' | 'other'

export interface FloorModel {
  polygons: Record<PolygonGroup, PolygonItem[]>
  lines: LineItem[]
  units: Map<string, UnitInfo>
  bbox: BBox | null
}

const GROUPS: readonly PolygonGroup[] = ['exterior', 'walkway', 'unit', 'vertical_link', 'amenity', 'fixture', 'zone']

function toPoint(p: Position): Point2 {
  return [Number(p[0]), Number(p[1])]
}

export function ringArea(ring: readonly Point2[]): number {
  let sum = 0
  for (let i = 0; i < ring.length; i += 1) {
    const [x1, y1] = ring[i]
    const [x2, y2] = ring[(i + 1) % ring.length]
    sum += x1 * y2 - x2 * y1
  }
  return sum / 2
}

export function polygonArea(rings: readonly Point2[][]): number {
  if (rings.length === 0) return 0
  const outer = Math.abs(ringArea(rings[0]))
  const holes = rings.slice(1).reduce((acc, r) => acc + Math.abs(ringArea(r)), 0)
  return outer - holes
}

export function bboxOfPoints(points: Iterable<Point2>): BBox | null {
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const [x, y] of points) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue
    if (x < minX) minX = x
    if (y < minY) minY = y
    if (x > maxX) maxX = x
    if (y > maxY) maxY = y
  }
  return minX === Infinity ? null : [minX, minY, maxX, maxY]
}

export function unionBBox(boxes: Iterable<BBox | null | undefined>): BBox | null {
  let result: BBox | null = null
  for (const b of boxes) {
    if (!b) continue
    result = result
      ? [Math.min(result[0], b[0]), Math.min(result[1], b[1]), Math.max(result[2], b[2]), Math.max(result[3], b[3])]
      : [...b]
  }
  return result
}

export function bboxCenter(b: BBox): Point2 {
  return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]
}

/** Area-weighted centroid of the exterior ring; falls back to the bbox center for degenerate rings. */
export function polygonCentroid(rings: readonly Point2[][]): Point2 {
  const ring = rings[0] ?? []
  const a = ringArea(ring)
  if (Math.abs(a) < 1e-9) {
    const b = bboxOfPoints(ring)
    return b ? bboxCenter(b) : [0, 0]
  }
  let cx = 0
  let cy = 0
  for (let i = 0; i < ring.length; i += 1) {
    const [x1, y1] = ring[i]
    const [x2, y2] = ring[(i + 1) % ring.length]
    const f = x1 * y2 - x2 * y1
    cx += (x1 + x2) * f
    cy += (y1 + y2) * f
  }
  return [cx / (6 * a), cy / (6 * a)]
}

function groupOf(kind: string): PolygonGroup {
  return (GROUPS as readonly string[]).includes(kind) ? (kind as PolygonGroup) : 'other'
}

function featureId(feature: Feature, index: number): string {
  const props = feature.properties ?? ({} as FeatureProps)
  const raw = feature.id ?? (props.entity_id as string | undefined) ?? `${props.kind ?? 'feature'}-${index}`
  return String(raw)
}

/** Split a GeoJSON FeatureCollection into render-ready groups and a unit index keyed by unit_code. */
export function buildFloorModel(fc: FeatureCollection | null | undefined): FloorModel {
  const polygons = Object.fromEntries([...GROUPS, 'other'].map((g) => [g, [] as PolygonItem[]])) as Record<
    PolygonGroup,
    PolygonItem[]
  >
  const lines: LineItem[] = []
  const units = new Map<string, UnitInfo>()
  const allBoxes: (BBox | null)[] = []
  ;(fc?.features ?? []).forEach((feature, index) => {
    const geometry = feature.geometry
    if (!geometry) return
    const props: FeatureProps = { ...(feature.properties ?? {}), kind: String(feature.properties?.kind ?? 'other') }
    const id = featureId(feature, index)
    const kind = String(props.kind)
    const parts: Point2[][][] = []
    if (geometry.type === 'Polygon') parts.push(geometry.coordinates.map((ring) => ring.map(toPoint)))
    else if (geometry.type === 'MultiPolygon') {
      for (const poly of geometry.coordinates) parts.push(poly.map((ring) => ring.map(toPoint)))
    } else if (geometry.type === 'LineString') {
      const path = geometry.coordinates.map(toPoint)
      lines.push({ id, featureId: id, kind, path, props })
      allBoxes.push(bboxOfPoints(path))
      return
    } else if (geometry.type === 'MultiLineString') {
      geometry.coordinates.forEach((coords, i) => {
        const path = coords.map(toPoint)
        lines.push({ id: `${id}#${i}`, featureId: id, kind, path, props })
        allBoxes.push(bboxOfPoints(path))
      })
      return
    } else {
      return
    }
    const items = parts
      .filter((rings) => rings.length > 0 && rings[0].length >= 3)
      .map((rings, i): PolygonItem => ({ id: parts.length > 1 ? `${id}#${i}` : id, featureId: id, kind, rings, props }))
    if (items.length === 0) return
    polygons[groupOf(kind)].push(...items)
    const boxes = items.map((item) => bboxOfPoints(item.rings[0]))
    allBoxes.push(...boxes)
    if (kind === 'unit') {
      const code = String(props.unit_code ?? id)
      const largest = items.reduce((best, item) => (polygonArea(item.rings) > polygonArea(best.rings) ? item : best))
      units.set(code, {
        code,
        featureId: id,
        props,
        parts: items,
        bbox: unionBBox(boxes) ?? [0, 0, 0, 0],
        centroid: polygonCentroid(largest.rings),
        area: items.reduce((acc, item) => acc + polygonArea(item.rings), 0),
      })
    }
  })
  return { polygons, lines, units, bbox: unionBBox(allBoxes) }
}

/** Floor code from a unit code following `{floor}-{band}{nn}` (e.g. "3F-N05" -> "3F"). */
export function floorOfUnitCode(code: string | null | undefined): string | null {
  if (!code) return null
  const match = /^([A-Za-z0-9]+)-./.exec(code)
  return match ? match[1].toUpperCase() : null
}

// ---------------------------------------------------------------------------------------------
// Orthographic view math (deck.gl OrthographicView with flipY=false: +y is up on screen).
// ---------------------------------------------------------------------------------------------

export interface OrthoViewState {
  target: [number, number, number]
  zoom: number
  minZoom: number
  maxZoom: number
}

export const MIN_ZOOM = -1
export const MAX_ZOOM = 7

export function fitViewState(
  bbox: BBox,
  width: number,
  height: number,
  options: { padding?: number; minSpan?: number; maxZoom?: number } = {},
): OrthoViewState {
  const padding = options.padding ?? 40
  const minSpan = options.minSpan ?? 6
  const spanX = Math.max(bbox[2] - bbox[0], minSpan)
  const spanY = Math.max(bbox[3] - bbox[1], minSpan)
  const availW = Math.max(width - 2 * padding, 40)
  const availH = Math.max(height - 2 * padding, 40)
  const zoom = Math.min(Math.max(Math.log2(Math.min(availW / spanX, availH / spanY)), MIN_ZOOM), options.maxZoom ?? MAX_ZOOM)
  const [cx, cy] = bboxCenter(bbox)
  return { target: [cx, cy, 0], zoom, minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM }
}

/** fitViewState for the part of the canvas right of `leftInset` pixels (e.g. beside a floating card). */
export function fitViewStateBeside(
  bbox: BBox,
  width: number,
  height: number,
  leftInset: number,
  options: { padding?: number; minSpan?: number; maxZoom?: number } = {},
): OrthoViewState {
  const inset = Math.max(0, Math.min(leftInset, width - 80))
  const view = fitViewState(bbox, width - inset, height, options)
  const shift = inset / 2 / 2 ** view.zoom
  return { ...view, target: [view.target[0] - shift, view.target[1], 0] }
}

export function worldToScreen(view: Pick<OrthoViewState, 'target' | 'zoom'>, width: number, height: number, p: Point2): Point2 {
  const scale = 2 ** view.zoom
  return [width / 2 + (p[0] - view.target[0]) * scale, height / 2 - (p[1] - view.target[1]) * scale]
}

export function screenToWorld(view: Pick<OrthoViewState, 'target' | 'zoom'>, width: number, height: number, p: Point2): Point2 {
  const scale = 2 ** view.zoom
  return [view.target[0] + (p[0] - width / 2) / scale, view.target[1] - (p[1] - height / 2) / scale]
}

/** Quadratic Bezier between a and b, bent sideways by `bend` x distance. Returns path and midpoint. */
export function curvedPath(a: Point2, b: Point2, bend = 0.15, segments = 24): { path: Point2[]; mid: Point2 } {
  const dx = b[0] - a[0]
  const dy = b[1] - a[1]
  const c: Point2 = [(a[0] + b[0]) / 2 - dy * bend, (a[1] + b[1]) / 2 + dx * bend]
  const path: Point2[] = []
  for (let i = 0; i <= segments; i += 1) {
    const t = i / segments
    const u = 1 - t
    path.push([u * u * a[0] + 2 * u * t * c[0] + t * t * b[0], u * u * a[1] + 2 * u * t * c[1] + t * t * b[1]])
  }
  const mid: Point2 = [0.25 * a[0] + 0.5 * c[0] + 0.25 * b[0], 0.25 * a[1] + 0.5 * c[1] + 0.25 * b[1]]
  return { path, mid }
}

/** Rough rendered width of a label in pixels (Hangul/CJK ~1em, Latin ~0.6em). */
export function estimateTextWidth(text: string, fontPx: number): number {
  let em = 0
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0
    if ((code >= 0x1100 && code <= 0x11ff) || (code >= 0x3000 && code <= 0x9fff) || (code >= 0xac00 && code <= 0xd7a3)) em += 1
    else if (ch === ' ') em += 0.3
    else if (ch >= 'A' && ch <= 'Z') em += 0.68
    else em += 0.56
  }
  return em * fontPx
}
