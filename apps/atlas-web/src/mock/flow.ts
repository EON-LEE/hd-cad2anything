// Mock GET /api/spatial/flow on the corridor centerlines of the synthetic floors (the same walkway network
// as the mock journeys and routes).
// - traffic: synthetic trips (escalator landings and, on 1F, entrances to stores; store to store) weighted
//   by brand drawing power, routed along the shortest corridor paths and counted per corridor stretch.
// - popup: the same traffic, with ranked candidates: the two busiest stretches far apart on each floor,
//   and popup or vacant units (scored by the traffic next to them).
// - egress: walkways narrower than the width criterion, and store entrances farther than the travel
//   criterion from a stair (or, on 1F, an entrance).
import { ApiError } from '../api/client'
import { segmentParam } from '../lib/journey'
import type { Flow, FlowFloor, FlowMarker, FlowParams, FlowSegment } from '../types/api'
import { MOCK_FLOORS } from './catalog'
import { CORRIDORS, ESCALATORS, pathLength, routeBetween, segmentLabelKo, segmentObject, unitEntrance } from './journey'
import { OPENINGS, type MockFloor, type MockUnit, type MockWorld } from './world'

type Pt = [number, number]

const EPS = 1e-6
const FLOW_REV = 'R2'

/** Defaults of the egress check (the mock's criteria; the backend has its own). */
export const DEFAULT_MIN_WIDTH_M = 4
export const DEFAULT_MAX_TRAVEL_M = 65

/** Customer-days of the whole store in the period, and the busiest stretch's share of them per floor. */
const BASE_CUSTOMER_DAYS = 520_000
const TRIPS_PER_CUSTOMER_DAY = 3.4
const PEAK_SHARE: Record<string, number> = { '1F': 0.31, '3F': 0.22 }
const STORE_HOURS = 10.5

const CAVEATS: Record<string, string> = {
  traffic: '결제 순서를 통로망 최단 경로로 이은 추정이에요. 실제로 걸은 길이 아니에요. (모의 데이터)',
  popup: '결제 순서로 추정한 유동이 많은 곳을 후보로 골랐어요. 실제 설치 가능 여부(소방·통로 폭)는 현장에서 확인해야 해요. (모의 데이터)',
  egress: '도면의 통로 폭과 통로망 거리로 계산한 점검이에요. 법정 피난 거리 판정이 아니고 집기와 임시 구조물은 넣지 않았어요. (모의 데이터)',
}

const LEGEND = { low: '적음', high: '많음', unit: '명(추정)', note: '결제 순서를 통로망 최단 경로로 이어 모은 값' }

const WARNING_COLOR = '#c62828'

const round2 = (v: number) => Math.round(v * 100) / 100
const dist = (a: Pt, b: Pt) => Math.hypot(a[0] - b[0], a[1] - b[1])
const horizontal = ([a, b]: readonly [Pt, Pt]) => Math.abs(a[1] - b[1]) < EPS

// --- corridor stretches (graph edges between corridor ends and crossings) ------------------------

function crossing(h: readonly [Pt, Pt], v: readonly [Pt, Pt]): Pt | null {
  const x = v[0][0]
  const y = h[0][1]
  const inX = x >= Math.min(h[0][0], h[1][0]) - EPS && x <= Math.max(h[0][0], h[1][0]) + EPS
  const inY = y >= Math.min(v[0][1], v[1][1]) - EPS && y <= Math.max(v[0][1], v[1][1]) + EPS
  return inX && inY ? [x, y] : null
}

let stretchCache: [Pt, Pt][] | null = null

/** Corridor centerlines cut at every end and crossing, so that stretches meet only at their ends. */
export function corridorStretches(): [Pt, Pt][] {
  if (stretchCache) return stretchCache
  const stretches: [Pt, Pt][] = []
  for (const corridor of CORRIDORS) {
    const cuts: Pt[] = [corridor[0], corridor[1]]
    for (const other of CORRIDORS) {
      if (other === corridor || horizontal(other) === horizontal(corridor)) continue
      for (const end of other) {
        // Ends of crossing corridors that touch this one (T junctions) cut it too.
        const onLine = horizontal(corridor) ? Math.abs(end[1] - corridor[0][1]) < EPS : Math.abs(end[0] - corridor[0][0]) < EPS
        if (onLine) cuts.push(end)
      }
      const p = horizontal(corridor) ? crossing(corridor, other) : crossing(other, corridor)
      if (p) cuts.push(p)
    }
    const axis = horizontal(corridor) ? 0 : 1
    const lo = Math.min(corridor[0][axis], corridor[1][axis])
    const hi = Math.max(corridor[0][axis], corridor[1][axis])
    const along = [...new Map(cuts.filter((p) => p[axis] >= lo - EPS && p[axis] <= hi + EPS).map((p) => [`${round2(p[0])},${round2(p[1])}`, p])).values()]
    along.sort((p, q) => p[axis] - q[axis])
    for (let i = 1; i < along.length; i += 1) {
      if (dist(along[i - 1], along[i]) > EPS) stretches.push([along[i - 1], along[i]])
    }
  }
  stretchCache = stretches
  return stretches
}

/** Length of the overlap between a walked leg and a stretch on the same corridor line. */
function overlap(leg: readonly [Pt, Pt], stretch: readonly [Pt, Pt]): number {
  if (horizontal(leg) !== horizontal(stretch)) return 0
  const axis = horizontal(stretch) ? 0 : 1
  const other = 1 - axis
  if (Math.abs(leg[0][other] - stretch[0][other]) > EPS || Math.abs(leg[1][other] - leg[0][other]) > EPS) return 0
  const lo = Math.max(Math.min(leg[0][axis], leg[1][axis]), Math.min(stretch[0][axis], stretch[1][axis]))
  const hi = Math.min(Math.max(leg[0][axis], leg[1][axis]), Math.max(stretch[0][axis], stretch[1][axis]))
  return Math.max(0, hi - lo)
}

// --- traffic ------------------------------------------------------------------------------------

const occupied = (floor: MockFloor) => floor.units.filter((u) => u.brandId)

/** Raw trip counts per stretch on one floor (relative values; scaled by the query later). */
function rawTraffic(world: MockWorld, floor: MockFloor): number[] {
  const stretches = corridorStretches()
  const counts = stretches.map(() => 0)
  const units = occupied(floor).sort((a, b) => a.code.localeCompare(b.code))
  const pull = (u: MockUnit) => world.brands.get(u.brandId!)?.attractiveness ?? 1
  const sources: { at: Pt; weight: number }[] = ESCALATORS.map((e) => ({ at: e.at, weight: floor.code === '1F' ? 0.6 : 1.4 }))
  for (const opening of OPENINGS[floor.code] ?? []) {
    const [a, b] = [opening.path[0], opening.path.at(-1)!]
    const mid: Pt = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
    sources.push({ at: mid, weight: opening.id === 'ENT-1' ? 2 : 0.8 })
  }
  const add = (from: Pt, to: Pt, weight: number) => {
    const path = routeBetween(from, to)
    for (let i = 1; i < path.length; i += 1) {
      const leg: [Pt, Pt] = [path[i - 1], path[i]]
      stretches.forEach((stretch, s) => {
        const shared = overlap(leg, stretch)
        if (shared > 0) counts[s] += (weight * shared) / dist(stretch[0], stretch[1])
      })
    }
  }
  for (const source of sources) for (const unit of units) add(source.at, unitEntrance(unit), source.weight * pull(unit))
  units.forEach((unit, i) => {
    for (const step of [3, 7]) {
      const other = units[(i + step) % units.length]
      if (other !== unit) add(unitEntrance(unit), unitEntrance(other), 0.35 * pull(unit) * pull(other))
    }
  })
  return counts
}

const trafficCache = new Map<string, number[]>()

function trafficFor(world: MockWorld, rev: string, floor: MockFloor): number[] {
  const key = `${rev}:${floor.code}:${floor.units.map((u) => `${u.code}=${u.brandId ?? ''}`).join(',')}`
  let counts = trafficCache.get(key)
  if (!counts) {
    counts = rawTraffic(world, floor)
    trafficCache.set(key, counts)
  }
  return counts
}

function walkwayName(floor: MockFloor, p: Pt): string | null {
  for (const walkway of floor.walkways) {
    const xs = walkway.ring.map((q) => q[0])
    const ys = walkway.ring.map((q) => q[1])
    if (p[0] >= Math.min(...xs) - EPS && p[0] <= Math.max(...xs) + EPS && p[1] >= Math.min(...ys) - EPS && p[1] <= Math.max(...ys) + EPS) {
      return walkway.name_ko
    }
  }
  return null
}

/** "3F-C05·3F-C06 앞 통로" for a stretch with store entrances along it; else the walkway's name. */
function stretchLabel(floor: MockFloor, stretch: readonly [Pt, Pt]): string {
  const mid: Pt = [(stretch[0][0] + stretch[1][0]) / 2, (stretch[0][1] + stretch[1][1]) / 2]
  const axis = horizontal(stretch) ? 0 : 1
  const lo = Math.min(stretch[0][axis], stretch[1][axis]) - 2
  const hi = Math.max(stretch[0][axis], stretch[1][axis]) + 2
  const near = floor.units
    .map((u) => ({ code: u.code, at: unitEntrance(u) }))
    .filter(({ at }) => at[axis] >= lo && at[axis] <= hi && Math.abs(at[1 - axis] - stretch[0][1 - axis]) <= 4)
    .sort((a, b) => dist(a.at, mid) - dist(b.at, mid))
    .slice(0, 2)
    .map((u) => u.code)
    .sort()
  if (near.length) return `${near.join('·')} 앞 통로`
  return walkwayName(floor, mid) ?? '통로'
}

interface Scale {
  customerDays: number
  factor: number
}

const AGE_SHARE = 0.24
const GENDER_SHARE: Record<string, number> = { F: 0.64, M: 0.36 }
const TIER_SHARE: Record<string, number> = { VIP: 0.12, VVIP: 0.04 }
const DAY_SHARE: Record<string, number> = { weekend: 0.42, weekday: 0.58 }

function parseHours(hours: FlowParams['hours']): [number, number] | null {
  if (Array.isArray(hours)) return hours.length === 2 && hours.every(Number.isFinite) ? [hours[0], hours[1]] : null
  const match = /^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$/.exec(hours ?? '')
  return match ? [Number(match[1]), Number(match[2])] : null
}

function queryScale(segment: Record<string, string>, dayType: string, hours: [number, number] | null): Scale {
  let factor = 1
  if (segment.age_band) factor *= AGE_SHARE
  if (segment.gender) factor *= GENDER_SHARE[segment.gender] ?? 0.5
  if (segment.tier) factor *= segment.tier.split('|').reduce((sum, tier) => sum + (TIER_SHARE[tier] ?? 0.1), 0)
  factor *= DAY_SHARE[dayType] ?? 1
  if (hours) factor *= Math.min(1, Math.max(0.05, (hours[1] - hours[0]) / STORE_HOURS))
  return { customerDays: Math.max(1, Math.round(BASE_CUSTOMER_DAYS * factor)), factor }
}

function trafficFloors(world: MockWorld, rev: string, floors: MockFloor[], scale: Scale): Record<string, FlowFloor> {
  const result: Record<string, FlowFloor> = {}
  const stretches = corridorStretches()
  for (const floor of floors) {
    const counts = trafficFor(world, rev, floor)
    const maxRaw = Math.max(...counts, EPS)
    const peak = scale.customerDays * (PEAK_SHARE[floor.code] ?? 0.2)
    const segments: FlowSegment[] = []
    stretches.forEach((stretch, i) => {
      const value = Math.round((counts[i] / maxRaw) * peak)
      if (value <= 0) return
      segments.push({ points: [stretch[0], stretch[1]], weight: 0, value, color: null, label: stretchLabel(floor, stretch) })
    })
    const maxValue = Math.max(...segments.map((s) => s.value ?? 0), 0)
    for (const segment of segments) segment.weight = maxValue ? round2((segment.value ?? 0) / maxValue) : 0
    result[floor.code] = { segments, max_value: maxValue, total: segments.reduce((sum, s) => sum + (s.value ?? 0), 0) }
  }
  return result
}

const isPopupUnit = (unit: MockUnit) => !unit.brandId || /-P\d+/.test(unit.code)

interface PopupCandidate {
  floor: string
  x: number
  y: number
  kind: 'spot' | 'unit'
  unit_code: string | null
  score: number
  place: string
}

function popupMarkers(world: MockWorld, floors: MockFloor[], data: Record<string, FlowFloor>): FlowMarker[] {
  const candidates: PopupCandidate[] = []
  for (const floor of floors) {
    const segments = [...(data[floor.code]?.segments ?? [])].filter((s) => dist(s.points[0], s.points[1]) >= 4).sort((a, b) => (b.value ?? 0) - (a.value ?? 0))
    const picked: Pt[] = []
    for (const segment of segments) {
      const [a, b] = segment.points
      const mid: Pt = [round2((a[0] + b[0]) / 2), round2((a[1] + b[1]) / 2)]
      if (picked.some((p) => dist(p, mid) < 14)) continue
      picked.push(mid)
      candidates.push({ floor: floor.code, x: mid[0], y: mid[1], kind: 'spot', unit_code: null, score: segment.value ?? 0, place: segment.label ?? '통로' })
      if (picked.length === 2) break
    }
    for (const unit of floor.units.filter(isPopupUnit)) {
      const [x0, y0, x1, y1] = unit.rect
      const center: Pt = [round2((x0 + x1) / 2), round2((y0 + y1) / 2)]
      const nearest = [...(data[floor.code]?.segments ?? [])].sort((s, t) => dist(midpoint(s), center) - dist(midpoint(t), center))[0]
      const brand = unit.brandId ? world.brands.get(unit.brandId)?.name : null
      candidates.push({
        floor: floor.code,
        x: center[0],
        y: center[1],
        kind: 'unit',
        unit_code: unit.code,
        score: (nearest?.value ?? 0) * 0.95,
        place: unit.brandId ? `${unit.code} 팝업 매장(지금 ${brand ?? unit.brandId})` : `${unit.code} 공실`,
      })
    }
  }
  return candidates
    .sort((a, b) => b.score - a.score)
    .slice(0, 5)
    .map((c, i): FlowMarker => ({
      floor: c.floor,
      x: c.x,
      y: c.y,
      rank: i + 1,
      kind: c.kind,
      label: String(i + 1),
      title: `후보 ${i + 1} · ${c.floor} ${c.place}`,
      unit_code: c.unit_code,
    }))
}

const midpoint = (segment: FlowSegment): Pt => [(segment.points[0][0] + segment.points[1][0]) / 2, (segment.points[0][1] + segment.points[1][1]) / 2]

// --- egress -------------------------------------------------------------------------------------

/** Stair doors on the corridor side (ST-1 opens to the south corridor, ST-2 to the north one). */
const STAIR_DOORS: readonly { id: string; at: Pt }[] = [
  { id: 'ST-1', at: [15, 12] },
  { id: 'ST-2', at: [105, 68] },
]

function exitsOf(floor: MockFloor): { id: string; at: Pt }[] {
  const exits = [...STAIR_DOORS]
  for (const opening of OPENINGS[floor.code] ?? []) {
    const [a, b] = [opening.path[0], opening.path.at(-1)!]
    exits.push({ id: opening.id, at: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] })
  }
  return exits
}

function egressFloors(floors: MockFloor[], minWidth: number): Record<string, FlowFloor> {
  const result: Record<string, FlowFloor> = {}
  for (const floor of floors) {
    const segments: FlowSegment[] = []
    for (const walkway of floor.walkways) {
      const xs = walkway.ring.map((p) => p[0])
      const ys = walkway.ring.map((p) => p[1])
      // Rectangular walkways only: the width is the short side.
      if (walkway.ring.length !== 5) continue
      const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
      const width = Math.min(x1 - x0, y1 - y0)
      if (width >= minWidth - EPS) continue
      const points: Pt[] = x1 - x0 >= y1 - y0 ? [[x0, round2((y0 + y1) / 2)], [x1, round2((y0 + y1) / 2)]] : [[round2((x0 + x1) / 2), y0], [round2((x0 + x1) / 2), y1]]
      segments.push({
        points,
        weight: round2(Math.min(1, 0.4 + (minWidth - width) / minWidth)),
        value: round2(width),
        color: WARNING_COLOR,
        label: `${walkway.name_ko} · 폭 ${round2(width)}m`,
      })
    }
    result[floor.code] = { segments, max_value: null, total: segments.length }
  }
  return result
}

function egressMarkers(world: MockWorld, floors: MockFloor[], maxTravel: number): FlowMarker[] {
  const warnings: { marker: FlowMarker; distance: number }[] = []
  for (const floor of floors) {
    const exits = exitsOf(floor)
    for (const unit of floor.units) {
      const at = unitEntrance(unit)
      const nearest = Math.min(...exits.map((exit) => pathLength(routeBetween(at, exit.at))))
      if (nearest <= maxTravel) continue
      const brand = unit.brandId ? world.brands.get(unit.brandId)?.name : null
      warnings.push({
        distance: nearest,
        marker: {
          floor: floor.code,
          x: round2(at[0]),
          y: round2(at[1]),
          rank: null,
          kind: 'warning',
          label: null,
          unit_code: unit.code,
          title: `${unit.code} ${brand ?? '(공실)'} 앞 · 계단·출입구까지 ${Math.round(nearest)}m`,
        },
      })
    }
  }
  return warnings
    .sort((a, b) => b.distance - a.distance)
    .slice(0, 8)
    .map(({ marker }, i) => ({ ...marker, rank: i + 1, label: String(i + 1) }))
}

// --- the flow payload ---------------------------------------------------------------------------

const DAY_LABELS: Record<string, string> = { weekend: '주말', weekday: '평일' }

function conditionText(dayType: string, hours: [number, number] | null): string {
  const parts = [DAY_LABELS[dayType] ?? '', hours ? `${hours[0]}~${hours[1]}시` : ''].filter(Boolean)
  return parts.length ? `(${parts.join(' ')})` : ''
}

const meters = (value: number) => `${round2(value)}m`

/** The flow payload of GET /api/spatial/flow (and the `flow` UI state); ApiError for bad arguments. */
export function mockFlow(world: MockWorld, params: FlowParams): Flow {
  const mode = params.mode
  if (!['traffic', 'popup', 'egress'].includes(mode)) throw new ApiError(400, `mode는 traffic, popup, egress 중 하나여야 합니다: ${String(mode)}`)
  const rev = params.rev || FLOW_REV
  const revision = world.revisions.get(rev)
  if (!revision) throw new ApiError(404, `알 수 없는 개정: ${rev}`)
  const dayType = params.day_type ?? 'all'
  if (!['all', 'weekday', 'weekend'].includes(dayType)) throw new ApiError(400, `day_type은 all, weekday, weekend 중 하나여야 합니다: ${dayType}`)
  const hours = parseHours(params.hours)
  if (params.hours && (!hours || hours[0] >= hours[1] || hours[0] < 0 || hours[1] > 24)) throw new ApiError(400, `hours는 10-14처럼 시작-끝 시각이어야 합니다: ${String(params.hours)}`)
  const codes = MOCK_FLOORS.map((f) => f.code as string).filter((code) => !params.floor || code === params.floor)
  const floors = codes.map((code) => revision.floors[code]).filter((f): f is MockFloor => !!f)
  if (!floors.length) throw new ApiError(400, `모의 데이터에 없는 층입니다: ${params.floor}`)
  const segment = segmentObject(params.segment ?? null)
  const segmentText = segmentLabelKo(params.segment ?? null)
  const query: Record<string, unknown> = {
    segment: Object.keys(segment).length ? segment : null,
    day_type: dayType,
    hours,
    floor: params.floor ?? null,
  }

  if (mode === 'egress') {
    const minWidth = params.min_width_m ?? DEFAULT_MIN_WIDTH_M
    const maxTravel = params.max_travel_m ?? DEFAULT_MAX_TRAVEL_M
    if (!(minWidth > 0) || !(maxTravel > 0)) throw new ApiError(400, 'min_width_m와 max_travel_m는 0보다 커야 합니다.')
    const data = egressFloors(floors, minWidth)
    const markers = egressMarkers(world, floors, maxTravel)
    const narrow = Object.values(data).reduce((sum, floor) => sum + floor.segments.length, 0)
    return {
      mode,
      title: `통로 폭·피난 거리 점검(기준 폭 ${meters(minWidth)}, 거리 ${meters(maxTravel)})`,
      rev,
      query: { ...query, min_width_m: minWidth, max_travel_m: maxTravel },
      floors: data,
      markers,
      legend: { low: null, high: null, unit: 'm', note: `기준 폭 ${meters(minWidth)}보다 좁은 통로 · 계단·출입구까지 ${meters(maxTravel)}가 넘는 지점` },
      summary: {
        customer_days: null,
        trips: null,
        floors: floors.filter((f) => data[f.code].segments.length || markers.some((m) => m.floor === f.code)).map((f) => f.code),
        narrow_segments: narrow,
        warnings: markers.length,
        min_width_m: minWidth,
        max_travel_m: maxTravel,
      },
      caveat: CAVEATS.egress,
    }
  }

  const scale = queryScale(segment, dayType, hours)
  const data = trafficFloors(world, rev, floors, scale)
  const markers = mode === 'popup' ? popupMarkers(world, floors, data) : []
  const who = `${segmentText || '전체'} 고객`
  return {
    mode,
    title: mode === 'popup' ? `${who} 기준 팝업 후보${conditionText(dayType, hours)}` : `${who}의 추정 유동${conditionText(dayType, hours)}`,
    rev,
    query: params.category ? { ...query, category: params.category } : query,
    floors: data,
    markers,
    legend: { ...LEGEND },
    summary: {
      customer_days: scale.customerDays,
      trips: Math.round(scale.customerDays * TRIPS_PER_CUSTOMER_DAY),
      floors: floors.map((f) => f.code),
    },
    caveat: CAVEATS[mode],
  }
}

/** Segment query text of a flow request (for tool arguments in the mock chat). */
export function flowSegmentParam(params: FlowParams): string {
  return segmentParam(params.segment ?? null)
}
