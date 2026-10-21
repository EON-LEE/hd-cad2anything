import { describe, expect, it } from 'vitest'
import type { AgUiEvent, UiState } from '../types/api'
import { appReducer, createAppState, touchedKeys, type AppAction, type AppState } from './reducer'
import { createInitialUiState } from './uiState'

const ui: UiState = createInitialUiState(null, { rev: 'R2', floor: '3F' })

function run(state: AppState, events: AgUiEvent[]): AppState {
  return events.reduce((s, event) => appReducer(s, { type: 'chat/event', event }), state)
}

describe('appReducer: chat event sequence', () => {
  it('assembles streamed text, tool calls and STATE_DELTA patches', () => {
    let state = appReducer(createAppState(ui), { type: 'chat/user', id: 'u1', text: '3층 저효율 zone과 원인' })
    expect(state.chat.running).toBe(true)

    state = run(state, [
      { type: 'RUN_STARTED', thread_id: 't1', run_id: 'r1' },
      { type: 'TEXT_MESSAGE_START', message_id: 'm1' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '3F 평효율을 ' },
      { type: 'TOOL_CALL_START', tool_call_id: 'c1', tool_name: 'analytics.productivity', args: { rev: 'R2', floor: '3F' } },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '계산했습니다.' },
    ])
    const midTool = state.chat.items.find((i) => i.kind === 'tool')
    expect(midTool).toMatchObject({ status: 'running', name: 'analytics.productivity' })
    expect(state.chat.items.find((i) => i.kind === 'assistant')).toMatchObject({ text: '3F 평효율을 계산했습니다.', streaming: true })

    state = run(state, [
      { type: 'TOOL_CALL_END', tool_call_id: 'c1', summary: '18개 매장 산출' },
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/heatmap', value: { metric: 'sales_per_m2', label: '평효율(원/㎡)', values: { '3F-N05': 0.82 } } },
          { op: 'add', path: '/highlights/-', value: { unit_code: '3F-N05', color: '#e53935', reason: '저효율 하위 10%' } },
          { op: 'replace', path: '/edges', value: [{ from: '3F-N05', to: '2F-C03', weight: 2.4, label: 'lift 2.4' }] },
          { op: 'replace', path: '/focus', value: { unit_codes: ['3F-N05'] } },
        ],
      },
      { type: 'TEXT_MESSAGE_END', message_id: 'm1' },
      { type: 'RUN_FINISHED', thread_id: 't1', run_id: 'r1' },
    ])

    expect(state.chat.running).toBe(false)
    expect(state.chat.threadId).toBe('t1')
    expect(state.chat.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'tool', 'state'])
    expect(state.chat.items[2]).toMatchObject({ status: 'done', summary: '18개 매장 산출' })
    expect(state.chat.items[3]).toMatchObject({ kind: 'state', paths: ['heatmap', 'highlights', 'edges', 'focus'] })
    expect(state.ui.heatmap?.values).toEqual({ '3F-N05': 0.82 })
    expect(state.ui.highlights).toEqual([{ unit_code: '3F-N05', color: '#e53935', reason: '저효율 하위 10%' }])
    expect(state.ui.edges[0]).toMatchObject({ from: '3F-N05', to: '2F-C03', weight: 2.4 })
    expect(state.ui.focus).toEqual({ unit_codes: ['3F-N05'] })
    expect(state.focusSeq).toBe(1)
    expect(state.panelSeq).toBe(0)
  })

  it('rejects an invalid STATE_DELTA atomically and records the error', () => {
    const before = createAppState(ui)
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
    expect(after.chat.items[0]).toMatchObject({ kind: 'state', error: expect.stringContaining('Operation 1 failed') })
  })

  it('switches panels and clears the selection when the floor changes', () => {
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
  })

  it('normalizes agent-provided values defensively', () => {
    const state = run(createAppState(ui), [
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/highlights', value: ['3F-C01', { color: '#fff' }] },
          { op: 'replace', path: '/panel', value: { type: 'unknown' } },
          { op: 'replace', path: '/heatmap', value: { metric: 'x', values: { a: 0.3, b: 'bad' } } },
        ],
      },
    ])
    expect(state.ui.highlights).toEqual([{ unit_code: '3F-C01' }])
    expect(state.ui.panel).toEqual({ type: 'none', payload: {} })
    expect(state.ui.heatmap).toEqual({ metric: 'x', label: 'x', values: { a: 0.3 } })
  })

  it('handles RUN_ERROR, content without START and stream closure', () => {
    let state = appReducer(createAppState(ui), { type: 'chat/user', id: 'u', text: 'hi' })
    state = run(state, [
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'orphan', delta: '부분 응답' },
      { type: 'TOOL_CALL_START', tool_call_id: 'c', tool_name: 'x', args: {} },
      { type: 'RUN_ERROR', message: '백엔드 오류' },
    ])
    expect(state.chat.running).toBe(false)
    expect(state.chat.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'tool', 'error'])
    expect(state.chat.items[1]).toMatchObject({ text: '부분 응답', streaming: false })
    expect(state.chat.items[2]).toMatchObject({ status: 'done' })

    const aborted = appReducer(appReducer(state, { type: 'chat/user', id: 'u2', text: 'again' }), { type: 'chat/closed', aborted: true })
    expect(aborted.chat.running).toBe(false)
    expect(aborted.chat.items.at(-1)).toMatchObject({ kind: 'notice' })
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
