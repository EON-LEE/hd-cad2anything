// Mock GET /api/analytics/journey: scripted customer-days on the synthetic floors. Stops sit at store
// entrances and the path between stores is the shortest route along corridor centerlines (escalators
// connect floors), like the backend's walkway-network estimate.
import { segmentParam } from '../lib/journey'
import type { Journey, JourneyParams, JourneySegment, JourneyStop, JourneyTransition, SegmentFilter } from '../types/api'
import { categoryName } from './catalog'
import type { MockUnit, MockWorld } from './world'

type Pt = [number, number]

export const JOURNEY_CAVEAT =
  '결제 기록의 순서입니다. 실제로 걸어간 경로는 알 수 없고, 매장 사이 경로는 통로망 최단 경로로 추정했습니다. 고객 ID는 가명입니다. (모의 데이터)'

/** Revision whose transactions the mock journeys come from. */
export const JOURNEY_REV = 'R2'

// Corridor centerlines of the mock floor template (see world.ts BASE_WALKWAYS); the atrium center is
// left out because the R1 popup store stands in it.
const CORRIDORS: readonly [Pt, Pt][] = [
  [[15, 15], [105, 15]],
  [[15, 65], [105, 65]],
  [[15, 15], [15, 65]],
  [[105, 15], [105, 65]],
  [[15, 40], [45, 40]],
  [[75, 40], [105, 40]],
  [[45, 15], [45, 65]],
  [[75, 15], [75, 65]],
  [[45, 31.5], [75, 31.5]],
  [[45, 48.5], [75, 48.5]],
  [[49.5, 31.5], [49.5, 48.5]],
  [[70.5, 31.5], [70.5, 48.5]],
  // Escalator landings: ESC-A is boarded from the west, ESC-B from the east.
  [[49.5, 40], [51, 40]],
  [[69, 40], [70.5, 40]],
]

export const ESCALATORS: readonly { id: string; at: Pt }[] = [
  { id: 'ESC-A', at: [51, 40] },
  { id: 'ESC-B', at: [69, 40] },
]

const EPS = 1e-6
const key = ([x, y]: Pt) => `${Math.round(x * 1000) / 1000},${Math.round(y * 1000) / 1000}`
const dist = (a: Pt, b: Pt) => Math.hypot(a[0] - b[0], a[1] - b[1])

function project(p: Pt, [a, b]: readonly [Pt, Pt]): Pt {
  const dx = b[0] - a[0]
  const dy = b[1] - a[1]
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
  return [a[0] + t * dx, a[1] + t * dy]
}

function onSegment(p: Pt, seg: readonly [Pt, Pt]): boolean {
  return dist(project(p, seg), p) < EPS
}

/** Closest point on any corridor centerline. */
export function corridorAccess(p: Pt): Pt {
  let best: Pt = p
  let bestDistance = Infinity
  for (const seg of CORRIDORS) {
    const q = project(p, seg)
    const d = dist(p, q)
    if (d < bestDistance - EPS) {
      best = q
      bestDistance = d
    }
  }
  return best
}

function intersection([a, b]: readonly [Pt, Pt], [c, d]: readonly [Pt, Pt]): Pt | null {
  // Corridors are axis-aligned: intersect a horizontal with a vertical segment.
  const horizontal = (s: readonly [Pt, Pt]) => Math.abs(s[0][1] - s[1][1]) < EPS
  const [h, v] = horizontal([a, b]) ? [[a, b] as const, [c, d] as const] : [[c, d] as const, [a, b] as const]
  if (!horizontal(h) || horizontal(v)) return null
  const x = v[0][0]
  const y = h[0][1]
  const inX = x >= Math.min(h[0][0], h[1][0]) - EPS && x <= Math.max(h[0][0], h[1][0]) + EPS
  const inY = y >= Math.min(v[0][1], v[1][1]) - EPS && y <= Math.max(v[0][1], v[1][1]) + EPS
  return inX && inY ? [x, y] : null
}

/** Shortest path between two points on the corridor network (both must lie on a corridor). */
function corridorPath(from: Pt, to: Pt): Pt[] {
  const nodes = new Map<string, Pt>()
  const addNode = (p: Pt) => nodes.set(key(p), p)
  for (const seg of CORRIDORS) {
    addNode(seg[0])
    addNode(seg[1])
  }
  for (let i = 0; i < CORRIDORS.length; i += 1) {
    for (let j = i + 1; j < CORRIDORS.length; j += 1) {
      const p = intersection(CORRIDORS[i], CORRIDORS[j])
      if (p) addNode(p)
    }
  }
  addNode(from)
  addNode(to)
  const edges = new Map<string, { to: string; w: number }[]>()
  const link = (a: Pt, b: Pt) => {
    const w = dist(a, b)
    if (w < EPS) return
    for (const [x, y] of [
      [a, b],
      [b, a],
    ] as const) {
      const list = edges.get(key(x)) ?? []
      list.push({ to: key(y), w })
      edges.set(key(x), list)
    }
  }
  for (const seg of CORRIDORS) {
    const along = [...nodes.values()].filter((p) => onSegment(p, seg)).sort((p, q) => dist(seg[0], p) - dist(seg[0], q))
    for (let i = 1; i < along.length; i += 1) link(along[i - 1], along[i])
  }
  // Dijkstra (a few dozen nodes).
  const distance = new Map<string, number>([[key(from), 0]])
  const previous = new Map<string, string>()
  const open = new Set<string>([key(from)])
  while (open.size) {
    let current = ''
    let currentDistance = Infinity
    for (const k of open) {
      const d = distance.get(k) ?? Infinity
      if (d < currentDistance) {
        current = k
        currentDistance = d
      }
    }
    open.delete(current)
    if (current === key(to)) break
    for (const edge of edges.get(current) ?? []) {
      const d = currentDistance + edge.w
      if (d < (distance.get(edge.to) ?? Infinity) - EPS) {
        distance.set(edge.to, d)
        previous.set(edge.to, current)
        open.add(edge.to)
      }
    }
  }
  if (!distance.has(key(to))) return [from, to]
  const path: Pt[] = [to]
  let cursor = key(to)
  while (cursor !== key(from)) {
    cursor = previous.get(cursor)!
    path.unshift(nodes.get(cursor)!)
  }
  return path
}

/** Drop repeated and collinear points; coordinates are rounded to centimeters like the backend. */
function simplify(points: Pt[]): Pt[] {
  const rounded = points.map(([x, y]): Pt => [Math.round(x * 100) / 100, Math.round(y * 100) / 100])
  const unique = rounded.filter((p, i) => i === 0 || dist(p, rounded[i - 1]) > EPS)
  return unique.filter((p, i) => {
    if (i === 0 || i === unique.length - 1) return true
    const [a, b] = [unique[i - 1], unique[i + 1]]
    return Math.abs((p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])) > EPS
  })
}

/** Walking line from point a to point b through the corridors (a and b may be store entrances). */
export function routeBetween(a: Pt, b: Pt): Pt[] {
  const ra = corridorAccess(a)
  const rb = corridorAccess(b)
  return simplify([a, ...corridorPath(ra, rb), b])
}

export function pathLength(points: readonly Pt[]): number {
  let total = 0
  for (let i = 1; i < points.length; i += 1) total += dist(points[i - 1], points[i])
  return total
}

/** Store entrance: middle of the side that faces the corridor. */
export function unitEntrance(unit: Pick<MockUnit, 'rect' | 'facing'>): Pt {
  const [x0, y0, x1, y1] = unit.rect
  const cx = (x0 + x1) / 2
  const cy = (y0 + y1) / 2
  switch (unit.facing) {
    case 'N':
      return [cx, y1]
    case 'S':
      return [cx, y0]
    case 'E':
      return [x1, cy]
    default:
      return [x0, cy]
  }
}

// --- scripted customer-days ----------------------------------------------------------------------

interface ScriptedJourney {
  customerId: string
  customerSegment: string
  date: string
  stops: readonly [time: string, unitCode: string, amountKrw: number][]
}

const SCRIPTED: readonly ScriptedJourney[] = [
  {
    customerId: 'C004211',
    customerSegment: '40대 여성 VIP',
    date: '2025-11-08',
    stops: [
      ['11:32', '1F-C03', 186_000],
      ['11:58', '1F-N03', 92_000],
      ['12:41', '3F-C05', 14_500],
      ['13:20', '3F-S03', 428_000],
      ['14:05', '3F-E02', 265_000],
      ['14:37', '1F-E01', 1_350_000],
    ],
  },
  {
    customerId: 'C010877',
    customerSegment: '40대 여성 VIP',
    date: '2025-11-15',
    stops: [
      ['10:48', '3F-C01', 312_000],
      ['11:20', '3F-N04', 540_000],
      ['11:52', '3F-W02', 12_000],
      ['12:35', '1F-S02', 2_100_000],
      ['13:02', '1F-C05', 480_000],
    ],
  },
  {
    customerId: 'C002390',
    customerSegment: '40대 여성 VVIP',
    date: '2025-11-22',
    stops: [
      ['15:05', '1F-W01', 68_000],
      ['15:31', '1F-C01', 54_000],
      ['16:10', '3F-C06', 289_000],
      ['16:48', '3F-N03', 176_000],
    ],
  },
]

const WEEKDAYS = '일월화수목금토'
const toMinutes = (time: string) => Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5))

const AGE_LABELS: Record<string, string> = { '20s': '20대', '30s': '30대', '40s': '40대', '50s': '50대', '60s+': '60대 이상' }
const GENDER_LABELS: Record<string, string> = { F: '여성', M: '남성' }

/** Parse "age_band:40s;tier:VIP|VVIP" (or an object) into a plain filter object. */
export function segmentObject(segment: SegmentFilter | null | undefined): Record<string, string> {
  const text = segmentParam(segment)
  const result: Record<string, string> = {}
  for (const part of text.split(';')) {
    const [k, v] = part.split(':')
    if (k && v) result[k.trim()] = v.trim()
  }
  return result
}

/** Korean description of a segment filter, e.g. "40대 여성 VIP·VVIP" ("" when empty). */
export function segmentLabelKo(segment: SegmentFilter | null | undefined): string {
  const s = segmentObject(segment)
  return [AGE_LABELS[s.age_band] ?? '', GENDER_LABELS[s.gender] ?? '', (s.tier ?? '').split('|').filter(Boolean).join('·')]
    .filter(Boolean)
    .join(' ')
}

function findUnit(world: MockWorld, code: string): MockUnit | undefined {
  return world.revisions.get(JOURNEY_REV)?.floors[code.split('-')[0]]?.units.find((u) => u.code === code)
}

function buildJourney(world: MockWorld, script: ScriptedJourney, segment: SegmentFilter | null, rank: number): Journey {
  const stops: JourneyStop[] = []
  for (const [time, code, amount] of script.stops) {
    const unit = findUnit(world, code)
    if (!unit) continue
    const brand = unit.brandId ? world.brands.get(unit.brandId) : undefined
    const [x, y] = unitEntrance(unit)
    const previous = stops.at(-1)
    stops.push({
      order: stops.length + 1,
      time,
      unit_code: code,
      floor: code.split('-')[0],
      brand_id: brand?.id ?? null,
      brand_name: brand?.name ?? null,
      category: unit.category,
      category_name: categoryName(unit.category),
      amount_krw: amount,
      receipts: 1,
      gap_min: previous ? toMinutes(time) - toMinutes(previous.time) : null,
      x,
      y,
    })
  }
  const segments: JourneySegment[] = []
  const transitions: JourneyTransition[] = []
  let walk = 0
  for (let i = 1; i < stops.length; i += 1) {
    const a = stops[i - 1]
    const b = stops[i]
    const pa: Pt = [a.x!, a.y!]
    const pb: Pt = [b.x!, b.y!]
    const orders = { from_order: a.order, to_order: b.order }
    if (a.floor === b.floor) {
      const points = routeBetween(pa, pb)
      walk += pathLength(points)
      segments.push({ floor: a.floor, points, ...orders })
      continue
    }
    const best = ESCALATORS.map((esc) => {
      const down = routeBetween(pa, esc.at)
      const up = routeBetween(esc.at, pb)
      return { esc, down, up, length: pathLength(down) + pathLength(up) }
    }).sort((x, y) => x.length - y.length)[0]
    walk += best.length
    segments.push({ floor: a.floor, points: best.down, ...orders }, { floor: b.floor, points: best.up, ...orders })
    transitions.push({ ...orders, from_floor: a.floor, to_floor: b.floor, via: best.esc.id })
  }
  const floors = [...new Set(stops.map((s) => s.floor))]
  const first = stops[0]
  const last = stops.at(-1)!
  return {
    title: `${script.customerSegment} 고객의 쇼핑 동선`,
    customer: { id: script.customerId, segment: script.customerSegment },
    date: script.date,
    weekday: WEEKDAYS[new Date(`${script.date}T00:00:00Z`).getUTCDay()],
    rev: JOURNEY_REV,
    query: { segment, rank },
    stops,
    segments,
    transitions,
    summary: {
      stops: stops.length,
      floors,
      total_krw: stops.reduce((acc, s) => acc + (s.amount_krw ?? 0), 0),
      duration_min: toMinutes(last.time) - toMinutes(first.time),
      walk_m: Math.round(walk / 10) * 10,
    },
    caveat: JOURNEY_CAVEAT,
  }
}

/** The `rank`-th journey (1-based) or the journey of `customer_id`; null when there is none (404). */
export function mockJourney(world: MockWorld, params: JourneyParams = {}): Journey | null {
  const segment = params.segment ?? null
  if (params.customer_id) {
    const index = SCRIPTED.findIndex((s) => s.customerId === params.customer_id && (!params.date || s.date === params.date))
    return index < 0 ? null : buildJourney(world, SCRIPTED[index], segment, index + 1)
  }
  const rank = Math.max(1, Math.round(params.rank ?? 1))
  const script = SCRIPTED[rank - 1]
  return script ? buildJourney(world, script, segment, rank) : null
}

export const MOCK_JOURNEY_COUNT = SCRIPTED.length
