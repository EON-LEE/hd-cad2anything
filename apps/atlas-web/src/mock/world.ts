// Synthetic two-floor revision world for mock mode (R0 -> R1 -> R2 -> H1). All brands are fictional.
import type { ChangeItem, RevisionInfo } from '../types/api'
import { categoryName, FIXTURE_DIMS, MOCK_CATEGORIES } from './catalog'

export type Rect = [number, number, number, number]
export type Facing = 'N' | 'S' | 'E' | 'W'
export type Ring = [number, number][]

export interface MockBrand {
  id: string
  name: string
  category: string
  attractiveness: number
}

export interface MockUnit {
  code: string
  rect: Rect
  facing: Facing
  band: string
  category: string
  brandId: string | null
  extraFixtures: string[]
}

export interface MockAmenity {
  type: string
  rect: Rect
}

export interface MockWalkway {
  key: string
  name_ko: string
  walkway_type: string
  ring: Ring
}

export interface MockFloor {
  code: string
  units: MockUnit[]
  amenities: MockAmenity[]
  walkways: MockWalkway[]
}

export interface MockRevision {
  info: RevisionInfo
  notes: string
  floors: Record<string, MockFloor>
  /** Change items from the parent revision. */
  changes: ChangeItem[]
}

export interface MockWorld {
  brands: Map<string, MockBrand>
  revisions: Map<string, MockRevision>
  order: string[]
}

export interface MockFixture {
  id: string
  type: string
  rect: Rect
}

// --- static template (same footprint as cad2anything.mockgen.layout) ------------------------------

export const rectRing = ([x0, y0, x1, y1]: Rect): Ring => [
  [x0, y0],
  [x1, y0],
  [x1, y1],
  [x0, y1],
  [x0, y0],
]

export const VERTICAL_LINKS: readonly { id: string; type: string; name_ko: string; rect: Rect }[] = [
  { id: 'ESC-A', type: 'escalator', name_ko: '에스컬레이터', rect: [51, 33, 55, 47] },
  { id: 'ESC-B', type: 'escalator', name_ko: '에스컬레이터', rect: [65, 33, 69, 47] },
  { id: 'EV-1', type: 'elevator', name_ko: '엘리베이터', rect: [0, 6, 6, 18] },
  { id: 'EV-2', type: 'elevator', name_ko: '엘리베이터', rect: [114, 62, 120, 74] },
  { id: 'ST-1', type: 'stair', name_ko: '계단', rect: [12, 0, 18, 12] },
  { id: 'ST-2', type: 'stair', name_ko: '계단', rect: [102, 68, 108, 80] },
]

export const ESCALATOR_RECTS: readonly Rect[] = VERTICAL_LINKS.filter((l) => l.type === 'escalator').map((l) => l.rect)

const walk = (key: string, walkway_type: string, name_ko: string, rect: Rect): MockWalkway => ({
  key,
  walkway_type,
  name_ko,
  ring: rectRing(rect),
})

const BASE_WALKWAYS: readonly MockWalkway[] = [
  walk('S', 'main', '남측 주통로', [12, 12, 108, 18]),
  walk('N', 'main', '북측 주통로', [12, 62, 108, 68]),
  walk('W', 'main', '서측 주통로', [12, 18, 18, 62]),
  walk('E', 'main', '동측 주통로', [102, 18, 108, 62]),
  walk('CW', 'central', '중앙 통로(서)', [18, 37, 42, 43]),
  walk('CE', 'central', '중앙 통로(동)', [78, 37, 102, 43]),
  walk('XW', 'cross', '서측 횡단 통로', [42, 18, 48, 62]),
  walk('XE', 'cross', '동측 횡단 통로', [72, 18, 78, 62]),
  walk('AS', 'atrium', '아트리움 남측', [48, 30, 72, 33]),
  walk('AN', 'atrium', '아트리움 북측', [48, 47, 72, 50]),
  walk('AW', 'atrium', '아트리움 서측', [48, 33, 51, 47]),
  walk('AM', 'atrium', '아트리움 중앙', [55, 33, 65, 47]),
  walk('AE', 'atrium', '아트리움 동측', [69, 33, 72, 47]),
  walk('LSW', 'lobby', '남서 코어 로비', [6, 6, 12, 18]),
  walk('LNW', 'lobby', '북서 코어 로비', [6, 62, 12, 74]),
  walk('LNE', 'lobby', '북동 코어 로비', [108, 62, 114, 74]),
  walk('LSE', 'lobby', '남동 코어 로비', [108, 6, 114, 18]),
]

const FLOOR_WALKWAYS: Record<string, readonly MockWalkway[]> = {
  '1F': [
    {
      key: 'ENT1',
      walkway_type: 'entrance',
      name_ko: '정문 홀',
      ring: [
        [54, 0],
        [66, 0],
        [66, 9],
        [62, 9],
        [62, 12],
        [54, 12],
        [54, 0],
      ],
    },
    walk('ENT2', 'entrance', '서측 출입 통로', [0, 37, 12, 43]),
    walk('ENT3', 'entrance', '동측 출입 통로', [108, 37, 120, 43]),
  ],
}

export const OPENINGS: Record<string, readonly { id: string; name_ko: string; path: Ring }[]> = {
  '1F': [
    { id: 'ENT-1', name_ko: '정문', path: [[56, 0], [64, 0]] },
    { id: 'ENT-2', name_ko: '서측 출입구', path: [[0, 38], [0, 42]] },
    { id: 'ENT-3', name_ko: '동측 출입구', path: [[120, 38], [120, 42]] },
  ],
}

const BASE_AMENITIES: readonly MockAmenity[] = [
  { type: 'RESTROOM', rect: [0, 0, 12, 6] },
  { type: 'RESTROOM', rect: [108, 74, 120, 80] },
]

const FLOOR_AMENITIES: Record<string, readonly MockAmenity[]> = {
  '1F': [
    { type: 'INFO', rect: [62, 9, 66, 12] },
    { type: 'ATM', rect: [12, 68, 18, 72] },
  ],
  '3F': [{ type: 'LOUNGE', rect: [12, 68, 18, 80] }],
}

// --- fictional brands -----------------------------------------------------------------------------

const BRANDS: readonly [string, string, string, number][] = [
  ['BR3001', 'Morvane', 'WOMEN_CONTEMPORARY', 1.08],
  ['BR3002', 'Brelune', 'WOMEN_CONTEMPORARY', 0.95],
  ['BR3003', 'Kaelith Femme', 'WOMEN_CONTEMPORARY', 1.12],
  ['BR3004', 'Nyssara', 'WOMEN_CONTEMPORARY', 0.98],
  ['BR3005', 'Studio Ilvane', 'WOMEN_CONTEMPORARY', 1.02],
  ['BR3006', 'Dovrelle', 'WOMEN_CONTEMPORARY', 1.15],
  ['BR3007', 'Talvessa', 'WOMEN_CONTEMPORARY', 0.82],
  ['BR3008', 'Sevrine Femme', 'WOMEN_CONTEMPORARY', 1.25],
  ['BR3009', 'Elvarre', 'WOMEN_CONTEMPORARY', 0.9],
  ['BR3010', 'Orsenne', 'WOMEN_CONTEMPORARY', 1.18],
  ['BR3011', 'Quorelle', 'WOMEN_CONTEMPORARY', 0.78],
  ['BR3012', 'Studio Velune', 'WOMEN_CONTEMPORARY', 0.88],
  ['BR3013', 'Fiorenne', 'WOMEN_CONTEMPORARY', 1.05],
  ['BR3014', 'Lumivane', 'WOMEN_CONTEMPORARY', 1.0],
  ['BR3101', 'Ilvora Leather', 'ACCESSORIES', 0.86],
  ['BR3102', 'Dravelle Bags', 'ACCESSORIES', 1.04],
  ['BR3103', 'Velmira', 'ACCESSORIES', 1.1],
  ['BR3104', 'Atelier Noa', 'ACCESSORIES', 1.2],
  ['BR3201', 'Cafe Halune', 'CAFE', 1.0],
  ['BR3202', '가온다방', 'CAFE', 0.95],
  ['BR1001', '메종 루미', 'LUXURY', 1.35],
  ['BR1002', 'Maison Velorra', 'LUXURY', 1.18],
  ['BR1003', 'Casa Ventara', 'LUXURY', 1.1],
  ['BR1004', 'Orlavie', 'LUXURY', 0.84],
  ['BR1005', 'Sevanne & Corin', 'LUXURY', 1.22],
  ['BR1006', 'Lumivelle', 'LUXURY', 0.92],
  ['BR1007', 'Casa Brivane', 'LUXURY', 1.12],
  ['BR1101', 'Vaelora Lab', 'COSMETICS', 1.15],
  ['BR1102', 'Nerissa Beauty', 'COSMETICS', 0.98],
  ['BR1103', '솔빛뷰티', 'COSMETICS', 1.05],
  ['BR1104', 'Elarine Skin', 'COSMETICS', 0.93],
  ['BR1105', 'Halcyne Cosmetics', 'COSMETICS', 0.8],
  ['BR1106', 'Ravelle Skin', 'COSMETICS', 1.08],
  ['BR1107', '윤슬뷰티', 'COSMETICS', 1.2],
  ['BR1108', 'Tovira Lab', 'COSMETICS', 0.76],
  ['BR1109', 'Mirelune Skin', 'COSMETICS', 1.0],
  ['BR1110', '다온뷰티', 'COSMETICS', 1.1],
  ['BR1201', 'Isken Horology', 'JEWELRY_WATCH', 1.14],
  ['BR1202', 'Kaelune Jewels', 'JEWELRY_WATCH', 1.06],
  ['BR1203', 'Atelier Corvane', 'JEWELRY_WATCH', 0.97],
]

const unit = (code: string, rect: Rect, facing: Facing, band: string, category: string, brandId: string | null): MockUnit => ({
  code,
  rect,
  facing,
  band,
  category,
  brandId,
  extraFixtures: [],
})

const BASE_UNITS: Record<string, readonly MockUnit[]> = {
  '3F': [
    unit('3F-S01', [18, 0, 39, 12], 'N', 'S', 'WOMEN_CONTEMPORARY', 'BR3001'),
    unit('3F-S02', [39, 0, 60, 12], 'N', 'S', 'WOMEN_CONTEMPORARY', 'BR3002'),
    unit('3F-S03', [60, 0, 81, 12], 'N', 'S', 'WOMEN_CONTEMPORARY', 'BR3003'),
    unit('3F-S04', [81, 0, 102, 12], 'N', 'S', 'WOMEN_CONTEMPORARY', 'BR3004'),
    unit('3F-N01', [18, 68, 39, 80], 'S', 'N', 'WOMEN_CONTEMPORARY', 'BR3005'),
    unit('3F-N02', [39, 68, 60, 80], 'S', 'N', 'WOMEN_CONTEMPORARY', 'BR3006'),
    unit('3F-N03', [60, 68, 81, 80], 'S', 'N', 'WOMEN_CONTEMPORARY', 'BR3007'),
    unit('3F-N04', [81, 68, 102, 80], 'S', 'N', 'WOMEN_CONTEMPORARY', 'BR3008'),
    unit('3F-W01', [0, 18, 12, 40], 'E', 'W', 'WOMEN_CONTEMPORARY', 'BR3009'),
    unit('3F-W02', [0, 40, 12, 62], 'E', 'W', 'WOMEN_CONTEMPORARY', 'BR3010'),
    unit('3F-E01', [108, 18, 120, 40], 'W', 'E', 'WOMEN_CONTEMPORARY', 'BR3011'),
    unit('3F-E02', [108, 40, 120, 62], 'W', 'E', 'ACCESSORIES', 'BR3101'),
    unit('3F-C01', [18, 18, 42, 37], 'N', 'C', 'WOMEN_CONTEMPORARY', 'BR3012'),
    unit('3F-C02', [48, 18, 72, 30], 'N', 'C', 'WOMEN_CONTEMPORARY', null),
    unit('3F-C03', [78, 18, 102, 37], 'N', 'C', 'ACCESSORIES', 'BR3102'),
    unit('3F-C04', [18, 43, 42, 62], 'S', 'C', 'WOMEN_CONTEMPORARY', 'BR3013'),
    unit('3F-C05', [48, 50, 72, 62], 'S', 'C', 'CAFE', 'BR3201'),
    unit('3F-C06', [78, 43, 102, 62], 'S', 'C', 'WOMEN_CONTEMPORARY', 'BR3014'),
  ],
  '1F': [
    unit('1F-S01', [18, 0, 36, 12], 'N', 'S', 'LUXURY', 'BR1005'),
    unit('1F-S02', [36, 0, 54, 12], 'N', 'S', 'LUXURY', 'BR1002'),
    unit('1F-S03', [66, 0, 84, 12], 'N', 'S', 'LUXURY', 'BR1003'),
    unit('1F-S04', [84, 0, 102, 12], 'N', 'S', 'LUXURY', 'BR1004'),
    unit('1F-N01', [18, 68, 39, 80], 'S', 'N', 'LUXURY', 'BR1001'),
    unit('1F-N02', [39, 68, 60, 80], 'S', 'N', 'LUXURY', 'BR1006'),
    unit('1F-N03', [60, 68, 81, 80], 'S', 'N', 'COSMETICS', 'BR1101'),
    unit('1F-N04', [81, 68, 102, 80], 'S', 'N', 'COSMETICS', 'BR1102'),
    unit('1F-W01', [0, 18, 12, 37], 'E', 'W', 'COSMETICS', 'BR1103'),
    unit('1F-W02', [0, 43, 12, 62], 'E', 'W', 'COSMETICS', 'BR1104'),
    unit('1F-E01', [108, 18, 120, 37], 'W', 'E', 'JEWELRY_WATCH', 'BR1201'),
    unit('1F-E02', [108, 43, 120, 62], 'W', 'E', 'COSMETICS', 'BR1105'),
    unit('1F-C01', [18, 18, 42, 37], 'N', 'C', 'COSMETICS', 'BR1106'),
    unit('1F-C02', [48, 18, 72, 30], 'N', 'C', 'JEWELRY_WATCH', 'BR1202'),
    unit('1F-C03', [78, 18, 102, 37], 'N', 'C', 'COSMETICS', 'BR1107'),
    unit('1F-C04', [18, 43, 42, 62], 'S', 'C', 'COSMETICS', 'BR1108'),
    unit('1F-C05', [48, 50, 72, 62], 'S', 'C', 'JEWELRY_WATCH', 'BR1203'),
    unit('1F-C06', [78, 43, 102, 62], 'S', 'C', 'COSMETICS', 'BR1109'),
  ],
}

// --- geometry helpers ---------------------------------------------------------------------------

export function rectArea([x0, y0, x1, y1]: Rect): number {
  return Math.round((x1 - x0) * (y1 - y0) * 100) / 100
}

export function rectCenter([x0, y0, x1, y1]: Rect): [number, number] {
  return [(x0 + x1) / 2, (y0 + y1) / 2]
}

export function distanceToRect([px, py]: [number, number], [x0, y0, x1, y1]: Rect): number {
  const dx = Math.max(x0 - px, 0, px - x1)
  const dy = Math.max(y0 - py, 0, py - y1)
  return Math.hypot(dx, dy)
}

export function escalatorDistance(rect: Rect): number {
  const center = rectCenter(rect)
  return Math.round(Math.min(...ESCALATOR_RECTS.map((r) => distanceToRect(center, r))) * 100) / 100
}

// --- fixtures (deterministic grid; counter first) ---------------------------------------------

function localDims([x0, y0, x1, y1]: Rect, facing: Facing): [number, number] {
  return facing === 'N' || facing === 'S' ? [x1 - x0, y1 - y0] : [y1 - y0, x1 - x0]
}

function toWorld([x0, y0, x1, y1]: Rect, facing: Facing, [u0, v0, u1, v1]: Rect): Rect {
  const r = (v: number) => Math.round(v * 1000) / 1000
  if (facing === 'N') return [r(x0 + u0), r(y1 - v1), r(x0 + u1), r(y1 - v0)]
  if (facing === 'S') return [r(x0 + u0), r(y0 + v0), r(x0 + u1), r(y0 + v1)]
  if (facing === 'E') return [r(x1 - v1), r(y0 + u0), r(x1 - v0), r(y0 + u1)]
  return [r(x0 + v0), r(y0 + u0), r(x0 + v1), r(y0 + u1)]
}

function fixtureGrid(u: MockUnit) {
  const [width, depth] = localDims(u.rect, u.facing)
  const cols = Math.max(1, Math.floor((width - 2) / 3.4))
  const rows = Math.max(1, Math.floor((depth - 2) / 3.0))
  const pitchU = (width - 2) / cols
  const pitchV = (depth - 2) / rows
  const cells: [number, number][] = []
  for (let j = 0; j < rows; j += 1) for (let i = 0; i < cols; i += 1) cells.push([1 + (i + 0.5) * pitchU, 1 + (j + 0.5) * pitchV])
  const base = Math.max(1, Math.min(cells.length - 1, Math.round((width * depth) / 16)))
  return { cells, pitchU, pitchV, base }
}

export function baseFixtureCount(u: MockUnit): number {
  return u.brandId ? fixtureGrid(u).base : 0
}

export function unitFixtures(u: MockUnit): MockFixture[] {
  if (!u.brandId) return []
  const types = MOCK_CATEGORIES.find((c) => c.code === u.category)?.fixtures ?? ['TABLE', 'COUNTER']
  const grid = fixtureGrid(u)
  const result: MockFixture[] = []
  const total = grid.base + u.extraFixtures.length
  const pool = types.filter((t) => t !== 'COUNTER')
  for (let k = 0; k < total && k < grid.cells.length; k += 1) {
    let type: string
    if (k >= grid.base) type = u.extraFixtures[k - grid.base]
    else if (k === 0 && types.includes('COUNTER')) type = 'COUNTER'
    else type = pool.length ? pool[k % pool.length] : types[k % types.length]
    const dims = FIXTURE_DIMS[type] ?? { w: 1.2, d: 0.8 }
    const w = Math.min(dims.w, grid.pitchU - 0.6)
    const d = Math.min(dims.d, grid.pitchV - 0.6)
    const [cu, cv] = grid.cells[k]
    result.push({
      id: `${u.code}-F${String(k + 1).padStart(3, '0')}`,
      type,
      rect: toWorld(u.rect, u.facing, [cu - w / 2, cv - d / 2, cu + w / 2, cv + d / 2]),
    })
  }
  return result
}

// --- revision editor (records ground-truth change items with Korean summaries) -----------------

function cloneFloors(floors: Record<string, MockFloor>): Record<string, MockFloor> {
  return Object.fromEntries(
    Object.entries(floors).map(([code, f]) => [
      code,
      {
        code,
        units: f.units.map((u) => ({ ...u, rect: [...u.rect] as Rect, extraFixtures: [...u.extraFixtures] })),
        amenities: f.amenities.map((a) => ({ ...a, rect: [...a.rect] as Rect })),
        walkways: f.walkways.map((w) => ({ ...w, ring: w.ring.map((p) => [...p] as [number, number]) })),
      },
    ]),
  )
}

class RevisionEditor {
  readonly floors: Record<string, MockFloor>
  readonly items: ChangeItem[] = []
  private readonly brands: Map<string, MockBrand>

  constructor(parent: Record<string, MockFloor>, brands: Map<string, MockBrand>) {
    this.floors = cloneFloors(parent)
    this.brands = brands
  }

  unit(floor: string, code: string): MockUnit {
    const found = this.floors[floor].units.find((u) => u.code === code)
    if (!found) throw new Error(`mock unit not found: ${code}`)
    return found
  }

  brandName(id: string | null): string {
    return id ? (this.brands.get(id)?.name ?? id) : '(공실)'
  }

  private record(item: Omit<ChangeItem, 'brand_ids_before' | 'brand_ids_after' | 'entity_ids_before' | 'entity_ids_after'> & Partial<ChangeItem>) {
    this.items.push({
      brand_ids_before: [],
      brand_ids_after: [],
      entity_ids_before: item.unit_codes_before.map((c) => `U-${c}`),
      entity_ids_after: item.unit_codes_after.map((c) => `U-${c}`),
      ...item,
    })
  }

  move(floor: string, from: string, to: string, reason: string) {
    const origin = this.unit(floor, from)
    const dest = this.unit(floor, to)
    const brand = this.brands.get(origin.brandId ?? '')
    if (!brand || dest.brandId) throw new Error(`invalid mock move ${from} -> ${to}`)
    const fixturesBefore = baseFixtureCount(origin)
    dest.brandId = brand.id
    dest.category = brand.category
    origin.brandId = null
    this.record({
      change_type: 'move',
      floor,
      unit_codes_before: [from],
      unit_codes_after: [to],
      brand_ids_before: [brand.id],
      brand_ids_after: [brand.id],
      summary: `${floor} ${brand.name}(${categoryName(brand.category)}) 매장을 ${from}에서 ${to}로 이전 (${reason})`,
      details: {
        brand_name: brand.name,
        category: brand.category,
        area_before_m2: rectArea(origin.rect),
        area_after_m2: rectArea(dest.rect),
        distance_to_escalator_before_m: escalatorDistance(origin.rect),
        distance_to_escalator_after_m: escalatorDistance(dest.rect),
        fixture_count_before: fixturesBefore,
        fixture_count_after: baseFixtureCount(dest),
        vacated_unit_code: from,
        reason,
      },
    })
  }

  swap(floor: string, a: string, b: string, reason: string) {
    const ua = this.unit(floor, a)
    const ub = this.unit(floor, b)
    const [ba, bb] = [ua.brandId, ub.brandId]
    ua.brandId = bb
    ub.brandId = ba
    ;[ua.category, ub.category] = [ub.category, ua.category]
    for (const [brandId, src, dst] of [
      [ba, a, b],
      [bb, b, a],
    ] as const) {
      this.record({
        change_type: 'move',
        floor,
        unit_codes_before: [src],
        unit_codes_after: [dst],
        brand_ids_before: brandId ? [brandId] : [],
        brand_ids_after: brandId ? [brandId] : [],
        summary: `${floor} ${this.brandName(brandId)} 매장을 ${src}에서 ${dst}로 이전 (맞교환, ${reason})`,
        details: { brand_name: this.brandName(brandId), swap_with_unit_code: dst, reason },
      })
    }
  }

  reassign(floor: string, code: string, brandId: string, reason: string) {
    const u = this.unit(floor, code)
    const brand = this.brands.get(brandId)
    if (!brand) throw new Error(`unknown mock brand ${brandId}`)
    const oldName = this.brandName(u.brandId)
    const oldCategory = u.category
    const note = oldCategory !== brand.category ? `, ${categoryName(oldCategory)}→${categoryName(brand.category)}` : ''
    const before = u.brandId ? [u.brandId] : []
    u.brandId = brand.id
    u.category = brand.category
    this.record({
      change_type: 'reassign',
      floor,
      unit_codes_before: [code],
      unit_codes_after: [code],
      brand_ids_before: before,
      brand_ids_after: [brand.id],
      summary: `${floor} ${code} 브랜드 교체: ${oldName} → ${brand.name}${note} (${reason})`,
      details: {
        brand_name_before: before.length ? oldName : null,
        brand_name_after: brand.name,
        category_before: oldCategory,
        category_after: brand.category,
        reason,
      },
    })
  }

  boundaryShift(floor: string, grow: string, shrink: string, shift: number) {
    const g = this.unit(floor, grow)
    const s = this.unit(floor, shrink)
    for (const [u, other, sign] of [
      [g, s, 1],
      [s, g, -1],
    ] as const) {
      const before = rectArea(u.rect)
      if (sign > 0) u.rect = [u.rect[0], u.rect[1], u.rect[2] + shift, u.rect[3]]
      else u.rect = [u.rect[0] + shift, u.rect[1], u.rect[2], u.rect[3]]
      this.record({
        change_type: 'resize',
        floor,
        unit_codes_before: [u.code],
        unit_codes_after: [u.code],
        brand_ids_before: u.brandId ? [u.brandId] : [],
        brand_ids_after: u.brandId ? [u.brandId] : [],
        summary: `${floor} ${g.code}·${s.code} 경계 ${shift} m 이동: ${u.code} ${sign > 0 ? '확장' : '축소'}`,
        details: {
          area_before_m2: before,
          area_after_m2: rectArea(u.rect),
          boundary_shift_m: sign * shift,
          neighbor_unit_code: other.code,
          reason: 'boundary_shift',
        },
      })
    }
  }

  addPopup(floor: string, code: string, rect: Rect, brandId: string) {
    const brand = this.brands.get(brandId)
    if (!brand) throw new Error(`unknown mock brand ${brandId}`)
    const popup = unit(code, rect, 'S', 'P', brand.category, brand.id)
    this.floors[floor].units.push(popup)
    this.record({
      change_type: 'add',
      floor,
      unit_codes_before: [],
      unit_codes_after: [code],
      brand_ids_after: [brand.id],
      summary: `${floor} 아트리움(ESC-A·ESC-B 사이)에 팝업 매장 ${code}(${brand.name}) 신설`,
      details: { area_after_m2: rectArea(rect), category: brand.category, location: '아트리움 중앙' },
    })
    const atrium = this.floors[floor].walkways.find((w) => w.key === 'AM')
    if (atrium) {
      // The popup sits against the atrium's north edge (y=47), so the walkway keeps a notch.
      const [x0, y0, x1, y1] = rect
      const before = 140
      atrium.ring = [
        [55, 33],
        [65, 33],
        [65, 47],
        [x1, y1],
        [x1, y0],
        [x0, y0],
        [x0, y1],
        [55, 47],
        [55, 33],
      ]
      const removed = (x1 - x0) * (y1 - y0)
      this.record({
        change_type: 'walkway_change',
        floor,
        unit_codes_before: [],
        unit_codes_after: [],
        entity_ids_before: [`W-${floor}-12`],
        entity_ids_after: [`W-${floor}-12`],
        summary: `${floor} 아트리움 중앙 통로 축소: 팝업 ${code} 설치 (-${removed.toFixed(1)}㎡)`,
        details: { reason: 'popup_added', popup_unit_code: code, area_before_m2: before, area_after_m2: before - removed },
      })
    }
  }

  merge(floor: string, a: string, b: string, reason: string) {
    const ua = this.unit(floor, a)
    const ub = this.unit(floor, b)
    const keep = [ua.brandId, ub.brandId]
      .map((id) => (id ? this.brands.get(id) : undefined))
      .filter((x): x is MockBrand => !!x)
      .sort((x, y) => y.attractiveness - x.attractiveness)[0]
    const merged = unit(`${a}M`, [Math.min(ua.rect[0], ub.rect[0]), ua.rect[1], Math.max(ua.rect[2], ub.rect[2]), ua.rect[3]], ua.facing, ua.band, keep.category, keep.id)
    const units = this.floors[floor].units
    units.splice(units.indexOf(ua), 1, merged)
    units.splice(units.indexOf(ub), 1)
    this.record({
      change_type: 'merge',
      floor,
      unit_codes_before: [a, b],
      unit_codes_after: [merged.code],
      brand_ids_before: [ua.brandId, ub.brandId].filter((x): x is string => !!x),
      brand_ids_after: [keep.id],
      summary: `${floor} ${a}·${b} 병합 → ${merged.code} (${keep.name} 확장, ${reason})`,
      details: { area_before_m2: [rectArea(ua.rect), rectArea(ub.rect)], area_after_m2: rectArea(merged.rect), kept_brand_id: keep.id, reason },
    })
  }

  removeToAmenity(floor: string, code: string, amenityType: string, amenityName: string) {
    const u = this.unit(floor, code)
    const units = this.floors[floor].units
    units.splice(units.indexOf(u), 1)
    this.floors[floor].amenities.push({ type: amenityType, rect: [...u.rect] as Rect })
    this.record({
      change_type: 'remove',
      floor,
      unit_codes_before: [code],
      unit_codes_after: [],
      brand_ids_before: u.brandId ? [u.brandId] : [],
      summary: `${floor} ${code}(${this.brandName(u.brandId)}) 매장 철거 → ${amenityName} 전환`,
      details: { area_before_m2: rectArea(u.rect), converted_to: amenityType },
    })
  }

  addFixture(floor: string, code: string, type: string) {
    const u = this.unit(floor, code)
    const index = baseFixtureCount(u) + u.extraFixtures.length + 1
    const before = index - 1
    u.extraFixtures.push(type)
    const fixtureId = `${code}-F${String(index).padStart(3, '0')}`
    this.record({
      change_type: 'fixture_change',
      floor,
      unit_codes_before: [code],
      unit_codes_after: [code],
      brand_ids_before: u.brandId ? [u.brandId] : [],
      brand_ids_after: u.brandId ? [u.brandId] : [],
      entity_ids_before: [],
      entity_ids_after: [`F-${fixtureId}`],
      summary: `${floor} ${code}(${this.brandName(u.brandId)}) 집기 변경: ${FIXTURE_DIMS[type]?.name_ko ?? type} ${fixtureId} 추가`,
      details: { fixture_count_before: before, fixture_count_after: before + 1, added_fixture_ids: [fixtureId], added_fixture_types: [type] },
    })
  }
}

// --- world construction ---------------------------------------------------------------------------

function baseFloors(): Record<string, MockFloor> {
  return Object.fromEntries(
    Object.entries(BASE_UNITS).map(([code, units]) => [
      code,
      {
        code,
        units: units.map((u) => ({ ...u, rect: [...u.rect] as Rect, extraFixtures: [] })),
        amenities: [...BASE_AMENITIES, ...(FLOOR_AMENITIES[code] ?? [])].map((a) => ({ ...a, rect: [...a.rect] as Rect })),
        walkways: [...BASE_WALKWAYS, ...(FLOOR_WALKWAYS[code] ?? [])].map((w) => ({ ...w, ring: w.ring.map((p) => [...p] as [number, number]) })),
      },
    ]),
  )
}

function revisionInfo(rev_id: string, kind: string, parent: string | null, date: string | null): RevisionInfo {
  return { rev_id, kind, parent_rev_id: parent, effective_date: date }
}

export function buildWorld(): MockWorld {
  const brands = new Map(BRANDS.map(([id, name, category, attractiveness]) => [id, { id, name, category, attractiveness }]))
  const revisions = new Map<string, MockRevision>()

  const r0 = baseFloors()
  revisions.set('R0', { info: revisionInfo('R0', 'actual', null, '2024-10-01'), notes: '기준 배치', floors: r0, changes: [] })

  const e1 = new RevisionEditor(r0, brands)
  e1.move('3F', '3F-W02', '3F-C02', '저노출 서측 매장에서 ESC-A 에스컬레이터 정면으로 이동')
  e1.boundaryShift('3F', '3F-S01', '3F-S02', 2.4)
  e1.addPopup('3F', '3F-P01', [57, 42, 63, 47], 'BR3103')
  e1.reassign('1F', '1F-C04', 'BR1110', '화장품 카운터 브랜드 교체')
  e1.merge('1F', '1F-N01', '1F-N02', '플래그십 확장')
  e1.removeToAmenity('1F', '1F-E02', 'LOUNGE', '고객라운지')
  e1.addFixture('1F', '1F-C01', 'SHOWCASE')
  revisions.set('R1', { info: revisionInfo('R1', 'actual', 'R0', '2025-03-01'), notes: 'S/S MD 개편', floors: e1.floors, changes: e1.items })

  const e2 = new RevisionEditor(e1.floors, brands)
  e2.reassign('3F', '3F-N03', 'BR3104', '카테고리 믹스 조정')
  e2.reassign('3F', '3F-W02', 'BR3202', '공실 해소: 카페 신규 입점')
  e2.addFixture('3F', '3F-C04', 'TABLE')
  e2.reassign('1F', '1F-S04', 'BR1007', '명품 브랜드 교체')
  revisions.set('R2', { info: revisionInfo('R2', 'actual', 'R1', '2025-09-01'), notes: 'F/W MD 개편', floors: e2.floors, changes: e2.items })

  const eh = new RevisionEditor(e2.floors, brands)
  eh.swap('3F', '3F-N04', '3F-C01', 'what-if: 고매력 브랜드 전면 배치')
  revisions.set('H1', {
    info: revisionInfo('H1', 'hypothetical', 'R2', null),
    notes: 'R2 기반 what-if: 3F 브랜드 맞교환',
    floors: eh.floors,
    changes: eh.items,
  })

  return { brands, revisions, order: ['R0', 'R1', 'R2', 'H1'] }
}

let worldSingleton: MockWorld | null = null

export function getWorld(): MockWorld {
  worldSingleton ??= buildWorld()
  return worldSingleton
}

// --- change sets between arbitrary revisions ----------------------------------------------------

const INVERSE_TYPE: Record<string, string> = { add: 'remove', remove: 'add', merge: 'split', split: 'merge' }

export function invertChange(item: ChangeItem): ChangeItem {
  return {
    ...item,
    change_type: INVERSE_TYPE[item.change_type] ?? item.change_type,
    unit_codes_before: item.unit_codes_after,
    unit_codes_after: item.unit_codes_before,
    brand_ids_before: item.brand_ids_after ?? [],
    brand_ids_after: item.brand_ids_before ?? [],
    entity_ids_before: item.entity_ids_after ?? [],
    entity_ids_after: item.entity_ids_before ?? [],
    summary: `(역방향) ${item.summary}`,
  }
}

function ancestors(world: MockWorld, rev: string): string[] {
  const chain: string[] = []
  let current: string | null = rev
  while (current && world.revisions.has(current) && !chain.includes(current)) {
    chain.push(current)
    current = world.revisions.get(current)?.info.parent_rev_id ?? null
  }
  return chain
}

function forward(world: MockWorld, from: string, to: string): ChangeItem[] {
  const chain = ancestors(world, to)
  const index = chain.indexOf(from)
  if (index < 0) return []
  return chain
    .slice(0, index)
    .reverse()
    .flatMap((rev) => world.revisions.get(rev)?.changes ?? [])
}

export function changesBetween(world: MockWorld, from: string, to: string): ChangeItem[] {
  if (from === to) return []
  if (ancestors(world, to).includes(from)) return forward(world, from, to)
  if (ancestors(world, from).includes(to)) return forward(world, to, from).map(invertChange).reverse()
  const common = ancestors(world, from).find((rev) => ancestors(world, to).includes(rev))
  if (!common) return []
  return [...forward(world, common, from).map(invertChange).reverse(), ...forward(world, common, to)]
}
