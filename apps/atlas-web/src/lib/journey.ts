// One customer's shopping path ("journey", docs/specs/atlas-api.md §3): defensive normalization of
// agent/REST payloads and Korean display text. Pure functions (unit-tested without a DOM).
import type {
  FloorInfo,
  Journey,
  JourneySegment,
  JourneyStop,
  JourneySummary,
  JourneyTransition,
  SegmentFilter,
} from '../types/api'
import { floorLabel, formatManwon, formatMinutes, formatWalk, withEuro } from './format'
import { floorOfUnitCode } from './geometry'

export const JOURNEY_COLOR = '#ff6d00'
export const DEFAULT_JOURNEY_TITLE = '고객 쇼핑 동선'

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

/** Minutes between two "HH:MM" times (null when either is not parseable or the order is reversed). */
export function minutesBetween(start: string, end: string): number | null {
  const parse = (value: string) => {
    const match = /^(\d{1,2}):(\d{2})/.exec(value.trim())
    return match ? Number(match[1]) * 60 + Number(match[2]) : null
  }
  const a = parse(start)
  const b = parse(end)
  return a === null || b === null || b < a ? null : b - a
}

function normalizeStops(value: unknown): JourneyStop[] {
  if (!Array.isArray(value)) return []
  const stops: JourneyStop[] = []
  value.forEach((item, index) => {
    if (!isRecord(item)) return
    const unitCode = text(item.unit_code) ?? ''
    const floor = text(item.floor) ?? floorOfUnitCode(unitCode) ?? ''
    if (!unitCode && !floor) return
    stops.push({
      ...item,
      order: finite(item.order) ?? index + 1,
      time: text(item.time) ?? '',
      unit_code: unitCode,
      floor,
      brand_name: text(item.brand_name),
      category: text(item.category),
      category_name: text(item.category_name),
      amount_krw: finite(item.amount_krw),
      gap_min: finite(item.gap_min),
      x: finite(item.x),
      y: finite(item.y),
    })
  })
  return stops.sort((a, b) => a.order - b.order)
}

function normalizeSegments(value: unknown): JourneySegment[] {
  if (!Array.isArray(value)) return []
  const segments: JourneySegment[] = []
  for (const item of value) {
    if (!isRecord(item)) continue
    const floor = text(item.floor)
    const points = Array.isArray(item.points) ? item.points.map(toPoint).filter((p): p is [number, number] => p !== null) : []
    if (!floor || points.length === 0) continue
    segments.push({ ...item, floor, points, from_order: finite(item.from_order), to_order: finite(item.to_order) })
  }
  return segments
}

/** Floor changes between consecutive stops (used when the payload has no `transitions`). */
export function deriveTransitions(stops: readonly JourneyStop[]): JourneyTransition[] {
  const result: JourneyTransition[] = []
  for (let i = 1; i < stops.length; i += 1) {
    const a = stops[i - 1]
    const b = stops[i]
    if (a.floor && b.floor && a.floor !== b.floor) {
      result.push({ from_order: a.order, to_order: b.order, from_floor: a.floor, to_floor: b.floor, via: null })
    }
  }
  return result
}

function normalizeTransitions(value: unknown, stops: readonly JourneyStop[]): JourneyTransition[] {
  if (!Array.isArray(value)) return deriveTransitions(stops)
  const floorOf = (order: number | null) => (order === null ? null : (stops.find((s) => s.order === order)?.floor ?? null))
  const result: JourneyTransition[] = []
  for (const item of value) {
    if (!isRecord(item)) continue
    const fromOrder = finite(item.from_order)
    const toOrder = finite(item.to_order)
    const fromFloor = text(item.from_floor) ?? floorOf(fromOrder)
    const toFloor = text(item.to_floor) ?? floorOf(toOrder)
    if (!fromFloor || !toFloor) continue
    result.push({ ...item, from_order: fromOrder, to_order: toOrder, from_floor: fromFloor, to_floor: toFloor, via: text(item.via) })
  }
  return result
}

/** Floors in the order the customer first visited them. */
export function journeyFloors(stops: readonly JourneyStop[]): string[] {
  const floors: string[] = []
  for (const stop of stops) if (stop.floor && !floors.includes(stop.floor)) floors.push(stop.floor)
  return floors
}

function normalizeSummary(value: unknown, stops: readonly JourneyStop[]): JourneySummary {
  const raw = isRecord(value) ? value : {}
  const floors = Array.isArray(raw.floors) ? raw.floors.map(text).filter((f): f is string => !!f) : journeyFloors(stops)
  const amounts = stops.map((s) => s.amount_krw).filter((a): a is number => a !== null)
  const first = stops[0]
  const last = stops.at(-1)
  return {
    ...raw,
    stops: finite(raw.stops) ?? stops.length,
    floors,
    total_krw: finite(raw.total_krw) ?? (amounts.length ? amounts.reduce((acc, a) => acc + a, 0) : null),
    duration_min: finite(raw.duration_min) ?? (first && last ? minutesBetween(first.time, last.time) : null),
    walk_m: finite(raw.walk_m),
  }
}

function normalizeQuery(value: unknown): Journey['query'] {
  if (!isRecord(value)) return null
  const { rev: rawRev, ...rest } = value
  const segment: SegmentFilter | null = isRecord(value.segment) || typeof value.segment === 'string' ? value.segment : null
  const rev = text(rawRev)
  return { ...rest, segment, rank: Math.max(1, Math.round(finite(value.rank) ?? 1)), ...(rev ? { rev } : {}) }
}

function normalizeCustomer(value: unknown): Journey['customer'] {
  if (!isRecord(value)) return null
  return { ...value, id: text(value.id) ?? '', segment: text(value.segment) ?? '' }
}

/**
 * Coerce an agent- or REST-provided journey into a renderable one. Returns null when the value is not
 * an object or has no usable stop; missing or malformed fields fall back to values derived from stops.
 */
export function normalizeJourney(raw: unknown): Journey | null {
  if (!isRecord(raw)) return null
  const stops = normalizeStops(raw.stops)
  if (stops.length === 0) return null
  return {
    ...raw,
    title: text(raw.title) ?? DEFAULT_JOURNEY_TITLE,
    customer: normalizeCustomer(raw.customer),
    date: text(raw.date),
    weekday: text(raw.weekday),
    rev: text(raw.rev),
    query: normalizeQuery(raw.query),
    stops,
    segments: normalizeSegments(raw.segments),
    transitions: normalizeTransitions(raw.transitions, stops),
    summary: normalizeSummary(raw.summary, stops),
    caveat: text(raw.caveat),
  }
}

/** Segment query string of the analytics endpoints: `age_band:40s;gender:F;tier:VIP|VVIP`. */
export function segmentParam(segment: SegmentFilter | null | undefined): string {
  if (!segment) return ''
  if (typeof segment === 'string') return segment.trim()
  return Object.entries(segment)
    .filter(([, v]) => v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && v.length === 0))
    .map(([key, v]) => `${key}:${Array.isArray(v) ? v.map(String).join('|') : String(v)}`)
    .join(';')
}

/** Number of stops per floor. */
export function journeyStopCounts(journey: Journey | null | undefined): Map<string, number> {
  const counts = new Map<string, number>()
  for (const stop of journey?.stops ?? []) if (stop.floor) counts.set(stop.floor, (counts.get(stop.floor) ?? 0) + 1)
  return counts
}

// --- display text ------------------------------------------------------------------------------

/** Ordinal of a floor: from /api/meta when known, else parsed from the code (B1 = -1, 1F = 0, 3F = 2). */
export function floorOrdinal(code: string, floors?: readonly FloorInfo[]): number | null {
  const known = floors?.find((f) => f.code === code)
  if (known) return known.ordinal
  const upper = code.toUpperCase()
  const basement = /^B(\d+)F?$/.exec(upper)
  if (basement) return -Number(basement[1])
  const above = /^(\d+)F$/.exec(upper)
  return above ? Number(above[1]) - 1 : null
}

/** +1 going up, -1 going down, 0 when unknown. */
export function transitionDirection(t: Pick<JourneyTransition, 'from_floor' | 'to_floor'>, floors?: readonly FloorInfo[]): number {
  const a = floorOrdinal(t.from_floor, floors)
  const b = floorOrdinal(t.to_floor, floors)
  if (a === null || b === null || a === b) return 0
  return b > a ? 1 : -1
}

const VIA_KINDS: [RegExp, string][] = [
  [/^ESC-?(.+)$/i, '에스컬레이터'],
  [/^EV-?(.+)$/i, '엘리베이터'],
  [/^ST-?(.+)$/i, '계단'],
]

/** "ESC-A" -> "에스컬레이터 A", "EV-1" -> "엘리베이터 1"; other ids are returned as is. */
export function viaLabel(via: string | null | undefined): string {
  if (!via) return ''
  for (const [pattern, name] of VIA_KINDS) {
    const match = pattern.exec(via)
    if (match) return `${name} ${match[1]}`
  }
  return via
}

/** Card row between stops: "↑ 3층으로 이동 (에스컬레이터 A)". */
export function transitionText(t: JourneyTransition, floors?: readonly FloorInfo[]): string {
  const dir = transitionDirection(t, floors)
  const arrow = dir > 0 ? '↑' : dir < 0 ? '↓' : '→'
  const via = viaLabel(t.via)
  return `${arrow} ${withEuro(floorLabel(t.to_floor))} 이동${via ? ` (${via})` : ''}`
}

/** Map label at the end of a departing segment: "→ 3층 (ESC-A)". */
export function departureLabel(t: Pick<JourneyTransition, 'to_floor' | 'via'>): string {
  return `→ ${floorLabel(t.to_floor)}${t.via ? ` (${t.via})` : ''}`
}

/** Map label at the start of an arriving segment: "1층에서 올라옴". */
export function arrivalLabel(t: Pick<JourneyTransition, 'from_floor' | 'to_floor'>, floors?: readonly FloorInfo[]): string {
  const dir = transitionDirection(t, floors)
  return `${floorLabel(t.from_floor)}에서 ${dir > 0 ? '올라옴' : dir < 0 ? '내려옴' : '이동'}`
}

/** "23분 후" before a stop (empty for the first stop or an unknown gap). */
export function gapText(gapMin: number | null | undefined): string {
  return gapMin === null || gapMin === undefined || !Number.isFinite(gapMin) ? '' : `${formatMinutes(gapMin)} 후`
}

/** Summary chips: "결제 4회", "3개 층", "총 51만원", "3시간 5분", "추정 이동 640m". */
export function journeySummaryChips(journey: Journey): string[] {
  const s = journey.summary
  const chips = [`결제 ${s.stops}회`]
  if (s.floors.length) chips.push(`${s.floors.length}개 층`)
  if (s.total_krw !== null) chips.push(`총 ${formatManwon(s.total_krw)}`)
  if (s.duration_min !== null && s.duration_min > 0) chips.push(formatMinutes(s.duration_min))
  if (s.walk_m !== null) chips.push(`추정 이동 ${formatWalk(s.walk_m)}`)
  return chips
}

/** "C004211 · 2025-11-08(토)" style subtitle (missing parts are skipped). */
export function journeySubtitle(journey: Journey): string {
  const parts: string[] = []
  if (journey.customer?.segment) parts.push(journey.customer.segment)
  if (journey.date) parts.push(journey.weekday ? `${journey.date}(${journey.weekday})` : journey.date)
  if (journey.customer?.id) parts.push(`고객 ${journey.customer.id}`)
  return parts.join(' · ')
}
