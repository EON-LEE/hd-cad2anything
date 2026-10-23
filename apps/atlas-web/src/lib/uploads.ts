// Drawing and metadata uploads: labels, client-side validation (mirrors docs/specs/atlas-api.md §1.2)
// and normalization of /api/uploads/* responses.
import type {
  DrawingProfile,
  LayerReviewItem,
  MetadataKind,
  MetadataStatus,
  MetadataUploadResult,
  RevisionInfo,
  UploadJob,
  UploadJobStep,
  UploadJobSummary,
} from '../types/api'
import { isRecord, list, num, optText, records, text, texts, type Rec } from './coerce'

/** The 16 layer meanings of the CAD pipeline, in the order the select shows them. */
export const LAYER_SEMANTICS: readonly { value: string; label: string }[] = [
  { value: 'exterior', label: '건물 외곽' },
  { value: 'wall', label: '벽' },
  { value: 'unit', label: '매장 경계' },
  { value: 'unit_label', label: '매장 라벨' },
  { value: 'zone', label: '존 경계' },
  { value: 'zone_label', label: '존 라벨' },
  { value: 'walkway', label: '보행 통로' },
  { value: 'escalator', label: '에스컬레이터' },
  { value: 'elevator', label: '엘리베이터' },
  { value: 'stair', label: '계단' },
  { value: 'opening', label: '출입구' },
  { value: 'amenity', label: '편의시설' },
  { value: 'amenity_label', label: '편의시설 라벨' },
  { value: 'fixture', label: '집기' },
  { value: 'annotation', label: '주석' },
  { value: 'noise', label: '잡음' },
]

export const LAYER_SEMANTIC_LABELS: Record<string, string> = Object.fromEntries(LAYER_SEMANTICS.map((s) => [s.value, s.label]))

export interface MetadataKindInfo {
  kind: MetadataKind
  label: string
  /** Display text of the accepted formats. */
  formats: string
  /** `accept` attribute of the file input. */
  accept: string
  extensions: readonly string[]
  required: readonly string[]
  optional: readonly string[]
  /** What an accepted upload does. */
  storage: string
}

const TABLE = { formats: 'CSV·XLSX', accept: '.csv,.xlsx', extensions: ['.csv', '.xlsx'], storage: '표 전체 교체' }
const DOCUMENT = { formats: 'YAML·JSON', accept: '.yaml,.yml,.json', extensions: ['.yaml', '.yml', '.json'] }

export const METADATA_KINDS: readonly MetadataKindInfo[] = [
  { kind: 'brands', label: '브랜드', ...TABLE, required: ['brand_id', 'brand_name', 'category'], optional: [] },
  { kind: 'tenancies', label: '입점 이력', ...TABLE, required: ['brand_id', 'unit_code', 'rev_id', 'valid_from', 'valid_to'], optional: [] },
  {
    kind: 'leases',
    label: '계약',
    ...TABLE,
    required: ['lease_id', 'brand_id', 'unit_code', 'lease_start', 'lease_end'],
    optional: ['contract_area_m2', 'commission_rate'],
  },
  { kind: 'pos_terminals', label: 'POS 단말', ...TABLE, required: ['pos_id', 'brand_id', 'unit_code', 'valid_from', 'valid_to'], optional: [] },
  {
    kind: 'relayout_brief',
    label: '개편 브리프',
    ...DOCUMENT,
    required: ['brief_id', 'title', 'season', 'year', 'effective_date', 'base_rev', 'floors'],
    optional: [],
    storage: '브리프 하나 추가·교체',
  },
  { kind: 'drawing_profile', label: '도면 규약 프로필', ...DOCUMENT, required: ['profile_id', 'title'], optional: [], storage: '프로필 하나 추가·교체' },
  {
    kind: 'stores',
    label: '비교 점포',
    ...TABLE,
    required: ['store_id', 'store_name', 'region', 'format', 'gross_area_m2'],
    optional: [],
    storage: '표 전체 교체(비교 대상 점포 목록)',
  },
  {
    kind: 'store_kpis',
    label: '점포 실적',
    ...TABLE,
    required: ['store_id', 'period', 'floor', 'category', 'area_m2', 'sales_krw'],
    optional: [],
    storage: '표 전체 교체(점포별 월·층·카테고리 실적, period는 YYYY-MM)',
  },
]

export const METADATA_KIND_LABELS: Record<string, string> = Object.fromEntries(METADATA_KINDS.map((k) => [k.kind, k.label]))

export function metadataKindInfo(kind: string): MetadataKindInfo | undefined {
  return METADATA_KINDS.find((k) => k.kind === kind)
}

export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: '대기 중',
  running: '처리 중',
  needs_review: '검토 필요',
  done: '완료',
  failed: '실패',
}

export const JOB_STATUS_TONE: Record<string, string> = { running: 'warn', needs_review: 'warn', done: 'ok', failed: 'danger' }

export const STEP_LABELS: Record<string, string> = {
  save: '파일 저장',
  parse: '도면 읽기',
  merge: '층 합치기',
  diff: '변경 비교',
  qa: '계약 정보 대조',
  impact: '영향 분석',
  generate: '산출물 재생성',
  relayout_check: '개편안 대조',
}

export const STEP_ORDER: readonly string[] = Object.keys(STEP_LABELS)

export const STEP_STATUS_LABELS: Record<string, string> = {
  pending: '대기',
  running: '진행 중',
  done: '완료',
  skipped: '건너뜀',
  failed: '실패',
}

/** Short Korean names of lease QA codes (chip text; the backend's long names appear in tool replies). */
export const QA_CODE_LABELS: Record<string, string> = {
  unit_missing_in_drawing: '계약 매장 누락',
  unit_without_lease: '계약 없는 매장',
  brand_mismatch: '브랜드 불일치',
  area_mismatch: '면적 불일치',
}

export const SEVERITY_LABELS: Record<string, string> = { error: '오류', warning: '경고', info: '참고' }

export const SEVERITY_TONE: Record<string, string> = { error: 'danger', warning: 'warn' }

/** Jobs stop changing in these states (`needs_review` still produced a model, with guessed layers). */
export function isJobSettled(status: string | null | undefined): boolean {
  return status === 'done' || status === 'failed' || status === 'needs_review'
}

/** True when the job produced a revision that can be opened (`done` or `needs_review`). */
export function jobHasRevision(status: string | null | undefined): boolean {
  return status === 'done' || status === 'needs_review'
}

// --- client-side validation -------------------------------------------------------------------

export const REV_ID_PATTERN = /^[A-Z0-9_-]{1,16}$/
export const MAX_FILE_BYTES = 30 * 1024 * 1024
export const MAX_TOTAL_BYTES = 100 * 1024 * 1024

function extension(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot).toLowerCase() : ''
}

/** 381220 -> "372KB", 31_457_280 -> "30MB". */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '–'
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1).replace(/\.0$/, '')}MB`
}

/**
 * Floor code in a drawing file name: "R3_4F.dxf" -> "4F", "6F.dxf" -> "6F", "B1.dxf" -> "B1",
 * "4층.dxf" -> "4F", "지하1층.dxf" -> "B1". Null when the name has none.
 */
export function floorFromFileName(name: string): string | null {
  const base = name.replace(/\.[^.]*$/, '')
  const underground = /지하\s*(\d{1,2})\s*층/.exec(base)
  if (underground) return `B${Number(underground[1])}`
  const korean = /(\d{1,2})\s*층/.exec(base)
  if (korean) return `${Number(korean[1])}F`
  const basement = /(?:^|[^A-Za-z0-9])B(\d{1,2})F?(?![A-Za-z0-9])/i.exec(base)
  if (basement) return `B${Number(basement[1])}`
  const above = /(?:^|[^A-Za-z0-9])(\d{1,2})F(?![A-Za-z0-9])/i.exec(base)
  if (above) return `${Number(above[1])}F`
  if (/(?:^|[^A-Za-z0-9])RF(?![A-Za-z0-9])/i.test(base)) return 'RF'
  return null
}

export type DrawingField = 'files' | 'rev_id' | 'effective_date'

export interface FieldError {
  field: DrawingField
  message: string
}

export interface DrawingFormInput {
  files: readonly { name: string; size: number }[]
  revId: string
  effectiveDate: string
  /** Known revisions: an existing ID that was not made by an upload cannot be reused. */
  revisions?: readonly RevisionInfo[]
}

function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value
}

/** Errors the backend would answer with 400/409, checked before uploading. */
export function validateDrawingUpload(input: DrawingFormInput): FieldError[] {
  const errors: FieldError[] = []
  const files = input.files
  if (files.length === 0) {
    errors.push({ field: 'files', message: '도면 파일을 하나 이상 고르세요.' })
  } else {
    const wrong = files.filter((f) => !['.dxf', '.zip'].includes(extension(f.name)))
    const zips = files.filter((f) => extension(f.name) === '.zip')
    if (wrong.length) {
      errors.push({ field: 'files', message: `DXF 또는 ZIP 파일만 올릴 수 있습니다: ${wrong.map((f) => f.name).join(', ')}` })
    } else if (zips.length > 1 || (zips.length === 1 && files.length > 1)) {
      errors.push({ field: 'files', message: 'ZIP 파일은 하나만, DXF 파일과 섞지 않고 올려 주세요.' })
    } else if (!zips.length) {
      const noFloor = files.filter((f) => !floorFromFileName(f.name))
      if (noFloor.length) {
        errors.push({
          field: 'files',
          message: `파일 이름에 층 코드가 없습니다: ${noFloor.map((f) => f.name).join(', ')} (예: R3_4F.dxf, 6F.dxf, B1.dxf, 4층.dxf)`,
        })
      }
      const byFloor = new Map<string, string[]>()
      for (const f of files) {
        const floor = floorFromFileName(f.name)
        if (floor) byFloor.set(floor, [...(byFloor.get(floor) ?? []), f.name])
      }
      for (const [floor, names] of byFloor) {
        if (names.length > 1) errors.push({ field: 'files', message: `같은 층(${floor}) 파일이 둘 이상입니다: ${names.join(', ')}` })
      }
    }
    for (const f of files) {
      if (f.size > MAX_FILE_BYTES) errors.push({ field: 'files', message: `파일 하나는 30MB까지 올릴 수 있습니다: ${f.name} (${formatBytes(f.size)})` })
    }
    const total = files.reduce((sum, f) => sum + f.size, 0)
    if (total > MAX_TOTAL_BYTES) errors.push({ field: 'files', message: `전체 크기는 100MB까지입니다 (지금 ${formatBytes(total)}).` })
  }

  const revId = input.revId.trim()
  if (!revId) errors.push({ field: 'rev_id', message: '새 개정 ID를 입력하세요.' })
  else if (!REV_ID_PATTERN.test(revId)) errors.push({ field: 'rev_id', message: '개정 ID는 영문 대문자·숫자·-·_로 1~16자여야 합니다.' })
  else {
    const existing = input.revisions?.find((r) => r.rev_id === revId)
    if (existing && existing.model_source !== 'uploaded') {
      errors.push({ field: 'rev_id', message: `${revId}는 이미 있는 개정입니다. 업로드로 만든 개정만 같은 ID로 다시 올릴 수 있습니다.` })
    }
  }

  if (!input.effectiveDate) errors.push({ field: 'effective_date', message: '적용일을 입력하세요.' })
  else if (!validDate(input.effectiveDate)) errors.push({ field: 'effective_date', message: '적용일은 YYYY-MM-DD 형식의 날짜여야 합니다.' })
  return errors
}

/** Null when the file can be sent for this metadata kind. */
export function validateMetadataFile(kind: string, file: { name: string } | null | undefined): string | null {
  if (!file) return '올릴 파일을 고르세요.'
  const info = metadataKindInfo(kind)
  if (!info) return '메타데이터 종류를 고르세요.'
  if (!info.extensions.includes(extension(file.name))) return `${info.label} 종류는 ${info.formats} 파일만 올릴 수 있습니다.`
  return null
}

// --- normalization ----------------------------------------------------------------------------

export function normalizeJobSummary(raw: unknown): UploadJobSummary | null {
  if (!isRecord(raw) || !optText(raw.job_id)) return null
  return {
    job_id: text(raw.job_id),
    kind: text(raw.kind, 'drawing'),
    status: text(raw.status, 'queued'),
    rev_id: optText(raw.rev_id),
    created_at: optText(raw.created_at),
    updated_at: optText(raw.updated_at),
  }
}

export function normalizeJobList(raw: unknown): UploadJobSummary[] {
  return list(raw)
    .map(normalizeJobSummary)
    .filter((j): j is UploadJobSummary => j !== null)
}

function normalizeStep(r: Rec): UploadJobStep {
  const name = text(r.name)
  return { name, label: text(r.label, STEP_LABELS[name] ?? name), status: text(r.status, 'pending'), message: optText(r.message) }
}

function normalizeReviewItem(r: Rec): LayerReviewItem {
  return {
    floor: optText(r.floor),
    layer: text(r.layer),
    semantic: optText(r.semantic),
    confidence: num(r.confidence),
    alternatives: texts(r.alternatives),
    samples: texts(r.samples),
    question: optText(r.question),
  }
}

function countMap(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {}
  return Object.fromEntries(Object.entries(value).flatMap(([k, v]) => (num(v) !== null ? [[k, num(v)!]] : [])))
}

function normalizeQa(qa: Rec): NonNullable<UploadJob['qa']> {
  const issues = records(qa.issues).map((i) => ({
    severity: text(i.severity, 'warning'),
    code: text(i.code),
    unit_code: optText(i.unit_code),
    message: text(i.message),
  }))
  return {
    errors: num(qa.errors) ?? issues.filter((i) => i.severity === 'error').length,
    warnings: num(qa.warnings) ?? issues.filter((i) => i.severity === 'warning').length,
    infos: num(qa.infos) ?? issues.filter((i) => i.severity === 'info').length,
    issues,
  }
}

export function normalizeUploadJob(raw: unknown): UploadJob {
  const summary = normalizeJobSummary(raw)
  if (!summary || !isRecord(raw)) throw new Error('업로드 작업 응답 형식이 계약과 다릅니다 (job_id 필요).')
  const parse = isRecord(raw.parse) ? raw.parse : null
  const issues = parse && isRecord(parse.issues) ? parse.issues : {}
  const changes = isRecord(raw.changes) ? raw.changes : null
  const qa = isRecord(raw.qa) ? raw.qa : null
  const check = isRecord(raw.relayout_check) ? raw.relayout_check : null
  return {
    ...summary,
    parent_rev: optText(raw.parent_rev),
    effective_date: optText(raw.effective_date),
    profile: optText(raw.profile),
    files: records(raw.files).map((f) => ({ name: text(f.name), floor: optText(f.floor), bytes: num(f.bytes) })),
    floors_uploaded: texts(raw.floors_uploaded),
    floors_inherited: texts(raw.floors_inherited),
    steps: records(raw.steps).map(normalizeStep),
    parse: parse
      ? {
          units: num(parse.units),
          issues: { error: num(issues.error) ?? 0, warning: num(issues.warning) ?? 0, info: num(issues.info) ?? 0 },
          review_queue: records(parse.review_queue).map(normalizeReviewItem),
        }
      : null,
    changes: changes ? { total: num(changes.total) ?? 0, by_type: countMap(changes.by_type) } : null,
    impact: records(raw.impact).map((i) => ({ generator: text(i.generator), title_ko: text(i.title_ko, text(i.generator)), risk_tier: text(i.risk_tier) })),
    artifacts: records(raw.artifacts).map((a) => ({ generator: text(a.generator), status: text(a.status) })),
    qa: qa ? normalizeQa(qa) : null,
    relayout_check: check
      ? { brief_id: text(check.brief_id), alt_id: optText(check.alt_id), conformance_pct: num(check.conformance_pct), summary: text(check.summary) }
      : null,
    error: optText(raw.error) ?? (isRecord(raw.error) ? optText(raw.error.message) : null),
  }
}

export function normalizeProfiles(raw: unknown): DrawingProfile[] {
  return records(raw)
    .filter((p) => optText(p.profile_id))
    .map((p) => ({ profile_id: text(p.profile_id), title: text(p.title, text(p.profile_id)), description: optText(p.description), units: optText(p.units) }))
}

export function normalizeMetadataResult(raw: unknown): MetadataUploadResult {
  if (!isRecord(raw)) throw new Error('메타데이터 업로드 응답 형식이 계약과 다릅니다.')
  return {
    kind: text(raw.kind),
    status: text(raw.status, 'rejected'),
    rows: num(raw.rows),
    columns: texts(raw.columns),
    errors: texts(raw.errors),
    warnings: texts(raw.warnings),
    backup: optText(raw.backup),
    id: optText(raw.id),
  }
}

export function normalizeMetadataStatus(raw: unknown): MetadataStatus[] {
  return records(raw).map((s) => ({
    kind: text(s.kind),
    present: s.present === true,
    rows: num(s.rows),
    updated_at: optText(s.updated_at),
    required_columns: texts(s.required_columns),
    items: texts(s.items),
  }))
}
