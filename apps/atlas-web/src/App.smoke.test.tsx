// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import type { AtlasDataSource } from './api/client'
import { createMockContext, type DataContextValue } from './api/source'
import { QUICK_PROMPTS } from './lib/prompts'
import { setMockSpeed } from './mock/chatScripts'
import type { AgUiEvent } from './types/api'

// jsdom has no WebGL, and importing the full deck.gl module graph is slow under the test runner.
// Replace deck.gl with light stand-ins: layers keep their props, and <DeckGL> renders a probe that
// exposes the layer ids, the number of unit polygons, journey stops and route markers.
vi.mock('@deck.gl/core', () => {
  class OrthographicView {
    props: unknown
    constructor(props: unknown) {
      this.props = props
    }
  }
  class LinearInterpolator {
    opts: unknown
    constructor(opts: unknown) {
      this.opts = opts
    }
  }
  return { OrthographicView, LinearInterpolator }
})
vi.mock('@deck.gl/layers', () => {
  class FakeLayer {
    id: string
    props: Record<string, unknown>
    constructor(props: Record<string, unknown>) {
      this.id = String(props.id)
      this.props = props
    }
  }
  return { PolygonLayer: FakeLayer, PathLayer: FakeLayer, ScatterplotLayer: FakeLayer, TextLayer: FakeLayer }
})
vi.mock('@deck.gl/react', async () => {
  const React = await import('react')
  return {
    default: (props: { layers?: { id: string; props: { data?: unknown[] } }[] }) => {
      const data = (id: string) => (props.layers ?? []).find((l) => l.id === id)?.props.data ?? []
      return React.createElement('div', {
        'data-testid': 'deck',
        'data-layers': (props.layers ?? []).map((l) => l.id).join(','),
        'data-units': data('units').length,
        'data-journey-stops': data('journey-stops').length,
        'data-route-places': (data('route-places') as { role?: string }[]).map((d) => d.role).join(','),
      })
    },
  }
})

const layerIds = () => screen.getByTestId('deck').getAttribute('data-layers')?.split(',') ?? []
const journeyStops = () => screen.getByTestId('deck').getAttribute('data-journey-stops')
const routePlaces = () => screen.getByTestId('deck').getAttribute('data-route-places')
const log = () => screen.getByRole('log')
const floorTab = (code: string) => within(screen.getByRole('tablist', { name: '층' })).getByRole('tab', { name: new RegExp(`^${code}`) })

/** A chat stream the test feeds event by event (null ends the stream). */
function controlledStream() {
  const queue: (AgUiEvent | null)[] = []
  let wake: (() => void) | null = null
  const streamChat: AtlasDataSource['streamChat'] = async (_body, onEvent) => {
    for (;;) {
      while (!queue.length) await new Promise<void>((resolve) => (wake = resolve))
      const event = queue.shift()!
      if (event === null) return
      onEvent(event)
    }
  }
  const push = async (...events: (AgUiEvent | null)[]) => {
    await act(async () => {
      queue.push(...events)
      wake?.()
    })
  }
  return { streamChat, push }
}

async function renderWithStream(streamChat: AtlasDataSource['streamChat']) {
  const context: DataContextValue = await createMockContext('테스트용 모의 데이터')
  render(<App context={{ ...context, source: { ...context.source, streamChat } }} />)
  const input = await screen.findByRole('textbox', { name: '에이전트에게 질문' })
  fireEvent.change(input, { target: { value: '4층 효율이 낮은 매장' } })
  fireEvent.keyDown(input, { key: 'Enter' })
}

describe('Atlas smoke test (mock mode, jsdom)', () => {
  beforeAll(() => {
    setMockSpeed(0)
    // Give the canvas a size so the view state (and the deck probe) is created.
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 1000 })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 700 })
    // No backend: /api/meta fails, so the app must fall back to the built-in fixtures.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
  })

  afterEach(() => cleanup())

  afterAll(() => {
    vi.unstubAllGlobals()
    setMockSpeed(1)
  })

  it('renders the shell, answers like a chat and drives the panels', async () => {
    render(<App />)

    // Shell and fallback to mock mode.
    expect(await screen.findByRole('heading', { name: 'CAD2Anything Atlas' })).toBeTruthy()
    expect(screen.getByText('모의 데이터')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain('/api/meta')

    // Revision selector, floor tabs from the mock meta (2 floors), canvas with the base layers.
    const floorTabs = within(screen.getByRole('tablist', { name: '층' })).getAllByRole('tab')
    expect(floorTabs.map((t) => t.textContent)).toEqual(['1F', '3F'])
    expect(floorTabs[1].getAttribute('aria-selected')).toBe('true')
    expect((screen.getByRole('combobox', { name: /개정/ }) as HTMLSelectElement).value).toBe('R2')
    await waitFor(() => expect(layerIds()).toEqual(expect.arrayContaining(['exterior', 'walkways', 'units', 'unit-labels'])))
    // 18 base units + the 3F-P01 popup added in R1.
    expect(screen.getByTestId('deck').getAttribute('data-units')).toBe('19')
    expect(screen.getByRole('complementary', { name: '범례' }).textContent).toContain('여성 컨템포러리')

    // Empty chat: intro and suggestion chips.
    expect(screen.getByRole('heading', { name: '무엇을 도와드릴까요?' })).toBeTruthy()
    const suggestions = within(screen.getByRole('group', { name: '추천 질문' })).getAllByRole('button')
    expect(suggestions.map((b) => b.textContent)).toEqual([...QUICK_PROMPTS])
    expect((screen.getByRole('textbox', { name: '에이전트에게 질문' }) as HTMLTextAreaElement).placeholder).toBe(
      '무엇이든 물어보세요 (Enter 전송, Shift+Enter 줄바꿈)',
    )

    // Low-efficiency question (the mock has no 4F, so it answers on 3F).
    fireEvent.click(screen.getByRole('button', { name: '4층에서 효율이 낮은 매장과 이유를 보여줘' }))
    await waitFor(() => expect(log().textContent).toContain('재배치 아이디어가 필요하면'))
    expect(within(log()).getByText('4층에서 효율이 낮은 매장과 이유를 보여줘')).toBeTruthy()
    // No tool names, raw summaries or patch logs in the conversation.
    for (const raw of ['analytics_productivity', 'consult_specialist', 'ui_show_heatmap', '화면 갱신', '"ok"']) {
      expect(log().textContent).not.toContain(raw)
    }
    // Collapsed steps above the answer; expanded they show labels with nested specialist calls.
    const toggle = within(log()).getByRole('button', { name: /^작업 과정 보기 · 8단계 · / })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(within(log()).queryByText('효율 저하 원인 분석')).toBeNull()
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(within(log()).getByText('공간 생산성 전문 에이전트에게 확인')).toBeTruthy()
    expect(within(log()).getByText('효율 저하 원인 분석').closest('ol')?.classList.contains('nested')).toBe(true)
    expect(within(log()).getByText('저효율 매장 2곳 강조')).toBeTruthy()
    expect(within(log()).getAllByRole('img', { name: '완료' })).toHaveLength(8)
    // Technical details only on demand.
    const tech = within(log()).getByRole('button', { name: '기술 정보' })
    expect(tech.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(tech)
    expect(log().textContent).toContain('analytics_productivity')
    expect(log().textContent).toContain('space-productivity-analyst')
    fireEvent.click(tech)
    expect(log().textContent).not.toContain('analytics_productivity')
    // The screen changed: heatmap, highlights, edges (one to 1F through a floor badge).
    await waitFor(() => expect(layerIds()).toEqual(expect.arrayContaining(['highlights', 'edges', 'edge-labels'])))
    const legend = screen.getByRole('complementary', { name: '범례' })
    expect(within(legend).getByRole('heading', { name: '평효율(원/㎡)' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /1F .*층으로 이동/ })).toBeTruthy()
    // After the first message the intro is gone and the suggestions shrink to one row.
    expect(screen.queryByRole('heading', { name: '무엇을 도와드릴까요?' })).toBeNull()
    expect(within(screen.getByRole('group', { name: '추천 질문' })).getAllByRole('button')).toHaveLength(QUICK_PROMPTS.length)

    // Revision review -> compare mode + review tab with changes, impact and artifacts.
    fireEvent.click(screen.getByRole('button', { name: 'S/S 개편 도면에서 뭐가 바뀌었는지 알려줘' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: '개정 검토' }).getAttribute('aria-selected')).toBe('true'))
    expect(await screen.findByRole('heading', { name: /변경 목록/ })).toBeTruthy()
    expect((screen.getByRole('combobox', { name: /^개정/ }) as HTMLSelectElement).value).toBe('R1')
    await waitFor(() => expect(layerIds()).toEqual(expect.arrayContaining(['compare-removed', 'compare-outlines'])))
    expect(screen.getByRole('complementary', { name: '범례' }).textContent).toContain('개정 비교 R0 → R1')
    expect(await screen.findByText('면적표', { selector: 'strong' })).toBeTruthy()
    const approve = await screen.findByRole('button', { name: '매대·집기 목록 승인' })
    fireEvent.click(approve)
    await waitFor(() => expect(screen.queryByRole('button', { name: '매대·집기 목록 승인' })).toBeNull())
    const card = screen.getByRole('article', { name: '매대·집기 목록' })
    expect(within(card).getByText('게시됨')).toBeTruthy()

    // Layout alternatives -> layout tab with three alternatives and a preview button.
    fireEvent.click(screen.getByRole('tab', { name: /에이전트/ }))
    fireEvent.click(screen.getByRole('button', { name: '4층 재배치안 3개 만들어 비교해줘' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: '레이아웃 대안' }).getAttribute('aria-selected')).toBe('true'))
    expect(await screen.findByRole('heading', { name: 'ALT-3F-A' })).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /미리보기/ })).toHaveLength(3)
    fireEvent.click(screen.getAllByRole('button', { name: /미리보기/ })[1])
    expect(screen.getByRole('button', { name: '미리보는 중' }).getAttribute('aria-pressed')).toBe('true')

    // Cosmetics cross-shopping -> relationship graph built from state.edges.
    fireEvent.click(screen.getByRole('tab', { name: /에이전트/ }))
    fireEvent.click(screen.getByRole('button', { name: '40대 VIP 고객은 화장품이랑 뭘 같이 사?' }))
    await waitFor(() => expect(log().textContent).toContain('화장품과 함께 많이 산 매장들이에요'))
    fireEvent.click(screen.getByRole('tab', { name: '관계 그래프' }))
    const graph = await screen.findByRole('group', { name: '매장 관계 그래프' })
    expect(within(graph).getAllByRole('button').length).toBe(8)
    const edgeTable = screen.getByRole('table', { name: '연결 목록' })
    expect(within(edgeTable).getAllByRole('row')).toHaveLength(6)

    // Clicking a unit opens the details card with KPIs from the productivity endpoint (via selection).
    fireEvent.click(within(graph).getByRole('button', { name: /^3F-C01/ }))
    const details = await screen.findByRole('complementary', { name: '3F-C01 매장 상세' })
    await waitFor(() => expect(details.textContent).toContain('평효율'))
  })

  it('draws one customer path across floors with its card', async () => {
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'VIP 고객 한 명의 쇼핑 동선을 보여줘' }))
    const journeyCard = await screen.findByRole('region', { name: '40대 여성 VIP 고객의 쇼핑 동선' })
    await waitFor(() => expect(log().textContent).toContain('결제 기록의 순서로 추정한 경로'))
    expect(within(log()).getByRole('button', { name: /^작업 과정 보기 · 3단계 · / })).toBeTruthy()

    // Card: summary chips, the timeline with floor changes, the caveat.
    const chips = within(within(journeyCard).getByRole('list', { name: '동선 요약' })).getAllByRole('listitem')
    expect(chips.map((c) => c.textContent).slice(0, 4)).toEqual(['결제 6회', '2개 층', '총 234만원', '3시간 5분'])
    expect(chips[4].textContent).toMatch(/^추정 이동 \d+m$/)
    expect(within(journeyCard).getByText('↑ 3층으로 이동 (에스컬레이터 B)')).toBeTruthy()
    expect(within(journeyCard).getByText('↓ 1층으로 이동 (에스컬레이터 B)')).toBeTruthy()
    expect(within(journeyCard).getByText('26분 후')).toBeTruthy()
    expect(journeyCard.textContent).toContain('고객 ID는 가명입니다')

    // Map: the first floor of the path with its stops; floor tabs mark floors with stops.
    await waitFor(() => expect(floorTab('1F').getAttribute('aria-selected')).toBe('true'))
    await waitFor(() => expect(layerIds()).toEqual(expect.arrayContaining(['journey-casing', 'journey-path', 'journey-stops', 'journey-times', 'journey-notes'])))
    expect(journeyStops()).toBe('3')
    expect(floorTab('1F').textContent).toContain('고객 동선 결제 3곳')
    expect(floorTab('3F').textContent).toContain('고객 동선 결제 3곳')
    const legend = screen.getByRole('complementary', { name: '범례' })
    expect(within(legend).getByRole('heading', { name: '고객 동선' })).toBeTruthy()
    expect(legend.textContent).toContain('결제 순서 기반 추정 경로')

    // Clicking a stop moves the map to its floor.
    const third = within(journeyCard).getByRole('button', { name: /^3번째 결제 12:41 3층 Cafe Halune/ })
    fireEvent.click(third)
    await waitFor(() => expect(floorTab('3F').getAttribute('aria-selected')).toBe('true'))
    expect(third.getAttribute('aria-current')).toBe('true')
    await waitFor(() => expect(journeyStops()).toBe('3'))

    // Other customers until there is none left.
    fireEvent.click(within(journeyCard).getByRole('button', { name: '다른 고객 보기' }))
    expect(await within(journeyCard).findByText(/고객 C010877/)).toBeTruthy()
    fireEvent.click(within(journeyCard).getByRole('button', { name: '다른 고객 보기' }))
    const third3 = await screen.findByRole('region', { name: '40대 여성 VVIP 고객의 쇼핑 동선' })
    fireEvent.click(within(third3).getByRole('button', { name: '다른 고객 보기' }))
    expect(await within(third3).findByText('더 보여 줄 고객이 없어요.')).toBeTruthy()

    // Close keeps the path; the legend reopens the card; clearing removes everything.
    fireEvent.click(within(third3).getByRole('button', { name: '닫기' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: /쇼핑 동선/ })).toBeNull())
    expect(layerIds()).toContain('journey-path')
    fireEvent.click(within(screen.getByRole('complementary', { name: '범례' })).getByRole('button', { name: '동선 목록 보기' }))
    const reopened = await screen.findByRole('region', { name: '40대 여성 VVIP 고객의 쇼핑 동선' })
    fireEvent.click(within(reopened).getByRole('button', { name: '동선 지우기' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: /쇼핑 동선/ })).toBeNull())
    await waitFor(() => expect(layerIds()).not.toContain('journey-path'))
    expect(within(screen.getByRole('complementary', { name: '범례' })).queryByRole('heading', { name: '고객 동선' })).toBeNull()
    expect(floorTab('1F').textContent).toBe('1F')
  })

  it('draws a walking route with its box, floor marks and legend, and clears it', async () => {
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '정문에서 3층까지 가는 길 알려줘' }))
    const box = await screen.findByRole('region', { name: 'ENT-1 정문 → Cafe Halune(3F-C05)' })
    await waitFor(() => expect(log().textContent).toContain('통로망 최단 경로로 계산한 거리라서'))
    expect(within(log()).getByRole('button', { name: /^작업 과정 보기 · 2단계 · / })).toBeTruthy()

    // Box: eyebrow, summary chips, the floors on the way and the caveat.
    expect(box.textContent).toContain('길 찾기')
    const chips = within(within(box).getByRole('list', { name: '경로 요약' })).getAllByRole('listitem')
    expect(chips.map((c) => c.textContent)).toEqual(['걷는 거리 85m', '에스컬레이터 A · 2개 층 이동 · 높이 10m'])
    const floorButtons = within(within(box).getByRole('list', { name: /경로가 지나는 층/ })).getAllByRole('button')
    expect(floorButtons.map((b) => b.getAttribute('aria-label'))).toEqual(['1층 출발, 도면 보기', '3층 도착, 도면 보기'])
    expect(box.textContent).toContain('에스컬레이터를 타는 구간과 층마다 갈아타는 거리는 넣지 않았어요.')

    // Map: the origin floor with the line, the start marker and the floor-change note; tabs mark both floors.
    await waitFor(() => expect(floorTab('1F').getAttribute('aria-selected')).toBe('true'))
    await waitFor(() =>
      expect(layerIds()).toEqual(expect.arrayContaining(['route-casing', 'route-path', 'route-places', 'route-place-labels', 'route-notes'])),
    )
    expect(routePlaces()).toBe('origin')
    expect(floorButtons[0].getAttribute('aria-current')).toBe('true')
    expect(floorTab('1F').textContent).toContain('길 찾기 경로: 출발 층')
    expect(floorTab('3F').textContent).toContain('길 찾기 경로: 도착 층')
    const legend = screen.getByRole('complementary', { name: '범례' })
    expect(within(legend).getByRole('heading', { name: '경로' })).toBeTruthy()
    expect(legend.textContent).toContain('통로망 최단 경로 · 걷는 거리 85m')

    // A floor in the box switches the map to that floor, where the end marker is.
    fireEvent.click(floorButtons[1])
    await waitFor(() => expect(floorTab('3F').getAttribute('aria-selected')).toBe('true'))
    await waitFor(() => expect(within(box).getByRole('button', { name: '3층 도착, 도면 보기' }).getAttribute('aria-current')).toBe('true'))
    await waitFor(() => expect(routePlaces()).toBe('destination'))

    // Another revision on screen: the box says which drawing the route was computed on.
    expect(box.textContent).not.toContain('도면으로 계산한 경로예요')
    fireEvent.change(screen.getByRole('combobox', { name: /^개정/ }), { target: { value: 'R1' } })
    await waitFor(() => expect(box.textContent).toContain('R2 도면으로 계산한 경로예요. 지금은 R1 도면을 보고 있어요.'))

    // "경로 지우기" clears the route from the map, the tabs and the legend.
    fireEvent.click(within(box).getByRole('button', { name: '경로 지우기' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: /Cafe Halune/ })).toBeNull())
    await waitFor(() => expect(layerIds()).not.toContain('route-path'))
    expect(floorTab('1F').textContent).toBe('1F')
    expect(floorTab('3F').textContent).toBe('3F')
    expect(within(screen.getByRole('complementary', { name: '범례' })).queryByRole('heading', { name: '경로' })).toBeNull()

    // Drawn again from the suggestion row; Esc inside the box clears it too.
    fireEvent.click(screen.getByRole('button', { name: '정문에서 3층까지 가는 길 알려줘' }))
    const again = await screen.findByRole('region', { name: 'ENT-1 정문 → Cafe Halune(3F-C05)' })
    fireEvent.keyDown(within(again).getByRole('button', { name: '경로 지우기' }), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('region', { name: /Cafe Halune/ })).toBeNull())
  })

  it('shows one live status line while working, then streams the answer', async () => {
    const stream = controlledStream()
    await renderWithStream(stream.streamChat)
    const status = await within(log()).findByRole('status')
    expect(status.textContent).toBe('생각하는 중…')
    await stream.push(
      { type: 'RUN_STARTED', thread_id: 't', run_id: 'r' },
      { type: 'TOOL_CALL_START', tool_call_id: 'c1', tool_name: 'consult_specialist', label: '공간 생산성 전문 에이전트에게 확인' },
    )
    await waitFor(() => expect(within(log()).getByRole('status').textContent).toBe('공간 생산성 전문 에이전트에게 확인'))
    // Without a label the local table names the call.
    await stream.push({ type: 'TOOL_CALL_START', tool_call_id: 'n1', tool_name: 'analytics_productivity', parent_tool_call_id: 'c1' })
    await waitFor(() => expect(within(log()).getByRole('status').textContent).toBe('공간 생산성 전문 에이전트에게 확인 · 공간 생산성 계산'))
    expect(screen.getByRole('button', { name: '중지' })).toBeTruthy()
    // No steps disclosure while only the status line is live.
    expect(within(log()).queryByRole('button', { name: /작업 과정 보기/ })).toBeNull()
    await stream.push(
      { type: 'TOOL_CALL_END', tool_call_id: 'n1' },
      { type: 'TOOL_CALL_END', tool_call_id: 'c1' },
      { type: 'TEXT_MESSAGE_START', message_id: 'm' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '먼저 3층과 비교해 볼게요.' },
    )
    // Text streams: the status line is gone and a cursor follows the text.
    await waitFor(() => expect(within(log()).queryByRole('status')).toBeNull())
    expect(log().querySelector('.answer .cursor')).not.toBeNull()
    expect(within(log()).getByRole('button', { name: '작업 과정 보기 · 2단계' })).toBeTruthy()
    // A tool call between two text parts: the live line comes back below the text, without a cursor.
    await stream.push({ type: 'TOOL_CALL_START', tool_call_id: 'c2', tool_name: 'ui_show_heatmap', label: '4층 도면에 ㎡당 매출 색칠' })
    const between = await within(log()).findByRole('status')
    expect(between.textContent).toBe('4층 도면에 ㎡당 매출 색칠')
    expect(log().querySelector('.answer')!.compareDocumentPosition(between) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(log().querySelector('.answer .cursor')).toBeNull()
    expect(within(log()).getByRole('button', { name: '작업 과정 보기 · 3단계' })).toBeTruthy()
    await stream.push(
      { type: 'TOOL_CALL_END', tool_call_id: 'c2', ok: true },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '\n\n' },
      { type: 'TEXT_MESSAGE_CONTENT', message_id: 'm', delta: '4층에서 효율이 낮은 매장은 두 곳이에요.' },
      { type: 'TEXT_MESSAGE_END', message_id: 'm' },
    )
    await stream.push({ type: 'RUN_FINISHED', thread_id: 't', run_id: 'r' }, null)
    await waitFor(() => expect(log().querySelector('.answer .cursor')).toBeNull())
    expect([...log().querySelectorAll('.answer p')].map((p) => p.textContent)).toEqual(['먼저 3층과 비교해 볼게요.', '4층에서 효율이 낮은 매장은 두 곳이에요.'])
    expect(within(log()).getByRole('button', { name: /^작업 과정 보기 · 3단계 · / })).toBeTruthy()
    expect(screen.getByRole('button', { name: '보내기' })).toBeTruthy()
    // "새 대화" clears the conversation.
    fireEvent.click(screen.getByRole('button', { name: '새 대화' }))
    expect(screen.getByRole('heading', { name: '무엇을 도와드릴까요?' })).toBeTruthy()
  })

  it('shows a friendly error with the technical message folded away', async () => {
    const stream = controlledStream()
    await renderWithStream(stream.streamChat)
    await stream.push(
      { type: 'RUN_STARTED', thread_id: 't', run_id: 'r' },
      { type: 'TOOL_CALL_START', tool_call_id: 'c1', tool_name: 'analytics_productivity', args: { floor: '4F' } },
      { type: 'TOOL_CALL_END', tool_call_id: 'c1', summary: 'raw summary' },
      { type: 'RUN_ERROR', message: '에이전트 실행 중 오류가 발생했습니다: RateLimitError' },
      null,
    )
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('답변을 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.')
    const details = within(alert).getByText('자세히 보기').closest('details')!
    expect(details.open).toBe(false)
    expect(details.textContent).toContain('RateLimitError')
    expect(within(alert).getByRole('button', { name: '다시 시도' })).toBeTruthy()
    fireEvent.click(within(log()).getByRole('button', { name: /^작업 과정 보기 · 1단계/ }))
    expect(within(log()).getByText('공간 생산성 계산')).toBeTruthy()
    expect(log().textContent).not.toContain('analytics_productivity')
    expect(log().textContent).not.toContain('raw summary')
  })
})
