// Seasonal relayout ("시즌 개편"): normalization of /api/relayout/* responses and display helpers.
import type {
  DatePeriod,
  GraphEdge,
  Highlight,
  Meta,
  PhasingStep,
  RelayoutAlternative,
  RelayoutBrief,
  RelayoutBriefSummary,
  RelayoutChange,
  RelayoutCheck,
  RelayoutDiagnosis,
  RelayoutEntry,
  RelayoutExit,
  RelayoutPhasing,
  RelayoutPlan,
  RelayoutProject,
  RevisionInfo,
} from '../types/api'
import { bool, count, isRecord, list, num, optText, records, text, texts, type Rec } from './coerce'
import { CHANGE_COLORS } from './colors'
import { formatKrw, formatNumber } from './format'
import { floorOfUnitCode } from './geometry'

export const RELAYOUT_STATUS_LABELS: Record<string, string> = {
  draft: '초안',
  diagnosed: '진단 완료',
  planned: '대안 생성',
  selected: '대안 결정',
  checked: '도면 대조 완료',
  // A brief file the backend could not read.
  invalid: '브리프 오류',
}

export const RELAYOUT_STATUS_TONE: Record<string, string> = { planned: 'warn', selected: 'ok', checked: 'ok', invalid: 'danger' }

/** Alternatives exist (planned, selected or checked): the brief notes and diagnosis can fold away. */
export function pastDiagnosis(status: string): boolean {
  return status === 'planned' || status === 'selected' || status === 'checked'
}

export const OBJECTIVE_LABELS: Record<string, string> = { commission: '수수료 수익', sales: '매출' }

export const MOVABLE_LABELS: Record<string, string> = { expiring: '계약 만료 매장만', all: '전체' }

export const RELAYOUT_CHANGE_LABELS: Record<string, string> = { entry: '입점', exit: '퇴점', move: '이전' }

const CHANGE_ORDER = ['entry', 'exit', 'move']

export const CHECK_STATUS_LABELS: Record<string, string> = {
  match: '일치',
  different_unit: '다른 매장',
  missing: '없음',
  not_exited: '퇴점 안 됨',
}

export const CHECK_STATUS_TONE: Record<string, string> = { match: 'ok', different_unit: 'warn', missing: 'danger', not_exited: 'danger' }

const SEASON_LABELS: Record<string, string> = { SS: 'S/S', FW: 'F/W' }

/** "2026 F/W" */
export function seasonText(season: string, year: number | null): string {
  const label = SEASON_LABELS[season.toUpperCase()] ?? season
  return year ? `${year} ${label}` : label
}

// --- normalization ---------------------------------------------------------------------------

function areaRange(value: unknown): [number, number] | null {
  if (Array.isArray(value)) {
    const values = value.map(num).filter((v): v is number => v !== null)
    if (values.length >= 2) return [Math.min(values[0], values[1]), Math.max(values[0], values[1])]
    if (values.length === 1) return [values[0], values[0]]
    return null
  }
  const single = num(value)
  return single === null ? null : [single, single]
}

function normalizeEntry(r: Rec): RelayoutEntry {
  return {
    brand_id: text(r.brand_id),
    brand_name: text(r.brand_name, text(r.brand_id)),
    category: optText(r.category),
    category_name: optText(r.category_name) ?? optText(r.category),
    target_area_m2: areaRange(r.target_area_m2),
    preferred_floors: texts(r.preferred_floors),
    commission_rate: num(r.commission_rate),
    note: optText(r.note),
    new_brand: bool(r.new_brand),
    attractiveness: num(r.attractiveness),
  }
}

function normalizeExit(r: Rec): RelayoutExit {
  return {
    brand_id: text(r.brand_id),
    brand_name: text(r.brand_name, text(r.brand_id)),
    unit_code: optText(r.unit_code),
    reason: optText(r.reason),
  }
}

export function normalizeBriefSummary(raw: unknown): RelayoutBriefSummary | null {
  if (!isRecord(raw) || !optText(raw.brief_id)) return null
  return {
    brief_id: text(raw.brief_id),
    title: text(raw.title, text(raw.brief_id)),
    season: text(raw.season),
    year: num(raw.year),
    effective_date: optText(raw.effective_date),
    base_rev: text(raw.base_rev),
    floors: texts(raw.floors),
    objective: text(raw.objective, 'sales'),
    status: text(raw.status, 'draft'),
    entries: count(raw.entries),
    exits: count(raw.exits),
    updated_at: optText(raw.updated_at),
  }
}

export function normalizeBriefList(raw: unknown): RelayoutBriefSummary[] {
  return list(raw)
    .map(normalizeBriefSummary)
    .filter((b): b is RelayoutBriefSummary => b !== null)
}

function normalizeBrief(raw: Rec): RelayoutBrief {
  return {
    brief_id: text(raw.brief_id),
    title: text(raw.title, text(raw.brief_id)),
    season: text(raw.season),
    season_label: optText(raw.season_label),
    year: num(raw.year),
    effective_date: optText(raw.effective_date),
    base_rev: text(raw.base_rev),
    floors: texts(raw.floors),
    objective: text(raw.objective, 'sales'),
    objective_label: optText(raw.objective_label),
    max_moves: num(raw.max_moves),
    downtime_weeks: num(raw.downtime_weeks),
    movable: optText(raw.movable),
    keep_anchors: bool(raw.keep_anchors),
    area_tolerance: num(raw.area_tolerance),
    entries: records(raw.entries).map(normalizeEntry),
    exits: records(raw.exits).map(normalizeExit),
    notes: optText(raw.notes),
  }
}

function period(value: unknown): DatePeriod | null {
  if (!isRecord(value)) return null
  const start = optText(value.start)
  const end = optText(value.end)
  if (!start || !end) return null
  const days = num(value.days)
  return days === null ? { start, end } : { start, end, days }
}

export function normalizeDiagnosis(raw: unknown): RelayoutDiagnosis | null {
  if (!isRecord(raw)) return null
  const season = isRecord(raw.season) ? raw.season : {}
  return {
    season: {
      code: text(season.code),
      label: text(season.label, text(season.code)),
      year: num(season.year),
      last: period(season.last),
      previous: period(season.previous),
    },
    period: period(raw.period),
    season_factors: records(raw.season_factors)
      .map((f) => ({ category: text(f.category), category_name: text(f.category_name, text(f.category)), factor: num(f.factor) }))
      .filter((f): f is { category: string; category_name: string; factor: number } => f.factor !== null),
    floors: records(raw.floors).map((f) => ({
      floor: text(f.floor),
      sales_per_m2: num(f.sales_per_m2),
      yoy_pct: num(f.yoy_pct),
      low_efficiency: num(f.low_efficiency),
    })),
    candidates: records(raw.candidates).map((c) => ({
      unit_code: text(c.unit_code),
      floor: optText(c.floor) ?? floorOfUnitCode(optText(c.unit_code)),
      brand_id: optText(c.brand_id),
      brand_name: optText(c.brand_name),
      category_name: optText(c.category_name),
      lease_end: optText(c.lease_end),
      sales_per_m2: num(c.sales_per_m2),
      percentile: num(c.percentile),
      low_efficiency: c.low_efficiency === true,
      reasons: texts(c.reasons),
    })),
    candidate_total: num(raw.candidate_total),
    vacancies: records(raw.vacancies).map((v) => ({
      unit_code: text(v.unit_code),
      floor: optText(v.floor) ?? floorOfUnitCode(optText(v.unit_code)),
      area_m2: num(v.area_m2),
    })),
    entries: records(raw.entries).map((e) => ({
      ...normalizeEntry(e),
      feasible_units: num(e.feasible_units) ?? (Array.isArray(e.feasible_unit_codes) ? e.feasible_unit_codes.length : null),
      feasible_unit_codes: texts(e.feasible_unit_codes),
      season_factor: num(e.season_factor),
    })),
    exits: records(raw.exits).map(normalizeExit),
    notes: texts(raw.notes),
  }
}

function normalizeChange(r: Rec): RelayoutChange {
  const fromUnit = optText(r.from_unit)
  const toUnit = optText(r.to_unit)
  return {
    type: text(r.type ?? r.change_type, 'move'),
    brand_id: text(r.brand_id),
    brand_name: text(r.brand_name, text(r.brand_id)),
    category_name: optText(r.category_name),
    from_unit: fromUnit,
    to_unit: toUnit,
    from_floor: optText(r.from_floor) ?? floorOfUnitCode(fromUnit),
    to_floor: optText(r.to_floor) ?? floorOfUnitCode(toUnit),
    delta_krw: num(r.delta_krw),
    explanation: optText(r.explanation),
  }
}

function normalizeRelayoutAlternative(r: Rec, index: number): RelayoutAlternative {
  const kpi = isRecord(r.kpi) ? r.kpi : {}
  const changes = records(r.changes).map(normalizeChange)
  return {
    alt_id: text(r.alt_id ?? r.id, `ALT-${index}`),
    rev_id: optText(r.rev_id),
    summary: text(r.summary),
    kpi: {
      sales_delta_krw: num(kpi.sales_delta_krw),
      sales_low_krw: num(kpi.sales_low_krw),
      sales_high_krw: num(kpi.sales_high_krw),
      objective_delta_krw: num(kpi.objective_delta_krw),
      move_cost_krw: num(kpi.move_cost_krw),
      net_krw: num(kpi.net_krw),
      vs_current_sales_krw: num(kpi.vs_current_sales_krw),
      vs_current_objective_krw: num(kpi.vs_current_objective_krw),
      moves: num(kpi.moves) ?? changes.filter((c) => c.type === 'move').length,
    },
    changes,
    constraints: records(r.constraints).map((c) => ({
      name: text(c.name ?? c.constraint, '제약'),
      ok: Boolean(c.ok ?? c.satisfied),
      ...(optText(c.detail) ? { detail: text(c.detail) } : {}),
    })),
    explanations: texts(r.explanations),
  }
}

export function normalizePlan(raw: unknown): RelayoutPlan | null {
  if (!isRecord(raw)) return null
  const current = isRecord(raw.current) ? raw.current : {}
  const baseline = isRecord(raw.baseline) ? raw.baseline : {}
  return {
    params: isRecord(raw.params) ? raw.params : {},
    current: { sales_krw: num(current.sales_krw), objective_krw: num(current.objective_krw) },
    baseline: { sales_krw: num(baseline.sales_krw), objective_krw: num(baseline.objective_krw), note: optText(baseline.note) },
    alternatives: records(raw.alternatives).map(normalizeRelayoutAlternative),
    unplaced: records(raw.unplaced).map((u) => ({
      brand_id: text(u.brand_id),
      brand_name: text(u.brand_name, text(u.brand_id)),
      reason: text(u.reason),
    })),
    caveats: texts(raw.caveats),
  }
}

export function normalizeCheck(raw: unknown): RelayoutCheck | null {
  if (!isRecord(raw)) return null
  return {
    rev: text(raw.rev),
    plan_rev: optText(raw.plan_rev),
    alt_id: optText(raw.alt_id),
    conformance_pct: num(raw.conformance_pct),
    items: records(raw.items).map((i) => ({
      type: text(i.type, 'move'),
      type_label: optText(i.type_label),
      brand_name: text(i.brand_name, text(i.brand_id)),
      from_unit: optText(i.from_unit),
      planned_unit: optText(i.planned_unit),
      actual_unit: optText(i.actual_unit),
      status: text(i.status),
      status_label: optText(i.status_label),
    })),
    unexpected: records(raw.unexpected).map((u) => ({
      unit_code: text(u.unit_code),
      before_brand: optText(u.before_brand),
      after_brand: optText(u.after_brand),
      before_brand_name: optText(u.before_brand_name),
      after_brand_name: optText(u.after_brand_name),
    })),
    area_changes: records(raw.area_changes).map((a) => ({
      unit_code: text(a.unit_code),
      before_m2: num(a.before_m2),
      after_m2: num(a.after_m2),
      delta_pct: num(a.delta_pct),
    })),
    summary: text(raw.summary),
  }
}

export function normalizeProject(raw: unknown): RelayoutProject {
  if (!isRecord(raw) || !isRecord(raw.brief)) throw new Error('개편 프로젝트 응답 형식이 계약과 다릅니다 (brief 객체 필요).')
  return {
    brief: normalizeBrief(raw.brief),
    status: text(raw.status, 'draft'),
    diagnosis: normalizeDiagnosis(raw.diagnosis),
    plan: normalizePlan(raw.plan),
    selected_alt_id: optText(raw.selected_alt_id),
    decided_by: optText(raw.decided_by),
    decided_at: optText(raw.decided_at),
    check: normalizeCheck(raw.check),
    phasing: normalizePhasing(raw.phasing),
    report_url: optText(raw.report_url),
    updated_at: optText(raw.updated_at),
  }
}

// --- construction order (공사 순서) ------------------------------------------------------------------

function normalizePhasingStep(r: Rec, index: number): PhasingStep {
  const unitCode = optText(r.unit_code)
  const start = num(r.start_week) ?? 0
  return {
    step: num(r.step) ?? index + 1,
    start_week: start,
    end_week: Math.max(start, num(r.end_week) ?? start),
    action: text(r.action, 'move'),
    action_label: optText(r.action_label),
    unit_code: unitCode,
    floor: optText(r.floor) ?? floorOfUnitCode(unitCode),
    brand_id: optText(r.brand_id),
    brand_name: text(r.brand_name, text(r.brand_id, '–')),
    from_unit: optText(r.from_unit),
    waits_for: list(r.waits_for)
      .map(num)
      .filter((n): n is number => n !== null),
    closed_weeks: num(r.closed_weeks) ?? 0,
    temporary_unit: optText(r.temporary_unit),
    lost_krw: num(r.lost_krw),
    note: optText(r.note),
  }
}

/** POST /api/relayout/{brief_id}/phasing and `project.phasing`; null when the value is not an object. */
export function normalizePhasing(raw: unknown): RelayoutPhasing | null {
  if (!isRecord(raw)) return null
  const params = isRecord(raw.params) ? raw.params : {}
  const steps = records(raw.steps).map(normalizePhasingStep)
  const lost = num(raw.lost_krw)
  const naive = num(raw.naive_lost_krw)
  return {
    brief_id: text(raw.brief_id),
    alt_id: optText(raw.alt_id),
    rev_id: optText(raw.rev_id),
    params: {
      crews: num(params.crews),
      fitout_weeks: num(params.fitout_weeks),
      temporary_sales_ratio: num(params.temporary_sales_ratio),
      use_temporary: bool(params.use_temporary),
    },
    steps,
    total_weeks: num(raw.total_weeks) ?? (steps.length ? Math.max(...steps.map((s) => s.end_week)) : null),
    lost_krw: lost,
    naive_lost_krw: naive,
    saved_krw: num(raw.saved_krw) ?? (lost !== null && naive !== null ? naive - lost : null),
    entry_delay_krw: num(raw.entry_delay_krw),
    entries: records(raw.entries).map((e) => ({
      brand_id: optText(e.brand_id),
      brand_name: text(e.brand_name, text(e.brand_id, '–')),
      unit_code: optText(e.unit_code),
      open_week: num(e.open_week),
      earliest_week: num(e.earliest_week),
    })),
    cycles: records(raw.cycles).map((c) => ({
      brands: texts(c.brands),
      units: texts(c.units),
      breaker: optText(c.breaker),
      resolved_by: text(c.resolved_by, 'close'),
      unit_code: optText(c.unit_code),
      weeks: num(c.weeks),
    })),
    summary: text(raw.summary),
    caveats: texts(raw.caveats),
    created_at: optText(raw.created_at),
  }
}

/** Short chip text of a construction step (the backend's long `action_label` goes into the chip title). */
export const PHASING_ACTION_LABELS: Record<string, string> = {
  exit: '퇴점',
  move: '이전',
  entry: '입점',
  temporary: '임시 영업',
  close: '휴점',
}

/** Long names used when a step has no `action_label`. */
export const PHASING_ACTION_FULL_LABELS: Record<string, string> = {
  exit: '퇴점·철거',
  move: '이전 공사',
  entry: '신규 입점 공사',
  temporary: '임시 매장 영업',
  close: '임시 휴점',
}

/** Chip classes (styles.css): entry green, exit and closure red, move orange, temporary blue. */
export const PHASING_ACTION_TONE: Record<string, string> = {
  exit: 'change-exit',
  move: 'change-move',
  entry: 'change-entry',
  temporary: 'temp',
  close: 'down',
}

const weeks = (value: number) => formatNumber(value, 1).replace(/\.0$/, '')

/** "0~2주" for a work, "0주" for an exit that takes no time. */
export function phasingWeekText(step: Pick<PhasingStep, 'start_week' | 'end_week'>): string {
  return step.end_week > step.start_week ? `${weeks(step.start_week)}~${weeks(step.end_week)}주` : `${weeks(step.start_week)}주`
}

/** Week count with its unit: "6주". */
export function weeksText(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? '–' : `${weeks(value)}주`
}

/** Short remarks of a step: where it comes from, closure weeks, the opening week, what it waits for, lost sales. */
export function phasingStepRemark(step: PhasingStep): string {
  const parts: string[] = []
  if (step.action === 'move' && step.from_unit) parts.push(`${step.from_unit}에서`)
  if (step.action === 'temporary' && step.from_unit) parts.push(`${step.from_unit}에서 임시 이전`)
  if (step.action === 'close') parts.push(`${step.from_unit ?? step.unit_code ?? ''} 휴점 ${weeksText(step.closed_weeks)}`.trim())
  if (step.action === 'entry') parts.push(`${weeks(step.end_week)}주차 개점`)
  if (step.waits_for.length) parts.push(`${step.waits_for.join('·')}번 작업 뒤`)
  if (step.lost_krw !== null && step.lost_krw > 0) parts.push(`손실 ${formatKrw(step.lost_krw)}`)
  return parts.join(' · ')
}

/** "A ↔ B" for a swap, "A → B → C → A" for a longer cycle. */
export function phasingCycleLoop(brands: readonly string[]): string {
  if (brands.length === 2) return brands.join(' ↔ ')
  return brands.length ? [...brands, brands[0]].join(' → ') : ''
}

/** "서로 자리를 바꾸는 순환(A ↔ B)은 A 매장을 2주 동안 임시 매장(4F-E02)으로 옮겨 풉니다." */
export function phasingCycleText(cycle: RelayoutPhasing['cycles'][number]): string {
  const loop = phasingCycleLoop(cycle.brands)
  const who = cycle.breaker ?? cycle.brands[0] ?? '한 브랜드'
  const span = cycle.weeks !== null ? ` ${weeksText(cycle.weeks)} 동안` : ''
  return cycle.resolved_by === 'temporary'
    ? `서로 자리를 바꾸는 순환(${loop})은 ${who} 매장을${span} 임시 매장${cycle.unit_code ? `(${cycle.unit_code})` : ''}으로 옮겨 풉니다.`
    : `서로 자리를 바꾸는 순환(${loop})은 ${who} 매장이${span} 휴점해서 풉니다.`
}

export interface PhasingForm {
  /** 1..6 */
  crews: number
  /** 1..12 */
  fitoutWeeks: number
  useTemporary: boolean
}

const clampInt = (value: number, min: number, max: number) => Math.min(max, Math.max(min, Math.round(value)))

/** Form defaults: the last phasing's parameters, else 2 crews, the brief's downtime weeks and temporary units on. */
export function phasingDefaults(project: Pick<RelayoutProject, 'brief' | 'phasing'>): PhasingForm {
  const params = project.phasing?.params
  return {
    crews: clampInt(params?.crews ?? 2, 1, 6),
    fitoutWeeks: clampInt(params?.fitout_weeks ?? project.brief.downtime_weeks ?? 2, 1, 12),
    useTemporary: params?.use_temporary ?? true,
  }
}

// --- display helpers ------------------------------------------------------------------------

/** Changes grouped as 입점, 퇴점, 이전 (then anything else), skipping empty groups. */
export function groupRelayoutChanges(changes: readonly RelayoutChange[]): { type: string; label: string; items: RelayoutChange[] }[] {
  const types = [...CHANGE_ORDER, ...new Set(changes.map((c) => c.type).filter((t) => !CHANGE_ORDER.includes(t)))]
  return types
    .map((type) => ({ type, label: RELAYOUT_CHANGE_LABELS[type] ?? type, items: changes.filter((c) => c.type === type) }))
    .filter((group) => group.items.length > 0)
}

export function changeFloor(change: RelayoutChange): string | null {
  return change.to_floor ?? change.from_floor ?? floorOfUnitCode(change.to_unit) ?? floorOfUnitCode(change.from_unit)
}

/** Floor of the first change that names one. */
export function firstChangedFloor(alt: RelayoutAlternative): string | null {
  for (const change of alt.changes) {
    const floor = changeFloor(change)
    if (floor) return floor
  }
  return null
}

const DEPARTURE_COLOR = '#757575'

export interface RelayoutPreview {
  rev: string | null
  compareRev: string
  /** First changed floor; null when unknown or not among `knownFloors`. */
  floor: string | null
  highlights: Highlight[]
  edges: GraphEdge[]
  codes: string[]
}

/**
 * Map display of an alternative: its hypothetical revision compared with the brief's base revision,
 * exits/entries/moves outlined (an arrival wins over a departure on the same unit) and moves as edges.
 */
export function relayoutPreview(alt: RelayoutAlternative, baseRev: string, knownFloors?: readonly string[]): RelayoutPreview {
  const highlights = new Map<string, Highlight>()
  const mark = (code: string | null, color: string, reason: string) => {
    if (code) highlights.set(code, { unit_code: code, color, reason })
  }
  for (const c of alt.changes) {
    if (c.type === 'exit') mark(c.from_unit, CHANGE_COLORS.remove, `${alt.alt_id}: ${c.brand_name} 퇴점`)
    else if (c.type === 'move') mark(c.from_unit, DEPARTURE_COLOR, `${alt.alt_id}: ${c.brand_name} 이전 전`)
  }
  for (const c of alt.changes) {
    if (c.type === 'entry') mark(c.to_unit, CHANGE_COLORS.add, `${alt.alt_id}: ${c.brand_name} 입점`)
    else if (c.type === 'move') mark(c.to_unit, CHANGE_COLORS.move, `${alt.alt_id}: ${c.brand_name} 이전 후`)
  }
  const edges: GraphEdge[] = alt.changes
    .filter((c) => c.type === 'move' && c.from_unit && c.to_unit)
    .map((c) => ({ from: c.from_unit!, to: c.to_unit!, weight: 2, label: c.brand_name, color: CHANGE_COLORS.move }))
  const floor = firstChangedFloor(alt)
  return {
    rev: alt.rev_id,
    compareRev: baseRev,
    floor: floor && (!knownFloors || knownFloors.includes(floor)) ? floor : null,
    highlights: [...highlights.values()],
    edges,
    codes: [...highlights.keys()],
  }
}

function byNewest(revisions: readonly RevisionInfo[]): RevisionInfo[] {
  return revisions
    .map((r, index) => ({ r, index }))
    .sort((a, b) => (b.r.effective_date ?? '').localeCompare(a.r.effective_date ?? '') || b.index - a.index)
    .map(({ r }) => r)
}

/** Actual revisions other than the base that a selected plan can be checked against (newest first). */
export function checkRevisionOptions(meta: Meta | null | undefined, baseRev: string): RevisionInfo[] {
  return byNewest((meta?.revisions ?? []).filter((r) => r.kind !== 'hypothetical' && r.rev_id !== baseRev))
}

/** Newest uploaded revision drawn on the base; then any child of the base; then the newest option. */
export function defaultCheckRevision(meta: Meta | null | undefined, baseRev: string): string | null {
  const options = checkRevisionOptions(meta, baseRev)
  return (
    options.find((r) => r.model_source === 'uploaded' && r.parent_rev_id === baseRev)?.rev_id ??
    options.find((r) => r.parent_rev_id === baseRev)?.rev_id ??
    options[0]?.rev_id ??
    null
  )
}

/** The plan's baseline alternative (`ALT-0`: exits and entries only, deltas 0). */
export function isBaselineAlternative(alt: RelayoutAlternative): boolean {
  return alt.alt_id.toUpperCase() === 'ALT-0' || /-A0$/i.test(alt.rev_id ?? '')
}

/**
 * Card to open first: the decided alternative, otherwise the non-baseline one with the highest net
 * effect (first wins ties), otherwise the first alternative.
 */
export function defaultExpandedAlternative(plan: RelayoutPlan | null, selectedAltId: string | null): string | null {
  const alternatives = plan?.alternatives ?? []
  if (selectedAltId && alternatives.some((a) => a.alt_id === selectedAltId)) return selectedAltId
  let best: RelayoutAlternative | null = null
  for (const alt of alternatives) {
    if (isBaselineAlternative(alt)) continue
    if (!best || (alt.kpi.net_krw ?? -Infinity) > (best.kpi.net_krw ?? -Infinity)) best = alt
  }
  return best?.alt_id ?? alternatives[0]?.alt_id ?? null
}

/** Display name of a brand in check results: the name, else the ID, else (공실). */
export function brandDisplay(name: string | null, id: string | null): string {
  return name ?? id ?? '(공실)'
}

/** Strongest and weakest categories of the season (factor above / below 1). */
export function seasonExtremes(factors: RelayoutDiagnosis['season_factors'], n = 3) {
  const sorted = [...factors].sort((a, b) => b.factor - a.factor)
  return {
    strong: sorted.filter((f) => f.factor > 1).slice(0, n),
    weak: sorted
      .filter((f) => f.factor < 1)
      .reverse()
      .slice(0, n),
  }
}
