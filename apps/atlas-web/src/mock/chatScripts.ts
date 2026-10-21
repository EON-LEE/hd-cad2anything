// Scripted AG-UI event streams for mock mode. Each script emits tool-call chips and STATE_DELTA
// patches exactly like the backend would (see docs/specs/atlas-api.md §2-3).
import { CHANGE_COLORS, CHANGE_LABELS, CHANGE_ORDER, DEFAULT_HIGHLIGHT_COLOR } from '../lib/colors'
import { APPROVAL_MODE_LABELS, RISK_LABELS, formatKrw, formatNumber } from '../lib/format'
import type { PatchOperation } from '../lib/jsonPatch'
import type { AgUiEvent, ChatRequest, GraphEdge, Highlight } from '../types/api'
import { mockLayoutAlternatives, mockProductivityDetails, planImpact } from './analytics'
import { categoryName } from './catalog'
import { changesBetween, type MockWorld } from './world'

export interface ScriptStep {
  event: AgUiEvent
  /** Delay before emitting this event, in milliseconds (scaled by the mock speed). */
  delay: number
}

let speedScale = 1

/** Scale all mock delays (0 = as fast as possible; used by tests). */
export function setMockSpeed(scale: number): void {
  speedScale = Math.max(0, scale)
}

export function getMockSpeed(): number {
  return speedScale
}

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
  const scaled = ms * speedScale
  if (scaled <= 0) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, scaled)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/** Split text into small streaming chunks, preferring word boundaries. */
export function chunkText(text: string): string[] {
  const chunks: string[] = []
  let current = ''
  for (const ch of text) {
    current += ch
    if ((current.length >= 4 && /[\s,.·:)\n]/.test(ch)) || current.length >= 7) {
      chunks.push(current)
      current = ''
    }
  }
  if (current) chunks.push(current)
  return chunks
}

class ScriptBuilder {
  readonly steps: ScriptStep[] = []
  private readonly threadId: string
  private readonly runId: string
  private counter = 0

  constructor(threadId: string, runId: string) {
    this.threadId = threadId
    this.runId = runId
    this.steps.push({ event: { type: 'RUN_STARTED', thread_id: threadId, run_id: runId }, delay: 150 })
  }

  say(text: string): this {
    const id = `msg-${this.runId}-${this.counter++}`
    this.steps.push({ event: { type: 'TEXT_MESSAGE_START', message_id: id }, delay: 120 })
    for (const delta of chunkText(text)) this.steps.push({ event: { type: 'TEXT_MESSAGE_CONTENT', message_id: id, delta }, delay: 18 })
    this.steps.push({ event: { type: 'TEXT_MESSAGE_END', message_id: id }, delay: 20 })
    return this
  }

  tool(name: string, args: unknown, summary: string, duration = 650): this {
    const id = `call-${this.runId}-${this.counter++}`
    this.steps.push({ event: { type: 'TOOL_CALL_START', tool_call_id: id, tool_name: name, args }, delay: 160 })
    this.steps.push({ event: { type: 'TOOL_CALL_END', tool_call_id: id, summary }, delay: duration })
    return this
  }

  /** A UI function tool call (ui_*) that the backend turns into a STATE_DELTA. */
  ui(name: string, args: unknown, delta: PatchOperation[], summary: string): this {
    const id = `call-${this.runId}-${this.counter++}`
    this.steps.push({ event: { type: 'TOOL_CALL_START', tool_call_id: id, tool_name: name, args }, delay: 120 })
    this.steps.push({ event: { type: 'STATE_DELTA', delta }, delay: 120 })
    this.steps.push({ event: { type: 'TOOL_CALL_END', tool_call_id: id, summary }, delay: 60 })
    return this
  }

  finish(): ScriptStep[] {
    this.steps.push({ event: { type: 'RUN_FINISHED', thread_id: this.threadId, run_id: this.runId }, delay: 80 })
    return this.steps
  }
}

const replace = (path: string, value: unknown): PatchOperation => ({ op: 'replace', path, value })

function detectFloor(message: string, fallback: string): string {
  const korean = /(\d+)\s*층/.exec(message)
  if (korean) return `${korean[1]}F`
  const code = /\b(B\d|\d{1,2}F)\b/i.exec(message)
  return code ? code[1].toUpperCase() : fallback
}

function brandNameAt(world: MockWorld, rev: string, code: string): string {
  const floor = code.split('-')[0]
  const u = world.revisions.get(rev)?.floors[floor]?.units.find((x) => x.code === code)
  return u?.brandId ? (world.brands.get(u.brandId)?.name ?? u.brandId) : '(공실)'
}

function unitExists(world: MockWorld, rev: string, code: string): boolean {
  const floor = code.split('-')[0]
  return !!world.revisions.get(rev)?.floors[floor]?.units.some((u) => u.code === code)
}

// --- scripts -----------------------------------------------------------------------------------

function revisionScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const message = req.message
  let to = req.ui_state.rev
  if (/S\/S|에스에스|봄/i.test(message)) to = 'R1'
  else if (/F\/W|가을/i.test(message)) to = 'R2'
  const from = world.revisions.get(to)?.info.parent_rev_id
  if (!from) {
    return b.say(`${to}은(는) 기준 개정이라 비교할 이전 개정이 없습니다. 다른 개정을 선택한 뒤 다시 요청해 주세요.`).finish()
  }
  const items = changesBetween(world, from, to)
  const impact = planImpact(items)
  const counts = new Map<string, number>()
  for (const item of items) counts.set(item.change_type, (counts.get(item.change_type) ?? 0) + 1)
  const countText = CHANGE_ORDER.filter((t) => counts.has(t))
    .map((t) => `${CHANGE_LABELS[t]} ${counts.get(t)}`)
    .join(' · ')
  const notes = world.revisions.get(to)?.notes ?? ''

  const floorCounts = new Map<string, number>()
  for (const item of items) floorCounts.set(item.floor, (floorCounts.get(item.floor) ?? 0) + 1)
  const focusFloor = floorCounts.has(req.ui_state.floor)
    ? req.ui_state.floor
    : ([...floorCounts.entries()].sort((x, y) => y[1] - x[1])[0]?.[0] ?? req.ui_state.floor)

  const highlights: Highlight[] = []
  const edges: GraphEdge[] = []
  for (const item of items.filter((i) => i.floor === focusFloor)) {
    for (const code of new Set([...item.unit_codes_after, ...item.unit_codes_before])) {
      if (!unitExists(world, to, code) || highlights.some((h) => h.unit_code === code)) continue
      highlights.push({ unit_code: code, color: CHANGE_COLORS[item.change_type], reason: `${CHANGE_LABELS[item.change_type]}: ${item.summary}` })
    }
    if (item.change_type === 'move') {
      edges.push({
        from: item.unit_codes_before[0],
        to: item.unit_codes_after[0],
        weight: 2,
        label: `${String(item.details?.brand_name ?? '')} 이전`,
        color: CHANGE_COLORS.move,
      })
    }
  }

  b.say(`${notes}(${to})를 직전 개정 ${from}과 비교하고 생성기 레지스트리로 재생성 영향을 분석하겠습니다.\n`)
  b.tool('revision.diff', { rev_from: from, rev_to: to }, `변경 ${items.length}건: ${countText}`, 800)
  b.tool(
    'registry.plan_impact',
    { rev_from: from, rev_to: to },
    `재생성 대상 ${impact.length}개 (게시 전 승인 ${impact.filter((i) => i.approval_mode === 'pre_publish').length}개)`,
    500,
  )
  b.ui('ui_compare', { rev: to, compare_rev: from }, [replace('/rev', to), replace('/compareRev', from), replace('/heatmap', null)], `${from} → ${to} 비교 표시`)
  b.ui(
    'ui_highlight_units',
    { floor: focusFloor, unit_codes: highlights.map((h) => h.unit_code) },
    [replace('/floor', focusFloor), replace('/highlights', highlights), replace('/edges', edges)],
    `${focusFloor} 변경 매장 ${highlights.length}개 강조`,
  )
  b.ui('ui_focus', { unit_codes: highlights.map((h) => h.unit_code) }, [replace('/focus', { unit_codes: highlights.map((h) => h.unit_code) })], '변경 구역으로 이동')
  b.ui('ui_show_panel', { type: 'changes' }, [replace('/panel', { type: 'changes', payload: { rev_from: from, rev_to: to } })], '개정 검토 패널 열기')

  const lines = [`**${notes}(${from} → ${to}) 변경점 ${items.length}건**`]
  for (const type of CHANGE_ORDER) {
    const group = items.filter((i) => i.change_type === type)
    if (!group.length) continue
    lines.push(`- ${CHANGE_LABELS[type]} ${group.length}건: ${group.map((g) => g.summary).join(' / ')}`)
  }
  lines.push('', `**재생성 대상 생성기 ${impact.length}개**`)
  for (const entry of impact) {
    lines.push(
      `- ${entry.title_ko} (${RISK_LABELS[entry.risk_tier] ?? entry.risk_tier} · ${APPROVAL_MODE_LABELS[entry.approval_mode] ?? entry.approval_mode}) — ${entry.floors.join(', ')}`,
    )
  }
  lines.push('', `도면에서 ${focusFloor} 변경 매장을 유형별 색으로 강조했고, '개정 검토' 탭에 변경 목록과 승인 대상을 열어 두었습니다. 고위험 산출물은 게시 전에 승인해야 합니다.`)
  return b.say(lines.join('\n')).finish()
}

function productivityScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const floor = detectFloor(req.message, req.ui_state.floor)
  const details = mockProductivityDetails(world, rev, floor)
  if (!details?.length) return b.say(`${rev} ${floor}의 생산성 데이터를 찾지 못했습니다.`).finish()
  const sorted = [...details].sort((x, y) => (x.percentile ?? 0) - (y.percentile ?? 0))
  const low = sorted.slice(0, 2)
  const best = sorted.at(-1)!
  const floorAvg = details.reduce((acc, d) => acc + d.distance_to_escalator_m, 0) / details.length
  const lowAvg = low.reduce((acc, d) => acc + d.distance_to_escalator_m, 0) / low.length
  const values = Object.fromEntries(details.map((d) => [d.unit_code, d.normalized]))
  const otherFloor = floor === '1F' ? '3F-C01' : '1F-C03'
  const highlights: Highlight[] = low.map((d) => ({
    unit_code: d.unit_code,
    color: DEFAULT_HIGHLIGHT_COLOR,
    reason: `저효율 ${d.percentile}백분위 · ${d.driver}`,
  }))
  const edges: GraphEdge[] = [
    { from: low[0].unit_code, to: best.unit_code, weight: 1.3, label: 'lift 1.3' },
    { from: low[1].unit_code, to: best.unit_code, weight: 1.1, label: 'lift 1.1' },
    { from: low[0].unit_code, to: otherFloor, weight: 0.9, label: 'lift 0.9' },
  ]
  const codes = low.map((d) => d.unit_code)

  b.say(`${rev} 기준 ${floor} 매장별 평효율을 계산하고 하위 매장의 노출 조건을 확인하겠습니다.\n`)
  b.tool('analytics.productivity', { rev, floor, level: 'unit' }, `${details.length}개 매장 평효율 산출 · 하위 20% ${details.filter((d) => d.low_efficiency).length}개`, 900)
  b.tool(
    'spatial.exposure',
    { rev, floor, unit_codes: codes },
    `저효율 매장의 에스컬레이터 평균 거리 ${lowAvg.toFixed(0)} m (층 평균 ${floorAvg.toFixed(0)} m)`,
    600,
  )
  b.tool('analytics.cross_shopping', { level: 'unit', focal: codes.join(','), segment: '' }, '저효율 매장과 상위 매장의 교차구매 lift 0.9~1.3', 500)
  b.ui(
    'ui_show_heatmap',
    { metric: 'sales_per_m2', floor },
    [replace('/floor', floor), replace('/compareRev', null), replace('/heatmap', { metric: 'sales_per_m2', label: '평효율(원/㎡)', values })],
    '평효율 heatmap 표시',
  )
  b.ui('ui_highlight_units', { unit_codes: codes }, [replace('/highlights', highlights)], `저효율 매장 ${codes.length}개 강조`)
  b.ui('ui_draw_edges', { edges: edges.length }, [replace('/edges', edges)], `교차구매 연결 ${edges.length}개 표시`)
  b.ui('ui_focus', { unit_codes: codes }, [replace('/focus', { unit_codes: codes })], '저효율 구역으로 이동')

  const lines = [`**${floor} 저효율 매장과 원인 (${rev})**`]
  for (const d of low) {
    lines.push(
      `- ${d.unit_code} ${d.brand_name}: 평효율 ${formatNumber(d.sales_per_m2)}원/㎡ (${d.percentile}백분위, 연매출 ${formatKrw(d.sales_krw)}). 원인: ${d.driver}`,
    )
  }
  lines.push(
    '',
    `공통 원인은 동선 노출입니다. 두 매장의 에스컬레이터 평균 거리는 ${lowAvg.toFixed(0)} m로 층 평균 ${floorAvg.toFixed(0)} m보다 멉니다. 최고 효율 매장 ${best.unit_code}(${best.brand_name})과의 교차구매 lift도 1.1~1.3에 그칩니다.`,
    '',
    `도면을 평효율 heatmap으로 칠하고 두 매장을 강조했습니다. 재배치 대안이 필요하면 "${floor === '1F' ? '1층' : '3층'} 재배치안 3개 비교"라고 요청하세요.`,
  )
  return b.say(lines.join('\n')).finish()
}

function crossShoppingScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const pairs: [string, string, number][] = [
    ['3F-C01', '1F-C03', 2.4],
    ['3F-S03', '1F-S01', 2.1],
    ['3F-N02', '3F-C06', 1.9],
    ['3F-C06', '1F-W01', 1.7],
    ['3F-C01', '3F-S03', 1.6],
  ]
  const edges: GraphEdge[] = pairs.map(([from, to, lift]) => ({ from, to, weight: lift, label: `lift ${lift.toFixed(1)}` }))
  const focal = '3F-C01'
  b.say('40대 VIP·VVIP 고객의 브랜드 교차구매를 분석하겠습니다.\n')
  b.tool(
    'analytics.cross_shopping',
    { level: 'brand', focal: brandNameAt(world, rev, focal), segment: 'age_band:40s;tier:VIP|VVIP' },
    `상위 연관 ${pairs.length}쌍 · 최대 lift ${Math.max(...pairs.map((p) => p[2])).toFixed(1)}`,
    900,
  )
  b.ui(
    'ui_draw_edges',
    { edges: edges.length },
    [replace('/floor', '3F'), replace('/heatmap', null), replace('/compareRev', null), replace('/edges', edges)],
    `교차구매 연결 ${edges.length}개 표시`,
  )
  b.ui(
    'ui_highlight_units',
    { unit_codes: [focal] },
    [replace('/highlights', [{ unit_code: focal, color: '#8e24aa', reason: '40대 VIP 교차구매 중심 브랜드' }])],
    '중심 브랜드 강조',
  )
  b.ui('ui_focus', { unit_codes: ['3F-C01', '3F-S03', '3F-N02', '3F-C06'] }, [replace('/focus', { unit_codes: ['3F-C01', '3F-S03', '3F-N02', '3F-C06'] })], '연관 매장으로 이동')
  const lines = ['**40대 VIP·VVIP 교차구매 상위 5쌍**']
  for (const [from, to, lift] of pairs) {
    lines.push(`- ${brandNameAt(world, rev, from)}(${from}) ↔ ${brandNameAt(world, rev, to)}(${to}): lift ${lift.toFixed(1)}`)
  }
  lines.push(
    '',
    `중심 브랜드는 ${brandNameAt(world, rev, focal)}입니다. 1층 화장품·명품과의 층간 연관이 강하므로 3F↔1F 연계 프로모션과 에스컬레이터 동선 안내가 유효합니다. 다른 층 매장은 도면 가장자리의 층 배지로 연결했고, '관계 그래프' 탭에서 전체 관계를 볼 수 있습니다.`,
  )
  return b.say(lines.join('\n')).finish()
}

function layoutScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const floor = detectFloor(req.message, req.ui_state.floor)
  const constraints = { max_moves: 3, keep_anchors: true }
  const alternatives = mockLayoutAlternatives(world, { rev, floors: [floor], constraints })
  if (!alternatives.length) return b.say(`${rev} ${floor}에는 생성할 수 있는 재배치 대안이 없습니다.`).finish()
  const violations = alternatives.reduce((acc, a) => acc + a.constraints.filter((c) => !c.ok).length, 0)
  const low = Math.min(...alternatives.map((a) => a.kpi_delta.low))
  const high = Math.max(...alternatives.map((a) => a.kpi_delta.high))
  const best = [...alternatives].sort((x, y) => y.kpi_delta.sales_krw - x.kpi_delta.sales_krw)[0]
  const moved = [...new Set(best.moves.flatMap((m) => [m.from_unit, m.to_unit]))]
  b.say(`${floor} 재배치 대안을 제약 조건(최대 이동 3, 앵커 고정)으로 생성하고 KPI 영향을 추정하겠습니다.\n`)
  b.tool('layout.generate_alternatives', { rev, floors: [floor], constraints }, `대안 ${alternatives.length}개 생성 · 제약 위반 ${violations}건`, 1100)
  b.tool('whatif.estimate_kpi', { alternatives: alternatives.map((a) => a.id) }, `매출 영향 범위 ${formatKrw(low, true)} ~ ${formatKrw(high, true)}`, 700)
  b.ui(
    'ui_show_panel',
    { type: 'layout' },
    [replace('/floor', floor), replace('/heatmap', null), replace('/compareRev', null), replace('/panel', { type: 'layout', payload: { rev, floors: [floor], alternatives } })],
    '레이아웃 대안 패널 열기',
  )
  b.ui(
    'ui_highlight_units',
    { unit_codes: moved },
    [
      replace(
        '/highlights',
        best.moves.map((m) => ({ unit_code: m.to_unit, color: CHANGE_COLORS.move, reason: `${best.id}: ${m.brand_name} 도착 예정` })),
      ),
      replace(
        '/edges',
        best.moves.map((m) => ({ from: m.from_unit, to: m.to_unit, weight: 2, label: m.brand_name, color: CHANGE_COLORS.move })),
      ),
      replace('/focus', { unit_codes: moved }),
    ],
    `${best.id} 이동 경로 표시`,
  )
  const lines = [`**${floor} 재배치 대안 ${alternatives.length}개 비교 (${rev} 기준)**`]
  for (const a of alternatives) {
    const failed = a.constraints.filter((c) => !c.ok).map((c) => c.name)
    lines.push(
      `- ${a.id}: 이동 ${a.moves.length}건, 목적함수 ${a.objective_before.toFixed(2)}→${a.objective_after.toFixed(2)}, 매출 ${formatKrw(a.kpi_delta.sales_krw, true)} (${formatKrw(a.kpi_delta.low, true)}~${formatKrw(a.kpi_delta.high, true)})${failed.length ? `, 제약 위반: ${failed.join(', ')}` : ', 제약 모두 충족'}`,
    )
  }
  lines.push('', `추천안은 ${best.id}입니다. 도면에 ${best.id}의 이동 경로를 표시했고, '레이아웃 대안' 탭에서 다른 대안을 미리볼 수 있습니다. 예측 범위는 합성 데이터 기반 추정치입니다.`)
  return b.say(lines.join('\n')).finish()
}

function unitScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder, codes: string[]): ScriptStep[] {
  const rev = req.ui_state.rev
  const floor = codes[0].split('-')[0]
  const details = mockProductivityDetails(world, rev, floor) ?? []
  const rows = details.filter((d) => codes.includes(d.unit_code))
  b.tool('analytics.productivity', { rev, floor, level: 'unit', unit_codes: codes }, `${rows.length}개 매장 KPI 조회`, 600)
  b.ui(
    'ui_highlight_units',
    { unit_codes: codes },
    [
      replace('/floor', floor),
      replace('/highlights', codes.map((c) => ({ unit_code: c, color: '#1e88e5', reason: '질문 대상 매장' }))),
      replace('/focus', { unit_codes: codes }),
    ],
    `${codes.length}개 매장 강조`,
  )
  const lines = rows.length
    ? rows.map(
        (d) =>
          `- ${d.unit_code} ${d.brand_name}(${categoryName(d.category)}): 평효율 ${formatNumber(d.sales_per_m2)}원/㎡, ${d.percentile}백분위, 연매출 ${formatKrw(d.sales_krw)} — ${d.driver}`,
      )
    : ['- 해당 매장의 KPI가 없습니다(공실이거나 다른 개정에 없는 매장).']
  return b.say([`**${codes.join(', ')} 요약 (${rev})**`, ...lines].join('\n')).finish()
}

function helpScript(_world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const selection = req.ui_state.selection
  if (selection.length) {
    b.ui('ui_focus', { unit_codes: selection }, [replace('/focus', { unit_codes: selection })], '선택 매장으로 이동')
  }
  return b
    .say(
      [
        '지금은 내장 모의 데이터로 동작하고 있어 아래 질문에 시나리오 응답을 드릴 수 있습니다.',
        '- "S/S 개편 도면 변경점과 재생성 대상 알려줘": 개정 diff, 영향 분석, 승인 대상',
        '- "3층 저효율 zone과 원인": 평효율 heatmap과 원인 분석',
        '- "40대 VIP 교차구매 보여줘": 교차구매 관계와 층간 연결',
        '- "3층 재배치안 3개 비교": 레이아웃 대안과 KPI 범위',
        '- "3층 평효율 표로 보여줘": 매장별 평효율 표(panel.type = table)',
        '- 매장 코드(예: 3F-C02)를 넣어 질문하면 해당 매장 KPI를 요약합니다.',
        selection.length ? `\n선택한 매장 ${selection.join(', ')}로 화면을 이동했습니다.` : '',
      ].join('\n'),
    )
    .finish()
}

function tableScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const floor = detectFloor(req.message, req.ui_state.floor)
  const details = mockProductivityDetails(world, rev, floor)
  if (!details?.length) return b.say(`${rev} ${floor}의 생산성 데이터를 찾지 못했습니다.`).finish()
  const rows = [...details]
    .sort((x, y) => (y.sales_per_m2 ?? 0) - (x.sales_per_m2 ?? 0))
    .map((d) => [d.unit_code, d.brand_name, categoryName(d.category), d.sales_per_m2, d.percentile, d.driver])
  b.tool('analytics.productivity', { rev, floor, level: 'unit' }, `${rows.length}개 매장 평효율 산출`, 700)
  b.ui(
    'ui_show_panel',
    { type: 'table' },
    [
      replace('/floor', floor),
      replace('/panel', {
        type: 'table',
        payload: { title: `${floor} 매장별 평효율 (${rev})`, columns: ['매장', '브랜드', '카테고리', '평효율(원/㎡)', '백분위', '요인'], rows },
      }),
    ],
    '평효율 표 열기',
  )
  return b.say(`${floor} 매장 ${rows.length}개의 평효율을 높은 순으로 표에 정리했습니다. 표는 도면 아래쪽에 열려 있고, 닫기 버튼으로 닫을 수 있습니다.`).finish()
}

export function buildMockScript(world: MockWorld, req: ChatRequest, threadId: string, runId: string): ScriptStep[] {
  const b = new ScriptBuilder(threadId, runId)
  const message = req.message
  if (/변경|재생성|개정|S\/S|F\/W/i.test(message)) return revisionScript(world, req, b)
  if (/재배치|대안|레이아웃/.test(message)) return layoutScript(world, req, b)
  if (/표로|표 |테이블|table/i.test(message)) return tableScript(world, req, b)
  if (/저효율|효율|원인|zone|존/i.test(message)) return productivityScript(world, req, b)
  if (/교차|VIP|연관/i.test(message)) return crossShoppingScript(world, req, b)
  const codes = [...new Set(message.toUpperCase().match(/\b(?:B\d|\d{1,2}F)-[A-Z]\d{2}[A-Z]?\b/g) ?? [])].filter((c) =>
    unitExists(world, req.ui_state.rev, c),
  )
  if (codes.length) return unitScript(world, req, b, codes)
  return helpScript(world, req, b)
}

export async function playScript(steps: ScriptStep[], onEvent: (event: AgUiEvent) => void, signal?: AbortSignal): Promise<void> {
  for (const step of steps) {
    await sleep(step.delay, signal)
    onEvent(step.event)
  }
}
