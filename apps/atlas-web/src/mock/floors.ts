// Mock GET /api/meta and GET /api/floors/{rev}/{floor} built from the synthetic world.
import { categoryColorByIndex } from '../lib/colors'
import type { Feature, FeatureCollection, FeatureProps, Geometry, Meta, RevisionInfo } from '../types/api'
import { BUILDING, MOCK_CATEGORIES, MOCK_DATA_THROUGH, MOCK_FACILITY, MOCK_FLOORS } from './catalog'
import { OPENINGS, VERTICAL_LINKS, rectArea, rectRing, unitFixtures, type MockRevision, type MockWorld, type Rect } from './world'

const AMENITY_NAMES: Record<string, string> = {
  RESTROOM: '화장실',
  INFO: '안내데스크',
  ATM: 'ATM',
  NURSING: '수유실',
  LOUNGE: '고객라운지',
}

function revisionMeta(revision: MockRevision): RevisionInfo {
  const { info } = revision
  const hypothetical = info.kind === 'hypothetical'
  return {
    ...info,
    // Actual revisions that start after the last transaction date have no sales yet.
    ...(hypothetical ? {} : { has_sales: !!info.effective_date && info.effective_date <= MOCK_DATA_THROUGH }),
    model_source: revision.modelSource ?? (hypothetical ? 'hypothetical' : 'parsed'),
    notes: revision.notes,
  }
}

export function buildMockMeta(world: MockWorld): Meta {
  return {
    facility: { ...MOCK_FACILITY },
    data_through: MOCK_DATA_THROUGH,
    revisions: world.order.map((rev) => revisionMeta(world.revisions.get(rev)!)),
    floors: MOCK_FLOORS.map((f) => ({ code: f.code, ordinal: f.ordinal, name: f.name })),
    categories: MOCK_CATEGORIES.map((c, i) => ({ code: c.code, name_ko: c.name_ko, color: categoryColorByIndex(i) })),
  }
}

const EMPTY_PROPS: Omit<FeatureProps, 'kind'> = {
  unit_code: null,
  brand_id: null,
  brand_name: null,
  category: null,
  vacant: null,
  area_m2: null,
  zone_code: null,
  link_id: null,
  link_type: null,
  amenity_type: null,
  fixture_type: null,
}

function feature(id: string, geometry: Geometry, props: Partial<FeatureProps> & { kind: string }): Feature {
  return { type: 'Feature', id, geometry, properties: { ...EMPTY_PROPS, ...props } }
}

const polygon = (rect: Rect): Geometry => ({ type: 'Polygon', coordinates: [rectRing(rect)] })

export function buildMockFloor(world: MockWorld, rev: string, floorCode: string, includeFixtures: boolean): FeatureCollection | null {
  const revision = world.revisions.get(rev)
  const floor = revision?.floors[floorCode]
  if (!revision || !floor) return null
  const program: string[] = [...(MOCK_FLOORS.find((f) => f.code === floorCode)?.program ?? [])]
  for (const u of floor.units) if (!program.includes(u.category)) program.push(u.category)

  const features: Feature[] = [
    feature(`X-${floorCode}`, polygon([0, 0, BUILDING.width, BUILDING.depth]), { kind: 'exterior', label: '건물 외곽' }),
  ]
  floor.walkways.forEach((w, i) => {
    features.push(
      feature(`W-${floorCode}-${i + 1}`, { type: 'Polygon', coordinates: [w.ring] }, {
        kind: 'walkway',
        label: w.name_ko,
        name_ko: w.name_ko,
        walkway_type: w.walkway_type,
        segment: w.key,
      }),
    )
  })
  for (const link of VERTICAL_LINKS) {
    features.push(
      feature(`V-${floorCode}-${link.id}`, polygon(link.rect), {
        kind: 'vertical_link',
        link_id: link.id,
        link_type: link.type,
        label: `${link.id} ${link.name_ko}`,
        name_ko: link.name_ko,
      }),
    )
  }
  const counters: Record<string, number> = {}
  for (const a of floor.amenities) {
    counters[a.type] = (counters[a.type] ?? 0) + 1
    features.push(
      feature(`A-${floorCode}-${a.type}-${counters[a.type]}`, polygon(a.rect), {
        kind: 'amenity',
        amenity_type: a.type,
        label: AMENITY_NAMES[a.type] ?? a.type,
        name_ko: AMENITY_NAMES[a.type] ?? a.type,
      }),
    )
  }
  for (const o of OPENINGS[floorCode] ?? []) {
    features.push(
      feature(`O-${floorCode}-${o.id}`, { type: 'LineString', coordinates: o.path }, {
        kind: 'opening',
        label: `${o.id} ${o.name_ko}`,
        name_ko: o.name_ko,
      }),
    )
  }
  for (const u of floor.units) {
    const brand = u.brandId ? world.brands.get(u.brandId) : undefined
    features.push(
      feature(`U-${u.code}`, polygon(u.rect), {
        kind: 'unit',
        unit_code: u.code,
        brand_id: brand?.id ?? null,
        brand_name: brand?.name ?? null,
        category: u.category,
        vacant: !brand,
        area_m2: rectArea(u.rect),
        zone_code: `${floorCode}-Z${program.indexOf(u.category) + 1}`,
        label: u.code,
      }),
    )
  }
  if (includeFixtures) {
    for (const u of floor.units) {
      for (const f of unitFixtures(u)) {
        features.push(
          feature(`F-${f.id}`, polygon(f.rect), { kind: 'fixture', fixture_type: f.type, unit_code: u.code, label: f.id }),
        )
      }
    }
  }
  return { type: 'FeatureCollection', features }
}
