// Scripted AG-UI event streams for mock mode. Like the backend (docs/specs/atlas-api.md §2-3), each
// script sends labelled tool calls (nested calls inside a specialist agent), STATE_DELTA patches for
// UI function tools and exactly one assistant message streamed in small deltas.
import { CHANGE_COLORS, CHANGE_LABELS, CHANGE_ORDER, DEFAULT_HIGHLIGHT_COLOR } from '../lib/colors'
import {
  APPROVAL_MODE_LABELS,
  RISK_LABELS,
  floorLabel,
  formatKrw,
  formatManwon,
  formatMinutes,
  formatNumber,
  formatWalk,
  withEuro,
} from '../lib/format'
import { segmentParam, transitionDirection, transitionText, viaLabel } from '../lib/journey'
import type { PatchOperation } from '../lib/jsonPatch'
import { QUICK_PROMPTS } from '../lib/prompts'
import { routeFloors } from '../lib/route'
import type { AgUiEvent, ChatRequest, GraphEdge, Highlight, RoutePlace } from '../types/api'
import { mockLayoutAlternatives, mockProductivityDetails, planImpact } from './analytics'
import { categoryName } from './catalog'
import { mockJourney, pathLength } from './journey'
import { mockRoute, placePhrase, roundWalk } from './route'
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

/** Split text into small streaming chunks (at most 5 characters, preferring word boundaries). */
export function chunkText(text: string): string[] {
  const chunks: string[] = []
  let current = ''
  let length = 0
  for (const ch of text) {
    current += ch
    length += 1
    if ((length >= 2 && /[\s,.·:)\n]/.test(ch)) || length >= 5) {
      chunks.push(current)
      current = ''
      length = 0
    }
  }
  if (current) chunks.push(current)
  return chunks
}

interface NestedCall {
  name: string
  label: string
  args: unknown
  summary: string
  duration?: number
}

const json = (value: unknown) => JSON.stringify(value)

class ScriptBuilder {
  readonly steps: ScriptStep[] = []
  private readonly threadId: string
  private readonly runId: string
  private counter = 0

  constructor(threadId: string, runId: string) {
    this.threadId = threadId
    this.runId = runId
    this.steps.push({ event: { type: 'RUN_STARTED', thread_id: threadId, run_id: runId }, delay: 120 })
  }

  private push(event: AgUiEvent, delay: number): void {
    this.steps.push({ event, delay })
  }

  private nextId(prefix: string): string {
    return `${prefix}-${this.runId}-${this.counter++}`
  }

  /** The model "thinks" a little longer before its first action. */
  private pause(normal: number): number {
    return this.steps.length <= 1 ? 600 : normal
  }

  tool(name: string, label: string, args: unknown, summary: string, duration = 700): this {
    const id = this.nextId('call')
    this.push({ type: 'TOOL_CALL_START', tool_call_id: id, tool_name: name, label, args }, this.pause(250))
    this.push({ type: 'TOOL_CALL_END', tool_call_id: id, summary, ok: true }, duration)
    return this
  }

  /** consult_specialist with the specialist's own tool calls nested under it. */
  consult(agent: string, label: string, args: unknown, calls: NestedCall[], summary: string): this {
    const id = this.nextId('call')
    this.push({ type: 'TOOL_CALL_START', tool_call_id: id, tool_name: 'consult_specialist', label, args }, this.pause(250))
    for (const call of calls) {
      const child = this.nextId('call')
      this.push(
        { type: 'TOOL_CALL_START', tool_call_id: child, tool_name: call.name, label: call.label, args: call.args, parent_tool_call_id: id, agent },
        400,
      )
      this.push({ type: 'TOOL_CALL_END', tool_call_id: child, summary: call.summary, ok: true }, call.duration ?? 700)
    }
    this.push({ type: 'TOOL_CALL_END', tool_call_id: id, summary, ok: true }, 500)
    return this
  }

  /** A UI function tool call (ui_*) that the backend turns into a STATE_DELTA. */
  ui(name: string, label: string, args: unknown, delta: PatchOperation[], summary = json({ ok: true })): this {
    const id = this.nextId('call')
    this.push({ type: 'TOOL_CALL_START', tool_call_id: id, tool_name: name, label, args }, this.pause(200))
    this.push({ type: 'STATE_DELTA', delta }, 180)
    this.push({ type: 'TOOL_CALL_END', tool_call_id: id, summary, ok: true }, 80)
    return this
  }

  /** The run's single assistant message, streamed in small deltas, then RUN_FINISHED. */
  answer(text: string): ScriptStep[] {
    const id = this.nextId('msg')
    this.push({ type: 'TEXT_MESSAGE_START', message_id: id }, this.pause(450))
    for (const delta of chunkText(text)) this.push({ type: 'TEXT_MESSAGE_CONTENT', message_id: id, delta }, 22)
    this.push({ type: 'TEXT_MESSAGE_END', message_id: id }, 20)
    this.push({ type: 'RUN_FINISHED', thread_id: this.threadId, run_id: this.runId }, 60)
    return this.steps
  }
}

const replace = (path: string, value: unknown): PatchOperation => ({ op: 'replace', path, value })

/** Later heatmap, highlight, edge and compare displays replace a drawn journey or route (contract §3). */
const CLEAR_PATHS: readonly PatchOperation[] = [replace('/journey', null), replace('/route', null)]

function detectFloor(message: string, fallback: string): string {
  const basement = /지하\s*(\d+)\s*층/.exec(message)
  if (basement) return `B${basement[1]}`
  const korean = /(\d+)\s*층/.exec(message)
  if (korean) return `${korean[1]}F`
  const code = /\b(B\d|\d{1,2}F)\b/i.exec(message)
  return code ? code[1].toUpperCase() : fallback
}

/** The mock world only has 1F and 3F: other floors fall back to 3F with a note for the answer. */
function resolveFloor(world: MockWorld, rev: string, requested: string): { floor: string; note: string } {
  const floors = world.revisions.get(rev)?.floors ?? {}
  if (floors[requested]) return { floor: requested, note: '' }
  const floor = floors['3F'] ? '3F' : (Object.keys(floors)[0] ?? requested)
  return { floor, note: `모의 데이터에는 ${floorLabel(requested)} 도면이 없어서 ${withEuro(floorLabel(floor))} 대신 보여 드릴게요.` }
}

function unitAt(world: MockWorld, rev: string, code: string) {
  return world.revisions.get(rev)?.floors[code.split('-')[0]]?.units.find((u) => u.code === code)
}

function brandNameAt(world: MockWorld, rev: string, code: string): string {
  const u = unitAt(world, rev, code)
  return u?.brandId ? (world.brands.get(u.brandId)?.name ?? u.brandId) : '(공실)'
}

function unitExists(world: MockWorld, rev: string, code: string): boolean {
  return !!unitAt(world, rev, code)
}

// --- scripts -----------------------------------------------------------------------------------

function revisionScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const message = req.message
  let to = req.ui_state.rev
  if (/S\/S|에스에스|봄/i.test(message)) to = 'R1'
  else if (/F\/W|가을/i.test(message)) to = 'R2'
  const from = world.revisions.get(to)?.info.parent_rev_id
  if (!from) {
    return b.answer(`${to}은(는) 기준 개정이라 비교할 이전 개정이 없어요. 다른 개정을 고른 뒤 다시 물어봐 주세요.`)
  }
  const items = changesBetween(world, from, to)
  const impact = planImpact(items)
  const counts = new Map<string, number>()
  for (const item of items) counts.set(item.change_type, (counts.get(item.change_type) ?? 0) + 1)
  const countText = CHANGE_ORDER.filter((t) => counts.has(t))
    .map((t) => `${CHANGE_LABELS[t]} ${counts.get(t)}`)
    .join(' · ')
  const notes = world.revisions.get(to)?.notes ?? to

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
  const codes = highlights.map((h) => h.unit_code)

  b.tool(
    'revision_diff',
    `${notes}(${to}) 도면 변경 내역 확인`,
    { rev_from: from, rev_to: to },
    json({ ok: true, rev_from: from, rev_to: to, changes: items.length, by_type: Object.fromEntries(counts) }),
    900,
  )
  b.tool(
    'registry_impact',
    '다시 만들 산출물 확인',
    { rev_from: from, rev_to: to },
    json({ ok: true, generators: impact.map((i) => ({ name: i.generator, risk: i.risk_tier, approval: i.approval_mode })) }),
    600,
  )
  b.ui('ui_compare', `${from} → ${to} 비교 표시`, { rev_from: from, rev_to: to }, [
    replace('/rev', to),
    replace('/compareRev', from),
    replace('/heatmap', null),
    ...CLEAR_PATHS,
  ])
  b.ui('ui_highlight_units', `${floorLabel(focusFloor)} 변경 매장 ${codes.length}곳 강조`, { unit_codes: codes }, [
    replace('/floor', focusFloor),
    replace('/highlights', highlights),
    replace('/edges', edges),
  ])
  b.ui('ui_focus', '변경 구역으로 확대', { unit_codes: codes }, [replace('/focus', { unit_codes: codes })])
  b.ui('ui_show_panel', '변경 목록 열기', { type: 'changes' }, [replace('/panel', { type: 'changes', payload: { rev_from: from, rev_to: to } })])

  const lines = [`${notes}(${to})에서는 직전 개정(${from})보다 **${items.length}건**이 바뀌었어요. (${countText})`, '', '### 주요 변경']
  for (const type of CHANGE_ORDER) {
    const group = items.filter((i) => i.change_type === type)
    if (group.length) lines.push(`- **${CHANGE_LABELS[type]} ${group.length}건**: ${group.map((g) => g.summary).join(' / ')}`)
  }
  lines.push('', '### 다시 만들어야 하는 산출물')
  for (const entry of impact) {
    lines.push(
      `- ${entry.title_ko} — ${RISK_LABELS[entry.risk_tier] ?? entry.risk_tier}, ${APPROVAL_MODE_LABELS[entry.approval_mode] ?? entry.approval_mode} (${entry.floors.join(', ')})`,
    )
  }
  lines.push(
    '',
    `도면에는 ${floorLabel(focusFloor)} 변경 매장을 유형별 색으로 표시했고, '개정 검토' 탭에 변경 목록과 승인 대상을 열어 두었어요. 고위험 산출물은 게시 전에 승인이 필요해요.`,
  )
  return b.answer(lines.join('\n'))
}

function productivityScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const { floor, note } = resolveFloor(world, rev, detectFloor(req.message, req.ui_state.floor))
  const details = mockProductivityDetails(world, rev, floor)
  if (!details?.length) return b.answer(`${rev} ${floorLabel(floor)}의 생산성 데이터를 찾지 못했어요.`)
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
  const place = floorLabel(floor)

  b.consult(
    'space-productivity-analyst',
    '공간 생산성 전문 에이전트에게 확인',
    { agent: 'space_productivity_analyst', request: `${rev} ${floor} 저효율 매장과 원인을 분석해 주세요.` },
    [
      {
        name: 'analytics_productivity',
        label: `${place} 공간 생산성 계산`,
        args: { rev, floor, level: 'unit' },
        summary: json({ ok: true, units: details.length, low_efficiency: details.filter((d) => d.low_efficiency).length }),
        duration: 900,
      },
      {
        name: 'analytics_drivers',
        label: '효율 저하 원인 분석',
        args: { rev, floor, unit_codes: codes },
        summary: json({ ok: true, escalator_distance_m: { low_units: Math.round(lowAvg), floor_mean: Math.round(floorAvg) } }),
        duration: 700,
      },
      {
        name: 'analytics_cross_shopping',
        label: '저효율 매장 교차구매 확인',
        args: { level: 'unit', focal: codes.join(','), segment: '' },
        summary: json({ ok: true, pairs: edges.length, lift_range: [0.9, 1.3] }),
        duration: 500,
      },
    ],
    json({ ok: true, agent: 'space_productivity_analyst', chars: 812, tool_calls: 3 }),
  )
  b.ui('ui_show_heatmap', `${place} 도면을 평효율로 색칠`, { metric: 'sales_per_m2', floor }, [
    replace('/floor', floor),
    replace('/compareRev', null),
    replace('/heatmap', { metric: 'sales_per_m2', label: '평효율(원/㎡)', values }),
    ...CLEAR_PATHS,
  ])
  b.ui('ui_highlight_units', `저효율 매장 ${codes.length}곳 강조`, { unit_codes: codes }, [replace('/highlights', highlights)])
  b.ui('ui_draw_edges', `교차구매 관계선 ${edges.length}개 표시`, { edges: edges.length }, [replace('/edges', edges)])
  b.ui('ui_focus', '저효율 매장으로 확대', { unit_codes: codes }, [replace('/focus', { unit_codes: codes })])

  const lines = note ? [note, ''] : []
  lines.push(`${rev} 기준 ${place}에서 평효율이 가장 낮은 매장 두 곳이에요.`, '')
  for (const d of low) {
    lines.push(
      `- **${d.brand_name}** (${d.unit_code}) — ㎡당 ${formatNumber(d.sales_per_m2)}원, 층 내 ${d.percentile}백분위, 연매출 ${formatKrw(d.sales_krw)}`,
    )
  }
  lines.push(
    '',
    '### 왜 낮을까요?',
    `- **동선 노출이 약해요.** 두 매장은 에스컬레이터까지 평균 ${lowAvg.toFixed(0)}m로, 층 평균(${floorAvg.toFixed(0)}m)보다 멀어요.`,
    `- **주변 매장과의 연계가 약해요.** 이 층 최고 효율 매장 ${best.brand_name}(${best.unit_code}) 고객의 교차구매 lift가 1.1~1.3에 그쳐요.`,
    `- 매장별로는 ${low.map((d) => `${d.brand_name}: ${d.driver}`).join(' / ')}`,
    '',
    `도면을 평효율로 색칠하고 두 매장을 빨간 테두리로 표시했어요. 재배치 아이디어가 필요하면 "${place} 재배치안 3개 만들어 비교해줘"라고 물어봐 주세요.`,
  )
  return b.answer(lines.join('\n'))
}

function categoryAt(world: MockWorld, rev: string, code: string): string {
  return categoryName(unitAt(world, rev, code)?.category)
}

function crossShoppingScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const cosmetics = /화장품|코스메틱|뷰티/.test(req.message)
  const segment = 'age_band:40s;tier:VIP|VVIP'
  const pairs: [string, string, number][] = cosmetics
    ? [
        ['1F-C03', '3F-C01', 2.4],
        ['1F-C03', '1F-E01', 2.1],
        ['1F-W01', '3F-C06', 1.9],
        ['1F-N03', '1F-S02', 1.7],
        ['1F-C03', '3F-C05', 1.6],
      ]
    : [
        ['3F-C01', '1F-C03', 2.4],
        ['3F-S03', '1F-S01', 2.1],
        ['3F-N02', '3F-C06', 1.9],
        ['3F-C06', '1F-W01', 1.7],
        ['3F-C01', '3F-S03', 1.6],
      ]
  const floor = cosmetics ? '1F' : '3F'
  const edges: GraphEdge[] = pairs.map(([from, to, lift]) => ({ from, to, weight: lift, label: `lift ${lift.toFixed(1)}` }))
  const onFloor = [...new Set(pairs.flatMap(([a, c]) => [a, c]))].filter((code) => code.startsWith(`${floor}-`))
  const centers = cosmetics ? [...new Set(pairs.map(([a]) => a))] : ['3F-C01']
  const highlights: Highlight[] = centers.map((code) => ({
    unit_code: code,
    color: '#8e24aa',
    reason: cosmetics ? '40대 VIP 교차구매의 기준 매장(화장품)' : '40대 VIP 교차구매 중심 브랜드',
  }))
  const maxLift = Math.max(...pairs.map((p) => p[2])).toFixed(1)

  b.tool(
    'analytics_cross_shopping',
    cosmetics ? '40대 VIP 고객의 화장품 교차구매 분석' : '40대 VIP 고객의 교차구매 분석',
    { level: cosmetics ? 'category' : 'brand', focal: cosmetics ? 'COSMETICS' : brandNameAt(world, rev, '3F-C01'), segment },
    json({ ok: true, segment, pairs: pairs.length, max_lift: Number(maxLift), n_customers: 1284 }),
    1000,
  )
  b.ui('ui_draw_edges', `교차구매 관계선 ${edges.length}개 표시`, { edges: edges.length }, [
    replace('/floor', floor),
    replace('/heatmap', null),
    replace('/compareRev', null),
    replace('/edges', edges),
    ...CLEAR_PATHS,
  ])
  b.ui('ui_highlight_units', cosmetics ? `화장품 매장 ${highlights.length}곳 강조` : '중심 브랜드 강조', { unit_codes: centers }, [
    replace('/highlights', highlights),
  ])
  b.ui('ui_focus', '연관 매장으로 확대', { unit_codes: onFloor }, [replace('/focus', { unit_codes: onFloor })])

  const pairLine = ([a, c, lift]: [string, string, number]) =>
    `- ${brandNameAt(world, rev, a)} + **${brandNameAt(world, rev, c)}** (${categoryAt(world, rev, c)}, ${floorLabel(c.split('-')[0])}) — lift ${lift.toFixed(1)}`
  const lines = cosmetics
    ? [
        '40대 VIP·VVIP 고객이 화장품과 함께 많이 산 매장들이에요. lift는 우연히 함께 살 때보다 몇 배 자주 같이 샀는지를 뜻해요.',
        '',
        ...pairs.map(pairLine),
        '',
        '### 이렇게 활용해 보세요',
        `- **3층 여성 컨템포러리와의 연계가 가장 강해요** (lift ${maxLift}). 1층 화장품 구매 고객에게 3층 혜택을 주는 층간 연계 프로모션을 검토할 만해요.`,
        '- 같은 1층의 주얼리·시계, 명품과도 자주 함께 사요. 화장품 매장과 가까운 동선 배치가 유리해요.',
        '',
        "도면에는 1층 화장품 매장을 중심으로 관계선을 그렸어요. 3층 매장은 오른쪽 가장자리의 층 배지로 이어지고, '관계 그래프' 탭에서 전체 관계를 볼 수 있어요.",
      ]
    : [
        '40대 VIP·VVIP 고객의 브랜드 교차구매 상위 5쌍이에요.',
        '',
        ...pairs.map(pairLine),
        '',
        `중심 브랜드는 ${brandNameAt(world, rev, '3F-C01')}예요. 1층 화장품·명품과의 층간 연관이 강해서 3층↔1층 연계 프로모션과 에스컬레이터 동선 안내가 효과적이에요. 다른 층 매장은 도면 가장자리의 층 배지로 연결했고, '관계 그래프' 탭에서 전체 관계를 볼 수 있어요.`,
      ]
  return b.answer(lines.join('\n'))
}

function layoutScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const { floor, note } = resolveFloor(world, rev, detectFloor(req.message, req.ui_state.floor))
  const place = floorLabel(floor)
  const constraints = { max_moves: 3, keep_anchors: true }
  const alternatives = mockLayoutAlternatives(world, { rev, floors: [floor], constraints })
  if (!alternatives.length) return b.answer(`${rev} ${place}에서는 만들 수 있는 재배치 대안이 없어요.`)
  const violations = alternatives.reduce((acc, a) => acc + a.constraints.filter((c) => !c.ok).length, 0)
  const low = Math.min(...alternatives.map((a) => a.kpi_delta.low))
  const high = Math.max(...alternatives.map((a) => a.kpi_delta.high))
  const best = [...alternatives].sort((x, y) => y.kpi_delta.sales_krw - x.kpi_delta.sales_krw)[0]
  const moved = [...new Set(best.moves.flatMap((m) => [m.from_unit, m.to_unit]))]

  b.tool(
    'layout_generate_alternatives',
    `${place} 재배치 대안 계산`,
    { rev, floors: [floor], constraints },
    json({ ok: true, alternatives: alternatives.map((a) => a.id), constraint_violations: violations }),
    1200,
  )
  b.tool(
    'whatif_score_moves',
    '이동 효과 추정',
    { alternatives: alternatives.map((a) => a.id) },
    json({ ok: true, sales_krw: { low, high } }),
    800,
  )
  b.ui('ui_show_panel', '재배치 대안 열기', { type: 'layout' }, [
    replace('/floor', floor),
    replace('/heatmap', null),
    replace('/compareRev', null),
    replace('/panel', { type: 'layout', payload: { rev, floors: [floor], alternatives } }),
  ])
  b.ui('ui_highlight_units', `${best.id} 이동 경로 표시`, { unit_codes: moved }, [
    replace(
      '/highlights',
      best.moves.map((m) => ({ unit_code: m.to_unit, color: CHANGE_COLORS.move, reason: `${best.id}: ${m.brand_name} 도착 예정` })),
    ),
    replace(
      '/edges',
      best.moves.map((m) => ({ from: m.from_unit, to: m.to_unit, weight: 2, label: m.brand_name, color: CHANGE_COLORS.move })),
    ),
    replace('/focus', { unit_codes: moved }),
    ...CLEAR_PATHS,
  ])

  const lines = note ? [note, ''] : []
  lines.push(
    `${place} 재배치 대안 ${alternatives.length}개를 만들었어요. (${rev} 기준, 최대 3곳 이동, 앵커 매장 고정)`,
    '',
    '| 대안 | 이동 | 예상 매출 변화 (범위) | 제약 조건 |',
    '|---|---|---|---|',
  )
  for (const a of alternatives) {
    const failed = a.constraints.filter((c) => !c.ok).map((c) => c.name)
    lines.push(
      `| ${a.id} | ${a.moves.length}건 | ${formatKrw(a.kpi_delta.sales_krw, true)} (${formatKrw(a.kpi_delta.low, true)} ~ ${formatKrw(a.kpi_delta.high, true)}) | ${failed.length ? `위반: ${failed.join(', ')}` : '모두 충족'} |`,
    )
  }
  lines.push(
    '',
    `**추천: ${best.id}** — 이동 ${best.moves.length}건으로 매출이 ${formatKrw(best.kpi_delta.sales_krw, true)} 바뀔 것으로 추정돼요.`,
    '',
    `도면에 ${best.id}의 이동 경로를 그렸고, '레이아웃 대안' 탭에서 다른 대안도 미리 볼 수 있어요. 예측 범위는 합성 데이터로 만든 추정치예요.`,
  )
  return b.answer(lines.join('\n'))
}

/** Segment filter mentioned in a question ("40대 여성 VIP" -> {age_band: "40s", gender: "F", tier: "VIP"}). */
export function segmentFromMessage(message: string): Record<string, string> {
  const segment: Record<string, string> = {}
  const age = /([2-9])0\s*대/.exec(message)
  if (age) segment.age_band = Number(age[1]) >= 6 ? '60s+' : `${age[1]}0s`
  if (/여성|여자/.test(message)) segment.gender = 'F'
  else if (/남성|남자/.test(message)) segment.gender = 'M'
  if (/VVIP/i.test(message)) segment.tier = 'VVIP'
  else if (/VIP/i.test(message)) segment.tier = 'VIP'
  return segment
}

function journeyScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const segment = segmentFromMessage(req.message)
  const args = { segment: segmentParam(segment), rank: 1 }
  const journey = mockJourney(world, { segment, rank: 1 })
  if (!journey) return b.answer('조건에 맞는 고객 동선을 찾지 못했어요. 조건을 바꿔 다시 물어봐 주세요.')
  const stops = journey.stops
  const s = journey.summary
  const firstFloor = stops[0].floor

  b.tool(
    'analytics_customer_journey',
    '고객 동선 찾기',
    args,
    json({ ok: true, customer: journey.customer, date: journey.date, stops: stops.length, floors: s.floors, total_krw: s.total_krw }),
    1000,
  )
  b.ui('ui_clear', '이전 분석 표시 지우기', { what: 'all' }, [
    replace('/heatmap', null),
    replace('/highlights', []),
    replace('/edges', []),
    replace('/journey', null),
    replace('/route', null),
    replace('/focus', null),
    replace('/compareRev', null),
  ])
  b.ui(
    'ui_show_journey',
    '도면에 고객 동선 그리기',
    args,
    [replace('/journey', journey), replace('/floor', firstFloor), replace('/panel', { type: 'journey', payload: {} })],
    json({ ok: true, shown: `${firstFloor} 도면에 동선과 결제 순서를 표시하고 동선 목록을 열었습니다.` }),
  )

  const lines = [
    `${journey.customer?.segment ?? ''} 고객 한 분(${journey.customer?.id ?? '가명'})의 ${journey.date}(${journey.weekday}) 하루예요. ${formatMinutes(s.duration_min)} 동안 ${s.floors.length}개 층에서 ${s.stops}번 결제했고, 모두 ${formatManwon(s.total_krw)}을 썼어요.`,
    '',
  ]
  stops.forEach((stop, i) => {
    const move = journey.transitions.find((t) => t.to_order === stop.order)
    const prefix = move ? `${transitionText(move)} → ` : ''
    lines.push(
      `${i + 1}. ${prefix}**${stop.time}** ${floorLabel(stop.floor)} ${stop.brand_name ?? stop.unit_code} (${stop.category_name ?? ''}) · ${formatManwon(stop.amount_krw)}`,
    )
  })
  const biggest = [...stops].sort((x, y) => (y.amount_krw ?? 0) - (x.amount_krw ?? 0))[0]
  const cafe = stops.findIndex((stop) => stop.category === 'CAFE')
  lines.push('', '### 눈여겨볼 점', `- 가장 큰 결제는 ${floorLabel(biggest.floor)} ${biggest.brand_name}(${biggest.category_name})의 ${formatManwon(biggest.amount_krw)}이었어요.`)
  if (cafe >= 0 && cafe < stops.length - 1) {
    lines.push(`- ${floorLabel(stops[cafe].floor)} 카페에 들른 뒤에도 결제가 ${stops.length - 1 - cafe}번 더 이어졌어요. 쉬어 갈 곳이 쇼핑 시간을 늘려 주는 흐름이에요.`)
  }
  lines.push(
    '',
    "도면에 이동 경로(주황 선)와 결제 순서를 표시했어요. 왼쪽 카드에서 결제 항목을 누르면 그 매장으로 이동하고, '다른 고객 보기'로 비슷한 고객의 하루를 볼 수 있어요.",
    '',
    '참고: 결제 기록의 순서로 추정한 경로라서 실제로 걸은 길과 다를 수 있어요.',
  )
  return b.answer(lines.join('\n'))
}

const UNIT_CODE_PATTERN = /\b(?:B\d|\d{1,2}F)-[A-Z]\d{2}[A-Z]?\b/g

/** Asking the way: "정문에서 3층까지 가는 길 알려줘", "카페 어떻게 가?", "3F-C05까지 경로". */
const ROUTE_PATTERN = /가는\s*(?:길|방법)|길\s*(?:찾|안내|알려)|찾아\s*가|어떻게\s*가|최단\s*경로|까지\s*(?:의\s*)?(?:경로|길)/

/** Store to walk to when a question names only a floor ("3층 매장까지"). */
const DEFAULT_ROUTE_DESTINATIONS: Readonly<Record<string, string>> = { '3F': '3F-C05', '1F': '1F-C05' }

/** The last floor named in a question ("1층 정문에서 3층 카페까지" -> 3F); null when none. */
export function lastFloorMention(message: string): string | null {
  let floor: string | null = null
  for (const match of message.matchAll(/지하\s*(\d+)\s*층|(\d+)\s*층|\b(B\d|\d{1,2}F)\b/gi)) {
    floor = match[1] ? `B${match[1]}` : match[2] ? `${match[2]}F` : match[3].toUpperCase()
  }
  return floor
}

/** Unit of a brand named in the question, if any. */
function unitByBrand(world: MockWorld, rev: string, message: string): string | null {
  const text = message.toLowerCase()
  for (const floor of Object.values(world.revisions.get(rev)?.floors ?? {})) {
    for (const unit of floor.units) {
      const name = unit.brandId ? world.brands.get(unit.brandId)?.name : undefined
      if (name && text.includes(name.toLowerCase())) return unit.code
    }
  }
  return null
}

/** "정문", "Cafe Halune": a place's name without its code, for step labels. */
function shortPlaceName(place: RoutePlace): string {
  if (place.kind === 'entrance') return place.label.split(' ').slice(1).join(' ') || place.label
  return place.label.replace(/\s*\([^)]*\)$/, '') || place.label
}

function routeScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const message = req.message
  const origin = /서측|서쪽/.test(message) ? 'ENT-2' : /동측|동쪽/.test(message) ? 'ENT-3' : 'ENT-1'
  const via = /엘리베이터/.test(message) ? 'elevator' : 'escalator'
  let note = ''
  let destination =
    [...new Set(message.toUpperCase().match(UNIT_CODE_PATTERN) ?? [])].find((c) => unitExists(world, rev, c)) ??
    unitByBrand(world, rev, message)
  if (!destination) {
    const resolved = resolveFloor(world, rev, lastFloorMention(message) ?? '3F')
    note = resolved.note
    destination = DEFAULT_ROUTE_DESTINATIONS[resolved.floor] ?? null
  }
  const route = destination ? mockRoute(world, { rev, origin, destination, via }) : null
  if (!route) {
    return b.answer('출발지나 목적지를 도면에서 찾지 못했어요. 매장 코드(예: 3F-C05)나 출입구(정문, 서측 출입구)를 넣어 다시 물어봐 주세요.')
  }
  const start = route.origin
  const end = route.destination
  const args = { rev, origin: start.ref, destination: end.ref, via }
  const change = route.transitions[0]

  b.tool(
    'spatial_route',
    `${shortPlaceName(start)}에서 ${floorLabel(end.floor)} ${shortPlaceName(end)}까지 경로 계산`,
    args,
    json({ ok: true, walk_m: route.walk_m, floors: routeFloors(route), via: change?.via ?? null }),
    900,
  )
  b.ui(
    'ui_show_route',
    '도면에 경로 그리기',
    args,
    [
      replace('/heatmap', null),
      replace('/highlights', []),
      replace('/edges', []),
      replace('/compareRev', null),
      replace('/journey', null),
      replace('/route', route),
      replace('/floor', start.floor),
    ],
    json({ ok: true, shown: `${start.floor} 도면에 경로와 출발 지점을 표시했습니다.` }),
  )

  const lines = note ? [note, ''] : []
  lines.push(
    `${placePhrase(start)}에서 ${floorLabel(end.floor)} ${placePhrase(end)}까지 가는 길이에요. 걷는 거리는 약 ${formatWalk(route.walk_m)}예요.`,
    '',
  )
  const [leave, arrive] = route.segments
  if (change && arrive) {
    const steps: string[] = []
    const toLink = roundWalk(pathLength(leave.points))
    if (toLink > 0) steps.push(`${floorLabel(change.from_floor)} ${placePhrase(start)}에서 ${viaLabel(change.via)}(${change.via})까지 약 ${formatWalk(toLink)} 걸어요.`)
    steps.push(
      `${via === 'elevator' ? '엘리베이터' : '에스컬레이터'}를 타고 ${withEuro(floorLabel(change.to_floor))} ${transitionDirection(change) < 0 ? '내려가요' : '올라가요'}. (${change.floors}개 층)`,
    )
    const fromLink = roundWalk(pathLength(arrive.points))
    if (fromLink > 0) steps.push(`${floorLabel(change.to_floor)}에서 내려 ${placePhrase(end)}까지 약 ${formatWalk(fromLink)} 더 걸어요.`)
    steps.forEach((step, i) => lines.push(`${i + 1}. ${step}`))
  } else {
    lines.push(`1. ${floorLabel(start.floor)} ${placePhrase(start)}에서 ${placePhrase(end)}까지 약 ${formatWalk(route.walk_m)} 걸어요.`)
  }
  lines.push(
    '',
    '도면에 경로를 파란 선으로 그렸어요. 초록 원이 출발, 파란 원이 도착 지점이에요. 왼쪽 위 길 찾기 상자에서 층을 누르면 그 층의 경로를 볼 수 있어요.',
    '',
    '참고: 통로망 최단 경로로 계산한 거리라서 실제로 걷는 거리와 조금 다를 수 있어요.',
  )
  return b.answer(lines.join('\n'))
}

function unitScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder, codes: string[]): ScriptStep[] {
  const rev = req.ui_state.rev
  const floor = codes[0].split('-')[0]
  const details = mockProductivityDetails(world, rev, floor) ?? []
  const rows = details.filter((d) => codes.includes(d.unit_code))
  b.tool('analytics_productivity', `${codes.join(', ')} 매장 성과 조회`, { rev, floor, level: 'unit', unit_codes: codes }, json({ ok: true, rows: rows.length }), 600)
  b.ui('ui_highlight_units', `매장 ${codes.length}곳 강조`, { unit_codes: codes }, [
    replace('/floor', floor),
    replace('/highlights', codes.map((c) => ({ unit_code: c, color: '#1e88e5', reason: '질문 대상 매장' }))),
    replace('/focus', { unit_codes: codes }),
    ...CLEAR_PATHS,
  ])
  const lines = rows.length
    ? rows.map(
        (d) =>
          `- **${d.brand_name}** (${d.unit_code}, ${categoryName(d.category)}) — ㎡당 ${formatNumber(d.sales_per_m2)}원, ${d.percentile}백분위, 연매출 ${formatKrw(d.sales_krw)}. ${d.driver}`,
      )
    : ['- 이 매장의 성과 데이터가 없어요. 공실이거나 이 개정에 없는 매장이에요.']
  return b.answer([`${rev} 기준으로 요약했어요.`, '', ...lines, '', '도면에서 해당 매장을 파란 테두리로 표시했어요.'].join('\n'))
}

function helpScript(_world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const selection = req.ui_state.selection
  if (selection.length) {
    b.ui('ui_focus', '선택한 매장으로 확대', { unit_codes: selection }, [replace('/focus', { unit_codes: selection })])
  }
  return b.answer(
    [
      '지금은 내장 모의 데이터로 동작하고 있어서 아래 질문에 시나리오 답변을 드릴 수 있어요.',
      '',
      ...QUICK_PROMPTS.map((prompt) => `- ${prompt}`),
      '- 3층 평효율 표로 보여줘',
      '- 매장 코드(예: 3F-C02)를 넣어 물어보면 그 매장의 성과를 요약해요.',
      ...(selection.length ? ['', `선택한 매장 ${selection.join(', ')}(으)로 화면을 옮겼어요.`] : []),
    ].join('\n'),
  )
}

function tableScript(world: MockWorld, req: ChatRequest, b: ScriptBuilder): ScriptStep[] {
  const rev = req.ui_state.rev
  const { floor, note } = resolveFloor(world, rev, detectFloor(req.message, req.ui_state.floor))
  const details = mockProductivityDetails(world, rev, floor)
  if (!details?.length) return b.answer(`${rev} ${floorLabel(floor)}의 생산성 데이터를 찾지 못했어요.`)
  const rows = [...details]
    .sort((x, y) => (y.sales_per_m2 ?? 0) - (x.sales_per_m2 ?? 0))
    .map((d) => [d.unit_code, d.brand_name, categoryName(d.category), d.sales_per_m2, d.percentile, d.driver])
  b.tool('analytics_productivity', `${floorLabel(floor)} 공간 생산성 계산`, { rev, floor, level: 'unit' }, json({ ok: true, units: rows.length }), 700)
  b.ui('ui_show_panel', '평효율 표 열기', { type: 'table' }, [
    replace('/floor', floor),
    replace('/panel', {
      type: 'table',
      payload: { title: `${floor} 매장별 평효율 (${rev})`, columns: ['매장', '브랜드', '카테고리', '평효율(원/㎡)', '백분위', '요인'], rows },
    }),
  ])
  const lines = note ? [note, ''] : []
  lines.push(`${floorLabel(floor)} 매장 ${rows.length}곳의 평효율을 높은 순으로 표에 정리했어요. 표는 도면 아래쪽에 열려 있고, × 버튼으로 닫을 수 있어요.`)
  return b.answer(lines.join('\n'))
}

export function buildMockScript(world: MockWorld, req: ChatRequest, threadId: string, runId: string): ScriptStep[] {
  const b = new ScriptBuilder(threadId, runId)
  const message = req.message
  if (/변경|재생성|개정|바뀌|S\/S|F\/W/i.test(message)) return revisionScript(world, req, b)
  if (/재배치|대안|레이아웃/.test(message)) return layoutScript(world, req, b)
  if (/표로|표 |테이블|table/i.test(message)) return tableScript(world, req, b)
  if (ROUTE_PATTERN.test(message)) return routeScript(world, req, b)
  if (/동선|경로|여정|journey/i.test(message)) return journeyScript(world, req, b)
  if (/저효율|효율|원인|zone|존/i.test(message)) return productivityScript(world, req, b)
  if (/교차|VIP|연관|같이 사|함께 사/i.test(message)) return crossShoppingScript(world, req, b)
  const codes = [...new Set(message.toUpperCase().match(UNIT_CODE_PATTERN) ?? [])].filter((c) => unitExists(world, req.ui_state.rev, c))
  if (codes.length) return unitScript(world, req, b, codes)
  return helpScript(world, req, b)
}

export async function playScript(steps: ScriptStep[], onEvent: (event: AgUiEvent) => void, signal?: AbortSignal): Promise<void> {
  for (const step of steps) {
    await sleep(step.delay, signal)
    onEvent(step.event)
  }
}
