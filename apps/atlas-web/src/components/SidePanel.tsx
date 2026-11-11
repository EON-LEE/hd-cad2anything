import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useAppState } from '../state/store'
import { panelTab } from '../state/uiState'
import { ChatPanel } from './ChatPanel'
import { GraphPanel } from './GraphPanel'
import { LayoutPanel } from './LayoutPanel'
import { ReviewPanel } from './ReviewPanel'
import './chatState.css'

type TabId = 'chat' | 'review' | 'layout' | 'graph'

const TABS: { id: TabId; label: string }[] = [
  { id: 'chat', label: '에이전트' },
  { id: 'review', label: '개정 검토' },
  { id: 'layout', label: '레이아웃 대안' },
  { id: 'graph', label: '관계 그래프' },
]

export function SidePanel() {
  const { ui, panelSeq, deferPanel, chat, composer } = useAppState()
  const [active, setActive] = useState<TabId>('chat')
  // Panels mount on first visit (avoids eager requests) and then stay mounted to keep their state.
  const [visited, setVisited] = useState<ReadonlySet<TabId>>(() => new Set<TabId>(['chat']))
  const [unread, setUnread] = useState(false)
  // Tabs with a prepared result the user has not opened yet (a later result for another tab does not replace it).
  const [pending, setPending] = useState<readonly TabId[]>([])
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])
  const wasRunning = useRef(chat.running)

  useEffect(() => {
    setVisited((prev) => (prev.has(active) ? prev : new Set([...prev, active])))
  }, [active])

  // Keep the conversation visible while the agent prepares a result; the user chooses when to open it.
  useEffect(() => {
    if (panelSeq === 0) return
    const tab = panelTab(ui.panel)
    if (tab && deferPanel) setPending((prev) => (prev.includes(tab) ? prev : [...prev, tab]))
    else if (tab) {
      setActive(tab)
      setPending((prev) => prev.filter((id) => id !== tab))
    } else if (ui.panel?.type === 'none') {
      // The panel was closed: nothing is left to announce. A journey or table opens elsewhere (map, table
      // panel) and leaves the prepared tabs alone.
      setPending([])
    }
    // Intentionally keyed on panelSeq only.
  }, [panelSeq])

  useEffect(() => {
    if (composer.seq > 0) {
      setActive('chat')
      setUnread(false)
    }
  }, [composer.seq])

  // An answer that finished while another tab was open is marked as unread.
  useEffect(() => {
    if (wasRunning.current && !chat.running && active !== 'chat') setUnread(true)
    wasRunning.current = chat.running
  }, [chat.running, active])

  const select = (id: TabId) => {
    setActive(id)
    if (id === 'chat') setUnread(false)
    setPending((prev) => (prev.includes(id) ? prev.filter((tab) => tab !== id) : prev))
  }

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % TABS.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + TABS.length) % TABS.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = TABS.length - 1
    else return
    event.preventDefault()
    select(TABS[next].id)
    tabRefs.current[next]?.focus()
  }

  return (
    <aside className="sidebar" aria-label="분석 패널">
      <div role="tablist" aria-label="패널" className="side-tabs">
        {TABS.map((tab, i) => {
          const selected = active === tab.id
          const badge = tab.id === 'chat' && (chat.running || unread) && !selected
          return (
            <button
              key={tab.id}
              ref={(el) => {
                tabRefs.current[i] = el
              }}
              id={`tab-${tab.id}`}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={`panel-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              className={selected ? 'active' : undefined}
              onClick={() => select(tab.id)}
              onKeyDown={(e) => onKeyDown(e, i)}
            >
              {tab.label}
              {badge && (
                <span className="dot" role="img" aria-label={chat.running ? '응답 중' : '새 메시지'} />
              )}
              {pending.includes(tab.id) && !selected && <span className="result-mark" aria-hidden="true">새 결과</span>}
            </button>
          )
        })}
      </div>
      {active === 'chat' && pending.length > 0 && (
        <div className="result-notice">
          <span role="status">결과 화면을 준비했어요. 채팅을 이어 읽거나 지금 열어볼 수 있어요.</span>
          {pending.map((target) => (
            <button
              key={target}
              type="button"
              onClick={() => {
                select(target)
                tabRefs.current[TABS.findIndex((tab) => tab.id === target)]?.focus()
              }}
            >
              {TABS.find((tab) => tab.id === target)?.label} 보기
            </button>
          ))}
        </div>
      )}
      {TABS.map((tab) => (
        <div
          key={tab.id}
          id={`panel-${tab.id}`}
          role="tabpanel"
          aria-labelledby={`tab-${tab.id}`}
          className={`tab-panel tab-panel-${tab.id}`}
          hidden={active !== tab.id}
        >
          {(visited.has(tab.id) || active === tab.id) && tab.id === 'chat' && <ChatPanel active={active === 'chat'} />}
          {(visited.has(tab.id) || active === tab.id) && tab.id === 'review' && <ReviewPanel />}
          {(visited.has(tab.id) || active === tab.id) && tab.id === 'layout' && <LayoutPanel />}
          {(visited.has(tab.id) || active === tab.id) && tab.id === 'graph' && <GraphPanel />}
        </div>
      ))}
    </aside>
  )
}
