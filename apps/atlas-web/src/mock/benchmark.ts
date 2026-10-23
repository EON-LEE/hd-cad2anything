// Mock peer-store benchmark (GET /api/analytics/stores) and past relayout review
// (GET /api/analytics/relayout-review, GET /api/reports/relayout-review/{rev}). All stores are fictional;
// the flagship's figures come from the mock productivity of R2, peers are fixed multiples of them.
import { ApiError } from '../api/client'
import { CHANGE_LABELS } from '../lib/colors'
import type {
  RelayoutReview,
  RelayoutReviewItem,
  StoreComparison,
  StoreComparisonParams,
  StoreComparisonRow,
  StoreInfo,
} from '../types/api'
import { mockProductivityDetails } from './analytics'
import { MOCK_DATA_THROUGH, MOCK_FACILITY, MOCK_FLOORS, categoryName } from './catalog'
import { changesBetween, type MockWorld } from './world'

export const MOCK_STORES: readonly StoreInfo[] = [
  { store_id: 'FLAGSHIP', store_name: MOCK_FACILITY.name, region: '서울', format: '플래그십', flagship: true },
  { store_id: 'ST-E', store_name: '가상 동부점', region: '서울', format: '도심형', flagship: false },
  { store_id: 'ST-W', store_name: '가상 서부점', region: '경기', format: '교외형', flagship: false },
  { store_id: 'ST-S', store_name: '가상 남부점', region: '부산', format: '도심형', flagship: false },
  { store_id: 'ST-N', store_name: '가상 북부점', region: '대구', format: '아웃렛형', flagship: false },
]

/** Peer value = flagship value × factor (by floor when given). */
const PEER_FACTORS: Record<string, { base: number; floors?: Record<string, number> }> = {
  'ST-E': { base: 0.93, floors: { '3F': 1.06 } },
  'ST-W': { base: 0.8 },
  'ST-S': { base: 1.04, floors: { '1F': 1.09 } },
  'ST-N': { base: 0.66, floors: { '3F': 0.86 } },
}

const BENCH_REV = 'R2'
const MONTHS = 12

/** Rows of the mock `store_kpis` table: peers × months × floors × categories. */
export function mockStoreKpiRows(): number {
  const peers = MOCK_STORES.filter((s) => !s.flagship).length
  const categories = MOCK_FLOORS.reduce((sum, floor) => sum + floor.program.length, 0)
  return peers * MONTHS * categories
}

const METRIC_LABELS: Record<string, string> = { sales_per_m2: '평효율(원/㎡)', sales_krw: '연매출(원)' }

interface Group {
  key: string
  label: string
  floor: string | null
  sales: number
  area: number
}

function median(values: number[]): number | null {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

const round1 = (value: number) => Math.round(value * 10) / 10

export function mockStoreComparison(world: MockWorld, params: StoreComparisonParams = {}): StoreComparison {
  const level = params.level ?? 'store'
  if (!['store', 'floor', 'category'].includes(level)) throw new ApiError(400, `level은 store, floor, category 중 하나여야 합니다: ${level}`)
  const metric = params.metric ?? 'sales_per_m2'
  if (!METRIC_LABELS[metric]) throw new ApiError(400, `지원하지 않는 지표입니다: ${metric}`)
  const floors = MOCK_FLOORS.map((f) => f.code as string).filter((code) => !params.floor || code === params.floor)
  if (!floors.length) throw new ApiError(400, `모의 데이터에 없는 층입니다: ${params.floor}`)

  const groups = new Map<string, Group>()
  for (const floor of floors) {
    for (const row of mockProductivityDetails(world, BENCH_REV, floor) ?? []) {
      if (params.category && row.category !== params.category) continue
      const area = row.sales_per_m2 ? (row.sales_krw ?? 0) / row.sales_per_m2 : 0
      const key = level === 'store' ? 'ALL' : level === 'floor' ? floor : String(row.category)
      const label = level === 'store' ? '점포 전체' : level === 'floor' ? floor : categoryName(row.category)
      const group = groups.get(key) ?? { key, label, floor: level === 'floor' ? floor : null, sales: 0, area: 0 }
      group.sales += row.sales_krw ?? 0
      group.area += area
      groups.set(key, group)
    }
  }

  const rows: StoreComparisonRow[] = [...groups.values()].map((group) => {
    const flagship = metric === 'sales_krw' ? group.sales : group.area ? Math.round(group.sales / group.area / 1000) * 1000 : 0
    const values: Record<string, number | null> = {}
    for (const store of MOCK_STORES) {
      if (store.flagship) {
        values[store.store_id] = flagship
        continue
      }
      const factor = PEER_FACTORS[store.store_id]
      const tweak = (group.floor && factor.floors?.[group.floor]) || 1
      values[store.store_id] = Math.round((flagship * factor.base * tweak) / 1000) * 1000
    }
    const peers = MOCK_STORES.filter((s) => !s.flagship).map((s) => values[s.store_id] ?? 0)
    const peerMedian = median(peers)
    const all = Object.values(values).filter((v): v is number => v !== null)
    return {
      key: group.key,
      label: group.label,
      flagship,
      peer_median: peerMedian,
      rank: 1 + all.filter((v) => v > flagship).length,
      n_stores: all.length,
      gap_pct: peerMedian ? round1(((flagship - peerMedian) / peerMedian) * 100) : null,
      values,
    }
  })

  return {
    level,
    metric,
    metric_label: METRIC_LABELS[metric],
    period: { start: '2025-10-01', end: MOCK_DATA_THROUGH, days: 365 },
    stores: MOCK_STORES.map((s) => ({ ...s })),
    rows,
    notes: [`${MOCK_FACILITY.name} 실적은 거래에서, 다른 점포는 store_kpis 메타데이터에서 읽었습니다. (모의 데이터)`],
    caveat: '점포마다 면적 기준과 MD 구성이 달라 단순 비교에는 한계가 있습니다.',
  }
}

// --- past relayout review ----------------------------------------------------------------------

/** Sales drift of all units on a floor after a revision took effect (the control group's change). */
const FLOOR_DRIFT: Record<string, number> = { '1F': 0.034, '3F': 0.021 }

const addDays = (date: string, days: number) => {
  const d = new Date(`${date}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

const minDate = (a: string, b: string) => (a < b ? a : b)

export function mockRelayoutReview(world: MockWorld, rev: string, windowDays = 90): RelayoutReview {
  const revision = world.revisions.get(rev)
  if (!revision) throw new ApiError(404, `알 수 없는 개정: ${rev}`)
  const { info } = revision
  if (info.kind === 'hypothetical') throw new ApiError(400, '가상 개정은 실적이 없어 성과를 볼 수 없습니다.')
  const parent = info.parent_rev_id
  const effective = info.effective_date
  if (!parent || !effective) throw new ApiError(400, `${rev}는 이전 개정이 없어 비교할 수 없습니다.`)
  if (effective > MOCK_DATA_THROUGH) throw new ApiError(400, `${rev} 적용일(${effective}) 뒤의 거래 실적이 없습니다.`)
  const days = Math.min(365, Math.max(7, Math.round(windowDays)))

  const daily = (code: string, revId: string) => {
    const floor = code.split('-')[0]
    const row = (mockProductivityDetails(world, revId, floor) ?? []).find((r) => r.unit_code === code)
    return row?.sales_krw ? row.sales_krw / 365 : null
  }
  const brandOf = (code: string, revId: string) => {
    const unit = world.revisions.get(revId)?.floors[code.split('-')[0]]?.units.find((u) => u.code === code)
    return unit?.brandId ? (world.brands.get(unit.brandId)?.name ?? unit.brandId) : null
  }

  const items: RelayoutReviewItem[] = []
  for (const change of changesBetween(world, parent, rev)) {
    if (change.change_type === 'walkway_change') continue
    const before = change.unit_codes_before[0] ?? null
    const after = change.unit_codes_after[0] ?? null
    const code = after ?? before
    if (!code) continue
    const drift = FLOOR_DRIFT[change.floor] ?? 0.02
    const beforeDaily = before ? daily(before, parent) : null
    const rawAfter = after ? daily(after, rev) : null
    // Units whose geometry and brand did not change still get a little lift from the new layout.
    const afterDaily = rawAfter === null ? null : rawAfter * (1 + drift) * (rawAfter === beforeDaily ? 1.03 : 1)
    const changePct = beforeDaily && afterDaily !== null ? round1(((afterDaily - beforeDaily) / beforeDaily) * 100) : null
    const controlPct = round1(drift * 100)
    items.push({
      change_type: change.change_type,
      unit_code: code,
      floor: change.floor,
      brand_name: (after && brandOf(after, rev)) ?? (before && brandOf(before, parent)) ?? null,
      before_daily_krw: beforeDaily === null ? null : Math.round(beforeDaily),
      after_daily_krw: afterDaily === null ? null : Math.round(afterDaily),
      change_pct: changePct,
      control_pct: controlPct,
      vs_control_pct: changePct === null ? null : round1(changePct - controlPct),
    })
  }

  const average = (values: (number | null)[]) => {
    const known = values.filter((v): v is number => v !== null)
    return known.length ? round1(known.reduce((a, b) => a + b, 0) / known.length) : null
  }
  const types = [...new Set(items.map((i) => i.change_type))]
  const floors = [...new Set(items.map((i) => i.floor ?? ''))].filter(Boolean)
  const vsControl = average(items.map((i) => i.vs_control_pct))
  const before = { start: addDays(effective, -days), end: addDays(effective, -1) }
  const after = { start: effective, end: minDate(addDays(effective, days - 1), MOCK_DATA_THROUGH) }
  return {
    rev,
    parent_rev: parent,
    effective_date: effective,
    window: { before, after, days },
    items,
    by_type: types.map((type) => {
      const group = items.filter((i) => i.change_type === type)
      return { change_type: type, label: CHANGE_LABELS[type] ?? type, count: group.length, change_pct: average(group.map((i) => i.change_pct)), vs_control_pct: average(group.map((i) => i.vs_control_pct)) }
    }),
    by_floor: floors.map((floor) => {
      const group = items.filter((i) => i.floor === floor)
      return { floor, count: group.length, control_pct: group[0]?.control_pct ?? null, vs_control_pct: average(group.map((i) => i.vs_control_pct)) }
    }),
    summary:
      `${rev}(${revision.notes}) 적용일 ${effective} 앞뒤 ${days}일의 일평균 매출을 비교했어요. 바뀐 매장 ${items.length}곳` +
      (vsControl === null ? '을 비교할 실적이 부족해요.' : `은 비교군보다 평균 ${Math.abs(vsControl)}%p ${vsControl >= 0 ? '높아요' : '낮아요'}.`),
    caveat: '같은 층에서 바뀌지 않은 매장을 비교군으로 쓴 단순 비교예요. 인과 효과가 아니에요. (모의 데이터)',
  }
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]!)
}

const won = (value: number | null) => (value === null ? '–' : `${Math.round(value).toLocaleString('ko-KR')}원`)
const pct = (value: number | null) => (value === null ? '–' : `${value > 0 ? '+' : ''}${value}%`)

/** HTML of GET /api/reports/relayout-review/{rev} for the mock (opened from a blob URL). */
export function relayoutReviewHtml(review: RelayoutReview): string {
  const rows = review.items
    .map(
      (i) =>
        `<tr><td>${escapeHtml(CHANGE_LABELS[i.change_type] ?? i.change_type)}</td><td>${escapeHtml(i.unit_code)}</td><td>${escapeHtml(i.brand_name ?? '–')}</td>` +
        `<td>${won(i.before_daily_krw)}</td><td>${won(i.after_daily_krw)}</td><td>${pct(i.change_pct)}</td><td>${pct(i.vs_control_pct)}</td></tr>`,
    )
    .join('')
  return [
    '<!doctype html><html lang="ko"><head><meta charset="utf-8">',
    `<title>${escapeHtml(review.rev)} 개편 성과 보고서</title>`,
    '<style>body{font-family:sans-serif;margin:32px;line-height:1.5}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style>',
    '</head><body>',
    `<h1>${escapeHtml(review.rev)} 개편 성과 보고서 (${review.window.days ?? '–'}일, 모의 데이터)</h1>`,
    `<p>${escapeHtml(review.summary)}</p>`,
    `<p>이전 ${escapeHtml(review.window.before?.start ?? '–')}~${escapeHtml(review.window.before?.end ?? '–')} · 이후 ${escapeHtml(review.window.after?.start ?? '–')}~${escapeHtml(review.window.after?.end ?? '–')}</p>`,
    '<table><thead><tr><th>변경</th><th>매장</th><th>브랜드</th><th>이전 일평균</th><th>이후 일평균</th><th>변화</th><th>비교군 대비</th></tr></thead>',
    `<tbody>${rows}</tbody></table>`,
    `<p>${escapeHtml(review.caveat ?? '')}</p>`,
    '</body></html>',
  ].join('')
}
