import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useData } from '../api/source'
import { QUICK_PROMPTS } from '../lib/prompts'
import type { ChatItem, ToolCallItem } from '../state/reducer'
import { useAppDispatch, useAppState } from '../state/store'
import type { ChatRequest } from '../types/api'
import { RichText } from './RichText'

const STATE_KEY_LABELS: Record<string, string> = {
  '/': '전체 상태',
  rev: '개정',
  compareRev: '비교 개정',
  floor: '층',
  layers: '레이어',
  heatmap: 'heatmap',
  highlights: '강조',
  edges: '연결선',
  focus: '화면 이동',
  panel: '패널',
}

function formatArgs(args: unknown): string {
  if (args === undefined) return '(인자 없음)'
  if (typeof args === 'string') {
    try {
      return JSON.stringify(JSON.parse(args), null, 2)
    } catch {
      return args
    }
  }
  return JSON.stringify(args, null, 2)
}

function shortArgs(args: unknown): string {
  if (args === undefined || args === null) return ''
  const text = typeof args === 'string' ? args : JSON.stringify(args)
  return text.length > 60 ? `${text.slice(0, 57)}…` : text
}

function ToolChip({ item }: { item: ToolCallItem }) {
  const running = item.status === 'running'
  return (
    <details className={`tool-chip ${running ? 'running' : 'done'}`}>
      <summary>
        <span className="tool-status" role="img" aria-label={running ? '실행 중' : '완료'}>
          {running ? '⏳' : '✔'}
        </span>
        <code className="tool-name">{item.name || '도구'}</code>
        <span className="tool-summary">{item.summary ?? (running ? '실행 중…' : shortArgs(item.args))}</span>
      </summary>
      <div className="tool-detail">
        <div className="muted">인자</div>
        <pre>{formatArgs(item.args)}</pre>
        {item.summary && (
          <>
            <div className="muted">결과 요약</div>
            <p>{item.summary}</p>
          </>
        )}
      </div>
    </details>
  )
}

function ChatEntry({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case 'user':
      return (
        <div className="msg user">
          <span className="visually-hidden">사용자: </span>
          {item.text}
        </div>
      )
    case 'assistant':
      return (
        <div className={`msg assistant${item.streaming ? ' streaming' : ''}`}>
          <span className="visually-hidden">에이전트: </span>
          <RichText text={item.text} />
          {item.streaming && <span className="cursor" aria-hidden="true" />}
        </div>
      )
    case 'tool':
      return <ToolChip item={item} />
    case 'state':
      return item.error ? (
        <div className="state-line error" role="alert">
          화면 갱신 실패 ({item.paths.map((p) => STATE_KEY_LABELS[p] ?? p).join(', ')}): {item.error}
        </div>
      ) : (
        <div className="state-line">화면 갱신: {item.paths.map((p) => STATE_KEY_LABELS[p] ?? p).join(', ')}</div>
      )
    case 'error':
      return (
        <div className="msg error" role="alert">
          오류: {item.message}
        </div>
      )
    case 'notice':
      return <div className="state-line">{item.text}</div>
    default:
      return null
  }
}

export function ChatPanel() {
  const { source } = useData()
  const { ui, selection, chat, composer } = useAppState()
  const dispatch = useAppDispatch()
  const [input, setInput] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (composer.seq === 0) return
    setInput(composer.text)
    inputRef.current?.focus()
  }, [composer.seq, composer.text])

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [chat.items])

  useEffect(() => () => abortRef.current?.abort(), [])

  const send = useCallback(
    async (text: string) => {
      const message = text.trim()
      if (!message || chat.running) return
      setInput('')
      dispatch({ type: 'chat/user', id: `user-${Date.now().toString(36)}-${chat.items.length}`, text: message })
      const controller = new AbortController()
      abortRef.current = controller
      const request: ChatRequest = {
        ...(chat.threadId ? { thread_id: chat.threadId } : {}),
        message,
        ui_state: { rev: ui.rev, floor: ui.floor, selection },
      }
      try {
        await source.streamChat(request, (event) => dispatch({ type: 'chat/event', event }), controller.signal)
        dispatch({ type: 'chat/closed' })
      } catch (error) {
        if (controller.signal.aborted) dispatch({ type: 'chat/closed', aborted: true })
        else {
          const detail = error instanceof Error ? error.message : String(error)
          dispatch({ type: 'chat/event', event: { type: 'RUN_ERROR', message: `채팅 요청 실패: ${detail}` } })
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null
      }
    },
    [chat.running, chat.threadId, chat.items.length, dispatch, source, ui.rev, ui.floor, selection],
  )

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    void send(input)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Korean IME: Enter that commits a composition must not submit.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void send(input)
    }
  }

  return (
    <div className="chat-panel">
      <div className="chat-log" ref={listRef} role="log" aria-label="에이전트 대화" aria-live="off">
        {chat.items.length === 0 && (
          <div className="chat-empty">
            <p>도면, 개정, 공간 생산성에 대해 질문하세요. 에이전트가 도구를 호출하면 아래에 과정이 표시되고, 도면이 자동으로 바뀝니다.</p>
          </div>
        )}
        {chat.items.map((item) => (
          <ChatEntry key={`${item.kind}-${item.id}`} item={item} />
        ))}
      </div>
      <p className="visually-hidden" aria-live="polite">
        {chat.running ? '에이전트가 응답하는 중입니다.' : chat.items.length ? '에이전트 응답이 끝났습니다.' : ''}
      </p>
      <div className="quick-prompts" role="group" aria-label="빠른 질문">
        {QUICK_PROMPTS.map((prompt) => (
          <button key={prompt} type="button" disabled={chat.running} onClick={() => void send(prompt)}>
            {prompt}
          </button>
        ))}
      </div>
      <form className="chat-form" onSubmit={onSubmit}>
        <label htmlFor="chat-input" className="visually-hidden">
          에이전트에게 질문
        </label>
        <textarea
          id="chat-input"
          ref={inputRef}
          value={input}
          rows={2}
          placeholder={selection.length ? `선택: ${selection.join(', ')} · 질문을 입력하세요` : '질문을 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)'}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="chat-actions">
          {chat.running ? (
            <button type="button" onClick={() => abortRef.current?.abort()}>
              중지
            </button>
          ) : (
            <button type="submit" className="primary" disabled={!input.trim()}>
              보내기
            </button>
          )}
          <button type="button" disabled={chat.running || chat.items.length === 0} onClick={() => dispatch({ type: 'chat/reset' })}>
            대화 지우기
          </button>
        </div>
      </form>
    </div>
  )
}
