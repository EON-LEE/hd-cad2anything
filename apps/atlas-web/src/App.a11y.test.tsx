// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { createMockContext } from './api/source'
import { setMockSpeed } from './mock/chatScripts'

// jsdom has no WebGL and the full deck.gl graph is slow under the runner: replace it with light stubs.
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
  return { default: () => React.createElement('div', { 'data-testid': 'deck' }) }
})

// Pass an explicit mock context so the shell boots regardless of how resolveDataSource treats mock mode.
async function renderApp() {
  const context = await createMockContext('테스트용 모의 데이터')
  render(<App context={context} />)
  await screen.findByRole('heading', { name: 'CAD2Anything Atlas' })
}

const floorTabs = () => within(screen.getByRole('tablist', { name: '층' })).getAllByRole('tab')

const originalMatchMedia = window.matchMedia
function mockCompact(compact: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: compact && query.includes('719.98'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

describe('web responsive and accessibility (jsdom)', () => {
  beforeAll(() => {
    setMockSpeed(0)
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 1000 })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 700 })
  })
  afterEach(() => {
    cleanup()
    window.matchMedia = originalMatchMedia
  })
  afterAll(() => {
    setMockSpeed(1)
  })

  it('floor tabs control the canvas tabpanel, which is labelled by the active tab (R4)', async () => {
    await renderApp()
    const tabs = floorTabs()
    expect(tabs.length).toBeGreaterThanOrEqual(2)
    for (const tab of tabs) {
      expect(tab.id).toMatch(/^floor-tab-/)
      expect(tab.getAttribute('aria-controls')).toBe('floor-canvas')
    }
    const canvas = document.getElementById('floor-canvas')!
    expect(canvas.getAttribute('role')).toBe('tabpanel')
    const selected = tabs.find((t) => t.getAttribute('aria-selected') === 'true')!
    expect(canvas.getAttribute('aria-labelledby')).toBe(selected.id)
  })

  it('floor tab arrow keys wrap at both ends (R4)', async () => {
    await renderApp()
    const tabs = floorTabs()
    tabs[0].focus()
    fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' })
    await waitFor(() => expect(document.activeElement).toBe(floorTabs().at(-1)))
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' })
    await waitFor(() => expect(document.activeElement).toBe(floorTabs()[0]))
  })

  it('selects a unit from the keyboard combobox, opening the details card (R3)', async () => {
    await renderApp()
    const combo = screen.getByRole('combobox', { name: '매장 찾기' }) as HTMLInputElement
    await waitFor(() => expect(combo.disabled).toBe(false))
    fireEvent.focus(combo)
    fireEvent.change(combo, { target: { value: '3F-C01' } })
    expect(await screen.findByRole('option', { name: /3F-C01/ })).toBeTruthy()
    fireEvent.keyDown(combo, { key: 'Enter' })
    expect(await screen.findByRole('complementary', { name: /3F-C01.*매장 상세/ })).toBeTruthy()
    // The field now reflects the selection and announces it.
    expect(combo.value).toContain('3F-C01')
  })

  it('offers a map / side view toggle for the stacked phone layout (R1)', async () => {
    await renderApp()
    const group = screen.getByRole('group', { name: '화면 전환' })
    expect(within(group).getByRole('button', { name: '지도' })).toBeTruthy()
    expect(within(group).getByRole('button', { name: /대화·패널/ })).toBeTruthy()
  })

  it('collapses the floating journey card to a one-line summary on a phone, with a 자세히 expander (R1)', async () => {
    mockCompact(true)
    await renderApp()
    fireEvent.click(await screen.findByRole('button', { name: 'VIP 고객 한 명의 쇼핑 동선을 보여줘' }))
    const card = await screen.findByRole('region', { name: '40대 여성 VIP 고객의 쇼핑 동선' })
    // Collapsed by default: the detailed summary list is hidden and an expander is shown.
    expect(within(card).queryByRole('list', { name: '동선 요약' })).toBeNull()
    const expand = within(card).getByRole('button', { name: '자세히' })
    expect(expand.getAttribute('aria-expanded')).toBe('false')
    // Expanding reveals the full card.
    fireEvent.click(expand)
    expect(within(card).getByRole('list', { name: '동선 요약' })).toBeTruthy()
    expect(within(card).getByRole('button', { name: '접기' }).getAttribute('aria-expanded')).toBe('true')
  })
})
