// Shortest walking route between two places ("route", docs/specs/atlas-api.md §3): defensive
// normalization of agent payloads and Korean display text. Pure functions (unit-tested without a DOM).
import type { FloorInfo, Route, RoutePlace, RouteSegment, RouteTransition } from '../types/api'
import { floorLabel, formatWalk, withEuro } from './format'
import { floorOfUnitCode } from './geometry'
import { floorOrdinal, viaLabel } from './journey'

export const ROUTE_COLOR = '#1e88e5'
/** Marker fills: green for the start, a darker blue than the path for the end (white text stays readable). */
export const ROUTE_ORIGIN_COLOR = '#2e7d32'
export const ROUTE_DESTINATION_COLOR = '#1565c0'

/** Words inside the start and end markers. */
export const ROUTE_MARKER_TEXT = { origin: '출발', destination: '도착' } as const

export type RoutePlaceRole = keyof typeof ROUTE_MARKER_TEXT

/** How a floor takes part in a route. */
export type RouteFloorRole = 'origin' | 'destination' | 'both' | 'pass'

export const ROUTE_FLOOR_ROLE_LABELS: Readonly<Record<RouteFloorRole, string>> = {
  origin: '출발',
  destination: '도착',
  both: '출발·도착',
  pass: '경유',
}

export const PLACE_KIND_LABELS: Readonly<Record<string, string>> = {
  unit: '매장',
  entrance: '출입구',
  escalator: '에스컬레이터',
  elevator: '엘리베이터',
  stairs: '계단',
  stair: '계단',
  amenity: '편의시설',
}

const VIA_TYPE_LABELS: Readonly<Record<string, string>> = {
  escalator: '에스컬레이터',
  elevator: '엘리베이터',
  stairs: '계단',
  stair: '계단',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Finite number (numeric strings accepted) or null. */
function finite(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function text(value: unknown): string | null {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed ? trimmed : null
}

function toPoint(value: unknown): [number, number] | null {
  if (Array.isArray(value)) {
    const x = finite(value[0])
    const y = finite(value[1])
    return x === null || y === null ? null : [x, y]
  }
  if (isRecord(value)) {
    const x = finite(value.x)
    const y = finite(value.y)
    return x === null || y === null ? null : [x, y]
  }
  return null
}

/** Floor of a place reference: "1F:ESC-A" -> "1F", "3F-C05" -> "3F", "ENT-1" -> null. */
export function floorOfRef(ref: string | null | undefined): string | null {
  const match = /^(B\d+F?|\d+F)[-:]/i.exec(ref?.trim() ?? '')
  return match ? match[1].toUpperCase() : null
}

export function placeKindLabel(kind: string | null | undefined): string {
  return PLACE_KIND_LABELS[kind ?? ''] ?? ''
}

function normalizePlace(value: unknown): RoutePlace | null {
  if (!isRecord(value)) return null
  const unitCode = text(value.unit_code)
  const ref = text(value.ref) ?? unitCode ?? ''
  const floor = text(value.floor) ?? floorOfUnitCode(unitCode) ?? floorOfRef(ref)
  if (!floor) return null
  const kind = text(value.kind) ?? (unitCode ? 'unit' : '')
  return {
    ...value,
    ref,
    kind,
    label: text(value.label) ?? (ref || placeKindLabel(kind) || floorLabel(floor)),
    floor,
    unit_code: unitCode,
    x: finite(value.x),
    y: finite(value.y),
  }
}

function normalizeSegments(value: unknown): RouteSegment[] {
  if (!Array.isArray(value)) return []
  const segments: RouteSegment[] = []
  for (const item of value) {
    if (!isRecord(item)) continue
    const floor = text(item.floor)
    const points = Array.isArray(item.points) ? item.points.map(toPoint).filter((p): p is [number, number] => p !== null) : []
    if (!floor || points.length === 0) continue
    segments.push({ ...item, floor, points })
  }
  return segments
}

/** Floor changes; without usable ones a route between two floors gets one (via unknown). */
function normalizeTransitions(value: unknown, origin: RoutePlace, destination: RoutePlace): RouteTransition[] {
  const result: RouteTransition[] = []
  for (const item of Array.isArray(value) ? value : []) {
    if (!isRecord(item)) continue
    const from = text(item.from_floor)
    const to = text(item.to_floor)
    if (!from || !to || from === to) continue
    result.push({ ...item, from_floor: from, to_floor: to, via: text(item.via), floors: finite(item.floors), height_m: finite(item.height_m) })
  }
  if (result.length === 0 && origin.floor !== destination.floor) {
    result.push({ from_floor: origin.floor, to_floor: destination.floor, via: null, floors: null, height_m: null })
  }
  return result
}

/**
 * Coerce an agent-provided route into a renderable one. Returns null unless the value is an object
 * with an origin and a destination whose floors are known; invalid points and segments are dropped.
 */
export function normalizeRoute(raw: unknown): Route | null {
  if (!isRecord(raw)) return null
  const origin = normalizePlace(raw.origin)
  const destination = normalizePlace(raw.destination)
  if (!origin || !destination) return null
  return {
    ...raw,
    title: text(raw.title) ?? `${origin.label} → ${destination.label}`,
    rev: text(raw.rev),
    via_type: text(raw.via_type),
    origin,
    destination,
    segments: normalizeSegments(raw.segments),
    transitions: normalizeTransitions(raw.transitions, origin, destination),
    walk_m: finite(raw.walk_m),
    caveat: text(raw.caveat),
  }
}

// --- floors ------------------------------------------------------------------------------------

/** Floors of the route in walking order (origin floor first, destination floor last). */
export function routeFloors(route: Route | null | undefined): string[] {
  if (!route) return []
  const floors: string[] = []
  const add = (floor: string) => {
    if (floor && !floors.includes(floor)) floors.push(floor)
  }
  add(route.origin.floor)
  for (const t of route.transitions) {
    add(t.from_floor)
    add(t.to_floor)
  }
  for (const segment of route.segments) add(segment.floor)
  add(route.destination.floor)
  return floors
}

/** floor -> how it takes part in the route (floor tabs and the route box mark these floors). */
export function routeFloorRoles(route: Route | null | undefined): Map<string, RouteFloorRole> {
  const roles = new Map<string, RouteFloorRole>()
  if (!route) return roles
  for (const floor of routeFloors(route)) {
    const origin = floor === route.origin.floor
    const destination = floor === route.destination.floor
    roles.set(floor, origin && destination ? 'both' : origin ? 'origin' : destination ? 'destination' : 'pass')
  }
  return roles
}

// --- display text ------------------------------------------------------------------------------

/** Floors moved by a transition: `floors` when given, else the difference of the floor ordinals. */
export function transitionFloorCount(t: RouteTransition, floors?: readonly FloorInfo[]): number | null {
  if (t.floors !== null && Number.isFinite(t.floors)) return Math.abs(Math.round(t.floors))
  const a = floorOrdinal(t.from_floor, floors)
  const b = floorOrdinal(t.to_floor, floors)
  return a === null || b === null ? null : Math.abs(b - a)
}

/** "에스컬레이터 A · 7개 층 이동 · 높이 35m" (unknown parts are left out). */
export function routeTransitionText(t: RouteTransition, viaType: string | null, floors?: readonly FloorInfo[]): string {
  const parts: string[] = []
  const via = viaLabel(t.via) || VIA_TYPE_LABELS[viaType ?? ''] || ''
  if (via) parts.push(via)
  const count = transitionFloorCount(t, floors)
  parts.push(count ? `${count}개 층 이동` : `${withEuro(floorLabel(t.to_floor))} 이동`)
  if (t.height_m !== null && t.height_m > 0) parts.push(`높이 ${formatWalk(t.height_m)}`)
  return parts.join(' · ')
}

/** Summary chips of the route box: "걷는 거리 85m", then one chip per floor change. */
export function routeSummaryChips(route: Route, floors?: readonly FloorInfo[]): string[] {
  const chips: string[] = []
  if (route.walk_m !== null) chips.push(`걷는 거리 ${formatWalk(route.walk_m)}`)
  for (const t of route.transitions) chips.push(routeTransitionText(t, route.via_type, floors))
  return chips
}

/** Legend line: "통로망 최단 경로 · 걷는 거리 85m". */
export function routeLegendText(route: Route): string {
  return route.walk_m !== null ? `통로망 최단 경로 · 걷는 거리 ${formatWalk(route.walk_m)}` : '통로망 최단 경로'
}
