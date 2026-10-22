// Mock GET /api/spatial/route: the shortest walk between two places of the synthetic floors along the
// corridor centerlines (the same network as the mock journeys). Floors change by escalator (default)
// or elevator; the walk is measured on the origin and destination floors only, like the backend.
import { floorLabel } from '../lib/format'
import { floorOrdinal } from '../lib/journey'
import type { Route, RoutePlace, RouteSegment, RouteTransition } from '../types/api'
import { BUILDING, MOCK_FLOORS } from './catalog'
import { ESCALATORS, pathLength, routeBetween, unitEntrance } from './journey'
import { OPENINGS, VERTICAL_LINKS, type MockWorld } from './world'

type Pt = [number, number]

/** Floor-to-floor height of the mock building, in meters. */
export const MOCK_FLOOR_HEIGHT_M = 5

const CAVEAT = '통로망 최단 경로로 계산한 걷는 거리예요.'
const CAVEAT_ESCALATOR = '에스컬레이터를 타는 구간과 층마다 갈아타는 거리는 넣지 않았어요.'
const CAVEAT_ELEVATOR = '엘리베이터를 타는 구간은 넣지 않았어요.'

/** Elevator doors on their lobby side (escalators use the journey landings). */
const ELEVATORS: readonly { id: string; at: Pt }[] = [
  { id: 'EV-1', at: [6, 12] },
  { id: 'EV-2', at: [114, 68] },
]

export interface MockRouteParams {
  rev: string
  /** Unit code ("3F-C05"), entrance id ("ENT-1") or floor-qualified vertical link ("1F:ESC-A"). */
  origin: string
  destination: string
  via?: 'escalator' | 'elevator'
}

const round2 = (v: number) => Math.round(v * 100) / 100

function entrancePlace(id: string): RoutePlace | null {
  for (const [floor, openings] of Object.entries(OPENINGS)) {
    const opening = openings.find((o) => o.id === id)
    if (!opening) continue
    // Middle of the doorway, 0.4 m inside the building.
    const [a, b] = [opening.path[0], opening.path.at(-1)!]
    const mid: Pt = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
    const dx = BUILDING.width / 2 - mid[0]
    const dy = BUILDING.depth / 2 - mid[1]
    const length = Math.hypot(dx, dy) || 1
    return {
      ref: id,
      kind: 'entrance',
      label: `${id} ${opening.name_ko}`,
      floor,
      unit_code: null,
      x: round2(mid[0] + (dx / length) * 0.4),
      y: round2(mid[1] + (dy / length) * 0.4),
    }
  }
  return null
}

function linkPlace(world: MockWorld, rev: string, floor: string, id: string): RoutePlace | null {
  const link = VERTICAL_LINKS.find((l) => l.id === id)
  const landing = [...ESCALATORS, ...ELEVATORS].find((l) => l.id === id)
  if (!link || !landing || !world.revisions.get(rev)?.floors[floor]) return null
  return {
    ref: `${floor}:${id}`,
    kind: link.type === 'stair' ? 'stairs' : link.type,
    label: `${id} ${link.name_ko}`,
    floor,
    unit_code: null,
    x: landing.at[0],
    y: landing.at[1],
  }
}

function unitPlace(world: MockWorld, rev: string, code: string): RoutePlace | null {
  const floor = code.split('-')[0]
  const unit = world.revisions.get(rev)?.floors[floor]?.units.find((u) => u.code === code)
  if (!unit) return null
  const brand = unit.brandId ? world.brands.get(unit.brandId) : undefined
  const [x, y] = unitEntrance(unit)
  return { ref: code, kind: 'unit', label: brand ? `${brand.name}(${code})` : `${code} (공실)`, floor, unit_code: code, x, y }
}

/** A place of the mock world from its reference; null when unknown (the API answers 404). */
export function mockPlace(world: MockWorld, rev: string, ref: string): RoutePlace | null {
  const id = ref.trim().toUpperCase()
  if (/^ENT-\d+$/.test(id)) return entrancePlace(id)
  const link = /^(B\d+|\d+F):(.+)$/.exec(id)
  if (link) return linkPlace(world, rev, link[1], link[2])
  return unitPlace(world, rev, id)
}

const ordinal = (floor: string) => floorOrdinal(floor, MOCK_FLOORS) ?? 0
/** Walking distances are reported in 5 m steps. */
export const roundWalk = (meters: number) => Math.round(meters / 5) * 5

/** The route payload of GET /api/spatial/route (and the `route` UI state); null when a place is unknown. */
export function mockRoute(world: MockWorld, params: MockRouteParams): Route | null {
  const origin = mockPlace(world, params.rev, params.origin)
  const destination = mockPlace(world, params.rev, params.destination)
  if (!origin || !destination) return null
  const from: Pt = [origin.x!, origin.y!]
  const to: Pt = [destination.x!, destination.y!]
  const viaType = params.via ?? 'escalator'
  const segments: RouteSegment[] = []
  const transitions: RouteTransition[] = []
  let walk: number
  if (origin.floor === destination.floor) {
    const points = routeBetween(from, to)
    walk = pathLength(points)
    segments.push({ floor: origin.floor, points })
  } else {
    // The vertical link with the shortest total walk; ties keep the list order (ESC-A before ESC-B).
    const candidates = (viaType === 'elevator' ? ELEVATORS : ESCALATORS).map((link) => {
      const leave = routeBetween(from, link.at)
      const arrive = routeBetween(link.at, to)
      return { link, leave, arrive, length: pathLength(leave) + pathLength(arrive) }
    })
    const best = candidates.reduce((a, b) => (b.length < a.length - 1e-6 ? b : a))
    walk = best.length
    segments.push({ floor: origin.floor, points: best.leave }, { floor: destination.floor, points: best.arrive })
    const floors = Math.abs(ordinal(destination.floor) - ordinal(origin.floor))
    transitions.push({
      from_floor: origin.floor,
      to_floor: destination.floor,
      via: best.link.id,
      floors,
      height_m: floors * MOCK_FLOOR_HEIGHT_M,
    })
  }
  const caveat = [CAVEAT, transitions.length ? (viaType === 'elevator' ? CAVEAT_ELEVATOR : CAVEAT_ESCALATOR) : '', '(모의 데이터)']
    .filter(Boolean)
    .join(' ')
  return {
    title: `${origin.label} → ${destination.label}`,
    rev: params.rev,
    via_type: viaType,
    origin,
    destination,
    segments,
    transitions,
    walk_m: roundWalk(walk),
    caveat,
  }
}

/** "정문(ENT-1)", "Cafe Halune(3F-C05)", "ESC-A 에스컬레이터(1층)" for answers. */
export function placePhrase(place: RoutePlace): string {
  if (place.kind === 'entrance') {
    const [id, ...name] = place.label.split(' ')
    return name.length ? `${name.join(' ')}(${id})` : place.label
  }
  if (place.kind === 'unit') return place.label
  return `${place.label}(${floorLabel(place.floor)})`
}
