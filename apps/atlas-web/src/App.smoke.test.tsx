// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { Dispatch, ReactNode } from 'react'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { normalizeMeta, type AtlasDataSource } from './api/client'
import { createMockContext, type DataContextValue } from './api/source'
import { LayoutPanel } from './components/LayoutPanel'
import { DiagnosisView } from './components/RelayoutBrief'
import { RelayoutPhasingPanel } from './components/RelayoutPhasing'
import { ReviewPanel } from './components/ReviewPanel'
import { SidePanel } from './components/SidePanel'
import { UploadArea } from './components/UploadArea'
import { QUICK_PROMPTS } from './lib/prompts'
import { normalizeDiagnosis, normalizeProject } from './lib/relayout'
import { setMockSpeed } from './mock/chatScripts'
import { AppProviders } from './state/AppProviders'
import { replaceOp, type AppAction, type AppState } from './state/reducer'
import { useAppDispatch, useAppState } from './state/store'
import type { AgUiEvent } from './types/api'

// All screen tests share this file so that the slow jsdom environment is set up once per run.
// On a busy machine one test can take several seconds, so the screen tests get a longer timeout.
const SCREEN_TEST_TIMEOUT = 20_000

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
        'data-flow-paths': data('flow-paths').length,
        'data-flow-markers': data('flow-markers').length,
      })
    },
  }
})

const layerIds = () => screen.getByTestId('deck').getAttribute('data-layers')?.split(',') ?? []
const journeyStops = () => screen.getByTestId('deck').getAttribute('data-journey-stops')
const routePlaces = () => screen.getByTestId('deck').getAttribute('data-route-places')
const flowPaths = () => Number(screen.getByTestId('deck').getAttribute('data-flow-paths'))
const flowMarkers = () => screen.getByTestId('deck').getAttribute('data-flow-markers')
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

describe('Atlas smoke test (mock mode, jsdom)', { timeout: SCREEN_TEST_TIMEOUT }, () => {
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

  it('draws walkway aggregates with their box, floor marks and legend, alone, and clears them', async () => {
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '주말 팝업 어디가 좋아?' }))
    const box = await screen.findByRole('region', { name: '전체 고객 기준 팝업 후보(주말)' })
    await waitFor(() => expect(log().textContent).toContain('팝업 후보 5곳'))
    expect(within(log()).getByRole('button', { name: /^작업 과정 보기 · 2단계 · / })).toBeTruthy()

    // Box: eyebrow, summary chips, the floors with data and the ranked candidates.
    expect(box.querySelector('.flow-eyebrow')?.textContent).toBe('팝업 후보')
    const chips = within(within(box).getByRole('list', { name: '팝업 후보 요약' })).getAllByRole('listitem')
    expect(chips.map((c) => c.textContent)).toEqual([expect.stringMatching(/^고객-일 [\d,]+$/), expect.stringMatching(/^이동 [\d,]+건$/), '2개 층', '후보 5곳'])
    const floorButtons = within(within(box).getByRole('list', { name: /표시가 있는 층/ })).getAllByRole('button')
    expect(floorButtons.map((b) => b.getAttribute('aria-label'))).toEqual(['1층 팝업 후보 2곳, 도면 보기', '3층 팝업 후보 3곳, 도면 보기'])
    const candidates = within(within(box).getByRole('list', { name: '팝업 후보 5곳' })).getAllByRole('button')
    expect(candidates).toHaveLength(5)
    // The titles already start with "후보 N", so the number is not repeated in the names.
    expect(candidates[0].getAttribute('aria-label')).toMatch(/^후보 1 · 1F .+, 1층 도면에서 보기$/)
    expect(candidates[4].getAttribute('aria-label')).toBe('후보 5 · 3F 3F-P01 팝업 매장(지금 Velmira), 3층 도면에서 보기')
    expect(box.textContent).toContain('설치 가능 여부(소방·통로 폭)는 현장에서 확인해야 해요.')

    // Map: the top candidate's floor with lines and numbered markers; tabs mark both floors; the legend explains it.
    await waitFor(() => expect(floorTab('1F').getAttribute('aria-selected')).toBe('true'))
    await waitFor(() => expect(layerIds()).toEqual(expect.arrayContaining(['flow-casing', 'flow-paths', 'flow-markers', 'flow-marker-text'])))
    expect(flowPaths()).toBeGreaterThan(10)
    expect(flowMarkers()).toBe('2')
    expect(floorTab('1F').textContent).toContain('(팝업 후보 2곳)')
    expect(floorTab('3F').textContent).toContain('(팝업 후보 3곳)')
    const legend = screen.getByRole('complementary', { name: '범례' })
    expect(within(legend).getByRole('heading', { name: '팝업 후보' })).toBeTruthy()
    for (const text of ['적음', '많음', '통로 위 후보 공간', '공실·팝업 매장', '단위 명(추정)']) expect(legend.textContent).toContain(text)

    // A candidate on another floor moves the map there.
    fireEvent.click(candidates[4])
    await waitFor(() => expect(floorTab('3F').getAttribute('aria-selected')).toBe('true'))
    await waitFor(() => expect(flowMarkers()).toBe('3'))
    expect(floorButtons[1].getAttribute('aria-current')).toBe('true')

    // Drawn alone: a route replaces the flow.
    fireEvent.click(screen.getByRole('button', { name: '정문에서 3층까지 가는 길 알려줘' }))
    expect(await screen.findByRole('region', { name: 'ENT-1 정문 → Cafe Halune(3F-C05)' })).toBeTruthy()
    await waitFor(() => expect(screen.queryByRole('region', { name: /팝업 후보/ })).toBeNull())
    expect(layerIds()).not.toContain('flow-paths')
    await waitFor(() => expect(log().textContent).toContain('통로망 최단 경로로 계산한 거리라서'))

    // And the flow replaces the route; "표시 지우기" clears it from the map, the tabs and the legend.
    fireEvent.click(screen.getByRole('button', { name: '주말 팝업 어디가 좋아?' }))
    const again = await screen.findByRole('region', { name: '전체 고객 기준 팝업 후보(주말)' })
    await waitFor(() => expect(screen.queryByRole('region', { name: /Cafe Halune/ })).toBeNull())
    fireEvent.click(within(again).getByRole('button', { name: '표시 지우기' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: /팝업 후보/ })).toBeNull())
    await waitFor(() => expect(layerIds()).not.toContain('flow-paths'))
    expect(floorTab('1F').textContent).toBe('1F')
    expect(floorTab('3F').textContent).toBe('3F')
    expect(within(screen.getByRole('complementary', { name: '범례' })).queryByRole('heading', { name: '팝업 후보' })).toBeNull()
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

// --- 시즌 개편 and upload screens ----------------------------------------------------------------

let latest: AppState
let dispatch: Dispatch<AppAction>

/** Exposes the store to the test. */
function Probe() {
  latest = useAppState()
  dispatch = useAppDispatch()
  return null
}

async function renderWith(ui: ReactNode, prepare?: (context: DataContextValue) => Promise<DataContextValue>) {
  let context = await createMockContext('테스트용 모의 데이터')
  if (prepare) context = await prepare(context)
  render(
    <AppProviders context={context}>
      {ui}
      <Probe />
    </AppProviders>,
  )
  return context
}

/** Upload R3 (drawn F/W relayout on R2) through the mock source and wait for the job to finish. */
async function withUploadedR3(context: DataContextValue): Promise<DataContextValue> {
  const { source } = context
  const created = await source.uploadDrawings({ files: [new File(['0'], 'R3_3F.dxf')], rev_id: 'R3', effective_date: '2026-10-01', profile: 'customer_style' })
  for (let i = 0; i < 10 && (await source.getUploadJob(created.job_id)).status !== 'done'; i += 1);
  return { ...context, meta: normalizeMeta(await source.getMeta()) }
}

describe('season relayout and upload screens (mock mode, jsdom)', { timeout: SCREEN_TEST_TIMEOUT }, () => {
  const createObjectURL = URL.createObjectURL

  beforeAll(() => {
    setMockSpeed(0)
    // jsdom has no blob URLs; the mock report link needs one.
    URL.createObjectURL = () => 'blob:mock-report'
  })

  afterEach(() => cleanup())

  afterAll(() => {
    setMockSpeed(1)
    URL.createObjectURL = createObjectURL
  })

  describe('시즌 개편 (layout tab)', () => {
    it('diagnoses, plans with options, previews on the map, records a decision and checks the drawing', async () => {
      await renderWith(<LayoutPanel />, withUploadedR3)
      // The default revision skips R3 (uploaded, no sales yet).
      expect(latest.ui.rev).toBe('R2')
      expect(screen.getByRole('tab', { name: '시즌 개편' }).getAttribute('aria-selected')).toBe('true')
      const picker = (await screen.findByRole('combobox', { name: '개편 브리프' })) as HTMLSelectElement
      await waitFor(() => expect(picker.value).toBe('FW2026'))
      expect(within(picker).getByRole('option', { name: '2026 F/W MD 개편 (FW2026) · 초안' })).toBeTruthy()
      expect(await screen.findByRole('heading', { name: '2026 F/W MD 개편' })).toBeTruthy()
      expect(screen.getByText('4F, 5F, 6F')).toBeTruthy()

      fireEvent.click(screen.getByRole('button', { name: '진단하기' }))
      const candidates = await screen.findByRole('table', { name: '계약 만료 또는 저효율 후보 매장' })
      expect(within(candidates).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['매장', '브랜드', '계약 만료', '효율', '사유'])
      const quorelle = within(candidates).getByText('Quorelle').closest('tr')!
      // Brand and category share one cell; the percentile reads 하위 n%.
      expect(within(quorelle).getAllByRole('cell')[1].textContent).toMatch(/^Quorelle\S/)
      expect(within(quorelle).getByText(/^하위 \d+%$/)).toBeTruthy()
      expect(within(quorelle).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['계약 만료', '저효율'])
      expect(within(screen.getByRole('list', { name: '강세 카테고리' })).getAllByRole('listitem')[0].textContent).toBe('아웃도어 ×1.32')
      expect(screen.getByText(/들어갈 수 있는 매장 2곳/)).toBeTruthy()
      const diagnosisBlock = screen.getByText('진단 결과').closest('details')!
      expect(diagnosisBlock.open).toBe(true)

      fireEvent.change(screen.getByRole('combobox', { name: '목적' }), { target: { value: 'sales' } })
      fireEvent.click(screen.getByRole('radio', { name: '전체' }))
      fireEvent.click(screen.getByRole('button', { name: '대안 만들기' }))
      // Busy state while the (long) plan call runs.
      expect((screen.getByRole('button', { name: /대안을 만드는 중/ }) as HTMLButtonElement).disabled).toBe(true)
      expect(screen.getByRole('button', { name: '다시 진단하기' }).hasAttribute('disabled')).toBe(true)

      expect(await screen.findByRole('heading', { name: 'ALT-1' })).toBeTruthy()
      // With alternatives the diagnosis folds away (its summary stays) and opens again on demand.
      await waitFor(() => expect(screen.getByText('진단 결과').closest('details')!.open).toBe(false))
      expect(screen.queryByRole('table', { name: '계약 만료 또는 저효율 후보 매장' })).toBeNull()
      expect(screen.getByText(/^후보 매장 \d+곳 · 강세 아웃도어/)).toBeTruthy()
      fireEvent.click(screen.getByText('진단 결과'))
      expect(await screen.findByRole('table', { name: '계약 만료 또는 저효율 후보 매장' })).toBeTruthy()

      const cards = screen.getAllByRole('article')
      expect(cards.map((c) => c.querySelector('h4')?.textContent)).toEqual(['ALT-0 기준안', 'ALT-1', 'ALT-2', 'ALT-3'])
      const toggles = cards.map((c, i) => within(c).getByRole('button', { name: `ALT-${i} 자세히` }))
      // Only the best alternative by net effect is open; the others show the summary and KPIs.
      expect(toggles.map((t) => t.getAttribute('aria-expanded'))).toEqual(['false', 'true', 'false', 'false'])
      expect(within(cards[2]).queryByRole('button', { name: 'ALT-2 도면에서 보기' })).toBeNull()
      const alt1 = cards[1]
      const kpi = (card: HTMLElement, label: string) => within(card).getByText(label).nextElementSibling!.textContent!
      for (const label of ['순효과', '목표 변화', '이전 비용']) expect(within(alt1).getByText(label)).toBeTruthy()
      expect(kpi(alt1, '목표 변화')).toContain('(매출)')
      expect(kpi(alt1, '기준안 대비 매출 변화')).toMatch(/^\+6\.2억 원 \(범위 \+3\.4억 원 ~ \+8\.8억 원\)$/)
      expect(kpi(alt1, '지금 대비 매출 변화')).not.toContain('범위')
      expect(['입점', '퇴점', '이전'].map((label) => within(alt1).getByText(label).tagName)).toEqual(['SPAN', 'SPAN', 'SPAN'])
      expect(within(alt1).getByText('Lumivane').closest('li')!.textContent).toMatch(/^3FLumivane 3F-C06 → 3F-E01 · /)
      expect(within(alt1).getByText('제약 3개 모두 충족')).toBeTruthy()
      expect(within(alt1).queryByRole('list', { name: 'ALT-1 제약 조건' })).toBeNull()
      fireEvent.click(within(alt1).getByRole('button', { name: 'ALT-1 제약 전체 보기' }))
      expect(within(within(alt1).getByRole('list', { name: 'ALT-1 제약 조건' })).getAllByRole('listitem')).toHaveLength(3)

      // Explanations stay folded until 근거 보기; the card's notes are listed as 참고.
      expect(within(alt1).queryByText(/파급 효과/)).toBeNull()
      fireEvent.click(within(alt1).getByRole('button', { name: 'ALT-1 근거 보기' }))
      expect(within(alt1).getByText(/^Norvane Outdoor\(3F-C06\) 입점: /)).toBeTruthy()
      expect(within(alt1).getByRole('heading', { name: '참고' })).toBeTruthy()
      expect(within(alt1).getByText(/파급 효과/)).toBeTruthy()

      expect(within(cards[3]).getByText('제약 위반 1')).toBeTruthy()
      fireEvent.click(toggles[3])
      expect(within(cards[3]).getByText('제약 위반 1개')).toBeTruthy()
      expect(within(within(cards[3]).getByRole('list', { name: 'ALT-3 제약 위반' })).getByText('중앙 섬 공실 없음')).toBeTruthy()
      expect(screen.getByText('자리를 찾지 못한 입점 브랜드 1개')).toBeTruthy()

      const preview = within(alt1).getByRole('button', { name: 'ALT-1 도면에서 보기' })
      fireEvent.click(preview)
      expect(latest.ui).toMatchObject({ rev: 'FW2026-A1', compareRev: 'R2', floor: '3F', heatmap: null, journey: null, route: null })
      expect(latest.ui.highlights.map((h) => h.unit_code)).toEqual(['3F-E01', '3F-E02', '3F-C06'])
      expect(latest.ui.edges).toEqual([expect.objectContaining({ from: '3F-C06', to: '3F-E01' })])
      expect(preview.getAttribute('aria-pressed')).toBe('true')

      // Decide ALT-2: open its details first.
      fireEvent.click(toggles[2])
      fireEvent.click(within(cards[2]).getByRole('button', { name: 'ALT-2 이 대안으로 결정' }))
      const decider = within(cards[2]).getByRole('textbox', { name: '결정자' }) as HTMLInputElement
      expect(decider.value).toBe('MD팀')
      fireEvent.change(decider, { target: { value: 'MD팀 김OO' } })
      fireEvent.click(within(cards[2]).getByRole('button', { name: '결정 기록' }))
      expect(await screen.findByText(/결정한 대안: ALT-2 \(FW2026-A2\)/)).toBeTruthy()
      expect(screen.getByText(/결정자 MD팀 김OO/)).toBeTruthy()
      await waitFor(() => expect(within(screen.getAllByRole('article')[2]).getByText('결정됨')).toBeTruthy())
      // The decided card is the one open now.
      await waitFor(() =>
        expect(screen.getAllByRole('button', { name: /^ALT-\d 자세히$/ }).map((t) => t.getAttribute('aria-expanded'))).toEqual(['false', 'false', 'true', 'false']),
      )

      const checkRev = screen.getByRole('combobox', { name: '대조할 실제 개정' }) as HTMLSelectElement
      expect(checkRev.value).toBe('R3')
      expect(within(checkRev).getByRole('option', { name: 'R3 · 2026-10-01 · 부모 R2 · 업로드 · 실적 없음' })).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: '도면 대조' }))
      expect(await screen.findByText('50%')).toBeTruthy()
      const items = screen.getByRole('table', { name: '계획한 입점·퇴점·이전과 도면의 대조' })
      expect(within(items).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['결과', '브랜드', '계획 → 도면'])
      const rows = within(items).getAllByRole('row').slice(1)
      expect(rows.map((r) => r.className)).toEqual(['', '', 'mismatch', 'mismatch'])
      expect(rows.map((r) => within(r).getAllByRole('cell')[0].textContent)).toEqual(['일치', '일치', '다른 매장', '다른 매장'])
      expect(rows[0].textContent).toBe('일치Quorelle퇴점 · 3F-E01퇴점 → 도면에 없음')
      expect(rows[2].textContent).toBe('다른 매장Studio Velune이전 · 3F-C01에서3F-E02 → 3F-C01')
      expect(rows[3].textContent).toBe('다른 매장Norvane Outdoor입점3F-C01 → 3F-C06')
      // Unplanned changes show brand names, not IDs.
      const unexpected = within(screen.getByRole('list', { name: '계획에 없던 브랜드 변경' })).getAllByRole('listitem')
      expect(unexpected.map((li) => li.textContent).sort()).toEqual(['3F-C06 Lumivane → Norvane Outdoor', '3F-N02 Dovrelle → Talvessa'])
      expect(within(screen.getByRole('table', { name: '기준 개정 대비 매장 면적 변경' })).getByText('−4.2%')).toBeTruthy()

      const report = screen.getByRole('link', { name: '제안서 열기' })
      expect(report.getAttribute('href')).toBe('blob:mock-report')
      expect(report.getAttribute('target')).toBe('_blank')
      expect(report.getAttribute('rel')).toContain('noopener')
      await waitFor(() => expect(within(picker).getByRole('option', { name: /FW2026\) · 도면 대조 완료$/ })).toBeTruthy())
    })

    it('shows the first 8 candidates with a toggle for the rest', () => {
      const diagnosis = normalizeDiagnosis({
        season: { code: 'FW', label: 'F/W' },
        candidates: Array.from({ length: 11 }, (_, i) => ({
          unit_code: `4F-C${String(i + 1).padStart(2, '0')}`,
          brand_name: `브랜드 ${i + 1}`,
          category_name: '여성 캐주얼',
          lease_end: '2026-09-30',
          percentile: i * 3,
          reasons: ['계약 만료'],
        })),
        candidate_total: 11,
      })!
      render(<DiagnosisView diagnosis={diagnosis} />)
      const table = screen.getByRole('table', { name: '계약 만료 또는 저효율 후보 매장' })
      expect(within(table).getAllByRole('row')).toHaveLength(9)
      expect(within(table).getByText('하위 1%')).toBeTruthy()
      const more = screen.getByRole('button', { name: '모두 보기 (11)' })
      expect(more.getAttribute('aria-expanded')).toBe('false')
      fireEvent.click(more)
      expect(within(table).getAllByRole('row')).toHaveLength(12)
      expect(screen.getByRole('button', { name: '처음 8곳만 보기' }).getAttribute('aria-expanded')).toBe('true')
    })

    it('computes the construction order with its options and busy state, and shows the stored one on load', async () => {
      const context = await renderWith(<LayoutPanel />, async (ctx) => {
        await ctx.source.planRelayout('FW2026', { objective: 'commission', max_moves: 6, downtime_weeks: 2, movable: 'expiring' })
        return ctx
      })
      const block = (await screen.findByRole('heading', { name: '공사 순서' })).closest('section')!
      expect(block.textContent).toContain('아직 결정한 대안이 없어 순효과가 가장 큰 ALT-1의 공사 순서를 정합니다.')
      const options = within(block).getByText('계산 조건').closest('details')!
      expect(options.open).toBe(false)
      expect(options.textContent).toContain('동시 공사 2곳 · 공사 2주 · 임시 매장 사용')
      fireEvent.click(within(block).getByText('계산 조건'))
      const crews = within(block).getByRole('spinbutton', { name: '동시 공사 수' }) as HTMLInputElement
      expect([crews.min, crews.max, crews.value]).toEqual(['1', '6', '2'])
      expect((within(block).getByRole('spinbutton', { name: '공사 기간(주)' }) as HTMLInputElement).value).toBe('2')
      fireEvent.change(crews, { target: { value: '9' } })
      expect(crews.value).toBe('6')
      fireEvent.change(crews, { target: { value: '1' } })
      fireEvent.click(within(block).getByRole('checkbox', { name: '임시 매장 사용' }))
      expect(options.textContent).toContain('동시 공사 1곳 · 공사 2주 · 임시 매장 안 씀')

      const run = within(block).getByRole('button', { name: '공사 순서 계산' })
      fireEvent.click(run)
      // Busy: the button says so and every relayout action waits; a live region announces it.
      expect(run.textContent).toBe('계산하는 중…')
      expect(run.hasAttribute('disabled')).toBe(true)
      expect(block.getAttribute('aria-busy')).toBe('true')
      expect(screen.getByRole('button', { name: '대안 다시 만들기' }).hasAttribute('disabled')).toBe(true)
      expect(screen.getAllByRole('status').some((s) => s.textContent === '공사 순서를 계산하는 중입니다.')).toBe(true)

      const table = await within(block).findByRole('table', { name: '주차별 공사 순서 (0주차가 공사 시작)' })
      await waitFor(() => expect(screen.getAllByRole('status').some((s) => s.textContent === '공사 순서를 계산했습니다. ALT-1 총 4주, 영업 손실 추정 0원.')).toBe(true))
      expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['주차', '매장', '작업', '브랜드/비고'])
      const rows = within(table).getAllByRole('row').slice(1)
      expect(rows.map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))).toEqual([
        ['0주', '3F-E01', '퇴점', '1. Quorelle'],
        ['0주', '3F-E02', '퇴점', '2. Ilvora Leather'],
        ['0~2주', '3F-E01', '이전', '3. Lumivane3F-C06에서'],
        ['2~4주', '3F-C06', '입점', '4. Norvane Outdoor4주차 개점 · 3번 작업 뒤'],
      ])
      expect(within(rows[2]).getByText('이전').getAttribute('title')).toBe('이전 공사')
      const kpi = (label: string) => within(block).getByText(label).nextElementSibling!.textContent!
      expect(kpi('대상 대안')).toBe('ALT-1 (FW2026-A1)')
      expect(kpi('전체 기간')).toBe('4주 (동시 공사 1곳 · 공사 2주)')
      expect(kpi('영업 손실(추정)')).toMatch(/^0원 \(모두 공사 기간만큼 휴점하면 [\d,.]+(만|억) 원\)$/)
      expect(within(block).queryByText('입점 지연 기회 손실')).toBeNull()
      expect(within(block).getByText(/4주차 개점$/, { selector: 'li' }).textContent).toBe('Norvane Outdoor 3F-C06 · 4주차 개점')
      expect(within(block).getByRole('list', { name: '공사 순서의 가정과 한계' })).toBeTruthy()
      // The backend's notes on demand.
      expect(within(table).queryByText('기존 매장 3F-C06에서 영업하다가 공사가 끝나면 옮겨요.')).toBeNull()
      const notes = within(block).getByRole('button', { name: '작업 설명 보기' })
      fireEvent.click(notes)
      expect(notes.getAttribute('aria-pressed')).toBe('true')
      expect(within(table).getByText('기존 매장 3F-C06에서 영업하다가 공사가 끝나면 옮겨요.')).toBeTruthy()
      expect(within(block).getByRole('button', { name: '공사 순서 다시 계산' })).toBeTruthy()

      // Stored in the project: shown again on load, with its conditions as the defaults.
      cleanup()
      render(
        <AppProviders context={context}>
          <LayoutPanel />
        </AppProviders>,
      )
      const reloaded = (await screen.findByRole('heading', { name: '공사 순서' })).closest('section')!
      expect(await within(reloaded).findByText(/^ALT-1 · 총 4주 · 영업 손실 0원$/)).toBeTruthy()
      expect(within(reloaded).getByRole('table', { name: '주차별 공사 순서 (0주차가 공사 시작)' })).toBeTruthy()
      expect(within(reloaded).getByText('계산 조건').closest('details')!.textContent).toContain('동시 공사 1곳 · 공사 2주 · 임시 매장 안 씀')
    })

    it('shows the totals with the entry delay, cycles, many steps and a notice when the order is for another alternative', () => {
      const steps = Array.from({ length: 13 }, (_, i) => ({
        step: i + 1,
        start_week: Math.floor(i / 2) * 2,
        end_week: Math.floor(i / 2) * 2 + 2,
        action: i === 0 ? 'close' : i % 3 === 0 ? 'entry' : 'move',
        action_label: i === 0 ? '임시 휴점' : null,
        unit_code: `4F-C${String(i + 1).padStart(2, '0')}`,
        brand_name: `브랜드 ${i + 1}`,
        from_unit: i === 0 ? '4F-C06' : `4F-W${String(i + 1).padStart(2, '0')}`,
        closed_weeks: i === 0 ? 4 : 0,
        lost_krw: i === 0 ? 66_481_534 : 0,
        waits_for: i > 1 ? [i] : [],
      }))
      const project = normalizeProject({
        brief: { brief_id: 'FW2026', title: '2026 F/W MD 개편', base_rev: 'R2', downtime_weeks: 2 },
        plan: { alternatives: [{ alt_id: 'ALT-1', kpi: { net_krw: 1 } }, { alt_id: 'ALT-2', kpi: { net_krw: 2 } }] },
        selected_alt_id: 'ALT-1',
        phasing: {
          alt_id: 'ALT-2',
          rev_id: 'FW2026-A2',
          params: { crews: 2, fitout_weeks: 2, use_temporary: true },
          steps,
          total_weeks: 10,
          lost_krw: 66_481_534,
          naive_lost_krw: 616_032_748,
          saved_krw: 549_551_214,
          entry_delay_krw: 147_000_000,
          entries: [{ brand_name: 'Norvane Outdoor', unit_code: '6F-S02B', open_week: 8, earliest_week: 6 }],
          cycles: [{ brands: ['Novier & Co', 'Kaelova Intimates'], units: ['4F-C06', '4F-C20'], breaker: 'Novier & Co', resolved_by: 'close', unit_code: null, weeks: 4 }],
          summary: 'ALT-2의 공사 9건을 공사팀 2개로 나눠 10주 만에 끝내요.',
          caveats: ['추정치예요.'],
        },
      })
      const onPhase = vi.fn().mockResolvedValue(true)
      render(<RelayoutPhasingPanel project={project} busy={null} error={null} onPhase={onPhase} />)
      const kpi = (label: string) => screen.getByText(label).nextElementSibling!.textContent!
      expect(kpi('전체 기간')).toBe('10주 (동시 공사 2곳 · 공사 2주)')
      expect(kpi('영업 손실(추정)')).toBe('6,648만 원 (모두 공사 기간만큼 휴점하면 6.2억 원)')
      expect(kpi('입점 지연 기회 손실')).toBe('1.5억 원 (공사팀이 모자라 늦게 여는 기간)')
      expect(screen.getByText('ALT-2 기준으로 계산한 결과입니다. 지금 대상은 ALT-1이니 다시 계산해 주세요.')).toBeTruthy()
      expect(screen.getByText('서로 자리를 바꾸는 순환(Novier & Co ↔ Kaelova Intimates)은 Novier & Co 매장이 4주 동안 휴점해서 풉니다.')).toBeTruthy()
      expect(screen.getByText('(가장 빠르면 6주차)')).toBeTruthy()
      const table = screen.getByRole('table', { name: '주차별 공사 순서 (0주차가 공사 시작)' })
      expect(within(table).getAllByRole('row')).toHaveLength(9)
      expect(within(table).getByText('휴점').className).toContain('down')
      expect(within(table).getByText('4F-C06 휴점 4주 · 손실 6,648만 원')).toBeTruthy()
      const more = screen.getByRole('button', { name: '모두 보기 (13)' })
      fireEvent.click(more)
      expect(within(table).getAllByRole('row')).toHaveLength(14)
      expect(more.getAttribute('aria-expanded')).toBe('true')
      // The decided alternative is the target; the request leaves alt_id to the backend.
      expect(screen.getByText('결정한 대안 ALT-1의 공사 순서를 영업 손실이 가장 적도록 정합니다.')).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: '공사 순서 다시 계산' }))
      expect(onPhase).toHaveBeenCalledWith({ crews: 2, fitout_weeks: 2, use_temporary: true })
    })
  })
  describe('업로드 (review tab)', () => {
    it('validates, uploads a drawing, follows the job, saves a layer mapping and opens the new revision', async () => {
      await renderWith(<UploadArea request={null} pollMs={10} />)
      const submit = screen.getByRole('button', { name: '올리기' })
      fireEvent.click(submit)
      expect(await screen.findByText('도면 파일을 하나 이상 고르세요.')).toBeTruthy()
      expect(screen.getByText('새 개정 ID를 입력하세요.')).toBeTruthy()
      expect(screen.getByText('적용일을 입력하세요.')).toBeTruthy()
      expect(screen.getByRole('alert').textContent).toBe('입력 내용을 확인해 주세요 (3건).')
      const files = screen.getByLabelText('도면 파일') as HTMLInputElement
      expect(files.getAttribute('aria-invalid')).toBe('true')
      expect(document.activeElement).toBe(files)
      expect(files.accept).toBe('.dxf,.zip')

      fireEvent.change(files, { target: { files: [new File(['0'], 'R3_3F.dxf'), new File(['1'], '3F.dxf')] } })
      const revId = screen.getByRole('textbox', { name: '새 개정 ID' }) as HTMLInputElement
      fireEvent.change(revId, { target: { value: 'r2' } })
      expect(revId.value).toBe('R2')
      fireEvent.change(screen.getByLabelText('적용일'), { target: { value: '2026-10-01' } })
      fireEvent.click(submit)
      expect(await screen.findByText(/같은 층\(3F\) 파일이 둘 이상입니다/)).toBeTruthy()
      expect(screen.getByText(/R2는 이미 있는 개정입니다/)).toBeTruthy()

      fireEvent.change(files, { target: { files: [new File(['0'], 'R3_3F.dxf')] } })
      fireEvent.change(revId, { target: { value: 'R3' } })
      expect((screen.getByRole('combobox', { name: '부모 개정' }) as HTMLSelectElement).value).toBe('R2')
      expect(await screen.findByRole('option', { name: '고객사 도면 스타일 (customer_style)' })).toBeTruthy()
      expect((screen.getByRole('checkbox', { name: '산출물 재생성' }) as HTMLInputElement).checked).toBe(true)
      fireEvent.click(submit)

      // The job is polled until it settles; the live line follows it.
      await waitFor(() => expect(screen.getByRole('status').textContent).toBe('R3 처리를 마쳤습니다. 레이어 의미를 확인해 주세요.'), { timeout: 5000 })
      const steps = within(screen.getByRole('list', { name: '처리 단계' })).getAllByRole('listitem')
      expect(steps).toHaveLength(8)
      expect(steps[0].textContent).toContain('파일 저장')
      expect(steps[7].textContent).toContain('건너뜀')
      // Lease QA: errors and warnings listed with one chip per row; infos (참고) behind a toggle.
      const qa = screen.getByRole('table', { name: '계약 정보와 다른 점' })
      expect(within(qa).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['유형', '매장', '내용'])
      expect(within(qa).getAllByRole('row')).toHaveLength(3)
      expect(within(qa).getByText('경고 · 면적 불일치').className).toContain('warn')
      expect(screen.getByText('계약 정보 대조 · 오류 0 · 경고 2 · 참고 2')).toBeTruthy()
      const infos = screen.getByRole('button', { name: '참고 2건 보기' })
      expect(infos.getAttribute('aria-expanded')).toBe('false')
      expect(screen.queryByRole('table', { name: '참고 사항' })).toBeNull()
      fireEvent.click(infos)
      const infoTable = screen.getByRole('table', { name: '참고 사항' })
      expect(within(infoTable).getAllByText('참고 · 계약 없는 매장')).toHaveLength(2)
      expect(infos.getAttribute('aria-expanded')).toBe('true')
      // /api/meta was reloaded: the new revision is listed (uploaded, no sales yet).
      await waitFor(() => expect(screen.getByRole('option', { name: 'R3 · 실제 · 2026-10-01 · 업로드 · 실적 없음' })).toBeTruthy())
      await waitFor(() => expect(within(screen.getByRole('list', { name: '최근 작업' })).getByText('검토 필요')).toBeTruthy())

      const semantic = screen.getByRole('combobox', { name: 'A-AREA-17 (3F) 레이어 의미' }) as HTMLSelectElement
      expect(semantic.value).toBe('unit')
      expect(within(semantic).getAllByRole('option')).toHaveLength(17)
      fireEvent.change(semantic, { target: { value: 'walkway' } })
      fireEvent.click(screen.getByRole('button', { name: '매핑 저장 후 다시 처리' }))
      await waitFor(() => expect(screen.getByRole('status').textContent).toBe('R3 처리를 마쳤습니다.'), { timeout: 5000 })
      expect(screen.queryByRole('button', { name: '매핑 저장 후 다시 처리' })).toBeNull()

      fireEvent.click(screen.getByRole('button', { name: '이 개정 보기' }))
      expect(latest.ui.rev).toBe('R3')
      expect(screen.getByRole('button', { name: 'R3 보는 중' }).hasAttribute('disabled')).toBe(true)
    })

    it('uploads metadata and shows the result and the status table', async () => {
      await renderWith(<UploadArea request={null} />)
      fireEvent.click(screen.getByRole('tab', { name: '메타데이터' }))
      const kind = (await screen.findByRole('combobox', { name: '종류' })) as HTMLSelectElement
      const file = screen.getByLabelText('파일') as HTMLInputElement
      expect(file.accept).toBe('.csv,.xlsx')
      fireEvent.change(kind, { target: { value: 'relayout_brief' } })
      expect(file.accept).toBe('.yaml,.yml,.json')
      fireEvent.change(kind, { target: { value: 'brands' } })

      fireEvent.click(screen.getByRole('button', { name: '올리기' }))
      expect(await screen.findByText('올릴 파일을 고르세요.')).toBeTruthy()

      fireEvent.change(file, { target: { files: [new File(['x'], 'bad_brands.csv')] } })
      fireEvent.click(screen.getByRole('button', { name: '올리기' }))
      const rejected = await screen.findByRole('region', { name: '브랜드 업로드 결과' })
      expect(within(rejected).getByText('거부됨')).toBeTruthy()
      expect(within(rejected).getByText(/필수 열이 없습니다: brand_name/)).toBeTruthy()
      expect(within(rejected).getByText(/기존 데이터는 바뀌지 않았습니다/)).toBeTruthy()

      const table = await screen.findByRole('table', { name: '종류별 메타데이터 현재 상태' })
      expect(within(table).getAllByRole('row')).toHaveLength(9)
      expect(within(table).getByText('비교 점포')).toBeTruthy()
      expect(within(table).getByText('점포 실적')).toBeTruthy()
      // The store benchmark tables are CSV·XLSX kinds.
      expect(within(kind).getByRole('option', { name: '비교 점포 (stores)' })).toBeTruthy()
      fireEvent.change(kind, { target: { value: 'store_kpis' } })
      expect(file.accept).toBe('.csv,.xlsx')
      expect(screen.getByText(/필수 store_id, period, floor, category, area_m2, sales_krw/)).toBeTruthy()

      fireEvent.change(kind, { target: { value: 'relayout_brief' } })
      const yaml = 'brief_id: SS2027\ntitle: 2027 S/S 개편\nseason: SS\nyear: 2027\neffective_date: 2027-03-01\nbase_rev: R2\nfloors: [3F]\n'
      fireEvent.change(file, { target: { files: [new File([yaml], 'ss2027.yaml')] } })
      fireEvent.click(screen.getByRole('button', { name: '올리기' }))
      const accepted = await screen.findByRole('region', { name: '개편 브리프 업로드 결과' })
      expect(within(accepted).getByText('반영됨')).toBeTruthy()
      expect(within(accepted).getByText('SS2027')).toBeTruthy()
      await waitFor(() => expect(within(screen.getByRole('table', { name: '종류별 메타데이터 현재 상태' })).getByText('FW2026, SS2027')).toBeTruthy())
      fireEvent.click(within(accepted).getByRole('button', { name: '시즌 개편에서 열기' }))
      expect(latest.ui.panel).toEqual({ type: 'layout', payload: { brief_id: 'SS2027' } })
    })
  })

  describe('개편 성과 보고서 (review tab)', () => {
    it('links the review report of a revision that replaced another and has sales, with a 30/90-day window', async () => {
      await renderWith(<ReviewPanel />)
      // R2, the default revision, replaced R1 and has sales after its effective date.
      const link = await screen.findByRole('link', { name: '개편 성과 보고서(90일) 열기' })
      expect(link.getAttribute('href')).toBe('blob:mock-report')
      expect(link.getAttribute('target')).toBe('_blank')
      expect(link.getAttribute('rel')).toBe('noopener noreferrer')
      fireEvent.change(screen.getByRole('combobox', { name: '비교 기간' }), { target: { value: '30' } })
      expect(screen.getByRole('link', { name: '개편 성과 보고서(30일) 열기' })).toBeTruthy()
      // The first revision has nothing to compare with.
      act(() => dispatch({ type: 'ui/patch', ops: [replaceOp('/rev', 'R0')] }))
      await waitFor(() => expect(screen.queryByRole('link', { name: /개편 성과 보고서/ })).toBeNull())
      expect(screen.queryByRole('combobox', { name: '비교 기간' })).toBeNull()
      act(() => dispatch({ type: 'ui/patch', ops: [replaceOp('/rev', 'R1')] }))
      expect(await screen.findByRole('link', { name: '개편 성과 보고서(30일) 열기' })).toBeTruthy()
    })

    it('leaves it out for a drawing uploaded for the coming season (no sales yet)', async () => {
      await renderWith(<ReviewPanel />, withUploadedR3)
      act(() => dispatch({ type: 'ui/patch', ops: [replaceOp('/rev', 'R3')] }))
      expect(await screen.findByText('R2 → R3')).toBeTruthy()
      expect(screen.queryByRole('link', { name: /개편 성과 보고서/ })).toBeNull()
    })
  })

  describe('agent panel requests', () => {
    it('open the upload area, the season brief and the what-if view in the side panel', async () => {
      await renderWith(<SidePanel />)
      act(() => {
        dispatch({
          type: 'chat/event',
          event: { type: 'STATE_DELTA', delta: [replaceOp('/panel', { type: 'uploads', payload: { job_id: 'J-unknown' } })] },
        })
      })
      await waitFor(() => expect(screen.getByRole('tab', { name: '개정 검토' }).getAttribute('aria-selected')).toBe('true'))
      expect(screen.getByRole('button', { name: '도면·메타데이터 올리기' }).getAttribute('aria-expanded')).toBe('true')
      expect(screen.getByRole('tab', { name: '도면 올리기' }).getAttribute('aria-selected')).toBe('true')
      expect(await screen.findByText(/업로드 작업을 찾을 수 없습니다: J-unknown/)).toBeTruthy()

      act(() => {
        dispatch({ type: 'chat/event', event: { type: 'STATE_DELTA', delta: [replaceOp('/panel', { type: 'layout', payload: { brief_id: 'FW2026' } })] } })
      })
      await waitFor(() => expect(screen.getByRole('tab', { name: '레이아웃 대안' }).getAttribute('aria-selected')).toBe('true'))
      expect(screen.getByRole('tab', { name: '시즌 개편' }).getAttribute('aria-selected')).toBe('true')
      expect(await screen.findByRole('heading', { name: '2026 F/W MD 개편' })).toBeTruthy()

      const alternatives = [{ id: 'ALT-3F-A', moves: [{ brand_id: 'BR3011', brand_name: 'Quorelle', from_unit: '3F-E01', to_unit: '3F-C06' }] }]
      act(() => {
        dispatch({
          type: 'chat/event',
          event: { type: 'STATE_DELTA', delta: [replaceOp('/panel', { type: 'layout', payload: { rev: 'R2', floors: ['3F'], alternatives } })] },
        })
      })
      await waitFor(() => expect(screen.getByRole('tab', { name: '배치 대안' }).getAttribute('aria-selected')).toBe('true'))
      expect(screen.getByRole('heading', { name: 'ALT-3F-A' })).toBeTruthy()

      // Unknown panel types change nothing.
      act(() => {
        dispatch({ type: 'chat/event', event: { type: 'STATE_DELTA', delta: [replaceOp('/panel', { type: 'wizard', payload: {} })] } })
      })
      expect(latest.ui.panel?.type).toBe('none')
      expect(screen.getByRole('tab', { name: '레이아웃 대안' }).getAttribute('aria-selected')).toBe('true')
    })
  })
})
