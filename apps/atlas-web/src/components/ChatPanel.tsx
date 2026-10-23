import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type WheelEvent,
} from 'react'
import { useData } from '../api/source'
import {
  ERROR_MESSAGE,
  THINKING_LABEL,
  failedUiSteps,
  isRunActive,
  runStatusText,
  stepDisplayLabel,
  stepTree,
  stepsSummaryText,
  type ChatRun,
  type RunStep,
  type StepNode,
} from '../lib/chatRun'
import { QUICK_PROMPTS } from '../lib/prompts'
import type { ChatItem } from '../state/reducer'
import { useAppDispatch, useAppState } from '../state/store'
import type { ChatRequest } from '../types/api'
import { RichText } from './RichText'

/** `?debug=1` shows the technical details (tool names, args, summaries) of every step by default. */
function debugRequested(): boolean {
  if (typeof window === 'undefined') return false
  const value = new URLSearchParams(window.location.search).get('debug')
  return value === '1' || value === 'true'
}

const MAX_TECH_TEXT = 1500

function techText(value: unknown): string {
  let text: string
  if (typeof value === 'string') {
    try {
      text = JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      text = value
    }
  } else {
    text = JSON.stringify(value, null, 2) ?? String(value)
  }
  return text.length > MAX_TECH_TEXT ? `${text.slice(0, MAX_TECH_TEXT)}…` : text
}

/**
 * Between two tool calls the status briefly has nothing running; keep the last step label for a moment
 * instead of flashing "생각하는 중…".
 */
function useSettledStatus(text: string | null, delayMs = 700): string | null {
  const [shown, setShown] = useState(text)
  const shownRef = useRef(text)
  useEffect(() => {
    const apply = () => {
      shownRef.current = text
      setShown(text)
    }
    if (text === THINKING_LABEL && shownRef.current !== null && shownRef.current !== THINKING_LABEL) {
      const timer = setTimeout(apply, delayMs)
      return () => clearTimeout(timer)
    }
    apply()
    return undefined
  }, [text, delayMs])
  return shown
}

function RunStatusLine({ run }: { run: ChatRun }) {
  const text = useSettledStatus(runStatusText(run))
  return (
    <div className="run-status" role="status" aria-live="polite">
      <span className="run-status-dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <span className="run-status-text">{text ?? THINKING_LABEL}</span>
    </div>
  )
}

const STEP_STATUS_LABELS: Record<RunStep['status'], string> = { running: '진행 중', done: '완료', error: '실패' }

function StepTech({ step }: { step: RunStep }) {
  return (
    <div className="step-tech">
      <div className="step-tech-head">
        <code>{step.toolName || 'STATE_DELTA'}</code>
        {step.agent && <span> · {step.agent}</span>}
      </div>
      {step.args !== undefined && step.args !== null && (
        <>
          <div className="step-tech-label">인자</div>
          <pre>{techText(step.args)}</pre>
        </>
      )}
      {step.summary && (
        <>
          <div className="step-tech-label">결과 원문</div>
          <pre>{techText(step.summary)}</pre>
        </>
      )}
      {step.patchKeys.length > 0 && <div className="step-tech-label">바뀐 화면 상태: {step.patchKeys.join(', ')}</div>}
      {step.error && <div className="step-tech-error">{step.error}</div>}
    </div>
  )
}

function StepItem({ node, tech }: { node: StepNode; tech: boolean }) {
  const { step } = node
  return (
    <li className={`step step-${step.status}`}>
      <div className="step-row">
        <span className={`step-icon ${step.status}`} role="img" aria-label={STEP_STATUS_LABELS[step.status]}>
          {step.status === 'done' ? '✓' : step.status === 'error' ? '!' : ''}
        </span>
        <span className="step-label">{stepDisplayLabel(step)}</span>
      </div>
      {tech && <StepTech step={step} />}
      {node.children.length > 0 && (
        <ol className="step-list nested">
          {node.children.map((child) => (
            <StepItem key={child.step.id} node={child} tech={tech} />
          ))}
        </ol>
      )}
    </li>
  )
}

/** Collapsed "작업 과정 보기 · 4단계 · 23초" disclosure with the labelled steps (and developer details). */
function RunSteps({ run, debug }: { run: ChatRun; debug: boolean }) {
  const [open, setOpen] = useState(false)
  const [tech, setTech] = useState(debug)
  const tree = useMemo(() => stepTree(run.steps), [run.steps])
  const bodyId = `run-steps-${run.id}`
  return (
    <div className={`run-steps${open ? ' open' : ''}`}>
      <button type="button" className="run-steps-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((v) => !v)}>
        <span className="run-steps-chevron" aria-hidden="true" />
        {stepsSummaryText(run)}
      </button>
      {open && (
        <div id={bodyId} className="run-steps-body">
          <ol className="step-list">
            {tree.map((node) => (
              <StepItem key={node.step.id} node={node} tech={tech} />
            ))}
          </ol>
          <button type="button" className="tech-toggle" aria-pressed={tech} onClick={() => setTech((v) => !v)}>
            기술 정보
          </button>
        </div>
      )}
    </div>
  )
}

function RunView({ run, debug, onRetry }: { run: ChatRun; debug: boolean; onRetry: (() => void) | null }) {
  const active = isRunActive(run)
  const answering = run.status === 'answering'
  const hasText = run.answer.trim().length > 0
  // Once text exists the disclosure stays put, so the answer does not jump when tools run in between.
  const showSteps = run.steps.length > 0 && (!active || hasText)
  const failedUi = failedUiSteps(run).length
  return (
    <article className="turn turn-assistant" aria-label="에이전트 답변">
      {showSteps && <RunSteps run={run} debug={debug} />}
      {hasText && (
        <div className={`answer${answering ? ' streaming' : ''}`}>
          <RichText text={run.answer} trailing={answering ? <span className="cursor" aria-hidden="true" /> : null} />
        </div>
      )}
      {active && !answering && <RunStatusLine run={run} />}
      {failedUi > 0 && <p className="run-note warn">화면 일부를 바꾸지 못했어요. 작업 과정 보기에서 확인할 수 있어요.</p>}
      {run.status === 'stopped' && <p className="run-note">응답을 중지했어요.</p>}
      {run.status === 'done' && !hasText && <p className="run-note">답변 내용이 비어 있어요.</p>}
      {run.status === 'error' && (
        <div className="run-error" role="alert">
          <p>{ERROR_MESSAGE}</p>
          {run.error && (
            <details>
              <summary>자세히 보기</summary>
              <p className="run-error-detail">{run.error}</p>
            </details>
          )}
          {onRetry && (
            <button type="button" className="text-button" onClick={onRetry}>
              다시 시도
            </button>
          )}
        </div>
      )}
    </article>
  )
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="turn turn-user">
      <div className="bubble">
        <span className="visually-hidden">사용자: </span>
        {text}
      </div>
    </div>
  )
}

function EmptyState({ onPick, disabled }: { onPick: (prompt: string) => void; disabled: boolean }) {
  return (
    <div className="chat-empty">
      <h2>무엇을 도와드릴까요?</h2>
      <p>도면 변경, 매장 효율, 교차구매, 고객 동선, 길 찾기, 통로 유동·팝업 후보를 물어보세요. 답변에 맞춰 도면도 함께 바뀌어요.</p>
      <div className="suggestions" role="group" aria-label="추천 질문">
        {QUICK_PROMPTS.map((prompt) => (
          <button key={prompt} type="button" className="suggestion" disabled={disabled} onClick={() => onPick(prompt)}>
            {prompt}
          </button>
        ))}
      </div>
    </div>
  )
}

function lastRun(items: readonly ChatItem[]): ChatRun | null {
  const item = items.at(-1)
  return item && item.kind === 'run' ? item.run : null
}

export function ChatPanel() {
  const { source } = useData()
  const { ui, selection, chat, composer } = useAppState()
  const dispatch = useAppDispatch()
  const [input, setInput] = useState('')
  const [debug] = useState(debugRequested)
  const abortRef = useRef<AbortController | null>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const stickToBottom = useRef(true)

  useEffect(() => {
    if (composer.seq === 0) return
    setInput(composer.text)
    inputRef.current?.focus()
  }, [composer.seq, composer.text])

  // Grow the input with its content (up to ~6 lines).
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    if (el.scrollHeight > 0) el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [input])

  // Follow new output unless the user scrolled up to read.
  useEffect(() => {
    const el = logRef.current
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight
  }, [chat.items])

  useEffect(() => () => abortRef.current?.abort(), [])

  const onScroll = () => {
    const el = logRef.current
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  // The one-row suggestion strip scrolls sideways with a regular mouse wheel too.
  const onChipsWheel = (event: WheelEvent<HTMLDivElement>) => {
    const el = event.currentTarget
    if (Math.abs(event.deltaY) > Math.abs(event.deltaX) && el.scrollWidth > el.clientWidth) el.scrollLeft += event.deltaY
  }

  const send = useCallback(
    async (text: string) => {
      const message = text.trim()
      if (!message || chat.running) return
      setInput('')
      stickToBottom.current = true
      dispatch({ type: 'chat/user', id: `user-${Date.now().toString(36)}-${chat.items.length}`, text: message, at: Date.now() })
      const controller = new AbortController()
      abortRef.current = controller
      const request: ChatRequest = {
        ...(chat.threadId ? { thread_id: chat.threadId } : {}),
        message,
        ui_state: { rev: ui.rev, floor: ui.floor, selection },
      }
      try {
        await source.streamChat(request, (event) => dispatch({ type: 'chat/event', event, at: Date.now() }), controller.signal)
        dispatch({ type: 'chat/closed', at: Date.now() })
      } catch (error) {
        if (controller.signal.aborted) dispatch({ type: 'chat/closed', aborted: true, at: Date.now() })
        else {
          const detail = error instanceof Error ? error.message : String(error)
          dispatch({ type: 'chat/event', event: { type: 'RUN_ERROR', message: `채팅 요청 실패: ${detail}` }, at: Date.now() })
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

  const items = chat.items
  const current = lastRun(items)
  const lastUserText = [...items].reverse().find((item) => item.kind === 'user')?.text ?? null
  const liveText = !current
    ? ''
    : current.status === 'answering'
      ? '답변을 작성하고 있어요.'
      : current.status === 'done'
        ? '답변을 마쳤어요.'
        : current.status === 'error'
          ? '답변을 만들지 못했어요.'
          : ''

  return (
    <div className="chat-panel">
      <div className="chat-log" ref={logRef} onScroll={onScroll} role="log" aria-label="에이전트 대화" aria-live="off">
        {items.length === 0 ? (
          <EmptyState onPick={(prompt) => void send(prompt)} disabled={chat.running} />
        ) : (
          items.map((item, index) =>
            item.kind === 'user' ? (
              <UserBubble key={`user-${item.id}`} text={item.text} />
            ) : (
              <RunView
                key={`run-${item.id}`}
                run={item.run}
                debug={debug}
                onRetry={index === items.length - 1 && item.run.status === 'error' && lastUserText && !chat.running ? () => void send(lastUserText) : null}
              />
            ),
          )
        )}
      </div>
      <p className="visually-hidden" aria-live="polite">
        {liveText}
      </p>
      <div className="composer-area">
        {items.length > 0 && (
          <div className="suggestion-row" role="group" aria-label="추천 질문" onWheel={onChipsWheel}>
            {QUICK_PROMPTS.map((prompt) => (
              <button key={prompt} type="button" className="suggestion compact" disabled={chat.running} onClick={() => void send(prompt)}>
                {prompt}
              </button>
            ))}
          </div>
        )}
        <form className="composer" onSubmit={onSubmit}>
          <label htmlFor="chat-input" className="visually-hidden">
            에이전트에게 질문
          </label>
          <textarea
            id="chat-input"
            ref={inputRef}
            value={input}
            rows={1}
            placeholder="무엇이든 물어보세요 (Enter 전송, Shift+Enter 줄바꿈)"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {chat.running ? (
            <button type="button" className="composer-button stop" aria-label="중지" title="중지" onClick={() => abortRef.current?.abort()}>
              <span className="stop-icon" aria-hidden="true" />
            </button>
          ) : (
            <button type="submit" className="composer-button send" aria-label="보내기" title="보내기" disabled={!input.trim()}>
              <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
                <path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
        </form>
        <div className="composer-meta">
          <span className="composer-context">{selection.length ? `선택한 매장 ${selection.join(', ')}도 함께 보내요` : ''}</span>
          <button type="button" className="text-button" disabled={chat.running || items.length === 0} onClick={() => dispatch({ type: 'chat/reset' })}>
            새 대화
          </button>
        </div>
      </div>
    </div>
  )
}
