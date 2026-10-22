import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { failedUiSteps, type ChatRun } from '../lib/chatRun'
import { QUICK_PROMPTS } from '../lib/prompts'
import { appReducer, createAppState, type AppState, type ChatItem } from '../state/reducer'
import { createInitialUiState } from '../state/uiState'
import type { AgUiEvent, ChatRequest } from '../types/api'
import { buildMockArtifacts, mockLayoutAlternatives, mockProductivity, planImpact } from './analytics'
import { buildMockScript, chunkText, lastFloorMention, playScript, segmentFromMessage, setMockSpeed } from './chatScripts'
import { buildMockFloor, buildMockMeta } from './floors'
import { ESCALATORS, JOURNEY_REV, MOCK_JOURNEY_COUNT, corridorAccess, mockJourney, segmentLabelKo, unitEntrance } from './journey'
import { createMockSource } from './mockSource'
import { mockPlace, mockRoute } from './route'
import { buildWorld, changesBetween } from './world'

const world = buildWorld()
const meta = buildMockMeta(world)

const request = (message: string, rev = 'R2', floor = '3F'): ChatRequest => ({ message, ui_state: { rev, floor, selection: [] } })

async function collect(req: ChatRequest): Promise<AgUiEvent[]> {
  const events: AgUiEvent[] = []
  await playScript(buildMockScript(world, req, 'thread', 'run'), (e) => events.push(e))
  return events
}

function reduce(events: AgUiEvent[]): AppState {
  const start = appReducer(createAppState(createInitialUiState(meta)), { type: 'chat/user', id: 'u', text: 'q', at: 0 })
  return events.reduce((s, event, i) => appReducer(s, { type: 'chat/event', event, at: i + 1 }), start)
}

const runOf = (state: AppState): ChatRun => (state.chat.items.at(-1) as Extract<ChatItem, { kind: 'run' }>).run

beforeAll(() => setMockSpeed(0))
afterAll(() => setMockSpeed(1))

describe('mock world fixtures', () => {
  it('exposes a two-floor meta with revisions and catalog categories', () => {
    expect(meta.floors.map((f) => f.code)).toEqual(['1F', '3F'])
    expect(meta.revisions.map((r) => `${r.rev_id}:${r.kind}:${r.parent_rev_id}`)).toEqual([
      'R0:actual:null',
      'R1:actual:R0',
      'R2:actual:R1',
      'H1:hypothetical:R2',
    ])
    expect(meta.categories).toHaveLength(23)
    expect(meta.categories.find((c) => c.code === 'WOMEN_CONTEMPORARY')?.name_ko).toBe('여성 컨템포러리')
  })

  it('builds floor GeoJSON that follows the contract', () => {
    const fc = buildMockFloor(world, 'R2', '3F', true)!
    const byKind = (kind: string) => fc.features.filter((f) => f.properties.kind === kind)
    expect(byKind('unit')).toHaveLength(19)
    expect(byKind('walkway').length).toBeGreaterThanOrEqual(17)
    expect(byKind('vertical_link').map((f) => f.properties.link_id)).toEqual(['ESC-A', 'ESC-B', 'EV-1', 'EV-2', 'ST-1', 'ST-2'])
    expect(byKind('amenity').length).toBeGreaterThanOrEqual(3)
    expect(byKind('fixture').length).toBeGreaterThan(100)
    for (const unit of byKind('unit')) {
      expect(Object.keys(unit.properties)).toEqual(
        expect.arrayContaining(['kind', 'unit_code', 'brand_id', 'brand_name', 'category', 'vacant', 'area_m2', 'zone_code']),
      )
      expect(unit.id).toBe(`U-${unit.properties.unit_code}`)
    }
    expect(buildMockFloor(world, 'R2', '3F', false)!.features.some((f) => f.properties.kind === 'fixture')).toBe(false)
    expect(buildMockFloor(world, 'R2', '8F', false)).toBeNull()
  })

  it('composes change sets along the lineage and inverts them backwards', () => {
    const r0r1 = changesBetween(world, 'R0', 'R1')
    expect(new Set(r0r1.map((c) => c.change_type))).toEqual(
      new Set(['move', 'resize', 'add', 'walkway_change', 'reassign', 'merge', 'remove', 'fixture_change']),
    )
    const r1r2 = changesBetween(world, 'R1', 'R2')
    expect(changesBetween(world, 'R0', 'R2')).toEqual([...r0r1, ...r1r2])
    const back = changesBetween(world, 'R1', 'R0')
    expect(back).toHaveLength(r0r1.length)
    expect(back.find((c) => c.change_type === 'split')?.unit_codes_after).toEqual(['1F-N01', '1F-N02'])
    expect(back.every((c) => c.summary.startsWith('(역방향)'))).toBe(true)
    expect(changesBetween(world, 'R2', 'R2')).toEqual([])
  })

  it('plans regeneration by risk tier like the generator registry', () => {
    const impact = planImpact(changesBetween(world, 'R0', 'R1'))
    expect(impact.map((i) => i.generator)).toEqual(['area_schedule', 'pos_space_kpi', 'fixture_schedule', 'floor_directory_svg'])
    expect(impact[0]).toMatchObject({ risk_tier: 'high', approval_mode: 'pre_publish', floors: ['1F', '3F'] })
    expect(planImpact([])).toEqual([])
  })

  it('exposes artifacts with review states, files and QA issues', () => {
    const r2 = buildMockArtifacts(world, 'R2')!
    const status = Object.fromEntries(r2.map((a) => [a.generator, a.status]))
    expect(status).toMatchObject({ area_schedule: 'awaiting_approval', pos_space_kpi: 'qa_failed', floor_directory_svg: 'published' })
    const svg = r2.find((a) => a.generator === 'floor_directory_svg')!
    expect(svg.files.map((f) => f.path)).toEqual(['floor_1F.svg', 'floor_3F.svg'])
    expect(svg.files[0].url).toMatch(/^data:image\/svg\+xml/)
    expect(decodeURIComponent(svg.files[0].url!.split(',')[1])).toContain('Sevanne &amp; Corin')
    expect(r2.find((a) => a.generator === 'pos_space_kpi')!.issues).toHaveLength(1)
  })

  it('computes productivity rows with percentiles and low-efficiency flags', () => {
    const result = mockProductivity(world, 'R2', '3F')!
    expect(result.rows).toHaveLength(19)
    expect(result.metric).toMatchObject({ name: 'sales_per_m2', label: '평효율(원/㎡)' })
    expect(Math.min(...result.rows.map((r) => r.percentile ?? -1))).toBe(0)
    expect(Math.max(...result.rows.map((r) => r.percentile ?? -1))).toBe(100)
    expect(result.rows.filter((r) => r.low_efficiency).length).toBeGreaterThanOrEqual(2)
    for (const row of result.rows) expect(row.sales_per_m2).toBeGreaterThan(0)
  })

  it('generates layout alternatives that respect max_moves', () => {
    const alternatives = mockLayoutAlternatives(world, { rev: 'R2', floors: ['3F'], constraints: { max_moves: 2 } })
    expect(alternatives.map((a) => a.id)).toEqual(['ALT-3F-A', 'ALT-3F-B', 'ALT-3F-C'])
    expect(alternatives[2].constraints.find((c) => c.name === '이동 수 ≤ 2')?.ok).toBe(false)
    expect(alternatives[0].moves[0]).toMatchObject({ from_unit: '3F-E01', to_unit: '3F-C06', brand_name: 'Quorelle' })
  })
})

describe('scripted mock chat streams', () => {
  it('splits text into small chunks without losing characters', () => {
    const text = '3F 평효율 하위 매장은 3F-E01(Quorelle)입니다.\n- 원인: 저노출'
    expect(chunkText(text).join('')).toBe(text)
    expect(Math.max(...chunkText(text).map((c) => [...c].length))).toBeLessThanOrEqual(5)
  })

  it('low-efficiency prompt sets a heatmap, highlights 2 units, draws 3 edges and focuses', async () => {
    const events = await collect(request('3층 저효율 zone과 원인'))
    const state = reduce(events)
    expect(state.ui.floor).toBe('3F')
    expect(state.ui.heatmap?.label).toBe('평효율(원/㎡)')
    const values = Object.values(state.ui.heatmap?.values ?? {})
    expect(values.length).toBe(19)
    expect(Math.min(...values)).toBe(0)
    expect(Math.max(...values)).toBe(1)
    expect(state.ui.highlights).toHaveLength(2)
    expect(state.ui.edges).toHaveLength(3)
    expect(state.ui.edges.some((e) => e.to.startsWith('1F-'))).toBe(true)
    expect(state.ui.focus?.unit_codes).toEqual(state.ui.highlights.map((h) => h.unit_code))
  })

  it.each([...QUICK_PROMPTS, '3층 평효율 표로 보여줘', '3F-C02 매장 어때?', '안녕하세요'])('produces a well-formed AG-UI stream for "%s"', async (prompt) => {
    const events = await collect(request(prompt))
    expect(events[0].type).toBe('RUN_STARTED')
    expect(events.at(-1)?.type).toBe('RUN_FINISHED')
    const starts = events.filter((e): e is Extract<AgUiEvent, { type: 'TOOL_CALL_START' }> => e.type === 'TOOL_CALL_START')
    const ended = events.filter((e) => e.type === 'TOOL_CALL_END').map((e) => (e as { tool_call_id: string }).tool_call_id)
    expect(ended.sort()).toEqual(starts.map((e) => e.tool_call_id).sort())
    // Every call carries a user-facing label; the answer is one message streamed in small deltas.
    for (const start of starts) expect(start.label?.length).toBeGreaterThan(2)
    expect(events.filter((e) => e.type === 'TEXT_MESSAGE_START')).toHaveLength(1)
    const deltas = events.flatMap((e) => (e.type === 'TEXT_MESSAGE_CONTENT' ? [e.delta] : []))
    expect(deltas.length).toBeGreaterThan(10)
    expect(Math.max(...deltas.map((d) => [...d].length))).toBeLessThanOrEqual(5)
    const state = reduce(events)
    const run = runOf(state)
    expect(run.status).toBe('done')
    expect(failedUiSteps(run)).toEqual([])
    expect(run.answer.length).toBeGreaterThan(20)
    expect(state.chat.running).toBe(false)
  })

  it('revision prompt switches to S/S (R1) and opens the changes panel in compare mode', async () => {
    const state = reduce(await collect(request('S/S 개편 도면에서 뭐가 바뀌었는지 알려줘')))
    expect(state.ui).toMatchObject({ rev: 'R1', compareRev: 'R0', panel: { type: 'changes', payload: { rev_from: 'R0', rev_to: 'R1' } } })
    expect(state.ui.highlights.length).toBeGreaterThan(0)
    expect(state.panelSeq).toBe(1)
    expect(runOf(state).steps.map((s) => s.label)).toEqual([
      'S/S MD 개편(R1) 도면 변경 내역 확인',
      '다시 만들 산출물 확인',
      'R0 → R1 비교 표시',
      `3층 변경 매장 ${state.ui.highlights.length}곳 강조`,
      '변경 구역으로 확대',
      '변경 목록 열기',
    ])
  })

  it('answers questions about floors the mock lacks on 3F and says so', async () => {
    const productivity = reduce(await collect(request('4층에서 효율이 낮은 매장과 이유를 보여줘', 'R2', '1F')))
    expect(productivity.ui.floor).toBe('3F')
    expect(runOf(productivity).answer.startsWith('모의 데이터에는 4층 도면이 없어서 3층으로 대신 보여 드릴게요.')).toBe(true)
    const layout = reduce(await collect(request('4층 재배치안 3개 만들어 비교해줘')))
    expect(layout.ui.panel).toMatchObject({ type: 'layout', payload: { floors: ['3F'] } })
    expect(runOf(layout).answer).toContain('| ALT-3F-A |')
  })

  it('nests the specialist agent calls under consult_specialist', async () => {
    const events = await collect(request('3층 저효율 zone과 원인'))
    const starts = events.filter((e): e is Extract<AgUiEvent, { type: 'TOOL_CALL_START' }> => e.type === 'TOOL_CALL_START')
    const consult = starts.find((e) => e.tool_name === 'consult_specialist')!
    const nested = starts.filter((e) => e.parent_tool_call_id === consult.tool_call_id)
    expect(nested.map((e) => e.tool_name)).toEqual(['analytics_productivity', 'analytics_drivers', 'analytics_cross_shopping'])
    expect(nested.every((e) => e.agent === 'space-productivity-analyst')).toBe(true)
    const steps = runOf(reduce(events)).steps
    expect(steps.filter((s) => s.parentId === consult.tool_call_id).map((s) => s.label)).toEqual([
      '3층 공간 생산성 계산',
      '효율 저하 원인 분석',
      '저효율 매장 교차구매 확인',
    ])
  })

  it('cosmetics cross-shopping prompt centers the relationships on 1F cosmetics stores', async () => {
    const state = reduce(await collect(request('40대 VIP 고객은 화장품이랑 뭘 같이 사?')))
    expect(state.ui.floor).toBe('1F')
    expect(state.ui.edges).toHaveLength(5)
    expect(state.ui.highlights.map((h) => h.unit_code)).toEqual(['1F-C03', '1F-W01', '1F-N03'])
    expect(runOf(state).answer).toContain('화장품')
  })

  it('journey prompt draws one customer path across floors and opens the journey card', async () => {
    const events = await collect(request('VIP 고객 한 명의 쇼핑 동선을 보여줘'))
    const state = reduce(events)
    const run = runOf(state)
    expect(run.steps.map((s) => [s.label, s.toolName])).toEqual([
      ['고객 동선 찾기', 'analytics_customer_journey'],
      ['이전 분석 표시 지우기', 'ui_clear'],
      ['도면에 고객 동선 그리기', 'ui_show_journey'],
    ])
    expect(run.steps[2].patchKeys).toEqual(['journey', 'floor', 'panel'])
    const journey = state.ui.journey!
    expect(state.ui).toMatchObject({ heatmap: null, highlights: [], edges: [], compareRev: null })
    expect(state.ui.panel).toEqual({ type: 'journey', payload: {} })
    expect(state.ui.floor).toBe(journey.stops[0].floor)
    expect(journey.query).toEqual({ segment: { tier: 'VIP' }, rank: 1 })
    expect(journey.summary.floors).toEqual(['1F', '3F'])
    // Stops are real units of the journey revision, located at their entrances.
    const floors = world.revisions.get(JOURNEY_REV)!.floors
    for (const stop of journey.stops) {
      const unit = floors[stop.floor].units.find((u) => u.code === stop.unit_code)!
      expect(unit).toBeTruthy()
      expect([stop.x, stop.y]).toEqual(unitEntrance(unit))
    }
    // Each floor change is split at an escalator; interior points follow the corridor centerlines.
    expect(journey.transitions.map((t) => `${t.from_floor}>${t.to_floor}:${t.via}`)).toEqual(['1F>3F:ESC-B', '3F>1F:ESC-B'])
    expect(journey.segments).toHaveLength(journey.stops.length - 1 + journey.transitions.length)
    for (const segment of journey.segments) {
      expect(segment.points.length).toBeGreaterThanOrEqual(2)
      for (const p of segment.points.slice(1, -1)) {
        const q = corridorAccess(p)
        expect(Math.hypot(q[0] - p[0], q[1] - p[1])).toBeLessThan(1e-6)
      }
    }
    expect(journey.summary.walk_m).toBeGreaterThan(100)
    expect(run.answer).toContain('↑ 3층으로 이동 (에스컬레이터 B)')
  })

  it('table prompt opens a table panel with one row per occupied unit', async () => {
    const state = reduce(await collect(request('3층 평효율 표로 보여줘')))
    expect(state.ui.panel?.type).toBe('table')
    const payload = state.ui.panel?.payload as { columns: string[]; rows: unknown[][] }
    expect(payload.columns).toHaveLength(6)
    expect(payload.rows).toHaveLength(19)
  })

  it('route prompt draws the shortest walk from the main entrance to a 3F store', async () => {
    const events = await collect(request('정문에서 3층 매장까지 가는 길 알려줘'))
    const state = reduce(events)
    const run = runOf(state)
    expect(run.steps.map((s) => [s.label, s.toolName])).toEqual([
      ['정문에서 3층 Cafe Halune까지 경로 계산', 'spatial_route'],
      ['도면에 경로 그리기', 'ui_show_route'],
    ])
    // ui_show_route clears the other analysis overlays like the backend does.
    expect(run.steps[1].patchKeys).toEqual(['heatmap', 'highlights', 'edges', 'compareRev', 'journey', 'route', 'floor'])
    expect(state.ui).toMatchObject({ floor: '1F', heatmap: null, highlights: [], edges: [], compareRev: null, journey: null })
    const route = state.ui.route!
    expect(route).toMatchObject({ title: 'ENT-1 정문 → Cafe Halune(3F-C05)', rev: 'R2', via_type: 'escalator', walk_m: 85 })
    expect(route.origin).toEqual({ ref: 'ENT-1', kind: 'entrance', label: 'ENT-1 정문', floor: '1F', unit_code: null, x: 60, y: 0.4 })
    const unit = world.revisions.get('R2')!.floors['3F'].units.find((u) => u.code === '3F-C05')!
    const [ex, ey] = unitEntrance(unit)
    expect(route.destination).toEqual({ ref: '3F-C05', kind: 'unit', label: 'Cafe Halune(3F-C05)', floor: '3F', unit_code: '3F-C05', x: ex, y: ey })
    expect(route.transitions).toEqual([{ from_floor: '1F', to_floor: '3F', via: 'ESC-A', floors: 2, height_m: 10 }])
    // One line per floor: entrance -> ESC-A landing on 1F, landing -> store entrance on 3F, along the corridors.
    expect(route.segments.map((s) => s.floor)).toEqual(['1F', '3F'])
    const [leave, arrive] = route.segments
    expect(leave.points[0]).toEqual([60, 0.4])
    expect(leave.points.at(-1)).toEqual(ESCALATORS[0].at)
    expect(arrive.points[0]).toEqual(ESCALATORS[0].at)
    expect(arrive.points.at(-1)).toEqual([ex, ey])
    for (const segment of route.segments) {
      for (const p of segment.points.slice(1, -1)) {
        const q = corridorAccess(p)
        expect(Math.hypot(q[0] - p[0], q[1] - p[1])).toBeLessThan(1e-6)
      }
    }
    expect(route.caveat).toContain('에스컬레이터를 타는 구간과 층마다 갈아타는 거리는 넣지 않았어요.')
    expect(run.answer).toContain('에스컬레이터를 타고 3층으로 올라가요. (2개 층)')
    expect(run.answer).toContain('걷는 거리는 약 85m예요.')
  })

  it('reads the entrance, the store, the floor and the vertical link from the question', async () => {
    const byCode = reduce(await collect(request('서측 출입구에서 3F-N04까지 엘리베이터로 가는 길 알려줘'))).ui.route!
    expect(byCode.origin.ref).toBe('ENT-2')
    expect(byCode.destination).toMatchObject({ unit_code: '3F-N04', label: 'Sevrine Femme(3F-N04)' })
    expect(byCode.via_type).toBe('elevator')
    expect(byCode.transitions[0].via).toMatch(/^EV-/)
    expect(byCode.caveat).toContain('엘리베이터를 타는 구간은 넣지 않았어요.')
    const byBrand = reduce(await collect(request('동측 출입구에서 Atelier Noa 어떻게 가?'))).ui.route!
    expect(byBrand.origin.ref).toBe('ENT-3')
    expect(byBrand.destination).toMatchObject({ unit_code: '3F-N03', label: 'Atelier Noa(3F-N03)' })
    const sameFloor = reduce(await collect(request('정문에서 1층 매장까지 가는 길 알려줘')))
    expect(sameFloor.ui.route!.transitions).toEqual([])
    expect(sameFloor.ui.route!.segments.map((s) => s.floor)).toEqual(['1F'])
    expect(runOf(sameFloor).answer).toContain('1. 1층 정문(ENT-1)에서 Atelier Corvane(1F-C05)까지 약')
    const missing = runOf(reduce(await collect(request('정문에서 8층 매장까지 가는 길 알려줘'))))
    expect(missing.answer.startsWith('모의 데이터에는 8층 도면이 없어서 3층으로 대신 보여 드릴게요.')).toBe(true)
    expect(lastFloorMention('1층 정문에서 3층 카페까지')).toBe('3F')
    expect(lastFloorMention('지하 1층 식품관')).toBe('B1')
    expect(lastFloorMention('정문에서 카페까지')).toBeNull()
  })

  it('shows a route and a journey alone and drops both for later analysis displays', async () => {
    const routeEvents = await collect(request('정문에서 3층까지 가는 길 알려줘'))
    const journeyEvents = await collect(request('VIP 고객 한 명의 쇼핑 동선을 보여줘'))
    const afterJourney = reduce([...routeEvents, ...journeyEvents]).ui
    expect(afterJourney.journey).not.toBeNull()
    expect(afterJourney.route).toBeNull()
    const afterRoute = reduce([...journeyEvents, ...routeEvents]).ui
    expect(afterRoute.route).not.toBeNull()
    expect(afterRoute.journey).toBeNull()
    for (const prompt of ['3층 저효율 zone과 원인', 'S/S 개편 도면에서 뭐가 바뀌었는지 알려줘', '40대 VIP 고객은 화장품이랑 뭘 같이 사?']) {
      const later = reduce([...routeEvents, ...(await collect(request(prompt)))]).ui
      expect(later.route).toBeNull()
    }
  })

  it('reads a customer segment from the question', () => {
    expect(segmentFromMessage('40대 여성 VIP 고객 동선')).toEqual({ age_band: '40s', gender: 'F', tier: 'VIP' })
    expect(segmentFromMessage('60대 남성 VVIP')).toEqual({ age_band: '60s+', gender: 'M', tier: 'VVIP' })
    expect(segmentFromMessage('고객 한 명')).toEqual({})
    expect(segmentLabelKo('age_band:40s;gender:F;tier:VIP|VVIP')).toBe('40대 여성 VIP·VVIP')
  })
})

describe('mock journeys', () => {
  it('ranks customer-days and returns 404 past the last one', async () => {
    const source = createMockSource()
    const customers = []
    for (let rank = 1; rank <= MOCK_JOURNEY_COUNT; rank += 1) {
      const journey = await source.getJourney({ segment: 'tier:VIP', rank })
      expect(journey.query).toEqual({ segment: 'tier:VIP', rank })
      expect(journey.stops.length).toBeGreaterThanOrEqual(4)
      expect(journey.weekday).toBe('토')
      customers.push(journey.customer?.id)
    }
    expect(new Set(customers).size).toBe(MOCK_JOURNEY_COUNT)
    await expect(source.getJourney({ segment: 'tier:VIP', rank: MOCK_JOURNEY_COUNT + 1 })).rejects.toMatchObject({ status: 404 })
    expect((await source.getJourney({ customer_id: 'C010877' })).query?.rank).toBe(2)
    await expect(source.getJourney({ customer_id: 'NOPE' })).rejects.toBeInstanceOf(ApiError)
  })

  it('computes gaps, totals and durations from the scripted purchases', () => {
    const journey = mockJourney(world, { rank: 1 })!
    expect(journey.stops.map((s) => s.gap_min)).toEqual([null, 26, 43, 39, 45, 32])
    expect(journey.summary).toMatchObject({ stops: 6, total_krw: 2_335_500, duration_min: 185 })
    expect(journey.title).toBe('40대 여성 VIP 고객의 쇼핑 동선')
  })
})

describe('mock routes', () => {
  it('resolves entrances, stores and floor-qualified vertical links; unknown places give no route', () => {
    expect(mockPlace(world, 'R2', 'ENT-3')).toEqual({ ref: 'ENT-3', kind: 'entrance', label: 'ENT-3 동측 출입구', floor: '1F', unit_code: null, x: 119.6, y: 40 })
    expect(mockPlace(world, 'R2', '1f:esc-b')).toMatchObject({ ref: '1F:ESC-B', kind: 'escalator', label: 'ESC-B 에스컬레이터', floor: '1F', x: 69, y: 40 })
    expect(mockPlace(world, 'R2', '3F:EV-2')).toMatchObject({ kind: 'elevator', floor: '3F', x: 114, y: 68 })
    expect(mockPlace(world, 'R0', '3F-C02')?.label).toBe('3F-C02 (공실)')
    expect(mockPlace(world, 'R2', '8F:ESC-A')).toBeNull()
    expect(mockPlace(world, 'R2', 'ENT-9')).toBeNull()
    expect(mockRoute(world, { rev: 'R2', origin: 'ENT-1', destination: '9F-X01' })).toBeNull()
  })

  it('starts on an escalator landing without a walking line on that floor', () => {
    const route = mockRoute(world, { rev: 'R2', origin: '1F:ESC-A', destination: '3F-C05' })!
    expect(route.segments[0]).toEqual({ floor: '1F', points: [[51, 40]] })
    expect(route.transitions[0]).toMatchObject({ via: 'ESC-A', floors: 2 })
    expect(route.walk_m).toBe(20)
  })
})

describe('mock data source', () => {
  it('reviews artifacts once and rejects a second review', async () => {
    const source = createMockSource()
    const updated = await source.reviewArtifact('R2', 'area_schedule', { decision: 'approve', reviewer: '테스터', note: '확인' })
    expect(updated).toMatchObject({ status: 'published', reviewed_by: '테스터', review_note: '확인' })
    expect((await source.getArtifacts('R2')).find((a) => a.generator === 'area_schedule')?.status).toBe('published')
    await expect(source.reviewArtifact('R2', 'area_schedule', { decision: 'reject', reviewer: 'x', note: '' })).rejects.toBeInstanceOf(ApiError)
    await expect(source.getFloor('R9', '3F', false)).rejects.toMatchObject({ status: 404 })
  })

  it('aborts a running chat stream', async () => {
    setMockSpeed(1)
    try {
      const source = createMockSource()
      const controller = new AbortController()
      const events: AgUiEvent[] = []
      const run = source.streamChat(request('3층 저효율 zone과 원인'), (e) => {
        events.push(e)
        controller.abort()
      }, controller.signal)
      await expect(run).rejects.toMatchObject({ name: 'AbortError' })
      expect(events).toHaveLength(1)
    } finally {
      setMockSpeed(0)
    }
  })
})
