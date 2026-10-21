import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgUiEvent } from '../types/api'
import { ApiError, createHttpSource, facilityName, normalizeMeta } from './client'

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('createHttpSource', () => {
  it('POSTs the chat request and streams decoded AG-UI events', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        'data: {"type":"RUN_STARTED","thread_id":"t1","run_id":"r1"}\n\n',
        'data: {"type":"TEXT_MESSAGE_CONTENT","message_id":"m","del',
        'ta":"안녕"}\n\ndata: {"type":"STATE_DELTA","delta":[{"op":"replace","path":"/floor","value":"1F"}]}\n\n',
        ': ping\n\ndata: {"type":"RUN_FINISHED","thread_id":"t1","run_id":"r1"}\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)
    const events: AgUiEvent[] = []
    await createHttpSource().streamChat({ message: '질문', ui_state: { rev: 'R2', floor: '3F', selection: ['3F-N05'] } }, (e) => events.push(e))

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>).Accept).toBe('text/event-stream')
    expect(JSON.parse(init.body as string)).toEqual({ message: '질문', ui_state: { rev: 'R2', floor: '3F', selection: ['3F-N05'] } })
    expect(events.map((e) => e.type)).toEqual(['RUN_STARTED', 'TEXT_MESSAGE_CONTENT', 'STATE_DELTA', 'RUN_FINISHED'])
    expect(events[1]).toMatchObject({ delta: '안녕' })
  })

  it('builds REST URLs from the contract', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(json({})))
    vi.stubGlobal('fetch', fetchMock)
    const source = createHttpSource()
    await source.getFloor('R2', '3F', true)
    await source.getFloor('R2', 'B1', false)
    await source.getChanges('R1', 'R2')
    await source.getArtifacts('R2')
    await source.getProductivity('R2', '3F')
    await source.reviewArtifact('R2', 'area_schedule', { decision: 'approve', reviewer: '검토자', note: '' })
    await source.getLayoutAlternatives({ rev: 'R2', floors: ['3F'], constraints: { max_moves: 3 } })
    const calls = fetchMock.mock.calls.map((call) => {
      const [url, init] = call as [string, RequestInit | undefined]
      return `${init?.method ?? 'GET'} ${url}`
    })
    expect(calls).toEqual([
      'GET /api/floors/R2/3F?include=fixtures',
      'GET /api/floors/R2/B1',
      'GET /api/changes/R1/R2',
      'GET /api/artifacts/R2',
      'GET /api/analytics/productivity?rev=R2&floor=3F&level=unit',
      'POST /api/artifacts/R2/area_schedule/review',
      'POST /api/layout/alternatives',
    ])
    expect(JSON.parse(fetchMock.mock.calls[5][1].body)).toEqual({ decision: 'approve', reviewer: '검토자', note: '' })
  })

  it('maps HTTP errors (including FastAPI "detail") to ApiError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'artifact is published' }, 409)))
    const error = await createHttpSource()
      .reviewArtifact('R2', 'x', { decision: 'reject', reviewer: 'a', note: '' })
      .catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409 })
    expect((error as Error).message).toContain('artifact is published')
  })

  it('derives artifact file URLs when the backend omits them', () => {
    const source = createHttpSource()
    expect(source.artifactFileUrl('R2', 'floor_directory_svg', { path: 'svg/floor 3F.svg' })).toBe(
      '/api/artifacts/R2/floor_directory_svg/svg/floor%203F.svg',
    )
    expect(source.artifactFileUrl('R2', 'g', { path: 'a.csv', url: '/files/a.csv' })).toBe('/files/a.csv')
  })
})

describe('normalizeMeta', () => {
  it('validates the shape and fills missing colors and names', () => {
    const meta = normalizeMeta({
      facility: { code: 'FLAGSHIP', name: '가상 플래그십점' },
      revisions: [{ rev_id: 'R0', kind: 'actual', parent_rev_id: null, effective_date: '2024-10-01' }],
      floors: [{ code: '1F', ordinal: 0, name_ko: '명품·화장품' }],
      categories: [{ code: 'LUXURY', name_ko: '명품' }],
    })
    expect(meta.floors[0]).toEqual({ code: '1F', ordinal: 0, name: '명품·화장품' })
    expect(meta.categories[0].color).toBe('#e5a8a8')
    expect(facilityName(meta.facility)).toBe('가상 플래그십점')
    expect(facilityName('플래그십')).toBe('플래그십')
    expect(() => normalizeMeta({ floors: [] })).toThrow()
  })
})
