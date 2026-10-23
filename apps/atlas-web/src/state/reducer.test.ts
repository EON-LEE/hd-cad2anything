import { describe, expect, it } from 'vitest'
import type { ChatRun } from '../lib/chatRun'
import type { AgUiEvent, UiState } from '../types/api'
import { appReducer, createAppState, touchedKeys, type AppAction, type AppState, type ChatItem } from './reducer'
import { createInitialUiState, panelPayloadString, panelTab } from './uiState'

const ui: UiState = createInitialUiState(null, { rev: 'R2', floor: '3F' })

function run(state: AppState, events: AgUiEvent[], at = 1000): AppState {
  return events.reduce((s, event, i) => appReducer(s, { type: 'chat/event', event, at: at + i * 1000 }), state)
}

const lastRun = (state: AppState): ChatRun => {
  const item = state.chat.items.at(-1) as Extract<ChatItem, { kind: 'run' }>
  expect(item.kind).toBe('run')
  return item.run
}

const journey = {
  title: '40대 여성 VIP 고객의 쇼핑 동선',
  stops: [
    { order: 1, time: '11:32', unit_code: '1F-C03', floor: '1F', amount_krw: 24000, gap_min: null, x: 90, y: 37 },
    { order: 2, time: '12:41', unit_code: '3F-C05', floor: '3F', amount_krw: 14500, gap_min: 69, x: 60, y: 50 },
  ],
  segments: [{ floor: '1F', points: [[90, 37], [90, 40], [70.5, 40]], from_order: 1, to_order: 2 }],
  transitions: [{ from_order: 1, to_order: 2, from_floor: '1F', to_floor: '3F', via: 'ESC-B' }],
}

const route = {
  title: 'ENT-1 정문 → Cafe Halune(3F-C05)',
  rev: 'R2',
  via_type: 'escalator',
  origin: { ref: 'ENT-1', kind: 'entrance', label: 'ENT-1 정문', floor: '1F', unit_code: null, x: 60, y: 0.4 },
  destination: { ref: '3F-C05', kind: 'unit', label: 'Cafe Halune(3F-C05)', floor: '3F', unit_code: '3F-C05', x: 60, y: 50 },
  segments: [
    { floor: '1F', points: [[60, 0.4], [60, 15], [45, 15], [45, 31.5], [49.5, 31.5], [49.5, 40], [51, 40]] },
    { floor: '3F', points: [[51, 40], [49.5, 40], [49.5, 48.5], [60, 48.5], [60, 50]] },
  ],
  transitions: [{ from_floor: '1F', to_floor: '3F', via: 'ESC-A', floors: 2, height_m: 10 }],
  walk_m: 85,
  caveat: '통로망 최단 경로로 계산한 걷는 거리예요.',
}

describe('appReducer: chat runs', () => {
  it('folds a run: labelled steps, STATE_DELTA patches and the streamed answer', () => {
    let state = appReducer(createAppState(ui), { type: 'chat/user', id: 'u1', text: '4층 저효율 매장', at: 0 })
    expect(state.chat.running).toBe(true)
    expect(state.chat.items.map((i) => i.kind)).toEqual(['user', 'run'])
    expect(lastRun(state).status).toBe('thinking')

    state = run(state, [
      { type: 'RUN_STARTED', thread_id: 't1', run_id: 'r1' },
      { type: 'TOOL_CALL_START', tool_call_id: 'c1', tool_name: 'analytics_productivity', args: { rev: 'R2', floor: '3F' } },
    ])
    expect(lastRun(state)).toMatchObject({ status: 'working', runId: 'r1' })
    expect(lastRun(state).steps[0]).toMatchObject({ label: '공간 생산성 계산', status: 'running' })

    state = run(
      state,
      [
        { type: 'TOOL_CALL_END', tool_call_id: 'c1', summary: '18개 매장 산출' },
        { type: 'TOOL_CALL_START', tool_call_id: 'u1', tool_name: 'ui_show_heatmap', label: '3층 도면을 평효율로 색칠' },
        {
          type: 'STATE_DELTA',
          delta: [
            { op: 'replace', path: '/heatmap', value: { metric: 'sales_per_m2', label: '평효율(원/㎡)', values: { '3F-N05': 0.82 } } },
            { op: 'add', path: '/highlights/-', value: { unit_code: '3F-N05', color: '#e53935', reason: '저효율 하위 10%' } },
            { op: 'replace', path: '/edges', value: [{ from: '3F-N05', to: '2F-C03', weight: 2.4, label: 'lift 2.4' }] },
            { op: 'replace', path: '/focus', value: { unit_codes: ['3F-N05'] } },
          ],
        },
        { type: 'TOOL_CALL_END', tool_call_id: 'u1' },
        { type: 'TEXT_MESSAGE_START', message_id: 'm1' },
        { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '3F 평효율을 ' },
        { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '계산했습니다.' },
        { type: 'TEXT_MESSAGE_END', message_id: 'm1' },
        { type: 'RUN_FINISHED', thread_id: 't1', run_id: 'r1' },
      ],
      3000,
    )

    expect(state.chat.running).toBe(false)
    expect(state.chat.threadId).toBe('t1')
    expect(state.chat.items.map((i) => i.kind)).toEqual(['user', 'run'])
    const finished = lastRun(state)
    expect(finished).toMatchObject({ status: 'done', answer: '3F 평효율을 계산했습니다.', startedAt: 0, endedAt: 11000 })
    expect(finished.steps.map((s) => [s.label, s.status])).toEqual([
      ['공간 생산성 계산', 'done'],
      ['3층 도면을 평효율로 색칠', 'done'],
    ])
    expect(finished.steps[1].patchKeys).toEqual(['heatmap', 'highlights', 'edges', 'focus'])
    expect(state.ui.heatmap?.values).toEqual({ '3F-N05': 0.82 })
    expect(state.ui.highlights).toEqual([{ unit_code: '3F-N05', color: '#e53935', reason: '저효율 하위 10%' }])
    expect(state.ui.edges[0]).toMatchObject({ from: '3F-N05', to: '2F-C03', weight: 2.4 })
    expect(state.ui.focus).toEqual({ unit_codes: ['3F-N05'] })
    expect(state.focusSeq).toBe(1)
    expect(state.panelSeq).toBe(0)
  })

  it('rejects an invalid STATE_DELTA atomically and marks the step', () => {
    const before = run(appReducer(createAppState(ui), { type: 'chat/user', id: 'u', text: 'x', at: 0 }), [
      { type: 'TOOL_CALL_START', tool_call_id: 'u1', tool_name: 'ui_set_floor' },
    ])
    const after = run(before, [
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/floor', value: '1F' },
          { op: 'replace', path: '/heatmap/values/3F-N05', value: 0.5 },
        ],
      },
    ])
    expect(after.ui).toBe(before.ui)
    expect(lastRun(after).steps[0]).toMatchObject({ status: 'error', patchError: expect.stringContaining('Operation 1 failed') })
  })

  it('applies STATE_DELTA outside a run without touching the transcript', () => {
    let state = appReducer(createAppState(ui), { type: 'selection/set', unitCodes: ['3F-C02'] })
    state = run(state, [
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/panel', value: { type: 'changes', payload: { rev_from: 'R1', rev_to: 'R2' } } },
          { op: 'replace', path: '/floor', value: '1F' },
        ],
      },
    ])
    expect(state.ui.panel).toEqual({ type: 'changes', payload: { rev_from: 'R1', rev_to: 'R2' } })
    expect(state.panelSeq).toBe(1)
    expect(state.selection).toEqual([])
    expect(state.chat.items).toEqual([])
    expect(state.chat.running).toBe(false)
  })

  it('accepts journey patches thanks to the journey: null default', () => {
    let state = run(createAppState(ui), [
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/journey', value: journey },
          { op: 'replace', path: '/floor', value: '1F' },
          { op: 'replace', path: '/panel', value: { type: 'journey', payload: {} } },
        ],
      },
    ])
    expect(state.ui.floor).toBe('1F')
    expect(state.ui.panel).toEqual({ type: 'journey', payload: {} })
    expect(state.ui.journey?.stops.map((s) => s.unit_code)).toEqual(['1F-C03', '3F-C05'])
    expect(state.ui.journey?.summary).toMatchObject({ stops: 2, floors: ['1F', '3F'], total_krw: 38500, duration_min: 69 })
    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/journey', value: null }] }])
    expect(state.ui.journey).toBeNull()
  })

  it('accepts route patches thanks to the route: null default', () => {
    let state = run(createAppState(ui), [
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/route', value: route },
          { op: 'replace', path: '/floor', value: '1F' },
        ],
      },
    ])
    expect(state.ui.floor).toBe('1F')
    expect(state.ui.route).toEqual(route)
    // A later patch of one field goes through normalization too.
    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/route/walk_m', value: '90' }] }])
    expect(state.ui.route?.walk_m).toBe(90)
    // The backend never sends a route and a journey together, but both still render side by side.
    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/journey', value: journey }] }])
    expect(state.ui.route?.origin.ref).toBe('ENT-1')
    expect(state.ui.journey?.stops).toHaveLength(2)
    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/route', value: null }] }])
    expect(state.ui.route).toBeNull()
    expect(state.ui.journey).not.toBeNull()
  })

  it('normalizes agent-provided values defensively', () => {
    const state = run(createAppState(ui), [
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/highlights', value: ['3F-C01', { color: '#fff' }] },
          { op: 'replace', path: '/panel', value: { type: 'unknown' } },
          { op: 'replace', path: '/heatmap', value: { metric: 'x', values: { a: 0.3, b: 'bad' } } },
          { op: 'replace', path: '/journey', value: { stops: 'none' } },
          { op: 'replace', path: '/route', value: { origin: { ref: 'ENT-1' }, destination: { unit_code: '3F-C05' } } },
        ],
      },
    ])
    expect(state.ui.highlights).toEqual([{ unit_code: '3F-C01' }])
    expect(state.ui.panel).toEqual({ type: 'none', payload: {} })
    expect(state.ui.heatmap).toEqual({ metric: 'x', label: 'x', values: { a: 0.3 } })
    expect(state.ui.journey).toBeNull()
    // "ENT-1" alone does not say which floor the walk starts on.
    expect(state.ui.route).toBeNull()
  })

  it('accepts the uploads panel and the layout brief payload, and still drops unknown panel types', () => {
    let state = run(createAppState(ui), [
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/panel', value: { type: 'uploads', payload: { job_id: 'J20261023-0001' } } }] },
    ])
    expect(state.ui.panel).toEqual({ type: 'uploads', payload: { job_id: 'J20261023-0001' } })
    expect(state.panelSeq).toBe(1)
    expect(panelTab(state.ui.panel)).toBe('review')
    expect(panelPayloadString(state.ui.panel, 'uploads', 'job_id')).toBe('J20261023-0001')

    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/panel', value: { type: 'uploads' } }] }])
    expect(state.ui.panel).toEqual({ type: 'uploads', payload: {} })
    expect(panelPayloadString(state.ui.panel, 'uploads', 'job_id')).toBeNull()
    expect(state.panelSeq).toBe(2)

    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/panel', value: { type: 'layout', payload: { brief_id: 'FW2026' } } }] }])
    expect(state.ui.panel).toEqual({ type: 'layout', payload: { brief_id: 'FW2026' } })
    expect(panelTab(state.ui.panel)).toBe('layout')
    expect(panelPayloadString(state.ui.panel, 'layout', 'brief_id')).toBe('FW2026')
    expect(panelPayloadString(state.ui.panel, 'uploads', 'brief_id')).toBeNull()

    // The older layout payload keeps working.
    const alternatives = [{ id: 'ALT-3F-A', moves: [] }]
    state = run(state, [
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/panel', value: { type: 'layout', payload: { rev: 'R2', floors: ['3F'], alternatives } } }] },
    ])
    expect(state.ui.panel).toEqual({ type: 'layout', payload: { rev: 'R2', floors: ['3F'], alternatives } })
    expect(panelPayloadString(state.ui.panel, 'layout', 'brief_id')).toBeNull()

    // A malformed payload is replaced by {}; an unknown type closes the panel without throwing.
    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/panel', value: { type: 'uploads', payload: ['J1'] } }] }])
    expect(state.ui.panel).toEqual({ type: 'uploads', payload: {} })
    state = run(state, [{ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/panel', value: { type: 'upload_wizard', payload: { job_id: 'J1' } } }] }])
    expect(state.ui.panel?.type).toBe('none')
    expect(panelTab(state.ui.panel)).toBeNull()
    expect(panelPayloadString(state.ui.panel, 'uploads', 'job_id')).toBeNull()
    expect(panelTab({ type: 'changes', payload: {} })).toBe('review')
    expect(panelTab(null)).toBeNull()
  })

  it('handles RUN_ERROR, content without START, aborts and streams that end early', () => {
    let state = appReducer(createAppState(ui), { type: 'chat/user', id: 'u', text: 'hi', at: 0 })
    state = run(state, [
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'orphan', delta: '부분 응답' },
      { type: 'TOOL_CALL_START', tool_call_id: 'c', tool_name: 'x', args: {} },
      { type: 'RUN_ERROR', message: '백엔드 오류' },
    ])
    expect(state.chat.running).toBe(false)
    expect(lastRun(state)).toMatchObject({ status: 'error', error: '백엔드 오류', answer: '부분 응답' })
    expect(lastRun(state).steps[0].status).toBe('error')

    const aborted = appReducer(appReducer(state, { type: 'chat/user', id: 'u2', text: 'again', at: 10 }), { type: 'chat/closed', aborted: true, at: 20 })
    expect(aborted.chat.running).toBe(false)
    expect(aborted.chat.items.map((i) => i.kind)).toEqual(['user', 'run', 'user', 'run'])
    expect(lastRun(aborted)).toMatchObject({ status: 'stopped', endedAt: 20 })

    const cut = appReducer(appReducer(state, { type: 'chat/user', id: 'u3', text: 'again', at: 10 }), { type: 'chat/closed', at: 30 })
    expect(lastRun(cut)).toMatchObject({ status: 'error', endedAt: 30 })
    // Closing after the run ended changes nothing.
    expect(appReducer(cut, { type: 'chat/closed' })).toBe(cut)
  })

  it('opens a run for agent output that arrives without a user message', () => {
    const state = run(createAppState(ui), [
      { type: 'TOOL_CALL_END', tool_call_id: 'stray' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '안내' },
      { type: 'RUN_FINISHED', thread_id: 't', run_id: 'r' },
    ])
    expect(state.chat.items.map((i) => i.kind)).toEqual(['run'])
    expect(lastRun(state)).toMatchObject({ status: 'done', answer: '안내', steps: [] })
    expect(appReducer(state, { type: 'chat/reset' }).chat).toEqual({ threadId: null, runId: null, running: false, items: [] })
  })

  it('applies user-originated patches through the same path', () => {
    const actions: AppAction[] = [
      { type: 'ui/patch', ops: [{ op: 'replace', path: '/layers/fixtures', value: true }] },
      { type: 'ui/patch', ops: [{ op: 'replace', path: '/compareRev', value: 'R1' }] },
      { type: 'ui/patch', ops: [{ op: 'remove', path: '/missing' }] },
    ]
    const state = actions.reduce(appReducer, createAppState(ui))
    expect(state.ui.layers.fixtures).toBe(true)
    expect(state.ui.compareRev).toBe('R1')
  })

  it('lists touched top-level keys', () => {
    expect(
      touchedKeys([
        { op: 'add', path: '/heatmap/values/3F-N05', value: 1 },
        { op: 'move', from: '/edges/0', path: '/edges/1' },
        { op: 'replace', path: '', value: {} },
      ]),
    ).toEqual(['heatmap', 'edges', '/'])
  })
})
