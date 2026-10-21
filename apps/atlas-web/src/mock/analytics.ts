// Mock analytics, impact plans, artifacts and layout alternatives for the synthetic world.
import { categoryColorByIndex } from '../lib/colors'
import type {
  Artifact,
  ArtifactFile,
  ChangeItem,
  ImpactEntry,
  LayoutAlternative,
  LayoutRequest,
  ProductivityResponse,
  ProductivityRow,
} from '../types/api'
import { BUILDING, GENERATOR_SPECS, MOCK_CATEGORIES, MOCK_FLOORS, RISK_ORDER, categoryName } from './catalog'
import { changesBetween, escalatorDistance, rectArea, unitFixtures, type MockWorld, type Rect } from './world'

const M2_PER_PYEONG = 3.305785

// --- productivity ------------------------------------------------------------------------------

const BASE_SALES_PER_M2: Record<string, number> = {
  LUXURY: 9_200_000,
  JEWELRY_WATCH: 7_800_000,
  COSMETICS: 8_400_000,
  WOMEN_CONTEMPORARY: 3_300_000,
  ACCESSORIES: 3_700_000,
  CAFE: 2_600_000,
}

export interface ProductivityDetail extends ProductivityRow {
  distance_to_escalator_m: number
  normalized: number
}

export function mockProductivityDetails(world: MockWorld, rev: string, floor: string): ProductivityDetail[] | null {
  const f = world.revisions.get(rev)?.floors[floor]
  if (!f) return null
  const scored = f.units
    .filter((u) => u.brandId)
    .map((u) => {
      const brand = world.brands.get(u.brandId!)!
      const dist = escalatorDistance(u.rect)
      const area = rectArea(u.rect)
      const exposure = 1 / (1 + dist / 22)
      const areaPenalty = area > 260 ? 0.9 : 1
      const perM2 = Math.round(((BASE_SALES_PER_M2[u.category] ?? 3_000_000) * (0.45 + exposure) * brand.attractiveness * areaPenalty) / 1000) * 1000
      return { u, brand, dist, area, perM2 }
    })
  const sorted = [...scored].sort((a, b) => a.perM2 - b.perM2)
  const min = sorted[0]?.perM2 ?? 0
  const max = sorted.at(-1)?.perM2 ?? 1
  return scored.map((s) => {
    const rank = sorted.indexOf(s)
    const percentile = sorted.length > 1 ? Math.round((rank / (sorted.length - 1)) * 100) : 50
    const low = percentile <= 20
    const causes: string[] = []
    if (s.dist > 30) causes.push(`에스컬레이터 원거리·저노출(${Math.round(s.dist)} m)`)
    if (s.brand.attractiveness < 0.85) causes.push('브랜드 집객력 약세')
    if (s.area > 260) causes.push('면적 대비 매출 부족(과대 면적)')
    const driver = low ? causes.join(' · ') || '복합 요인(동선·MD 구성)' : percentile >= 80 ? '고노출 입지·집객력 우수' : '평균 수준'
    return {
      unit_code: s.u.code,
      brand_name: s.brand.name,
      category: s.u.category,
      sales_krw: Math.round((s.perM2 * s.area) / 10_000) * 10_000,
      sales_per_m2: s.perM2,
      sales_per_pyeong: Math.round(s.perM2 * M2_PER_PYEONG),
      percentile,
      low_efficiency: low,
      driver,
      distance_to_escalator_m: s.dist,
      normalized: max > min ? Math.round(((s.perM2 - min) / (max - min)) * 1000) / 1000 : 0.5,
    }
  })
}

export function mockProductivity(world: MockWorld, rev: string, floor: string): ProductivityResponse | null {
  const details = mockProductivityDetails(world, rev, floor)
  if (!details) return null
  const values = details.map((d) => d.sales_per_m2 ?? 0)
  return {
    rows: details.map((d) => ({
      unit_code: d.unit_code,
      brand_name: d.brand_name,
      category: d.category,
      sales_krw: d.sales_krw,
      sales_per_m2: d.sales_per_m2,
      sales_per_pyeong: d.sales_per_pyeong,
      percentile: d.percentile,
      low_efficiency: d.low_efficiency,
      driver: d.driver,
    })),
    metric: { name: 'sales_per_m2', label: '평효율(원/㎡)', min: Math.min(...values), max: Math.max(...values) },
  }
}

// --- impact plan and artifacts ------------------------------------------------------------------

export function planImpact(items: ChangeItem[]): ImpactEntry[] {
  return GENERATOR_SPECS.flatMap((spec) => {
    const matched = items.filter((item) => spec.triggers.includes(item.change_type))
    if (!matched.length) return []
    return [
      {
        generator: spec.name,
        title_ko: spec.title_ko,
        risk_tier: spec.risk_tier,
        approval_mode: spec.approval_mode,
        floors: [...new Set(matched.map((m) => m.floor))].sort(),
        reasons: matched.map((m) => m.summary),
      },
    ]
  }).sort((a, b) => RISK_ORDER[a.risk_tier] - RISK_ORDER[b.risk_tier] || a.generator.localeCompare(b.generator))
}

function escapeXml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[ch]!)
}

function csvCell(value: unknown): string {
  const text = value === null || value === undefined ? '' : String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function csvDataUrl(rows: unknown[][]): string {
  const body = rows.map((r) => r.map(csvCell).join(',')).join('\n')
  return `data:text/csv;charset=utf-8,${encodeURIComponent(`\ufeff${body}`)}`
}

function categoryHex(code: string): string {
  const index = MOCK_CATEGORIES.findIndex((c) => c.code === code)
  return index >= 0 ? categoryColorByIndex(index) : '#d9d9d9'
}

function floorSvgDataUrl(world: MockWorld, rev: string, floor: string): string {
  const scale = 6
  const f = world.revisions.get(rev)!.floors[floor]
  const y = (v: number) => (BUILDING.depth - v) * scale
  const rect = ([x0, y0, x1, y1]: Rect, fill: string) =>
    `<rect x="${x0 * scale}" y="${y(y1)}" width="${(x1 - x0) * scale}" height="${(y1 - y0) * scale}" fill="${fill}" stroke="#555" stroke-width="0.6"/>`
  const parts = [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${BUILDING.width * scale} ${BUILDING.depth * scale + 30}" font-family="sans-serif">`,
    `<rect x="0" y="0" width="${BUILDING.width * scale}" height="${BUILDING.depth * scale}" fill="#fafafa" stroke="#333"/>`,
    ...f.units.map((u) => {
      const name = u.brandId ? (world.brands.get(u.brandId)?.name ?? '') : '(공실)'
      const [cx, cy] = [((u.rect[0] + u.rect[2]) / 2) * scale, y((u.rect[1] + u.rect[3]) / 2)]
      return `${rect(u.rect, u.brandId ? categoryHex(u.category) : '#eeeeee')}<text x="${cx}" y="${cy}" font-size="9" text-anchor="middle">${escapeXml(name)}</text>`
    }),
    `<text x="6" y="${BUILDING.depth * scale + 20}" font-size="14">${escapeXml(`${floor} 층별 안내도 · ${rev} (모의 데이터)`)}</text>`,
    '</svg>',
  ]
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(parts.join(''))}`
}

function artifactFiles(world: MockWorld, rev: string, generator: string): ArtifactFile[] {
  const floors = MOCK_FLOORS.map((f) => f.code)
  const revision = world.revisions.get(rev)!
  const units = floors.flatMap((floor) => revision.floors[floor]?.units.map((u) => ({ floor, u })) ?? [])
  const brandName = (id: string | null) => (id ? (world.brands.get(id)?.name ?? id) : '(공실)')
  switch (generator) {
    case 'area_schedule':
      return [
        { path: 'area_schedule.xlsx', url: null },
        {
          path: 'area_schedule.csv',
          url: csvDataUrl([
            ['floor', 'unit_code', 'brand_name', 'category', 'area_m2', 'area_pyeong'],
            ...units.map(({ floor, u }) => [floor, u.code, brandName(u.brandId), categoryName(u.category), rectArea(u.rect), (rectArea(u.rect) / M2_PER_PYEONG).toFixed(1)]),
          ]),
        },
      ]
    case 'fixture_schedule':
      return [
        { path: 'fixture_schedule.xlsx', url: null },
        {
          path: 'fixture_schedule.csv',
          url: csvDataUrl([
            ['floor', 'unit_code', 'brand_name', 'fixture_count'],
            ...units.map(({ floor, u }) => [floor, u.code, brandName(u.brandId), unitFixtures(u).length]),
          ]),
        },
      ]
    case 'floor_directory_svg':
      return floors.map((floor) => ({ path: `floor_${floor}.svg`, url: floorSvgDataUrl(world, rev, floor) }))
    case 'pos_space_kpi':
      return [
        { path: 'pos_space_kpi.xlsx', url: null },
        {
          path: 'pos_space_kpi.csv',
          url: csvDataUrl([
            ['floor', 'unit_code', 'brand_name', 'sales_per_m2'],
            ...floors.flatMap((floor) => (mockProductivity(world, rev, floor)?.rows ?? []).map((r) => [floor, r.unit_code, r.brand_name, r.sales_per_m2])),
          ]),
        },
      ]
    default:
      return []
  }
}

export function buildMockArtifacts(world: MockWorld, rev: string): Artifact[] | null {
  const revision = world.revisions.get(rev)
  if (!revision) return null
  const parent = revision.info.parent_rev_id
  const impact: ImpactEntry[] = parent
    ? planImpact(changesBetween(world, parent, rev))
    : GENERATOR_SPECS.map((spec) => ({
        generator: spec.name,
        title_ko: spec.title_ko,
        risk_tier: spec.risk_tier,
        approval_mode: spec.approval_mode,
        floors: MOCK_FLOORS.map((f) => f.code),
        reasons: ['기준 개정 최초 생성'],
      }))
  const floors = MOCK_FLOORS.map((f) => f.code)
  const units = floors.flatMap((floor) => revision.floors[floor]?.units ?? [])
  return impact.map((entry): Artifact => {
    let status = entry.approval_mode === 'pre_publish' ? 'awaiting_approval' : 'published'
    let reviewed_by: string | null = null
    let review_note: string | null = null
    const issues: Artifact['issues'] = []
    if (rev === 'R0' || (rev === 'R1' && entry.generator === 'area_schedule')) {
      status = 'published'
      reviewed_by = '공간기획팀'
      review_note = '면적 검증 완료'
    }
    if (rev === 'R2' && entry.generator === 'pos_space_kpi') {
      status = 'qa_failed'
      issues.push({ severity: 'error', message: 'POS 단말 2대가 3F-W02 신규 입점 매장에 매핑되지 않았습니다.', entity_ids: ['U-3F-W02'] })
    }
    if (rev === 'R2' && entry.generator === 'fixture_schedule') {
      issues.push({ severity: 'warning', message: '3F-C04 집기 밀도가 기준(16㎡당 1개)을 넘었습니다.', entity_ids: ['U-3F-C04'] })
    }
    const summary: Record<string, unknown> = { floors: entry.floors.join(', ') }
    if (entry.generator === 'area_schedule') {
      summary.units = units.length
      summary.total_unit_area_m2 = Math.round(units.reduce((acc, u) => acc + rectArea(u.rect), 0))
    } else if (entry.generator === 'fixture_schedule') {
      summary.units = units.filter((u) => u.brandId).length
      summary.fixtures = units.reduce((acc, u) => acc + unitFixtures(u).length, 0)
    } else if (entry.generator === 'floor_directory_svg') {
      summary.files = floors.length
    } else if (entry.generator === 'pos_space_kpi') {
      summary.mapped_units = units.filter((u) => u.brandId).length
      summary.unmapped_terminals = status === 'qa_failed' ? 2 : 0
    }
    return {
      generator: entry.generator,
      title_ko: entry.title_ko,
      status,
      approval_mode: entry.approval_mode,
      files: artifactFiles(world, rev, entry.generator),
      summary,
      issues,
      reviewed_by,
      review_note,
    }
  })
}

// --- layout alternatives -----------------------------------------------------------------------

interface LayoutPlan {
  id: string
  moves: [string, string][]
  objective: [number, number]
  delta: [number, number, number]
  constraints: [string, boolean][]
  explain: (name: (code: string) => string) => string[]
}

const LAYOUT_PLANS: Record<string, LayoutPlan[]> = {
  '3F': [
    {
      id: 'ALT-3F-A',
      moves: [
        ['3F-E01', '3F-C06'],
        ['3F-C06', '3F-E01'],
      ],
      objective: [0.62, 0.71],
      delta: [280_000_000, 110_000_000, 420_000_000],
      constraints: [
        ['카테고리 존 연속성', true],
        ['앵커 브랜드 위치 유지', true],
        ['최소 전면폭 3 m', true],
      ],
      explain: (n) => [
        `저효율 3F-E01(${n('3F-E01')})을 아트리움 인접 3F-C06으로 옮겨 노출을 높입니다.`,
        `집객력이 높은 ${n('3F-C06')}는 동측 외곽에서도 목적 방문을 유도합니다.`,
      ],
    },
    {
      id: 'ALT-3F-B',
      moves: [
        ['3F-W01', '3F-C04'],
        ['3F-C04', '3F-W01'],
      ],
      objective: [0.62, 0.68],
      delta: [160_000_000, 40_000_000, 290_000_000],
      constraints: [
        ['카테고리 존 연속성', true],
        ['앵커 브랜드 위치 유지', true],
        ['최소 전면폭 3 m', true],
      ],
      explain: (n) => [
        `서측 외곽 3F-W01(${n('3F-W01')})을 중앙 섬 3F-C04로 옮겨 서측 횡단 통로 유입을 활용합니다.`,
        `${n('3F-C04')}는 집기 밀도가 높아 깊은 외곽 매장에서도 진열 효율을 유지합니다.`,
      ],
    },
    {
      id: 'ALT-3F-C',
      moves: [
        ['3F-E02', '3F-N02'],
        ['3F-N02', '3F-W01'],
        ['3F-W01', '3F-E02'],
      ],
      objective: [0.62, 0.66],
      delta: [90_000_000, -60_000_000, 210_000_000],
      constraints: [
        ['카테고리 존 연속성', false],
        ['앵커 브랜드 위치 유지', true],
        ['최소 전면폭 3 m', true],
      ],
      explain: (n) => [
        `잡화 ${n('3F-E02')}를 북측 3F-N02로 옮겨 Atelier 계열 잡화와 묶습니다.`,
        '3개 매장을 순환 이동하므로 공사 기간과 매출 공백 위험이 큽니다.',
        '여성 컨템포러리 존이 끊겨 카테고리 존 연속성 제약을 위반합니다.',
      ],
    },
  ],
  '1F': [
    {
      id: 'ALT-1F-A',
      moves: [
        ['1F-E01', '1F-C05'],
        ['1F-C05', '1F-E01'],
      ],
      objective: [0.71, 0.77],
      delta: [350_000_000, 150_000_000, 520_000_000],
      constraints: [
        ['카테고리 존 연속성', true],
        ['앵커 브랜드 위치 유지', true],
        ['최소 전면폭 3 m', true],
      ],
      explain: (n) => [
        `주얼리 ${n('1F-E01')}를 아트리움 북측 1F-C05로 옮겨 정문 동선 노출을 높입니다.`,
        '두 매장 모두 주얼리·시계여서 존 구성이 유지됩니다.',
      ],
    },
    {
      id: 'ALT-1F-B',
      moves: [
        ['1F-W02', '1F-C06'],
        ['1F-C06', '1F-W02'],
      ],
      objective: [0.71, 0.74],
      delta: [120_000_000, 20_000_000, 240_000_000],
      constraints: [
        ['카테고리 존 연속성', true],
        ['앵커 브랜드 위치 유지', true],
        ['최소 전면폭 3 m', true],
      ],
      explain: (n) => [`서측 화장품 ${n('1F-W02')}를 동측 섬으로 옮겨 서측 출입구 의존도를 낮춥니다.`],
    },
    {
      id: 'ALT-1F-C',
      moves: [
        ['1F-S02', '1F-C02'],
        ['1F-C02', '1F-S02'],
      ],
      objective: [0.71, 0.72],
      delta: [40_000_000, -90_000_000, 160_000_000],
      constraints: [
        ['카테고리 존 연속성', false],
        ['앵커 브랜드 위치 유지', false],
        ['최소 전면폭 3 m', true],
      ],
      explain: (n) => [`명품 ${n('1F-S02')}와 주얼리 ${n('1F-C02')}를 맞바꿉니다.`, '앵커 명품 위치가 바뀌어 승인 리스크가 큽니다.'],
    },
  ],
}

export function mockLayoutAlternatives(world: MockWorld, request: LayoutRequest): LayoutAlternative[] {
  const revision = world.revisions.get(request.rev)
  if (!revision) return []
  const maxMoves = typeof request.constraints?.max_moves === 'number' ? request.constraints.max_moves : null
  const floors = request.floors?.length ? request.floors : MOCK_FLOORS.map((f) => f.code)
  return floors.flatMap((floor) => {
    const units = revision.floors[floor]?.units ?? []
    const byCode = new Map(units.map((u) => [u.code, u]))
    const name = (code: string) => {
      const id = byCode.get(code)?.brandId
      return id ? (world.brands.get(id)?.name ?? id) : '(공실)'
    }
    return (LAYOUT_PLANS[floor] ?? []).flatMap((plan): LayoutAlternative[] => {
      const moves = plan.moves
        .filter(([from, to]) => byCode.get(from)?.brandId && byCode.has(to))
        .map(([from, to]) => {
          const brandId = byCode.get(from)!.brandId!
          return { brand_id: brandId, brand_name: world.brands.get(brandId)?.name ?? brandId, from_unit: from, to_unit: to }
        })
      if (!moves.length) return []
      const constraints = plan.constraints.map(([n, ok]) => ({ name: n, ok }))
      if (maxMoves !== null) constraints.push({ name: `이동 수 ≤ ${maxMoves}`, ok: moves.length <= maxMoves })
      return [
        {
          id: plan.id,
          moves,
          objective_before: plan.objective[0],
          objective_after: plan.objective[1],
          kpi_delta: { sales_krw: plan.delta[0], low: plan.delta[1], high: plan.delta[2] },
          constraints,
          explanations: plan.explain(name),
        },
      ]
    })
  })
}
