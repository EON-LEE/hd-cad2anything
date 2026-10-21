// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { setMockSpeed } from './mock/chatScripts'

// jsdom has no WebGL, and importing the full deck.gl module graph is slow under the test runner.
// Replace deck.gl with light stand-ins: layers keep their props, and <DeckGL> renders a probe that
// exposes the layer ids and the number of unit polygons.
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
    default: (props: { layers?: { id: string; props: { data?: unknown[] } }[] }) =>
      React.createElement('div', {
        'data-testid': 'deck',
        'data-layers': (props.layers ?? []).map((l) => l.id).join(','),
        'data-units': (props.layers ?? []).find((l) => l.id === 'units')?.props.data?.length ?? 0,
      }),
  }
})

const layerIds = () => screen.getByTestId('deck').getAttribute('data-layers')?.split(',') ?? []

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

  it('renders the shell, runs scripted agent turns and drives the panels', async () => {
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

    // Quick prompt: low-efficiency analysis -> tool chips, streamed text, heatmap/highlights/edges/focus.
    fireEvent.click(screen.getByRole('button', { name: '3층 저효율 zone과 원인' }))
    expect(await screen.findByText('analytics.productivity')).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('log').textContent).toContain('재배치 대안이 필요하면'))
    expect(screen.getByRole('log').textContent).toContain('화면 갱신')
    await waitFor(() => expect(layerIds()).toEqual(expect.arrayContaining(['highlights', 'edges', 'edge-labels'])))
    const legend = screen.getByRole('complementary', { name: '범례' })
    expect(within(legend).getByRole('heading', { name: '평효율(원/㎡)' })).toBeTruthy()
    expect(legend.textContent).toContain('데이터 없음')
    // One edge goes to a 1F unit, so a floor badge appears at the canvas edge.
    expect(screen.getByRole('button', { name: /1F .*층으로 이동/ })).toBeTruthy()

    // Quick prompt: revision review -> compare mode + review tab with changes, impact and artifacts.
    fireEvent.click(screen.getByRole('button', { name: 'S/S 개편 도면 변경점과 재생성 대상 알려줘' }))
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

    // Quick prompt: layout alternatives -> layout tab with three alternatives and a preview button.
    fireEvent.click(screen.getByRole('tab', { name: /에이전트/ }))
    fireEvent.click(screen.getByRole('button', { name: '3층 재배치안 3개 비교' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: '레이아웃 대안' }).getAttribute('aria-selected')).toBe('true'))
    expect(await screen.findByRole('heading', { name: 'ALT-3F-A' })).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /미리보기/ })).toHaveLength(3)
    fireEvent.click(screen.getAllByRole('button', { name: /미리보기/ })[1])
    expect(screen.getByRole('button', { name: '미리보는 중' }).getAttribute('aria-pressed')).toBe('true')

    // Quick prompt: cross-shopping -> relationship graph built from state.edges.
    fireEvent.click(screen.getByRole('tab', { name: /에이전트/ }))
    fireEvent.click(screen.getByRole('button', { name: '40대 VIP 교차구매 보여줘' }))
    await waitFor(() => expect(screen.getByRole('log').textContent).toContain('교차구매 상위 5쌍'))
    fireEvent.click(screen.getByRole('tab', { name: '관계 그래프' }))
    const graph = await screen.findByRole('group', { name: '매장 관계 그래프' })
    expect(within(graph).getAllByRole('button').length).toBe(7)
    const edgeTable = screen.getByRole('table', { name: '연결 목록' })
    expect(within(edgeTable).getAllByRole('row')).toHaveLength(6)

    // Clicking a unit opens the details card with KPIs from the productivity endpoint (via selection).
    fireEvent.click(within(graph).getByRole('button', { name: /^3F-C01/ }))
    const details = await screen.findByRole('complementary', { name: '3F-C01 매장 상세' })
    await waitFor(() => expect(details.textContent).toContain('평효율'))
  })
})
