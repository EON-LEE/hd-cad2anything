import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgUiEvent } from '../types/api'
import { ApiError, createHttpSource, drawingUploadForm, errorText, facilityName, journeyQuery, normalizeMeta } from './client'

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

  it('requests one customer journey with the segment string and normalizes it', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        json({ title: '40대 여성 VIP 고객의 쇼핑 동선', stops: [{ order: 1, time: '11:32', unit_code: 'B1-C05', floor: 'B1', x: 52.4, y: 31 }] }),
      )
      .mockResolvedValueOnce(json({ detail: 'no journey' }, 404))
      .mockResolvedValueOnce(json({ title: 'empty', stops: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const source = createHttpSource()
    const journey = await source.getJourney({ segment: { age_band: '40s', gender: 'F', tier: ['VIP', 'VVIP'] }, rank: 2 })
    expect(journey.stops[0]).toMatchObject({ unit_code: 'B1-C05', floor: 'B1', x: 52.4 })
    expect(journey.summary.stops).toBe(1)
    const url = fetchMock.mock.calls[0][0] as string
    expect(url.startsWith('/api/analytics/journey?')).toBe(true)
    expect(Object.fromEntries(new URL(url, 'http://x').searchParams)).toEqual({ segment: 'age_band:40s;gender:F;tier:VIP|VVIP', rank: '2' })
    await expect(source.getJourney({ customer_id: 'C1', date: '2025-11-08' })).rejects.toMatchObject({ status: 404 })
    expect(fetchMock.mock.calls[1][0]).toBe('/api/analytics/journey?customer_id=C1&date=2025-11-08')
    await expect(source.getJourney({})).rejects.toMatchObject({ status: 404 })
    expect(journeyQuery({ segment: '', rank: 0 })).toBe('')
    expect(journeyQuery({ segment: 'tier:VIP', rank: 3, rev: 'R2' })).toBe('?segment=tier%3AVIP&rank=3&rev=R2')
  })

  it('derives artifact file URLs when the backend omits them', () => {
    const source = createHttpSource()
    expect(source.artifactFileUrl('R2', 'floor_directory_svg', { path: 'svg/floor 3F.svg' })).toBe(
      '/api/artifacts/R2/floor_directory_svg/svg/floor%203F.svg',
    )
    expect(source.artifactFileUrl('R2', 'g', { path: 'a.csv', url: '/files/a.csv' })).toBe('/files/a.csv')
  })
})

const calls = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.map((call) => {
    const [url, init] = call as [string, RequestInit | undefined]
    return `${init?.method ?? 'GET'} ${url}`
  })

describe('createHttpSource: seasonal relayout', () => {
  const project = {
    brief: { brief_id: 'FW 2026', title: '2026 F/W MD 개편', base_rev: 'R2', floors: ['4F'], entries: [{ brand_id: 'B1', target_area_m2: [220, 150] }], exits: [] },
    status: 'planned',
    diagnosis: null,
    plan: { alternatives: [{ alt_id: 'ALT-0', rev_id: 'FW2026-A0', kpi: { net_krw: 0 }, changes: [], explanations: ['기준안'] }] },
    check: null,
    report_url: '/api/relayout/FW2026/report',
  }

  it('calls the relayout endpoints with the contract methods, paths and bodies', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json([{ brief_id: 'FW2026', title: 'F/W', entries: 2, exits: [1, 2], status: 'draft' }, { title: 'no id' }]))
      .mockResolvedValueOnce(json(project))
      .mockResolvedValueOnce(json({ season: { code: 'FW' }, candidates: [{ unit_code: '4F-E05', percentile: '2.1' }] }))
      .mockResolvedValueOnce(json({ alternatives: [], unplaced: [], caveats: ['추정치'] }))
      .mockResolvedValueOnce(json(project))
      .mockResolvedValueOnce(json({ rev: 'R3', conformance_pct: 75, items: [{ type: 'move', status: 'different_unit', status_label: '다른 매장' }] }))
    vi.stubGlobal('fetch', fetchMock)
    const source = createHttpSource()

    const briefs = await source.listRelayoutBriefs()
    expect(briefs).toEqual([expect.objectContaining({ brief_id: 'FW2026', entries: 2, exits: 2, status: 'draft' })])
    const loaded = await source.getRelayoutProject('FW 2026')
    expect(loaded.brief.entries[0].target_area_m2).toEqual([150, 220])
    expect(loaded.plan?.alternatives[0]).toMatchObject({ alt_id: 'ALT-0', rev_id: 'FW2026-A0', explanations: ['기준안'] })
    const diagnosis = await source.diagnoseRelayout('FW2026')
    expect(diagnosis.candidates[0]).toMatchObject({ unit_code: '4F-E05', floor: '4F', percentile: 2.1, reasons: [] })
    await source.planRelayout('FW2026', { objective: 'sales', max_moves: 4, downtime_weeks: 3, movable: 'all' })
    await source.selectRelayoutAlternative('FW2026', { alt_id: 'ALT-1', decided_by: 'MD팀' })
    const check = await source.checkRelayout('FW2026', 'R3')
    expect(check.items[0]).toMatchObject({ status: 'different_unit', status_label: '다른 매장', type_label: null })

    expect(calls(fetchMock)).toEqual([
      'GET /api/relayout/briefs',
      'GET /api/relayout/FW%202026',
      'POST /api/relayout/FW2026/diagnose',
      'POST /api/relayout/FW2026/plan',
      'POST /api/relayout/FW2026/select',
      'POST /api/relayout/FW2026/check',
    ])
    const init = (i: number) => fetchMock.mock.calls[i][1] as RequestInit
    expect(init(2).body).toBeUndefined()
    expect(JSON.parse(init(3).body as string)).toEqual({ objective: 'sales', max_moves: 4, downtime_weeks: 3, movable: 'all' })
    expect((init(3).headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(JSON.parse(init(4).body as string)).toEqual({ alt_id: 'ALT-1', decided_by: 'MD팀' })
    expect(JSON.parse(init(5).body as string)).toEqual({ rev: 'R3' })
    expect(source.relayoutReportUrl('FW2026')).toBe('/api/relayout/FW2026/report')
    expect(createHttpSource('http://api.test').relayoutReportUrl('A/B')).toBe('http://api.test/api/relayout/A%2FB/report')
  })

  it('passes the Korean `detail` of 400/404/409 responses through ApiError.detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(json({ detail: '선택한 대안이 없습니다.' }, 400))
        .mockResolvedValueOnce(json({ detail: [{ loc: ['body', 'rev'], msg: 'field required' }] }, 422))
        .mockResolvedValueOnce(new Response('upstream down', { status: 502 })),
    )
    const source = createHttpSource()
    const error = await source.checkRelayout('FW2026', 'R3').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 400, detail: '선택한 대안이 없습니다.' })
    expect(errorText(error)).toBe('선택한 대안이 없습니다.')
    expect(errorText(await source.checkRelayout('FW2026', '').catch((e: unknown) => e))).toBe('field required')
    const raw = await source.getRelayoutProject('X').catch((e: unknown) => e)
    expect(raw).toMatchObject({ status: 502, detail: null })
    expect(errorText(raw)).toContain('upstream down')
  })
})

describe('createHttpSource: uploads', () => {
  it('sends drawings as multipart FormData without a JSON content type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ job_id: 'J20261023-0001', status: 'queued' }, 202))
    vi.stubGlobal('fetch', fetchMock)
    const files = [new File(['0'], 'R3_4F.dxf'), new File(['1'], 'R3_5F.dxf')]
    const created = await createHttpSource().uploadDrawings({
      files,
      rev_id: 'R3',
      parent_rev: 'R2',
      effective_date: '2026-10-01',
      units: 'mm',
      profile: 'customer_style',
      run_generators: false,
    })
    expect(created).toEqual({ job_id: 'J20261023-0001', status: 'queued' })
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(`${init.method} ${url}`).toBe('POST /api/uploads/drawings')
    expect(init.headers).not.toHaveProperty('Content-Type')
    const form = init.body as FormData
    expect(form).toBeInstanceOf(FormData)
    expect((form.getAll('files') as File[]).map((f) => f.name)).toEqual(['R3_4F.dxf', 'R3_5F.dxf'])
    expect(Object.fromEntries([...form.entries()].filter(([key]) => key !== 'files'))).toEqual({
      rev_id: 'R3',
      parent_rev: 'R2',
      effective_date: '2026-10-01',
      units: 'mm',
      profile: 'customer_style',
      run_generators: 'false',
    })
    // Optional fields are left out so that the backend defaults apply.
    const minimal = drawingUploadForm({ files: [files[0]], rev_id: 'R3', effective_date: '2026-10-01', parent_rev: null, units: null })
    expect([...minimal.keys()]).toEqual(['files', 'rev_id', 'effective_date'])
  })

  it('reads jobs, maps layers, lists profiles and uploads metadata', async () => {
    const job = {
      job_id: 'J1',
      status: 'needs_review',
      rev_id: 'R3',
      steps: [{ name: 'parse', status: 'done' }],
      parse: { units: 50, issues: { warning: 3 }, review_queue: [{ layer: 'LAYER_17', confidence: 0.55, alternatives: ['walkway'] }] },
      qa: { issues: [{ code: 'area_mismatch', message: '6.0% 다릅니다.' }] },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json([{ job_id: 'J1', status: 'done', rev_id: 'R3' }, { status: 'x' }]))
      .mockResolvedValueOnce(json(job))
      .mockResolvedValueOnce(json({ ...job, status: 'running' }))
      .mockResolvedValueOnce(json([{ profile_id: 'default', title: '기본' }]))
      .mockResolvedValueOnce(json({ kind: 'brands', status: 'rejected', rows: 0, errors: ['필수 열이 없습니다: brand_name', { message: '3행' }] }))
      .mockResolvedValueOnce(json([{ kind: 'brands', present: true, rows: 40, items: [] }]))
    vi.stubGlobal('fetch', fetchMock)
    const source = createHttpSource()
    expect(await source.listUploadJobs()).toEqual([expect.objectContaining({ job_id: 'J1', status: 'done', kind: 'drawing' })])
    const loaded = await source.getUploadJob('J1')
    expect(loaded.steps[0]).toEqual({ name: 'parse', label: '도면 읽기', status: 'done', message: null })
    expect(loaded.parse).toMatchObject({ units: 50, issues: { error: 0, warning: 3, info: 0 } })
    expect(loaded.parse?.review_queue[0]).toMatchObject({ layer: 'LAYER_17', confidence: 0.55, samples: [] })
    expect(loaded.qa).toMatchObject({ errors: 0, issues: [{ severity: 'warning', code: 'area_mismatch', unit_code: null }] })
    const resumed = await source.submitLayerMapping('J1', [{ layer: 'LAYER_17', semantic: 'walkway', floor: '4F' }])
    expect(resumed.status).toBe('running')
    expect(await source.listDrawingProfiles()).toEqual([{ profile_id: 'default', title: '기본', description: null, units: null }])
    const file = new File(['brand_id\n'], 'brands.csv', { type: 'text/csv' })
    const result = await source.uploadMetadata('brands', file)
    expect(result).toMatchObject({ status: 'rejected', errors: ['필수 열이 없습니다: brand_name', '3행'], warnings: [], backup: null, id: null })
    expect(await source.getMetadataStatus()).toEqual([{ kind: 'brands', present: true, rows: 40, updated_at: null, required_columns: [], items: [] }])

    expect(calls(fetchMock)).toEqual([
      'GET /api/uploads/jobs',
      'GET /api/uploads/jobs/J1',
      'POST /api/uploads/jobs/J1/layer-mapping',
      'GET /api/uploads/profiles',
      'POST /api/uploads/metadata',
      'GET /api/uploads/metadata',
    ])
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ mappings: [{ layer: 'LAYER_17', semantic: 'walkway', floor: '4F' }] })
    const metadataForm = fetchMock.mock.calls[4][1].body as FormData
    expect(metadataForm.get('kind')).toBe('brands')
    expect((metadataForm.get('file') as File).name).toBe('brands.csv')
  })

  it('rejects a duplicate revision with the 409 detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: '이미 있는 개정입니다: R2' }, 409)))
    const error = await createHttpSource()
      .uploadDrawings({ files: [new File(['0'], 'R2_3F.dxf')], rev_id: 'R2', effective_date: '2026-10-01' })
      .catch((e: unknown) => e)
    expect(error).toMatchObject({ status: 409, detail: '이미 있는 개정입니다: R2' })
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

  it('keeps data_through, has_sales and model_source but not the free-text source', () => {
    const meta = normalizeMeta({
      facility: 'F',
      data_through: '2026-09-30',
      revisions: [
        { rev_id: 'R2', kind: 'actual', parent_rev_id: 'R1', effective_date: '2025-09-01', has_sales: true, model_source: 'parsed', source: 'cad2anything parse v1' },
        { rev_id: 'R3', kind: 'actual', parent_rev_id: 'R2', effective_date: '2026-10-01', has_sales: false, model_source: 'uploaded' },
        { rev_id: 'H1', kind: 'hypothetical', has_sales: null },
      ],
      floors: [],
    })
    expect(meta.data_through).toBe('2026-09-30')
    expect(meta.revisions[0]).toEqual({ rev_id: 'R2', kind: 'actual', parent_rev_id: 'R1', effective_date: '2025-09-01', has_sales: true, model_source: 'parsed' })
    expect(meta.revisions[1]).toMatchObject({ has_sales: false, model_source: 'uploaded' })
    expect(meta.revisions[2]).not.toHaveProperty('has_sales')
    expect(normalizeMeta({ revisions: [], floors: [] })).not.toHaveProperty('data_through')
  })
})
