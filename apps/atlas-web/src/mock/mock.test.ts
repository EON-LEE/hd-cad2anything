import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { QUICK_PROMPTS } from '../lib/prompts'
import { appReducer, createAppState, type AppState } from '../state/reducer'
import { createInitialUiState } from '../state/uiState'
import type { AgUiEvent, ChatRequest } from '../types/api'
import { buildMockArtifacts, mockLayoutAlternatives, mockProductivity, planImpact } from './analytics'
import { buildMockScript, chunkText, playScript, setMockSpeed } from './chatScripts'
import { buildMockFloor, buildMockMeta } from './floors'
import { createMockSource } from './mockSource'
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
  return events.reduce((s, event) => appReducer(s, { type: 'chat/event', event }), createAppState(createInitialUiState(meta)))
}

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
    expect(Math.max(...chunkText(text).map((c) => [...c].length))).toBeLessThanOrEqual(7)
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
    const started = events.filter((e) => e.type === 'TOOL_CALL_START').map((e) => (e as { tool_call_id: string }).tool_call_id)
    const ended = events.filter((e) => e.type === 'TOOL_CALL_END').map((e) => (e as { tool_call_id: string }).tool_call_id)
    expect(ended.sort()).toEqual(started.sort())
    const state = reduce(events)
    expect(state.chat.items.filter((i) => i.kind === 'state' && i.error)).toEqual([])
    expect(state.chat.items.some((i) => i.kind === 'assistant' && i.text.length > 20)).toBe(true)
    expect(state.chat.running).toBe(false)
  })

  it('revision prompt switches to S/S (R1) and opens the changes panel in compare mode', async () => {
    const state = reduce(await collect(request('S/S 개편 도면 변경점과 재생성 대상 알려줘')))
    expect(state.ui).toMatchObject({ rev: 'R1', compareRev: 'R0', panel: { type: 'changes', payload: { rev_from: 'R0', rev_to: 'R1' } } })
    expect(state.ui.highlights.length).toBeGreaterThan(0)
    expect(state.panelSeq).toBe(1)
  })

  it('table prompt opens a table panel with one row per occupied unit', async () => {
    const state = reduce(await collect(request('3층 평효율 표로 보여줘')))
    expect(state.ui.panel?.type).toBe('table')
    const payload = state.ui.panel?.payload as { columns: string[]; rows: unknown[][] }
    expect(payload.columns).toHaveLength(6)
    expect(payload.rows).toHaveLength(19)
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
