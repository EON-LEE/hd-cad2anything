// Data access for the Atlas API contract (docs/specs/atlas-api.md).
import { categoryColorByIndex } from '../lib/colors'
import { normalizeJourney, segmentParam } from '../lib/journey'
import { decodeAgUiMessage, readSseStream } from '../lib/sse'
import type {
  AgUiEvent,
  Artifact,
  ArtifactFile,
  ChangesResponse,
  ChatRequest,
  FeatureCollection,
  Journey,
  JourneyParams,
  LayoutRequest,
  LayoutResponse,
  Meta,
  ProductivityResponse,
  ReviewRequest,
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
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
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

async function errorFrom(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => '')
  let detail = text
  try {
    const parsed = JSON.parse(text) as { detail?: unknown }
    if (parsed && parsed.detail !== undefined) detail = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail)
  } catch {
    // keep raw text
  }
  return new ApiError(res.status, `${res.status} ${res.statusText}${detail ? `: ${detail.slice(0, 300)}` : ''}`)
}

export function createHttpSource(baseUrl = ''): AtlasDataSource {
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (init.body) headers['Content-Type'] = 'application/json'
    const res = await fetch(`${baseUrl}${path}`, { ...init, headers: { ...headers, ...(init.headers as Record<string, string>) } })
    if (!res.ok) throw await errorFrom(res)
    return (await res.json()) as T
  }

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
    revisions: raw.revisions
      .filter(isRecord)
      .map((r) => ({
        rev_id: String(r.rev_id),
        kind: String(r.kind ?? 'actual'),
        parent_rev_id: r.parent_rev_id ? String(r.parent_rev_id) : null,
        effective_date: r.effective_date ? String(r.effective_date) : null,
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
