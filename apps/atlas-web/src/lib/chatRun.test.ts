import { describe, expect, it } from 'vitest'
import type { AgUiEvent } from '../types/api'
import {
  THINKING_LABEL,
  closeRun,
  createRun,
  describeUiChange,
  failedUiSteps,
  finishRun,
  foldRunEvent,
  formatElapsed,
  runStatusText,
  stepDisplayLabel,
  stepsSummaryText,
  stepTree,
  toolLabel,
  type ChatRun,
} from './chatRun'

/** Fold events one second apart, starting at t=1000 ms; returns every intermediate run. */
function trace(events: AgUiEvent[], run: ChatRun = createRun('r', 0)): ChatRun[] {
  const runs: ChatRun[] = []
  events.reduce((current, event, i) => {
    const next = foldRunEvent(current, event, { at: (i + 1) * 1000 })
    runs.push(next)
    return next
  }, run)
  return runs
}

const fold = (events: AgUiEvent[], run?: ChatRun) => trace(events, run).at(-1)!

describe('chat run fold: journey example from the contract', () => {
  const events: AgUiEvent[] = [
    { type: 'RUN_STARTED', thread_id: 't', run_id: 'run-1' },
    { type: 'TOOL_CALL_START', tool_call_id: 'c1', tool_name: 'analytics_customer_journey', label: '고객 동선 찾기', args: { segment: 'tier:VIP' } },
    { type: 'TOOL_CALL_END', tool_call_id: 'c1', ok: true, summary: '{"stops": 4}' },
    { type: 'TOOL_CALL_START', tool_call_id: 'f1', tool_name: 'ui_show_journey', label: '도면에 고객 동선 그리기' },
    {
      type: 'STATE_DELTA',
      delta: [
        { op: 'replace', path: '/journey', value: { stops: [] } },
        { op: 'replace', path: '/floor', value: 'B1' },
        { op: 'replace', path: '/panel', value: { type: 'journey', payload: {} } },
      ],
    },
    { type: 'TOOL_CALL_END', tool_call_id: 'f1' },
    { type: 'TEXT_MESSAGE_START', message_id: 'm1' },
    { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '40대 여성 VIP 고객은 ' },
    { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '지하 1층에서 시작해' },
    { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: ' 3층까지 갔어요.' },
    { type: 'TEXT_MESSAGE_END', message_id: 'm1' },
    { type: 'RUN_FINISHED', thread_id: 't', run_id: 'run-1' },
  ]

  it('moves thinking -> working -> answering -> done with one live status line', () => {
    const runs = trace(events)
    expect(runStatusText(createRun('r', 0))).toBe('생각하는 중…')
    expect(runs.map((r) => r.status)).toEqual([
      'thinking',
      'working',
      'working',
      'working',
      'working',
      'working',
      'working',
      'answering',
      'answering',
      'answering',
      'answering',
      'done',
    ])
    expect(runs.map(runStatusText)).toEqual([
      '생각하는 중…',
      '고객 동선 찾기',
      '생각하는 중…',
      '도면에 고객 동선 그리기',
      '도면에 고객 동선 그리기',
      '생각하는 중…',
      '생각하는 중…',
      null,
      null,
      null,
      null,
      null,
    ])
  })

  it('keeps labelled steps, the patched keys and the streamed answer', () => {
    const run = fold(events)
    expect(run.runId).toBe('run-1')
    expect(run.steps.map((s) => [s.label, s.status, s.labelSource])).toEqual([
      ['고객 동선 찾기', 'done', 'backend'],
      ['도면에 고객 동선 그리기', 'done', 'backend'],
    ])
    expect(run.steps[1].patchKeys).toEqual(['journey', 'floor', 'panel'])
    expect(run.steps[0]).toMatchObject({ toolName: 'analytics_customer_journey', args: { segment: 'tier:VIP' }, summary: '{"stops": 4}' })
    expect(run.answer).toBe('40대 여성 VIP 고객은 지하 1층에서 시작해 3층까지 갔어요.')
    expect(run).toMatchObject({ startedAt: 0, endedAt: 12000, error: null })
    expect(stepsSummaryText(run)).toBe('작업 과정 보기 · 2단계 · 12초')
    expect(failedUiSteps(run)).toEqual([])
  })
})

describe('chat run fold: labels', () => {
  it('falls back to the local table when the backend sends no label', () => {
    expect(toolLabel('analytics_productivity')).toBe('공간 생산성 계산')
    expect(toolLabel('analytics.cross_shopping')).toBe('교차구매 분석')
    expect(toolLabel('atlas_registry_impact')).toBe('다시 만들 산출물 확인')
    expect(toolLabel('consult_specialist')).toBe('전문 에이전트에게 확인')
    expect(toolLabel('code_interpreter')).toBe('계산 실행')
    expect(toolLabel('analytics_new_metric')).toBe('데이터 분석')
    expect(toolLabel('relayout_diagnose')).toBe('시즌 개편 확인')
    expect(toolLabel('upload_job_status')).toBe('업로드 확인')
    expect(toolLabel('mystery')).toBe('작업 중')
    expect(toolLabel('')).toBe('작업 중')
  })

  it('replaces the start label with the end label and names unknown finished steps', () => {
    const run = fold([
      { type: 'TOOL_CALL_START', tool_call_id: 'a', tool_name: 'analytics_productivity', args: { floor: '4F' } },
      { type: 'TOOL_CALL_END', tool_call_id: 'a', label: '4층 공간 생산성 계산 (매장 31곳)', summary: 'raw' },
      { type: 'TOOL_CALL_START', tool_call_id: 'b', tool_name: 'mystery' },
      { type: 'TOOL_CALL_END', tool_call_id: 'b' },
    ])
    expect(run.steps.map((s) => s.label)).toEqual(['4층 공간 생산성 계산 (매장 31곳)', '작업 중'])
    expect(run.steps.map(stepDisplayLabel)).toEqual(['4층 공간 생산성 계산 (매장 31곳)', '기타 작업'])
  })

  it('describes UI tool steps from the patch they applied when no label is sent', () => {
    const run = fold([
      { type: 'TOOL_CALL_START', tool_call_id: 'u1', tool_name: 'ui_set_floor', args: { floor: '4F' } },
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/floor', value: '4F' }] },
      { type: 'TOOL_CALL_END', tool_call_id: 'u1', summary: '{"ok": true, "floor": "4F"}' },
      { type: 'TOOL_CALL_START', tool_call_id: 'u2', tool_name: 'ui_highlight_units' },
      {
        type: 'STATE_DELTA',
        delta: [{ op: 'replace', path: '/highlights', value: ['4F-A', '4F-B', '4F-C', '4F-D', '4F-E'].map((unit_code) => ({ unit_code })) }],
      },
      { type: 'TOOL_CALL_END', tool_call_id: 'u2' },
      { type: 'TOOL_CALL_START', tool_call_id: 'u3', tool_name: 'ui_focus', label: '매장 5곳으로 확대' },
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/focus', value: { unit_codes: ['4F-A'] } }] },
      { type: 'TOOL_CALL_END', tool_call_id: 'u3' },
      { type: 'TOOL_CALL_START', tool_call_id: 'u4', tool_name: 'ui_clear' },
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/heatmap', value: null }, { op: 'replace', path: '/journey', value: null }] },
      { type: 'TOOL_CALL_END', tool_call_id: 'u4' },
    ])
    expect(run.steps.map((s) => s.label)).toEqual(['4층으로 이동', '매장 5곳 강조', '매장 5곳으로 확대', '화면 정리'])
    expect(run.steps.map((s) => s.labelSource)).toEqual(['ui', 'ui', 'backend', 'fallback'])
  })

  it('records UI changes that arrive outside a tool call as a step', () => {
    const run = fold([
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/floor', value: 'B1' }, { op: 'replace', path: '/highlights', value: [{ unit_code: 'B1-C05' }] }] },
      { type: 'STATE_DELTA', delta: [{ op: 'add', path: '/heatmap/values/3F-N05', value: 0.2 }] },
    ])
    expect(run.status).toBe('working')
    expect(run.steps.map((s) => [s.label, s.status])).toEqual([
      ['지하 1층으로 이동 · 매장 1곳 강조', 'done'],
      ['화면 바꾸기', 'done'],
    ])
    expect(describeUiChange(null, [{ op: 'replace', path: '/panel', value: { type: 'table', payload: { title: '4층 평효율' } } }])).toBe('표 열기 · 4층 평효율')
    expect(describeUiChange('ui_show_panel', [{ op: 'replace', path: '/panel', value: { type: 'uploads', payload: { job_id: 'J1' } } }])).toBe('도면 업로드 열기')
    expect(describeUiChange('ui_show_panel', [{ op: 'replace', path: '/panel', value: { type: 'layout', payload: { brief_id: 'FW2026' } } }])).toBe(
      '시즌 개편 열기 · FW2026',
    )
    expect(describeUiChange('ui_show_panel', [{ op: 'replace', path: '/panel', value: { type: 'layout', payload: { floors: ['3F'] } } }])).toBe('재배치 대안 열기')
    expect(describeUiChange(null, [{ op: 'replace', path: '/rev', value: 'R1' }, { op: 'replace', path: '/compareRev', value: 'R0' }])).toBe(
      'R1 개정 보기 · R0 → R1 비교',
    )
    expect(describeUiChange('ui_compare', [{ op: 'replace', path: '/rev', value: 'R1' }, { op: 'replace', path: '/compareRev', value: 'R0' }])).toBe(
      'R0 → R1 비교',
    )
    expect(describeUiChange('ui_show_journey', [{ op: 'replace', path: '/journey', value: {} }])).toBeNull()
  })

  it('names the spatial and route tools and describes route patches', () => {
    expect(toolLabel('spatial_query')).toBe('공간 조건 조회')
    expect(toolLabel('spatial_route')).toBe('경로 계산')
    expect(toolLabel('spatial_describe_floor')).toBe('층 구조 확인')
    expect(toolLabel('atlas_spatial_route')).toBe('경로 계산')
    expect(toolLabel('ui_show_route')).toBe('도면에 경로 그리기')
    const run = fold([
      { type: 'TOOL_CALL_START', tool_call_id: 'r1', tool_name: 'spatial_route', args: { origin: 'ENT-1', destination: '8F-C05' } },
      { type: 'TOOL_CALL_END', tool_call_id: 'r1', ok: true },
      { type: 'TOOL_CALL_START', tool_call_id: 'u1', tool_name: 'ui_show_route' },
      {
        type: 'STATE_DELTA',
        delta: [
          { op: 'replace', path: '/journey', value: null },
          { op: 'replace', path: '/route', value: { title: 'ENT-1 정문 → Orlix Coffee(8F-C05)' } },
          { op: 'replace', path: '/floor', value: '1F' },
        ],
      },
      { type: 'TOOL_CALL_END', tool_call_id: 'u1' },
    ])
    expect(run.steps.map((s) => [s.label, s.labelSource])).toEqual([
      ['경로 계산', 'fallback'],
      ['경로 그리기', 'ui'],
    ])
    expect(run.steps[1].patchKeys).toEqual(['journey', 'route', 'floor'])
    expect(describeUiChange('ui_show_route', [{ op: 'replace', path: '/route', value: null }])).toBe('경로 지우기')
    expect(describeUiChange('ui_show_route', [{ op: 'replace', path: '/route/walk_m', value: 90 }])).toBeNull()
    expect(describeUiChange(null, [{ op: 'replace', path: '/route', value: {} }, { op: 'replace', path: '/floor', value: '8F' }])).toBe(
      '8층으로 이동 · 경로 그리기',
    )
    expect(describeUiChange(null, [{ op: 'remove', path: '/route' }])).toBe('경로 지우기')
  })

  it('names the flow tools and describes flow patches by mode', () => {
    expect(toolLabel('spatial_flow')).toBe('통로 집계 계산')
    expect(toolLabel('ui_show_flow')).toBe('도면에 통로 집계 그리기')
    expect(toolLabel('relayout_phasing')).toBe('공사 순서 계산')
    const run = fold([
      { type: 'TOOL_CALL_START', tool_call_id: 'u1', tool_name: 'ui_show_flow' },
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/route', value: null }, { op: 'replace', path: '/flow', value: { mode: 'popup', floors: {} } }] },
      { type: 'TOOL_CALL_END', tool_call_id: 'u1' },
    ])
    expect(run.steps.map((s) => [s.label, s.labelSource])).toEqual([['팝업 후보 표시', 'ui']])
    expect(describeUiChange('ui_show_flow', [{ op: 'replace', path: '/flow', value: { mode: 'egress' } }])).toBe('통로 폭·피난 점검 표시')
    expect(describeUiChange('ui_show_flow', [{ op: 'replace', path: '/flow', value: null }])).toBe('통로 집계 지우기')
    expect(describeUiChange(null, [{ op: 'replace', path: '/flow', value: { mode: 'traffic' } }, { op: 'replace', path: '/floor', value: '3F' }])).toBe(
      '3층으로 이동 · 추정 유동 표시',
    )
  })
})

describe('chat run fold: nested specialist calls', () => {
  const events: AgUiEvent[] = [
    { type: 'TOOL_CALL_START', tool_call_id: 'c1', tool_name: 'consult_specialist', label: '공간 생산성 전문가에게 확인', args: { agent: 'space_productivity_analyst' } },
    { type: 'TOOL_CALL_START', tool_call_id: 'n1', tool_name: 'analytics_productivity', parent_tool_call_id: 'c1', agent: 'space-productivity-analyst' },
    { type: 'TOOL_CALL_END', tool_call_id: 'n1' },
    { type: 'TOOL_CALL_START', tool_call_id: 'n2', tool_name: 'analytics_drivers', label: '효율 저하 원인 분석', parent_tool_call_id: 'c1' },
    { type: 'TOOL_CALL_END', tool_call_id: 'c1', summary: 'space_productivity_analyst 응답 812자' },
  ]

  it('shows "parent · child" while a nested step runs and the parent alone otherwise', () => {
    expect(trace(events).map(runStatusText)).toEqual([
      '공간 생산성 전문가에게 확인',
      '공간 생산성 전문가에게 확인 · 공간 생산성 계산',
      '공간 생산성 전문가에게 확인',
      '공간 생산성 전문가에게 확인 · 효율 저하 원인 분석',
      '생각하는 중…',
    ])
  })

  it('nests children under their parent and closes them with the parent', () => {
    const run = fold(events)
    expect(run.steps.map((s) => [s.id, s.status, s.parentId, s.agent])).toEqual([
      ['c1', 'done', null, null],
      ['n1', 'done', 'c1', 'space-productivity-analyst'],
      ['n2', 'done', 'c1', null],
    ])
    const tree = stepTree([...run.steps, { ...run.steps[1], id: 'orphan', parentId: 'gone' }])
    expect(tree.map((n) => [n.step.id, n.children.map((c) => c.step.id)])).toEqual([
      ['c1', ['n1', 'n2']],
      ['orphan', []],
    ])
  })
})

describe('chat run fold: errors and endings', () => {
  it('handles text that streams before, between and after tool calls in one message', () => {
    const runs = trace([
      { type: 'TEXT_MESSAGE_START', message_id: 'm' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '4층 데이터를 확인해 볼게요.' },
      { type: 'TOOL_CALL_START', tool_call_id: 'a', tool_name: 'analytics_productivity', label: '4층 공간 생산성 계산' },
      { type: 'TOOL_CALL_END', tool_call_id: 'a', ok: true },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '\n\n' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '효율이 낮은 매장은 두 곳이에요.' },
      { type: 'TEXT_MESSAGE_END', message_id: 'm' },
      { type: 'RUN_FINISHED', thread_id: 't', run_id: 'r' },
    ])
    expect(runs.map((r) => r.status)).toEqual(['thinking', 'answering', 'working', 'working', 'answering', 'answering', 'answering', 'done'])
    expect(runs.map(runStatusText)).toEqual([THINKING_LABEL, null, '4층 공간 생산성 계산', THINKING_LABEL, null, null, null, null])
    expect(runs.at(-1)!.answer).toBe('4층 데이터를 확인해 볼게요.\n\n효율이 낮은 매장은 두 곳이에요.')
  })
  it('marks failed tool calls from `ok` and from legacy summaries', () => {
    const run = fold([
      { type: 'TOOL_CALL_START', tool_call_id: 'a', tool_name: 'analytics_productivity' },
      { type: 'TOOL_CALL_END', tool_call_id: 'a', ok: false, summary: 'floor 9F not found' },
      { type: 'TOOL_CALL_START', tool_call_id: 'b', tool_name: 'web_search' },
      { type: 'TOOL_CALL_END', tool_call_id: 'b', summary: '오류: timeout' },
      { type: 'TOOL_CALL_START', tool_call_id: 'c', tool_name: 'ui_show_heatmap' },
      { type: 'TOOL_CALL_END', tool_call_id: 'c', summary: '{"ok": false, "error": "heatmap provider unavailable"}' },
      { type: 'TOOL_CALL_START', tool_call_id: 'd', tool_name: 'ui_focus' },
      { type: 'TOOL_CALL_END', tool_call_id: 'd', summary: '{"ok": true}', ok: true },
    ])
    expect(run.steps.map((s) => s.status)).toEqual(['error', 'error', 'error', 'done'])
    expect(run.steps[0].error).toBe('floor 9F not found')
    expect(run.status).toBe('working')
  })

  it('records rejected UI patches on the step', () => {
    const run = [
      { type: 'TOOL_CALL_START', tool_call_id: 'u', tool_name: 'ui_set_floor' } as AgUiEvent,
    ].reduce((r, e) => foldRunEvent(r, e, { at: 1 }), createRun('r', 0))
    const patched = foldRunEvent(run, { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/floor', value: '9F' }] }, { at: 2, patch: { keys: ['floor'], error: 'Operation 0 failed' } })
    const ended = foldRunEvent(patched, { type: 'TOOL_CALL_END', tool_call_id: 'u' }, { at: 3 })
    expect(ended.steps[0]).toMatchObject({ status: 'error', patchError: 'Operation 0 failed', label: '9층으로 이동' })
    expect(failedUiSteps(ended)).toHaveLength(1)
  })

  it('ends with an error on RUN_ERROR and closes running steps', () => {
    const run = fold([
      { type: 'TOOL_CALL_START', tool_call_id: 'a', tool_name: 'analytics_productivity' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '부분 응답' },
      { type: 'RUN_ERROR', message: '에이전트 실행 중 오류가 발생했습니다: RateLimitError' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: ' (무시됨)' },
    ])
    expect(run.status).toBe('error')
    expect(run.error).toBe('에이전트 실행 중 오류가 발생했습니다: RateLimitError')
    expect(run.answer).toBe('부분 응답')
    expect(run.steps[0]).toMatchObject({ status: 'error', endedAt: 3000 })
    expect(runStatusText(run)).toBeNull()
  })

  it('joins extra messages as paragraphs and ignores empty deltas', () => {
    const run = fold([
      { type: 'TEXT_MESSAGE_START', message_id: 'm1' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '첫 문단' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm2', delta: '둘째 문단' },
    ])
    expect(run.answer).toBe('첫 문단\n\n둘째 문단')
    expect(trace([{ type: 'TEXT_MESSAGE_START', message_id: 'm1' }, { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm1', delta: '' }]).map((r) => r.status)).toEqual([
      'thinking',
      'thinking',
    ])
  })

  it('handles user aborts and streams that close without RUN_FINISHED', () => {
    const running = fold([{ type: 'TOOL_CALL_START', tool_call_id: 'a', tool_name: 'registry_run' }])
    const stopped = finishRun(running, 'stopped', 5000)
    expect(stopped).toMatchObject({ status: 'stopped', endedAt: 5000 })
    expect(stopped.steps[0]).toMatchObject({ status: 'error', error: '중지됨' })
    expect(finishRun(stopped, 'done', 6000)).toBe(stopped)

    expect(closeRun(running, 7000)).toMatchObject({ status: 'error', error: expect.stringContaining('RUN_FINISHED') })
    const answered = fold([{ type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '답변' }])
    expect(closeRun(answered, 7000)).toMatchObject({ status: 'done', endedAt: 7000 })
  })

  it('formats the elapsed time of a run', () => {
    expect(formatElapsed(300)).toBe('1초 미만')
    expect(formatElapsed(23_400)).toBe('23초')
    expect(formatElapsed(65_000)).toBe('1분 5초')
    expect(formatElapsed(120_000)).toBe('2분')
    expect(stepsSummaryText(createRun('r', 0))).toBe('작업 과정 보기 · 0단계')
  })
})
