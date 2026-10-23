// Mock seasonal relayout ("시즌 개편") projects behind /api/relayout/*. The mock world has no 4F~6F
// drawings, so the FW2026 brief (4F·5F·6F) is diagnosed, planned and checked on 3F units of R2; the
// brief notes, diagnosis notes and plan caveats say so. All brands are fictional.
import { ApiError } from '../api/client'
import { CHECK_STATUS_LABELS, RELAYOUT_CHANGE_LABELS } from '../lib/relayout'
import type {
  RelayoutAlternative,
  RelayoutBrief,
  RelayoutBriefSummary,
  RelayoutChange,
  RelayoutCheck,
  RelayoutDiagnosis,
  RelayoutPhasing,
  RelayoutPhasingRequest,
  RelayoutPlan,
  RelayoutPlanOptions,
  RelayoutProject,
  RelayoutSelectRequest,
  UploadJob,
} from '../types/api'
import { mockProductivityDetails } from './analytics'
import { MOCK_DATA_THROUGH, categoryName } from './catalog'
import { planConstruction } from './phasing'
import { buildSeasonRevision, rectArea, type MockFloor, type MockWorld, type SeasonChange } from './world'

const STAND_IN_FLOOR = '3F'
const STAND_IN_NOTE = '모의 데이터에는 4~6층 도면이 없어 3층 매장으로 대신 보여 드립니다.'

export const FW2026_BRIEF: RelayoutBrief = {
  brief_id: 'FW2026',
  title: '2026 F/W MD 개편',
  season: 'FW',
  season_label: 'F/W(9~2월)',
  year: 2026,
  effective_date: '2026-10-01',
  base_rev: 'R2',
  floors: ['4F', '5F', '6F'],
  objective: 'commission',
  objective_label: '수수료 수익',
  max_moves: 6,
  downtime_weeks: 2,
  movable: 'expiring',
  keep_anchors: true,
  area_tolerance: 0.15,
  entries: [
    {
      brand_id: 'BR3301',
      brand_name: 'Norvane Outdoor',
      category: 'OUTDOOR',
      category_name: '아웃도어',
      target_area_m2: [240, 460],
      preferred_floors: ['6F'],
      commission_rate: 0.26,
      note: 'F/W 아웃도어 수요 대응',
      new_brand: true,
      attractiveness: 1.22,
    },
    {
      brand_id: 'BR3302',
      brand_name: 'Velkira Knit',
      category: 'WOMEN_CASUAL',
      category_name: '여성 캐주얼',
      target_area_m2: [120, 180],
      preferred_floors: ['4F'],
      commission_rate: 0.3,
      note: '니트 단독 매장',
      new_brand: true,
      attractiveness: 1.05,
    },
  ],
  exits: [
    { brand_id: 'BR3011', brand_name: 'Quorelle', unit_code: '3F-E01', reason: '계약 만료·저효율' },
    { brand_id: 'BR3101', brand_name: 'Ilvora Leather', unit_code: '3F-E02', reason: '계약 만료' },
  ],
  notes: STAND_IN_NOTE,
}

/** Mock lease end dates (other brands: DEFAULT_LEASE_END). */
const LEASE_END: Record<string, string> = {
  BR3011: '2026-09-30',
  BR3101: '2026-08-31',
  BR3014: '2026-09-30',
  BR3012: '2026-09-30',
  BR3009: '2027-02-28',
}
const DEFAULT_LEASE_END = '2027-12-31'

/** Mock lease end date of a brand (also used by the upload QA step). */
export function mockLeaseEnd(brandId: string): string {
  return LEASE_END[brandId] ?? DEFAULT_LEASE_END
}

const SEASON_FACTORS: Record<string, [string, number][]> = {
  FW: [
    ['OUTDOOR', 1.32],
    ['MEN_FORMAL', 1.17],
    ['WOMEN_CONTEMPORARY', 1.08],
    ['ACCESSORIES', 1.04],
    ['WOMEN_CASUAL', 1.02],
    ['COSMETICS', 0.97],
    ['CAFE', 0.91],
    ['SPORTS', 0.86],
  ],
  SS: [
    ['SPORTS', 1.21],
    ['CAFE', 1.12],
    ['WOMEN_CASUAL', 1.08],
    ['COSMETICS', 1.03],
    ['ACCESSORIES', 0.99],
    ['WOMEN_CONTEMPORARY', 0.95],
    ['MEN_FORMAL', 0.88],
    ['OUTDOOR', 0.79],
  ],
}

function seasonInfo(season: string, year: number) {
  if (season === 'SS') {
    return {
      code: 'SS',
      label: 'S/S(3~8월)',
      year,
      last: { start: `${year - 1}-03-01`, end: `${year - 1}-08-31` },
      previous: { start: `${year - 2}-03-01`, end: `${year - 2}-08-31` },
    }
  }
  return {
    code: 'FW',
    label: 'F/W(9~2월)',
    year,
    last: { start: `${year - 1}-10-01`, end: `${year}-02-28` },
    previous: { start: `${year - 2}-10-01`, end: `${year - 1}-02-28` },
  }
}

interface AltSpec {
  moves: SeasonChange[]
  /** Unit the placed entry brand opens in. */
  entryUnit: string
  summary: string
  explanations: string[]
  /** Expected sales change against the baseline: [mid, low, high] (KRW per year). */
  sales: [number, number, number]
  constraints?: { name: string; ok: boolean; detail?: string }[]
}

const MOVE_LUMIVANE: SeasonChange = { type: 'move', brandId: 'BR3014', from: '3F-C06', to: '3F-E01', reason: '계약 만료 매장을 동측 외곽으로 옮겨 중앙 섬을 비웁니다.' }
const MOVE_VELUNE: SeasonChange = { type: 'move', brandId: 'BR3012', from: '3F-C01', to: '3F-E02', reason: '계약 만료 매장을 동측 외곽으로 옮겨 서측 중앙 섬을 비웁니다.' }

/**
 * ALT-0 (baseline) and ALT-1..3. `explanations` holds only the extra notes the backend sends besides
 * the per-change explanations: the entry brand's shift from its baseline unit and the neighbors'
 * affinity spillover. ALT-1 has the highest net effect under both objectives.
 */
const ALT_SPECS: readonly AltSpec[] = [
  {
    moves: [],
    entryUnit: '3F-E01',
    summary: '퇴점·입점만 반영한 기준안입니다. 기존 브랜드는 옮기지 않습니다.',
    explanations: [],
    sales: [0, 0, 0],
  },
  {
    moves: [MOVE_LUMIVANE],
    entryUnit: '3F-C06',
    summary: 'Lumivane을 3F-E01로 옮기고, 아트리움에 가까운 3F-C06에 Norvane Outdoor를 넣습니다.',
    explanations: [
      '신규 입점 Norvane Outdoor 위치 3F-E01→3F-C06: 기대 매출 +0.9억 원 (추정치).',
      '이웃 매장 인접 친화도 변화에 따른 파급 효과: +0.2억 원 (추정치).',
    ],
    sales: [620_000_000, 340_000_000, 880_000_000],
  },
  {
    moves: [MOVE_VELUNE],
    entryUnit: '3F-C01',
    summary: 'Studio Velune을 3F-E02로 옮기고, 서측 중앙 3F-C01에 Norvane Outdoor를 넣습니다.',
    explanations: [
      '신규 입점 Norvane Outdoor 위치 3F-E01→3F-C01: 기대 매출 +0.6억 원 (추정치).',
      '이웃 매장 인접 친화도 변화에 따른 파급 효과: −0.1억 원 (추정치).',
    ],
    sales: [450_000_000, 200_000_000, 690_000_000],
  },
  {
    moves: [MOVE_LUMIVANE, MOVE_VELUNE],
    entryUnit: '3F-C06',
    summary: '두 브랜드를 동측 외곽으로 옮기고 3F-C06에 Norvane Outdoor를 넣습니다. 3F-C01이 공실로 남습니다.',
    explanations: [
      '신규 입점 Norvane Outdoor 위치 3F-E01→3F-C06: 기대 매출 +0.9억 원 (추정치).',
      '이웃 매장 인접 친화도 변화에 따른 파급 효과: −0.3억 원 (추정치). 중앙 섬 3F-C01이 비어 주변 유입이 줄어듭니다.',
    ],
    sales: [630_000_000, 150_000_000, 1_020_000_000],
    constraints: [{ name: '중앙 섬 공실 없음', ok: false, detail: '3F-C01이 공실로 남습니다.' }],
  },
]
/** Expected yearly sales of the entry brand in the baseline unit. */
const ENTRY_SALES = 820_000_000
const SEASON_WEIGHT = 1.08
const DEFAULT_COMMISSION = 0.27

interface MockProject {
  brief: RelayoutBrief
  status: string
  diagnosis: RelayoutDiagnosis | null
  plan: RelayoutPlan | null
  selected_alt_id: string | null
  decided_by: string | null
  decided_at: string | null
  check: RelayoutCheck | null
  phasing: RelayoutPhasing | null
  updated_at: string
  version: number
  report: { version: number; url: string | null } | null
}

/** "2026-10-23T10:20:28+09:00" (KST). */
export function kstNow(now = new Date()): string {
  return `${new Date(now.getTime() + 9 * 3600_000).toISOString().slice(0, 19)}+09:00`
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]!)
}

const eok = (krw: number | null) => (krw === null ? '–' : `${(krw / 1e8).toFixed(1)}억 원`)

function reportHtml(p: MockProject): string {
  const rows = (p.plan?.alternatives ?? [])
    .map(
      (a) =>
        `<tr><td>${escapeHtml(a.alt_id)}</td><td>${escapeHtml(a.summary)}</td><td>${eok(a.kpi.net_krw)}</td><td>${eok(a.kpi.vs_current_sales_krw)}</td></tr>`,
    )
    .join('')
  const parts = [
    '<!doctype html><html lang="ko"><head><meta charset="utf-8">',
    `<title>${escapeHtml(p.brief.title)} 제안서</title>`,
    '<style>body{font-family:sans-serif;margin:32px;line-height:1.5}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style>',
    '</head><body>',
    `<h1>${escapeHtml(p.brief.title)} 제안서 (모의 데이터)</h1>`,
    `<p>기준 개정 ${escapeHtml(p.brief.base_rev)} · 적용일 ${escapeHtml(p.brief.effective_date ?? '–')} · 대상 층 ${escapeHtml(p.brief.floors.join(', '))}</p>`,
    '<h2>진단</h2>',
    p.diagnosis ? `<p>검토 후보 매장 ${p.diagnosis.candidate_total ?? p.diagnosis.candidates.length}곳</p>` : '<p>아직 진단하지 않았습니다.</p>',
    '<h2>대안 비교</h2>',
    rows ? `<table><thead><tr><th>대안</th><th>요약</th><th>순효과</th><th>지금 대비 매출</th></tr></thead><tbody>${rows}</tbody></table>` : '<p>아직 대안이 없습니다.</p>',
    '<h2>선택안</h2>',
    p.selected_alt_id ? `<p>${escapeHtml(p.selected_alt_id)} · ${escapeHtml(p.decided_by ?? '')} · ${escapeHtml(p.decided_at ?? '')}</p>` : '<p>아직 결정하지 않았습니다.</p>',
    '<h2>공사 순서</h2>',
    p.phasing
      ? `<p>${escapeHtml(p.phasing.summary)}</p><ol>${p.phasing.steps
          .map((s) => `<li>${s.start_week}~${s.end_week}주 · ${escapeHtml(s.unit_code ?? '–')} · ${escapeHtml(s.action_label ?? s.action)} · ${escapeHtml(s.brand_name)}</li>`)
          .join('')}</ol>`
      : '<p>아직 공사 순서를 계산하지 않았습니다.</p>',
    '<h2>도면 대조</h2>',
    p.check ? `<p>${escapeHtml(p.check.rev)}: 일치율 ${p.check.conformance_pct}% · ${escapeHtml(p.check.summary)}</p>` : '<p>아직 대조하지 않았습니다.</p>',
    '<h2>가정과 한계</h2>',
    `<ul>${[...(p.plan?.caveats ?? []), STAND_IN_NOTE].map((c) => `<li>${escapeHtml(c)}</li>`).join('')}</ul>`,
    '</body></html>',
  ]
  return parts.join('')
}

function unitOf(floors: Record<string, MockFloor>, brandId: string): string | null {
  for (const floor of Object.values(floors)) {
    const unit = floor.units.find((u) => u.brandId === brandId)
    if (unit) return unit.code
  }
  return null
}

function round1(value: number): number {
  return Math.round(value * 10) / 10
}

export type MockRelayout = ReturnType<typeof createMockRelayout>

/**
 * `publish` makes a hypothetical revision created by a plan visible in /api/meta.
 * State lives per mock data source.
 */
export function createMockRelayout(world: MockWorld, publish: (rev: string) => void) {
  const projects = new Map<string, MockProject>()

  const fresh = (brief: RelayoutBrief, updatedAt: string): MockProject => ({
    brief,
    status: 'draft',
    diagnosis: null,
    plan: null,
    selected_alt_id: null,
    decided_by: null,
    decided_at: null,
    check: null,
    phasing: null,
    updated_at: updatedAt,
    version: 0,
    report: null,
  })
  projects.set(FW2026_BRIEF.brief_id, fresh(structuredClone(FW2026_BRIEF), '2026-10-20T09:00:00+09:00'))

  const touch = (p: MockProject) => {
    p.version += 1
    p.updated_at = kstNow()
  }

  const projectOf = (briefId: string): MockProject => {
    const project = projects.get(briefId)
    if (!project) throw new ApiError(404, `개편 브리프를 찾을 수 없습니다: ${briefId}`)
    return project
  }

  const baseFloors = (p: MockProject) => {
    const base = world.revisions.get(p.brief.base_rev)
    if (!base) throw new ApiError(400, `기준 개정 ${p.brief.base_rev}가 없습니다.`)
    return base.floors
  }

  const salesByBrand = (p: MockProject) => {
    const rows = mockProductivityDetails(world, p.brief.base_rev, STAND_IN_FLOOR) ?? []
    const units = baseFloors(p)[STAND_IN_FLOOR]?.units ?? []
    const byUnit = new Map(rows.map((r) => [r.unit_code, r]))
    return { rows, sales: (brandId: string) => byUnit.get(units.find((u) => u.brandId === brandId)?.code ?? '')?.sales_krw ?? 0 }
  }

  const reportUrl = (p: MockProject): string | null => {
    if (typeof URL.createObjectURL !== 'function' || typeof Blob === 'undefined') return null
    if (p.report?.version === p.version) return p.report.url
    if (p.report?.url && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(p.report.url)
    const url = URL.createObjectURL(new Blob([reportHtml(p)], { type: 'text/html;charset=utf-8' }))
    p.report = { version: p.version, url }
    return url
  }

  const view = (p: MockProject): RelayoutProject =>
    structuredClone({
      brief: p.brief,
      status: p.status,
      diagnosis: p.diagnosis,
      plan: p.plan,
      selected_alt_id: p.selected_alt_id,
      decided_by: p.decided_by,
      decided_at: p.decided_at,
      check: p.check,
      phasing: p.phasing,
      report_url: reportUrl(p),
      updated_at: p.updated_at,
    })

  function diagnoseProject(p: MockProject): RelayoutDiagnosis {
    const { brief } = p
    const floor = baseFloors(p)[STAND_IN_FLOOR]
    const { rows } = salesByBrand(p)
    const byUnit = new Map(rows.map((r) => [r.unit_code, r]))
    const effective = brief.effective_date ?? '9999-12-31'
    const candidates = (floor?.units ?? [])
      .filter((u) => u.brandId)
      .map((u) => {
        const row = byUnit.get(u.code)
        const leaseEnd = mockLeaseEnd(u.brandId!)
        const reasons = [...(leaseEnd < effective ? ['계약 만료'] : []), ...(row?.low_efficiency ? ['저효율'] : [])]
        const brand = world.brands.get(u.brandId!)
        return {
          unit_code: u.code,
          floor: STAND_IN_FLOOR,
          brand_id: u.brandId,
          brand_name: brand?.name ?? u.brandId,
          category_name: categoryName(u.category),
          lease_end: leaseEnd,
          sales_per_m2: row?.sales_per_m2 ?? null,
          percentile: row?.percentile ?? null,
          low_efficiency: !!row?.low_efficiency,
          reasons,
        }
      })
      .filter((c) => c.reasons.length > 0)
      .sort((a, b) => b.reasons.length - a.reasons.length || (a.lease_end ?? '').localeCompare(b.lease_end ?? ''))
    const exitUnits = new Set(brief.exits.map((e) => e.unit_code).filter((c): c is string => !!c))
    const free = (floor?.units ?? []).filter((u) => !u.brandId || exitUnits.has(u.code))
    const tolerance = brief.area_tolerance ?? 0.15
    const factors = SEASON_FACTORS[brief.season] ?? SEASON_FACTORS.FW
    const salesPerM2 = rows.map((r) => r.sales_per_m2 ?? 0)
    return {
      season: seasonInfo(brief.season, brief.year ?? 2026),
      period: { start: '2025-10-01', end: MOCK_DATA_THROUGH, days: 365 },
      season_factors: factors.map(([category, factor]) => ({ category, category_name: categoryName(category), factor })),
      floors: [
        {
          floor: STAND_IN_FLOOR,
          sales_per_m2: salesPerM2.length ? Math.round(salesPerM2.reduce((a, b) => a + b, 0) / salesPerM2.length) : null,
          yoy_pct: 2.4,
          low_efficiency: rows.filter((r) => r.low_efficiency).length,
        },
      ],
      candidates,
      candidate_total: candidates.length,
      vacancies: (floor?.units ?? []).filter((u) => !u.brandId).map((u) => ({ unit_code: u.code, floor: STAND_IN_FLOOR, area_m2: rectArea(u.rect) })),
      entries: brief.entries.map((e) => {
        const [min, max] = e.target_area_m2 ?? [0, Infinity]
        const fits = free.filter((u) => rectArea(u.rect) >= min * (1 - tolerance) && rectArea(u.rect) <= max * (1 + tolerance)).map((u) => u.code)
        return { ...e, feasible_units: fits.length, feasible_unit_codes: fits, season_factor: factors.find(([c]) => c === e.category)?.[1] ?? null }
      }),
      exits: brief.exits,
      notes: [STAND_IN_NOTE, '계약 만료일은 모의 계약(leases) 데이터 기준입니다.', '평효율은 최근 12개월 거래로 계산했습니다.'],
    }
  }

  function planProject(p: MockProject, options: RelayoutPlanOptions): RelayoutPlan {
    const { brief } = p
    if (!p.diagnosis) {
      p.diagnosis = diagnoseProject(p)
      if (p.status === 'draft') p.status = 'diagnosed'
    }
    const objective = options.objective ?? (brief.objective === 'sales' ? 'sales' : 'commission')
    const maxMoves = Math.max(0, Math.round(options.max_moves ?? brief.max_moves ?? 6))
    const weeks = Math.max(0, options.downtime_weeks ?? brief.downtime_weeks ?? 2)
    const movable = options.movable ?? (brief.movable === 'all' ? 'all' : 'expiring')
    const count = Math.min(5, Math.max(1, Math.round(options.n_alternatives ?? 3)))
    const rate = objective === 'commission' ? DEFAULT_COMMISSION : 1
    const weight = SEASON_WEIGHT * rate
    const { rows, sales } = salesByBrand(p)
    const current = rows.reduce((sum, r) => sum + (r.sales_krw ?? 0), 0)
    const placed = p.diagnosis.entries.find((e) => (e.feasible_units ?? 0) > 0)
    const baselineSales = current - brief.exits.reduce((sum, e) => sum + sales(e.brand_id), 0) + (placed ? ENTRY_SALES : 0)
    const baselineGain = baselineSales - current
    const brandName = (id: string) => world.brands.get(id)?.name ?? id
    const categoryOf = (id: string) => categoryName(world.brands.get(id)?.category)

    const specs = [ALT_SPECS[0], ...ALT_SPECS.slice(1).filter((s) => s.moves.length <= maxMoves).slice(0, count)]
    const alternatives: RelayoutAlternative[] = specs.map((spec, index) => {
      const altId = `ALT-${index}`
      const revId = `${brief.brief_id}-A${index}`
      const seasonChanges: SeasonChange[] = [
        ...brief.exits.filter((e) => e.unit_code).map((e): SeasonChange => ({ type: 'exit', brandId: e.brand_id, from: e.unit_code!, reason: e.reason ?? '퇴점' })),
        ...spec.moves,
        ...(placed ? [{ type: 'entry', brandId: placed.brand_id, to: spec.entryUnit, reason: '신규 입점' } satisfies SeasonChange] : []),
      ]
      const revision = buildSeasonRevision(world, revId, brief.base_rev, seasonChanges, `${brief.title} ${altId}`)
      if (revision) {
        world.revisions.set(revId, revision)
        publish(revId)
      }
      const moveCost = Math.round(spec.moves.reduce((sum, m) => sum + (sales(m.brandId) / 52) * weeks * rate, 0))
      const objectiveDelta = Math.round(spec.sales[0] * weight)
      const explain = (c: SeasonChange, name: string): string => {
        if (c.type === 'exit') return `${name}(${c.from}) 퇴점: ${c.reason}. 지금 기대 매출 ${eok(sales(c.brandId))}/년이 빠집니다.`
        if (c.type === 'entry') {
          const factor = placed?.season_factor ?? 1
          const commission = placed?.commission_rate ?? DEFAULT_COMMISSION
          return `${name}(${c.to}) 입점: 기대 매출 ${eok(ENTRY_SALES)}/년, 시즌 지수 ${factor.toFixed(2)}, 수수료율 ${(commission * 100).toFixed(1)}%.`
        }
        return `${name}(${categoryOf(c.brandId)}) ${c.from}→${c.to}: ${c.reason}`
      }
      const changes: RelayoutChange[] = seasonChanges.map((c) => {
        const name = c.type === 'entry' && placed ? placed.brand_name : brandName(c.brandId)
        return {
          type: c.type,
          brand_id: c.brandId,
          brand_name: name,
          category_name: c.type === 'entry' && placed ? placed.category_name : categoryOf(c.brandId),
          from_unit: c.from ?? null,
          to_unit: c.to ?? null,
          from_floor: c.from ? STAND_IN_FLOOR : null,
          to_floor: c.to ? STAND_IN_FLOOR : null,
          delta_krw: c.type === 'exit' ? -sales(c.brandId) : c.type === 'entry' ? ENTRY_SALES : Math.round(spec.sales[0] / Math.max(1, spec.moves.length) / 2),
          explanation: explain(c, name),
        }
      })
      return {
        alt_id: altId,
        rev_id: revision ? revId : null,
        summary: spec.summary,
        kpi: {
          sales_delta_krw: spec.sales[0],
          sales_low_krw: spec.sales[1],
          sales_high_krw: spec.sales[2],
          objective_delta_krw: objectiveDelta,
          move_cost_krw: moveCost,
          net_krw: objectiveDelta - moveCost,
          vs_current_sales_krw: Math.round(baselineGain + spec.sales[0]),
          vs_current_objective_krw: Math.round(baselineGain * weight + objectiveDelta),
          moves: spec.moves.length,
        },
        changes,
        constraints: [
          { name: '앵커 고정', ok: true, detail: '앵커 브랜드는 옮기지 않습니다.' },
          { name: `최대 이동 ${maxMoves}건`, ok: spec.moves.length <= maxMoves, detail: `${spec.moves.length}건 이전` },
          {
            name: movable === 'expiring' ? '계약 만료 매장만 이전' : '이전 대상 제한 없음',
            ok: true,
            detail: spec.moves.length ? spec.moves.map((m) => `${brandName(m.brandId)} 계약 만료 ${mockLeaseEnd(m.brandId)}`).join(', ') : '이전 없음',
          },
          ...(spec.constraints ?? []),
        ],
        explanations: spec.explanations,
      }
    })

    return {
      params: {
        objective,
        season: brief.season,
        max_moves: maxMoves,
        downtime_weeks: weeks,
        movable,
        floors: brief.floors,
        n_alternatives: count,
        extra_exits: options.extra_exits ?? [],
        extra_entries: options.extra_entries ?? [],
      },
      current: { sales_krw: current, objective_krw: Math.round(current * weight) },
      baseline: { sales_krw: baselineSales, objective_krw: Math.round(baselineSales * weight), note: '퇴점·입점만 반영한 기준안' },
      alternatives,
      unplaced: p.diagnosis.entries
        .filter((e) => e !== placed)
        .map((e) => ({
          brand_id: e.brand_id,
          brand_name: e.brand_name,
          reason: e.target_area_m2
            ? `3층에 목표 면적(${e.target_area_m2[0]}~${e.target_area_m2[1]}㎡)에 맞는 빈 매장이 없습니다.`
            : '면적·선호 층에 맞는 빈 매장이 없습니다.',
        })),
      caveats: [
        '금액은 모두 연 단위 추정치입니다.',
        `이전 비용은 공사 기간(${weeks}주) 영업 손실만 반영했습니다. 인테리어 공사비는 넣지 않았습니다.`,
        STAND_IN_NOTE,
      ],
    }
  }

  function phaseProject(p: MockProject, body: RelayoutPhasingRequest): RelayoutPhasing {
    const plan = p.plan
    if (!plan) throw new ApiError(400, '먼저 대안을 만들어 주세요.')
    let alternative: RelayoutAlternative | undefined
    if (body.alt_id) {
      let wanted = body.alt_id.trim().toUpperCase()
      if (/^\d+$/.test(wanted)) wanted = `ALT-${wanted}`
      alternative = plan.alternatives.find((a) => a.alt_id === wanted)
      if (!alternative) throw new ApiError(400, `대안을 찾을 수 없습니다: ${body.alt_id}`)
    } else if (p.selected_alt_id) {
      alternative = plan.alternatives.find((a) => a.alt_id === p.selected_alt_id)
    }
    if (!alternative) {
      const options = plan.alternatives.filter((a) => a.alt_id !== 'ALT-0')
      alternative = (options.length ? options : plan.alternatives).reduce((best, a) => ((a.kpi.net_krw ?? -Infinity) > (best.kpi.net_krw ?? -Infinity) ? a : best))
    }
    const crews = body.crews ?? 2
    const weeks = body.fitout_weeks ?? Math.min(Math.max(p.brief.downtime_weeks ?? 2, 1), 12)
    const ratio = body.temporary_sales_ratio ?? 0.6
    if (!Number.isInteger(crews) || crews < 1 || crews > 6 || !(weeks >= 1 && weeks <= 12) || !(ratio >= 0 && ratio <= 1)) {
      throw new ApiError(400, 'crews는 1~6, fitout_weeks는 1~12, temporary_sales_ratio는 0~1이어야 합니다.')
    }
    const floor = baseFloors(p)[STAND_IN_FLOOR]
    const { rows } = salesByBrand(p)
    const result = planConstruction({
      altId: alternative.alt_id,
      changes: alternative.changes,
      occupancy: new Map((floor?.units ?? []).filter((u) => u.brandId).map((u) => [u.code, u.brandId!])),
      areas: new Map((floor?.units ?? []).map((u) => [u.code, rectArea(u.rect)])),
      annualSales: new Map(rows.map((r) => [r.unit_code, r.sales_krw ?? 0])),
      baseRev: p.brief.base_rev,
      crews,
      weeks,
      ratio,
      useTemporary: body.use_temporary ?? true,
    })
    return {
      brief_id: p.brief.brief_id,
      alt_id: alternative.alt_id,
      rev_id: alternative.rev_id,
      ...result,
      caveats: [...result.caveats, STAND_IN_NOTE],
      created_at: kstNow(),
    }
  }

  function checkProject(p: MockProject, rev: string): RelayoutCheck {
    if (!p.selected_alt_id || !p.plan) throw new ApiError(400, '선택한 대안이 없습니다. 먼저 대안을 결정해 주세요.')
    const actual = world.revisions.get(rev)
    if (!actual) throw new ApiError(404, `알 수 없는 개정: ${rev}`)
    if (actual.info.kind === 'hypothetical') throw new ApiError(400, '실제 개정만 대조할 수 있습니다.')
    const alt = p.plan.alternatives.find((a) => a.alt_id === p.selected_alt_id)
    if (!alt) throw new ApiError(400, `선택한 대안을 찾을 수 없습니다: ${p.selected_alt_id}`)
    const base = baseFloors(p)
    const items = alt.changes.map((c) => {
      const at = unitOf(actual.floors, c.brand_id)
      const status = c.type === 'exit' ? (at ? 'not_exited' : 'match') : at === c.to_unit ? 'match' : at ? 'different_unit' : 'missing'
      return {
        type: c.type,
        type_label: RELAYOUT_CHANGE_LABELS[c.type] ?? c.type,
        brand_name: c.brand_name,
        from_unit: c.from_unit,
        planned_unit: c.type === 'exit' ? null : c.to_unit,
        actual_unit: at,
        status,
        status_label: CHECK_STATUS_LABELS[status],
      }
    })
    const planned = new Set(alt.changes.flatMap((c) => [c.from_unit, c.to_unit]).filter((u): u is string => !!u))
    const brandName = (id: string | null) => (id ? (world.brands.get(id)?.name ?? id) : null)
    const unexpected: RelayoutCheck['unexpected'] = []
    const areaChanges: RelayoutCheck['area_changes'] = []
    for (const [code, floor] of Object.entries(actual.floors)) {
      const before = new Map((base[code]?.units ?? []).map((u) => [u.code, u]))
      for (const unit of floor.units) {
        const old = before.get(unit.code)
        if (!old) continue
        if (old.brandId !== unit.brandId && !planned.has(unit.code)) {
          unexpected.push({
            unit_code: unit.code,
            before_brand: old.brandId ?? null,
            before_brand_name: brandName(old.brandId ?? null),
            after_brand: unit.brandId ?? null,
            after_brand_name: brandName(unit.brandId ?? null),
          })
        }
        const [a0, a1] = [rectArea(old.rect), rectArea(unit.rect)]
        if (a0 > 0 && Math.abs(a1 - a0) / a0 > 0.005) {
          areaChanges.push({ unit_code: unit.code, before_m2: a0, after_m2: a1, delta_pct: round1(((a1 - a0) / a0) * 100) })
        }
      }
    }
    const matched = items.filter((i) => i.status === 'match').length
    const pct = items.length ? round1((matched / items.length) * 100) : 100
    const counts = (status: string) => items.filter((i) => i.status === status).length
    return {
      rev,
      plan_rev: alt.rev_id,
      alt_id: alt.alt_id,
      conformance_pct: pct,
      items,
      unexpected,
      area_changes: areaChanges,
      summary:
        `계획 ${items.length}건 중 ${matched}건이 도면과 일치합니다(${pct}%). ` +
        `다른 매장 ${counts('different_unit')}건, 없음 ${counts('missing')}건, 퇴점 안 됨 ${counts('not_exited')}건. ` +
        `계획에 없는 변경 ${unexpected.length}건, 면적 변경 ${areaChanges.length}건.`,
    }
  }

  return {
    listBriefs(): RelayoutBriefSummary[] {
      return [...projects.values()].map((p) => ({
        brief_id: p.brief.brief_id,
        title: p.brief.title,
        season: p.brief.season,
        year: p.brief.year,
        effective_date: p.brief.effective_date,
        base_rev: p.brief.base_rev,
        floors: [...p.brief.floors],
        objective: p.brief.objective,
        status: p.status,
        entries: p.brief.entries.length,
        exits: p.brief.exits.length,
        updated_at: p.updated_at,
      }))
    },
    getProject: (briefId: string) => view(projectOf(briefId)),
    diagnose(briefId: string): RelayoutDiagnosis {
      const p = projectOf(briefId)
      p.diagnosis = diagnoseProject(p)
      if (p.status === 'draft') p.status = 'diagnosed'
      touch(p)
      return structuredClone(p.diagnosis)
    },
    plan(briefId: string, options: RelayoutPlanOptions): RelayoutPlan {
      const p = projectOf(briefId)
      p.plan = planProject(p, options)
      // New alternatives replace the earlier decision, construction order and drawing check.
      p.selected_alt_id = null
      p.decided_by = null
      p.decided_at = null
      p.check = null
      p.phasing = null
      p.status = 'planned'
      touch(p)
      return structuredClone(p.plan)
    },
    select(briefId: string, body: RelayoutSelectRequest): RelayoutProject {
      const p = projectOf(briefId)
      if (!p.plan) throw new ApiError(400, '먼저 대안을 만들어 주세요.')
      const alt = p.plan.alternatives.find((a) => a.alt_id === body.alt_id)
      if (!alt) throw new ApiError(400, `대안을 찾을 수 없습니다: ${body.alt_id}`)
      const decidedBy = (body.decided_by ?? '').trim()
      if (!decidedBy) throw new ApiError(400, '결정자를 입력해 주세요.')
      p.selected_alt_id = alt.alt_id
      p.decided_by = decidedBy
      p.decided_at = kstNow()
      p.check = null
      // A construction order made for another alternative no longer applies.
      if (p.phasing?.alt_id !== alt.alt_id) p.phasing = null
      p.status = 'selected'
      touch(p)
      return view(p)
    },
    check(briefId: string, rev: string): RelayoutCheck {
      const p = projectOf(briefId)
      p.check = checkProject(p, rev)
      p.status = 'checked'
      touch(p)
      return structuredClone(p.check)
    },
    /** Construction order of an alternative (default: the decided one, else the best net effect); stored in the project. */
    phasing(briefId: string, body: RelayoutPhasingRequest): RelayoutPhasing {
      const p = projectOf(briefId)
      p.phasing = phaseProject(p, body)
      touch(p)
      return structuredClone(p.phasing)
    },
    reportUrl: (briefId: string) => {
      const p = projects.get(briefId)
      return p ? reportUrl(p) : null
    },
    /** Checks an uploaded revision against the decided plan of a brief based on its parent (job step). */
    checkUpload(rev: string, parentRev: string): UploadJob['relayout_check'] {
      for (const p of projects.values()) {
        if (!p.selected_alt_id || !p.plan || p.brief.base_rev !== parentRev) continue
        p.check = checkProject(p, rev)
        p.status = 'checked'
        touch(p)
        return { brief_id: p.brief.brief_id, alt_id: p.check.alt_id, conformance_pct: p.check.conformance_pct, summary: p.check.summary }
      }
      return null
    },
    /** A `relayout_brief` metadata upload (adds or replaces one brief, which starts over as a draft). */
    putBrief(fields: Partial<RelayoutBrief> & { brief_id: string }): void {
      const season = fields.season === 'SS' ? 'SS' : 'FW'
      const brief: RelayoutBrief = {
        ...structuredClone(FW2026_BRIEF),
        ...fields,
        season,
        season_label: season === 'SS' ? 'S/S(3~8월)' : 'F/W(9~2월)',
        title: fields.title || fields.brief_id,
        floors: fields.floors?.length ? fields.floors : FW2026_BRIEF.floors,
        notes: `업로드한 브리프입니다. ${STAND_IN_NOTE} 입점·퇴점 목록은 F/W 예시를 씁니다.`,
      }
      projects.set(brief.brief_id, fresh(brief, kstNow()))
    },
    briefIds: () => [...projects.keys()],
  }
}
