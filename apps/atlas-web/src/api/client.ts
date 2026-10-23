// Data access for the Atlas API contract (docs/specs/atlas-api.md).
import { categoryColorByIndex } from '../lib/colors'
import { normalizeJourney, segmentParam } from '../lib/journey'
import { normalizeBriefList, normalizeCheck, normalizeDiagnosis, normalizePlan, normalizeProject } from '../lib/relayout'
import { decodeAgUiMessage, readSseStream } from '../lib/sse'
import {
  normalizeJobList,
  normalizeMetadataResult,
  normalizeMetadataStatus,
  normalizeProfiles,
  normalizeUploadJob,
} from '../lib/uploads'
import type {
  AgUiEvent,
  Artifact,
  ArtifactFile,
  ChangesResponse,
  ChatRequest,
  DrawingProfile,
  DrawingUploadRequest,
  FeatureCollection,
  Journey,
  JourneyParams,
  LayerMapping,
  LayoutRequest,
  LayoutResponse,
  MetadataKind,
  MetadataStatus,
  MetadataUploadResult,
  Meta,
  ProductivityResponse,
  RelayoutBriefSummary,
  RelayoutCheck,
  RelayoutDiagnosis,
  RelayoutPlan,
  RelayoutPlanOptions,
  RelayoutProject,
  RelayoutSelectRequest,
  ReviewRequest,
  UploadJob,
  UploadJobCreated,
  UploadJobSummary,
} from '../types/api'

export interface AtlasDataSource {
  readonly mode: 'live' | 'mock'
  getMeta(): Promise<Meta>
  getFloor(rev: string, floor: string, includeFixtures: boolean): Promise<FeatureCollection>
  getChanges(revFrom: string, revTo: string): Promise<ChangesResponse>
  getArtifacts(rev: string): Promise<Artifact[]>
  reviewArtifact(rev: string, generator: string, body: ReviewRequest): Promise<Artifact>
  getProductivity(rev: string, floor: string): Promise<ProductivityResponse>
  getLayoutAlternatives(body: LayoutRequest): Promise<LayoutResponse>
  /** GET /api/analytics/journey; rejects with ApiError(404) when no journey matches. */
  getJourney(params: JourneyParams): Promise<Journey>
  /** POST /api/chat and deliver each AG-UI event; resolves when the stream ends. */
  streamChat(body: ChatRequest, onEvent: (event: AgUiEvent) => void, signal?: AbortSignal): Promise<void>
  artifactFileUrl(rev: string, generator: string, file: ArtifactFile): string | null

  // Seasonal relayout (§1.1)
  listRelayoutBriefs(): Promise<RelayoutBriefSummary[]>
  getRelayoutProject(briefId: string): Promise<RelayoutProject>
  diagnoseRelayout(briefId: string): Promise<RelayoutDiagnosis>
  /** Can take tens of seconds (diagnoses first when needed). */
  planRelayout(briefId: string, options: RelayoutPlanOptions): Promise<RelayoutPlan>
  selectRelayoutAlternative(briefId: string, body: RelayoutSelectRequest): Promise<RelayoutProject>
  checkRelayout(briefId: string, rev: string): Promise<RelayoutCheck>
  /** HTML proposal to open in a new tab; null when this source cannot provide one. */
  relayoutReportUrl(briefId: string): string | null

  // Drawing and metadata uploads (§1.2)
  uploadDrawings(request: DrawingUploadRequest): Promise<UploadJobCreated>
  listUploadJobs(): Promise<UploadJobSummary[]>
  getUploadJob(jobId: string): Promise<UploadJob>
  submitLayerMapping(jobId: string, mappings: LayerMapping[]): Promise<UploadJob>
  listDrawingProfiles(): Promise<DrawingProfile[]>
  uploadMetadata(kind: MetadataKind, file: File): Promise<MetadataUploadResult>
  getMetadataStatus(): Promise<MetadataStatus[]>
}

export class ApiError extends Error {
  readonly status: number
  /** Server-provided reason (FastAPI `detail`), shown to users as is; null when there is none. */
  readonly detail: string | null

  constructor(status: number, message: string, detail: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** User-facing text of an error: the server's `detail` when there is one, otherwise the message. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError && error.detail) return error.detail
  if (error instanceof Error) return error.message
  return String(error)
}

const enc = encodeURIComponent

/** `?customer_id=&date=&segment=&rank=` with empty parameters left out. */
export function journeyQuery(params: JourneyParams): string {
  const query = new URLSearchParams()
  if (params.customer_id) query.set('customer_id', params.customer_id)
  if (params.date) query.set('date', params.date)
  const segment = segmentParam(params.segment)
  if (segment) query.set('segment', segment)
  if (typeof params.rank === 'number' && Number.isFinite(params.rank) && params.rank >= 1) query.set('rank', String(Math.round(params.rank)))
  if (params.rev) query.set('rev', params.rev)
  const text = query.toString()
  return text ? `?${text}` : ''
}

/** FastAPI `detail`: a string, or a list of validation errors (`[{loc, msg}]`). */
function detailText(detail: unknown): string | null {
  if (typeof detail === 'string') return detail.trim() || null
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => (typeof d === 'string' ? d : d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : JSON.stringify(d)))
    return parts.join('; ') || null
  }
  if (detail === undefined || detail === null) return null
  return JSON.stringify(detail)
}

async function errorFrom(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => '')
  let detail: string | null = null
  try {
    const parsed = JSON.parse(text) as { detail?: unknown }
    if (parsed && typeof parsed === 'object') detail = detailText(parsed.detail)
  } catch {
    // not JSON: keep the raw text in the message only
  }
  const shown = detail ?? text
  return new ApiError(res.status, `${res.status} ${res.statusText}${shown ? `: ${shown.slice(0, 300)}` : ''}`, detail)
}

/** multipart body for POST /api/uploads/drawings (optional fields are left out when empty). */
export function drawingUploadForm(request: DrawingUploadRequest): FormData {
  const form = new FormData()
  for (const file of request.files) form.append('files', file, file.name)
  form.append('rev_id', request.rev_id)
  if (request.parent_rev) form.append('parent_rev', request.parent_rev)
  form.append('effective_date', request.effective_date)
  if (request.units) form.append('units', request.units)
  if (request.profile) form.append('profile', request.profile)
  if (typeof request.run_generators === 'boolean') form.append('run_generators', String(request.run_generators))
  return form
}

export function createHttpSource(baseUrl = ''): AtlasDataSource {
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' }
    // FormData bodies get their multipart Content-Type (with the boundary) from fetch itself.
    if (init.body && !(init.body instanceof FormData)) headers['Content-Type'] = 'application/json'
    const res = await fetch(`${baseUrl}${path}`, { ...init, headers: { ...headers, ...(init.headers as Record<string, string>) } })
    if (!res.ok) throw await errorFrom(res)
    return (await res.json()) as T
  }

  const post = <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', ...(body === undefined ? {} : { body: body instanceof FormData ? body : JSON.stringify(body) }) })
  const relayout = (briefId: string, action = '') => `/api/relayout/${enc(briefId)}${action ? `/${action}` : ''}`

  return {
    mode: 'live',
    getMeta: () => request<Meta>('/api/meta', { signal: AbortSignal.timeout(8000) }),
    getFloor: (rev, floor, includeFixtures) =>
      request<FeatureCollection>(`/api/floors/${enc(rev)}/${enc(floor)}${includeFixtures ? '?include=fixtures' : ''}`),
    getChanges: (revFrom, revTo) => request<ChangesResponse>(`/api/changes/${enc(revFrom)}/${enc(revTo)}`),
    getArtifacts: (rev) => request<Artifact[]>(`/api/artifacts/${enc(rev)}`),
    reviewArtifact: (rev, generator, body) =>
      request<Artifact>(`/api/artifacts/${enc(rev)}/${enc(generator)}/review`, { method: 'POST', body: JSON.stringify(body) }),
    getProductivity: (rev, floor) =>
      request<ProductivityResponse>(`/api/analytics/productivity?${new URLSearchParams({ rev, floor, level: 'unit' })}`),
    getLayoutAlternatives: (body) =>
      request<LayoutResponse>('/api/layout/alternatives', { method: 'POST', body: JSON.stringify(body) }),
    async getJourney(params) {
      const raw = await request<unknown>(`/api/analytics/journey${journeyQuery(params)}`)
      const journey = normalizeJourney(raw)
      if (!journey) throw new ApiError(404, '동선 응답에 표시할 결제 기록이 없습니다.')
      return journey
    },
    async streamChat(body, onEvent, signal) {
      const res = await fetch(`${baseUrl}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body),
        signal,
      })
      if (!res.ok) throw await errorFrom(res)
      if (!res.body) throw new ApiError(res.status, '응답 본문이 비어 있습니다.')
      await readSseStream(
        res.body,
        (message) => {
          for (const event of decodeAgUiMessage(message)) onEvent(event)
        },
        signal,
      )
    },
    artifactFileUrl(rev, generator, file) {
      if (file.url) return file.url
      if (!file.path) return null
      return `${baseUrl}/api/artifacts/${enc(rev)}/${enc(generator)}/${file.path.split('/').map(enc).join('/')}`
    },

    listRelayoutBriefs: async () => normalizeBriefList(await request<unknown>('/api/relayout/briefs')),
    getRelayoutProject: async (briefId) => normalizeProject(await request<unknown>(relayout(briefId))),
    async diagnoseRelayout(briefId) {
      const diagnosis = normalizeDiagnosis(await post<unknown>(relayout(briefId, 'diagnose')))
      if (!diagnosis) throw new ApiError(500, '진단 응답 형식이 계약과 다릅니다.')
      return diagnosis
    },
    async planRelayout(briefId, options) {
      const plan = normalizePlan(await post<unknown>(relayout(briefId, 'plan'), options))
      if (!plan) throw new ApiError(500, '대안 응답 형식이 계약과 다릅니다.')
      return plan
    },
    selectRelayoutAlternative: async (briefId, body) => normalizeProject(await post<unknown>(relayout(briefId, 'select'), body)),
    async checkRelayout(briefId, rev) {
      const check = normalizeCheck(await post<unknown>(relayout(briefId, 'check'), { rev }))
      if (!check) throw new ApiError(500, '도면 대조 응답 형식이 계약과 다릅니다.')
      return check
    },
    relayoutReportUrl: (briefId) => `${baseUrl}${relayout(briefId, 'report')}`,

    uploadDrawings: (body) => post<UploadJobCreated>('/api/uploads/drawings', drawingUploadForm(body)),
    listUploadJobs: async () => normalizeJobList(await request<unknown>('/api/uploads/jobs')),
    getUploadJob: async (jobId) => normalizeUploadJob(await request<unknown>(`/api/uploads/jobs/${enc(jobId)}`)),
    submitLayerMapping: async (jobId, mappings) =>
      normalizeUploadJob(await post<unknown>(`/api/uploads/jobs/${enc(jobId)}/layer-mapping`, { mappings })),
    listDrawingProfiles: async () => normalizeProfiles(await request<unknown>('/api/uploads/profiles')),
    async uploadMetadata(kind, file) {
      const form = new FormData()
      form.append('kind', kind)
      form.append('file', file, file.name)
      return normalizeMetadataResult(await post<unknown>('/api/uploads/metadata', form))
    },
    getMetadataStatus: async () => normalizeMetadataStatus(await request<unknown>('/api/uploads/metadata')),
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Validate the /api/meta payload and fill gaps (category colors, floor names). */
export function normalizeMeta(raw: unknown): Meta {
  if (!isRecord(raw) || !Array.isArray(raw.revisions) || !Array.isArray(raw.floors)) {
    throw new Error('meta 응답 형식이 계약과 다릅니다 (revisions/floors 배열 필요).')
  }
  const categories = Array.isArray(raw.categories) ? raw.categories : []
  return {
    facility: (raw.facility as Meta['facility']) ?? '',
    ...(typeof raw.data_through === 'string' && raw.data_through ? { data_through: raw.data_through } : {}),
    revisions: raw.revisions
      .filter(isRecord)
      .map((r) => ({
        rev_id: String(r.rev_id),
        kind: String(r.kind ?? 'actual'),
        parent_rev_id: r.parent_rev_id ? String(r.parent_rev_id) : null,
        effective_date: r.effective_date ? String(r.effective_date) : null,
        ...(typeof r.has_sales === 'boolean' ? { has_sales: r.has_sales } : {}),
        // `model_source` is the model origin; `source` is a free-text tool description and is not kept.
        ...(typeof r.model_source === 'string' && r.model_source ? { model_source: r.model_source } : {}),
        ...(typeof r.notes === 'string' && r.notes ? { notes: r.notes } : {}),
      })),
    floors: raw.floors
      .filter(isRecord)
      .map((f, i) => ({ code: String(f.code), ordinal: Number(f.ordinal ?? i), name: String(f.name ?? f.name_ko ?? '') })),
    categories: categories.filter(isRecord).map((c, i) => ({
      code: String(c.code),
      name_ko: String(c.name_ko ?? c.name ?? c.code),
      color: typeof c.color === 'string' && c.color ? c.color : categoryColorByIndex(i),
    })),
  }
}

export function facilityName(facility: Meta['facility'] | undefined): string {
  if (!facility) return ''
  if (typeof facility === 'string') return facility
  return facility.name_ko ?? facility.name ?? facility.code ?? ''
}
