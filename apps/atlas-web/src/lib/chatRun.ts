// Per-run chat model: folds the AG-UI events of one run into a conversational view (status line,
// labelled steps, answer text). Pure and unit-tested; the reducer applies STATE_DELTA patches to the
// UI state and passes the outcome in. Tool names, args and summaries are kept for the developer view only.
import type { AgUiEvent } from '../types/api'
import { floorLabel, withEuro } from './format'
import { parsePointer, touchedKeys, type PatchOperation } from './jsonPatch'

/** thinking -> working -> answering -> done | error (stopped = cancelled by the user). */
export type RunStatus = 'thinking' | 'working' | 'answering' | 'done' | 'error' | 'stopped'
export type StepStatus = 'running' | 'done' | 'error'

export interface RunStep {
  id: string
  /** User-facing Korean label. */
  label: string
  /** backend = event label, fallback = local tool-name table, ui = described from the applied UI patch. */
  labelSource: 'backend' | 'fallback' | 'ui'
  parentId: string | null
  agent: string | null
  /** Empty for UI changes that arrived without a tool call. */
  toolName: string
  args: unknown
  summary: string | null
  status: StepStatus
  /** Top-level UI state keys changed by STATE_DELTA while this step ran. */
  patchKeys: string[]
  /** Set when a STATE_DELTA of this step was rejected (nothing changed on screen). */
  patchError: string | null
  error: string | null
  startedAt: number
  endedAt: number | null
}

export interface ChatRun {
  id: string
  runId: string | null
  status: RunStatus
  steps: RunStep[]
  messageIds: string[]
  /** Assistant answer (Markdown subset, rendered by RichText). */
  answer: string
  /** Technical error message (RUN_ERROR or transport failure). */
  error: string | null
  startedAt: number
  endedAt: number | null
}

export interface FoldContext {
  /** Event time in ms (Date.now()). */
  at: number
  /** Outcome of applying a STATE_DELTA to the UI state (provided by the reducer). */
  patch?: { keys: string[]; error?: string }
}

export const THINKING_LABEL = '생각하는 중…'
export const UNKNOWN_TOOL_LABEL = '작업 중'
export const ERROR_MESSAGE = '답변을 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.'

// --- labels ------------------------------------------------------------------------------------

/** Korean labels used when an event has no `label` (backends that predate the field). */
export const TOOL_LABELS: Readonly<Record<string, string>> = {
  spatial_list_revisions: '개정 목록 확인',
  spatial_floor_summary: '층 정보 확인',
  spatial_find_units: '매장 찾기',
  spatial_unit_details: '매장 상세 확인',
  spatial_nearby_units: '주변 매장 찾기',
  spatial_query: '공간 조건 조회',
  spatial_route: '경로 계산',
  spatial_describe_floor: '층 구조 확인',
  revision_diff: '도면 변경 내역 확인',
  revision_process: '도면 분석',
  revision_create_hypothetical: '가상 배치 만들기',
  registry_list_generators: '산출물 종류 확인',
  registry_impact: '다시 만들 산출물 확인',
  registry_run: '산출물 만들기',
  registry_validate: '산출물 검증',
  render_adhoc_table: '표 만들기',
  analytics_productivity: '공간 생산성 계산',
  analytics_drivers: '효율 저하 원인 분석',
  analytics_cross_shopping: '교차구매 분석',
  analytics_floor_transitions: '층간 이동 흐름 분석',
  analytics_adjacency_effect: '인접 효과 검증',
  analytics_before_after: '개편 전후 비교',
  analytics_time_patterns: '시간대별 매출 분석',
  analytics_customer_journey: '고객 동선 찾기',
  layout_generate_alternatives: '재배치 대안 계산',
  whatif_score_moves: '이동 효과 추정',
  ui_set_floor: '층 이동',
  ui_show_heatmap: '도면 색칠',
  ui_highlight_units: '매장 강조',
  ui_draw_edges: '관계선 표시',
  ui_focus: '화면 확대',
  ui_compare: '개정 비교 표시',
  ui_show_panel: '패널 열기',
  ui_show_journey: '도면에 고객 동선 그리기',
  ui_show_route: '도면에 경로 그리기',
  ui_clear: '화면 정리',
  consult_specialist: '전문 에이전트에게 확인',
  web_search: '웹 검색',
  code_interpreter: '계산 실행',
}

// Tools added to the backend later still get a meaningful label from their family prefix.
const PREFIX_LABELS: readonly [string, string][] = [
  ['spatial_', '공간 정보 확인'],
  ['revision_', '도면 개정 확인'],
  ['registry_', '산출물 확인'],
  ['render_', '표 만들기'],
  ['analytics_', '데이터 분석'],
  ['layout_', '배치 계산'],
  ['whatif_', '효과 추정'],
  ['ui_', '화면 바꾸기'],
]

/** "analytics.productivity" / "Analytics-Productivity" -> "analytics_productivity". */
export function normalizeToolName(name: string | null | undefined): string {
  return (name ?? '').trim().toLowerCase().replace(/[.:\-/\s]+/g, '_')
}

function knownToolKey(name: string | null | undefined): string | null {
  const key = normalizeToolName(name)
  if (!key) return null
  if (TOOL_LABELS[key]) return key
  // Server-qualified MCP names such as "atlas_analytics_productivity".
  return Object.keys(TOOL_LABELS).find((known) => key.endsWith(`_${known}`)) ?? null
}

/** Friendly Korean label for a tool name (fallback when the backend sends no label). */
export function toolLabel(name: string | null | undefined): string {
  const known = knownToolKey(name)
  if (known) return TOOL_LABELS[known]
  const key = normalizeToolName(name)
  return PREFIX_LABELS.find(([prefix]) => key.startsWith(prefix))?.[1] ?? UNKNOWN_TOOL_LABEL
}

// UI tools whose generic label can be made specific from the patch they produced.
const UI_PRIMARY_KEY: Readonly<Record<string, string>> = {
  ui_set_floor: 'floor',
  ui_highlight_units: 'highlights',
  ui_draw_edges: 'edges',
  ui_show_heatmap: 'heatmap',
  ui_focus: 'focus',
  ui_compare: 'compareRev',
  ui_show_panel: 'panel',
  ui_show_route: 'route',
}

const DESCRIBE_ORDER = ['floor', 'rev', 'compareRev', 'journey', 'route', 'heatmap', 'highlights', 'edges', 'panel', 'focus', 'layers']

const PANEL_LABELS: Readonly<Record<string, string>> = {
  changes: '변경 목록 열기',
  artifact: '산출물 열기',
  layout: '재배치 대안 열기',
  table: '표 열기',
  journey: '동선 목록 열기',
  none: '패널 닫기',
}

const PARTIAL = Symbol('partial')

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function describeKey(key: string, values: Map<string, unknown>): string | null {
  const value = values.get(key)
  if (value === PARTIAL) return null
  switch (key) {
    case 'floor':
      return typeof value === 'string' && value ? `${withEuro(floorLabel(value))} 이동` : null
    case 'rev':
      return typeof value === 'string' && value ? `${value} 개정 보기` : null
    case 'compareRev': {
      if (value === null) return '비교 끄기'
      if (typeof value !== 'string' || !value) return null
      const rev = values.get('rev')
      return typeof rev === 'string' && rev ? `${value} → ${rev} 비교` : `${value} 기준 비교`
    }
    case 'highlights':
      if (!Array.isArray(value)) return null
      return value.length ? `매장 ${value.length}곳 강조` : '강조 지우기'
    case 'edges':
      if (!Array.isArray(value)) return null
      return value.length ? `관계선 ${value.length}개 표시` : '관계선 지우기'
    case 'heatmap':
      if (value === null) return '색칠 끄기'
      if (!isRecord(value)) return null
      return typeof value.label === 'string' && value.label ? `도면 색칠 · ${value.label}` : '도면 색칠'
    case 'focus':
      return isRecord(value) || Array.isArray(value) ? '화면 확대' : null
    case 'panel': {
      if (!isRecord(value)) return null
      const type = String(value.type ?? '')
      const title = isRecord(value.payload) && typeof value.payload.title === 'string' ? value.payload.title : ''
      if (type === 'table' && title) return `표 열기 · ${title}`
      return PANEL_LABELS[type] ?? null
    }
    case 'journey':
      return value === null ? '동선 지우기' : isRecord(value) ? '고객 동선 그리기' : null
    case 'route':
      return value === null ? '경로 지우기' : isRecord(value) ? '경로 그리기' : null
    case 'layers':
      return '레이어 바꾸기'
    default:
      return null
  }
}

/**
 * Describe a UI patch in Korean ("3층으로 이동", "매장 5곳 강조"). With a UI tool name, only that tool's
 * main key is described (null keeps the tool's own label); without one (a STATE_DELTA outside any
 * tool call) up to two changes are joined.
 */
export function describeUiChange(toolName: string | null, ops: readonly PatchOperation[]): string | null {
  const values = new Map<string, unknown>()
  for (const op of ops) {
    if (!op || typeof op.path !== 'string') continue
    let tokens: string[]
    try {
      tokens = parsePointer(op.path)
    } catch {
      continue
    }
    if (tokens.length === 0) continue
    if (tokens.length > 1 || op.op === 'move' || op.op === 'copy' || op.op === 'test') values.set(tokens[0], PARTIAL)
    else if (op.op === 'remove') values.set(tokens[0], null)
    else values.set(tokens[0], op.value)
  }
  if (toolName !== null) {
    const key = UI_PRIMARY_KEY[normalizeToolName(toolName)]
    return key && values.has(key) ? describeKey(key, values) : null
  }
  const parts = DESCRIBE_ORDER.filter((key) => values.has(key))
    .map((key) => describeKey(key, values))
    .filter((part): part is string => !!part)
  return parts.length ? parts.slice(0, 2).join(' · ') : null
}

// --- folding -----------------------------------------------------------------------------------

export function createRun(id: string, at: number): ChatRun {
  return { id, runId: null, status: 'thinking', steps: [], messageIds: [], answer: '', error: null, startedAt: at, endedAt: null }
}

export function isRunActive(run: ChatRun): boolean {
  return run.status === 'thinking' || run.status === 'working' || run.status === 'answering'
}

// Backends that predate `ok` report failures in the summary text.
const FAILED_SUMMARY = /^\s*(?:오류\s*:|\{\s*"ok"\s*:\s*false\b)/

function endFailed(event: Extract<AgUiEvent, { type: 'TOOL_CALL_END' }>): boolean {
  if (event.ok !== undefined) return !event.ok
  return typeof event.summary === 'string' && FAILED_SUMMARY.test(event.summary)
}

function lastIndexWhere<T>(items: readonly T[], predicate: (item: T) => boolean): number {
  for (let i = items.length - 1; i >= 0; i -= 1) if (predicate(items[i])) return i
  return -1
}

function replaceStep(run: ChatRun, index: number, step: RunStep): RunStep[] {
  const steps = run.steps.slice()
  steps[index] = step
  return steps
}

function workingStatus(run: ChatRun): RunStatus {
  return isRunActive(run) ? 'working' : run.status
}

function toolStart(run: ChatRun, event: Extract<AgUiEvent, { type: 'TOOL_CALL_START' }>, at: number): ChatRun {
  const id = event.tool_call_id || `call-${run.steps.length + 1}`
  const index = run.steps.findIndex((s) => s.id === id)
  if (index >= 0) {
    // Repeated START (e.g. args streamed later): refresh details, keep the status.
    const step = run.steps[index]
    const toolName = event.tool_name || step.toolName
    const label = event.label ?? (step.labelSource === 'fallback' ? toolLabel(toolName) : step.label)
    return {
      ...run,
      steps: replaceStep(run, index, {
        ...step,
        toolName,
        label,
        labelSource: event.label ? 'backend' : step.labelSource,
        args: event.args ?? step.args,
        parentId: event.parent_tool_call_id ?? step.parentId,
        agent: event.agent ?? step.agent,
      }),
    }
  }
  const step: RunStep = {
    id,
    label: event.label ?? toolLabel(event.tool_name),
    labelSource: event.label ? 'backend' : 'fallback',
    parentId: event.parent_tool_call_id ?? null,
    agent: event.agent ?? null,
    toolName: event.tool_name,
    args: event.args,
    summary: null,
    status: 'running',
    patchKeys: [],
    patchError: null,
    error: null,
    startedAt: at,
    endedAt: null,
  }
  return { ...run, steps: [...run.steps, step], status: workingStatus(run) }
}

function toolEnd(run: ChatRun, event: Extract<AgUiEvent, { type: 'TOOL_CALL_END' }>, at: number): ChatRun {
  const failed = endFailed(event)
  const index = event.tool_call_id ? run.steps.findIndex((s) => s.id === event.tool_call_id) : -1
  if (index < 0) {
    // END without START: record it as a finished step.
    const step: RunStep = {
      id: event.tool_call_id || `call-${run.steps.length + 1}`,
      label: event.label ?? UNKNOWN_TOOL_LABEL,
      labelSource: event.label ? 'backend' : 'fallback',
      parentId: null,
      agent: null,
      toolName: '',
      args: undefined,
      summary: event.summary ?? null,
      status: failed ? 'error' : 'done',
      patchKeys: [],
      patchError: null,
      error: failed ? (event.summary ?? null) : null,
      startedAt: at,
      endedAt: at,
    }
    return { ...run, steps: [...run.steps, step], status: workingStatus(run) }
  }
  const step = run.steps[index]
  const status: StepStatus = step.status === 'error' || failed ? 'error' : 'done'
  const steps = run.steps.map((s, i): RunStep => {
    if (i === index) {
      return {
        ...s,
        status,
        label: event.label ?? s.label,
        labelSource: event.label ? 'backend' : s.labelSource,
        summary: event.summary ?? s.summary,
        error: failed && !s.error ? (event.summary ?? '실패') : s.error,
        endedAt: at,
      }
    }
    // Nested calls cannot outlive their parent.
    return s.parentId === step.id && s.status === 'running' ? { ...s, status: 'done', endedAt: at } : s
  })
  return { ...run, steps }
}

function stateDelta(run: ChatRun, ops: PatchOperation[], ctx: FoldContext): ChatRun {
  const keys = ctx.patch?.keys ?? touchedKeys(ops)
  const error = ctx.patch?.error ?? null
  let index = lastIndexWhere(run.steps, (s) => s.status === 'running' && normalizeToolName(s.toolName).startsWith('ui_'))
  if (index < 0) index = lastIndexWhere(run.steps, (s) => s.status === 'running')
  if (index >= 0) {
    const step = run.steps[index]
    const described = step.labelSource === 'fallback' ? describeUiChange(step.toolName, ops) : null
    return {
      ...run,
      steps: replaceStep(run, index, {
        ...step,
        patchKeys: [...new Set([...step.patchKeys, ...keys])],
        status: error ? 'error' : step.status,
        patchError: error ?? step.patchError,
        error: error ?? step.error,
        ...(described ? { label: described, labelSource: 'ui' as const } : {}),
      }),
    }
  }
  const step: RunStep = {
    id: `ui-${run.steps.length + 1}`,
    label: describeUiChange(null, ops) ?? '화면 바꾸기',
    labelSource: 'ui',
    parentId: null,
    agent: null,
    toolName: '',
    args: undefined,
    summary: null,
    status: error ? 'error' : 'done',
    patchKeys: keys,
    patchError: error,
    error,
    startedAt: ctx.at,
    endedAt: ctx.at,
  }
  return { ...run, steps: [...run.steps, step], status: run.status === 'thinking' ? 'working' : run.status }
}

function withMessage(run: ChatRun, messageId: string): ChatRun {
  if (run.messageIds.includes(messageId)) return run
  // The contract sends one message per run; extra messages become new paragraphs of the answer.
  const answer = run.answer.trim() && !run.answer.endsWith('\n\n') ? `${run.answer}\n\n` : run.answer
  return { ...run, messageIds: [...run.messageIds, messageId], answer }
}

/** End a run (RUN_FINISHED, RUN_ERROR or user abort); steps still running are closed. */
export function finishRun(run: ChatRun, outcome: 'done' | 'error' | 'stopped', at: number, message?: string): ChatRun {
  if (!isRunActive(run)) return run
  const steps = run.steps.map((s): RunStep => {
    if (s.status !== 'running') return s
    if (outcome === 'done') return { ...s, status: 'done', endedAt: at }
    return { ...s, status: 'error', error: outcome === 'stopped' ? '중지됨' : (s.error ?? '실행이 끝나지 않음'), endedAt: at }
  })
  return {
    ...run,
    steps,
    status: outcome,
    error: outcome === 'error' ? (message || '알 수 없는 오류') : run.error,
    endedAt: at,
  }
}

/** The stream closed without RUN_FINISHED/RUN_ERROR. */
export function closeRun(run: ChatRun, at: number): ChatRun {
  if (!isRunActive(run)) return run
  return run.answer.trim()
    ? finishRun(run, 'done', at)
    : finishRun(run, 'error', at, '응답 스트림이 RUN_FINISHED 없이 끝났습니다.')
}

/** Fold one AG-UI event into the run. Events after the run ended are ignored. */
export function foldRunEvent(run: ChatRun, event: AgUiEvent, ctx: FoldContext): ChatRun {
  if (!isRunActive(run)) return run
  switch (event.type) {
    case 'RUN_STARTED':
      return { ...run, runId: event.run_id || run.runId }
    case 'TEXT_MESSAGE_START':
      return withMessage(run, event.message_id)
    case 'TEXT_MESSAGE_CONTENT': {
      const next = withMessage(run, event.message_id)
      if (!event.delta) return next
      return { ...next, answer: next.answer + event.delta, status: 'answering' }
    }
    case 'TEXT_MESSAGE_END':
      return run
    case 'TOOL_CALL_START':
      return toolStart(run, event, ctx.at)
    case 'TOOL_CALL_END':
      return toolEnd(run, event, ctx.at)
    case 'STATE_DELTA':
      return stateDelta(run, event.delta, ctx)
    case 'RUN_FINISHED':
      return finishRun(run, 'done', ctx.at)
    case 'RUN_ERROR':
      return finishRun(run, 'error', ctx.at, event.message)
    default:
      return run
  }
}

// --- view helpers ------------------------------------------------------------------------------

/** The live status line: null once the answer streams or the run ended. */
export function runStatusText(run: ChatRun): string | null {
  if (!isRunActive(run) || run.status === 'answering') return null
  const index = lastIndexWhere(run.steps, (s) => s.status === 'running')
  if (index < 0) return THINKING_LABEL
  const running = run.steps[index]
  const parent = running.parentId ? run.steps.find((s) => s.id === running.parentId) : undefined
  return parent ? `${parent.label} · ${running.label}` : running.label
}

/** Label shown in the finished step list ("작업 중" reads oddly once a step is done). */
export function stepDisplayLabel(step: RunStep): string {
  return step.status !== 'running' && step.labelSource === 'fallback' && step.label === UNKNOWN_TOOL_LABEL ? '기타 작업' : step.label
}

export interface StepNode {
  step: RunStep
  children: StepNode[]
}

/** Steps nested under their parents (unknown parents put the step at the top level). */
export function stepTree(steps: readonly RunStep[]): StepNode[] {
  const nodes = new Map(steps.map((step) => [step.id, { step, children: [] as StepNode[] }]))
  const roots: StepNode[] = []
  for (const step of steps) {
    const node = nodes.get(step.id)!
    const parent = step.parentId && step.parentId !== step.id ? nodes.get(step.parentId) : undefined
    if (parent) parent.children.push(node)
    else roots.push(node)
  }
  return roots
}

/** "1초 미만", "23초", "1분 5초", "2분". */
export function formatElapsed(ms: number): string {
  const seconds = Math.round(Math.max(0, ms) / 1000)
  if (seconds < 1) return '1초 미만'
  if (seconds < 60) return `${seconds}초`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return s ? `${m}분 ${s}초` : `${m}분`
}

/** Disclosure summary: "작업 과정 보기 · 4단계 · 23초" (time only once the run ended). */
export function stepsSummaryText(run: ChatRun): string {
  const parts = ['작업 과정 보기', `${run.steps.length}단계`]
  if (run.endedAt !== null) parts.push(formatElapsed(run.endedAt - run.startedAt))
  return parts.join(' · ')
}

/** Steps whose UI change was rejected (STATE_DELTA failed to apply). */
export function failedUiSteps(run: ChatRun): RunStep[] {
  return run.steps.filter((s) => s.patchError !== null)
}
