// Server-Sent Events parsing for POST responses (EventSource only supports GET).
import type { AgUiEvent } from '../types/api'

export interface SseMessage {
  event: string
  data: string
  id?: string
}

export interface SseParserOptions {
  /**
   * Leniency for servers that put one JSON object per `data:` line without blank-line separators.
   * When a new `data:` line arrives and the buffered data already parses as JSON, the buffered data
   * is dispatched first. Standard multi-line JSON (split across `data:` lines) is unaffected.
   */
  splitJsonLines?: boolean
}

function isCompleteJson(text: string): boolean {
  const trimmed = text.trim()
  if (!trimmed || (trimmed[0] !== '{' && trimmed[0] !== '[')) return false
  try {
    JSON.parse(trimmed)
    return true
  } catch {
    return false
  }
}

/** Incremental SSE parser (WHATWG "event stream interpretation"). Feed decoded text chunks. */
export class SseParser {
  private buffer = ''
  private dataLines: string[] = []
  private eventType = ''
  private lastEventId: string | undefined
  private readonly onMessage: (message: SseMessage) => void
  private readonly splitJsonLines: boolean

  constructor(onMessage: (message: SseMessage) => void, options: SseParserOptions = {}) {
    this.onMessage = onMessage
    this.splitJsonLines = options.splitJsonLines ?? true
  }

  push(chunk: string): void {
    this.buffer += chunk
    this.drain(false)
  }

  /** Flush a trailing line without terminator and dispatch any pending event (end of stream). */
  end(): void {
    this.drain(true)
    if (this.buffer.length > 0) {
      this.processLine(this.buffer)
      this.buffer = ''
    }
    this.dispatch()
  }

  private drain(final: boolean): void {
    let start = 0
    const text = this.buffer
    for (;;) {
      let end = -1
      for (let i = start; i < text.length; i += 1) {
        const ch = text.charCodeAt(i)
        if (ch === 10 || ch === 13) {
          end = i
          break
        }
      }
      if (end === -1) break
      // A lone CR at the end of the buffer may be the first half of CRLF: wait for more input.
      if (text.charCodeAt(end) === 13 && end === text.length - 1 && !final) break
      this.processLine(text.slice(start, end))
      start = text.charCodeAt(end) === 13 && text.charCodeAt(end + 1) === 10 ? end + 2 : end + 1
    }
    this.buffer = text.slice(start)
  }

  private processLine(line: string): void {
    if (line === '') {
      this.dispatch()
      return
    }
    if (line.startsWith(':')) return
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    switch (field) {
      case 'data':
        if (this.splitJsonLines && this.dataLines.length > 0 && isCompleteJson(this.dataLines.join('\n'))) {
          this.dispatch()
        }
        this.dataLines.push(value)
        break
      case 'event':
        this.eventType = value
        break
      case 'id':
        if (!value.includes('\0')) this.lastEventId = value
        break
      default:
        break
    }
  }

  private dispatch(): void {
    if (this.dataLines.length === 0) {
      this.eventType = ''
      return
    }
    const message: SseMessage = { event: this.eventType || 'message', data: this.dataLines.join('\n') }
    if (this.lastEventId !== undefined) message.id = this.lastEventId
    this.dataLines = []
    this.eventType = ''
    this.onMessage(message)
  }
}

/** Read a byte stream (e.g. `fetch().body`) as SSE; UTF-8 sequences split across chunks are handled. */
export async function readSseStream(
  stream: ReadableStream<Uint8Array>,
  onMessage: (message: SseMessage) => void,
  signal?: AbortSignal,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8')
  const parser = new SseParser(onMessage)
  const abort = () => {
    reader.cancel().catch(() => undefined)
  }
  signal?.addEventListener('abort', abort, { once: true })
  try {
    for (;;) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      const { done, value } = await reader.read()
      if (done) break
      if (value) parser.push(decoder.decode(value, { stream: true }))
    }
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    parser.push(decoder.decode())
    parser.end()
  } finally {
    signal?.removeEventListener('abort', abort)
    reader.releaseLock()
  }
}

// ---------------------------------------------------------------------------------------------
// AG-UI event decoding
// ---------------------------------------------------------------------------------------------

const KNOWN_TYPES = new Set([
  'RUN_STARTED',
  'TEXT_MESSAGE_START',
  'TEXT_MESSAGE_CONTENT',
  'TEXT_MESSAGE_END',
  'TOOL_CALL_START',
  'TOOL_CALL_END',
  'STATE_DELTA',
  'RUN_FINISHED',
  'RUN_ERROR',
])

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? fallback : String(value)
}

/**
 * Normalize one decoded JSON payload to the contract's snake_case event shape.
 * camelCase aliases used by AG-UI SDKs (threadId, messageId, toolCallId, toolCallName) are accepted.
 * Returns null for unknown or malformed events.
 */
export function normalizeAgUiEvent(raw: unknown, sseEventName = 'message'): AgUiEvent | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const type = str(r.type ?? (sseEventName !== 'message' ? sseEventName : undefined))
  if (!KNOWN_TYPES.has(type)) return null
  const threadId = str(r.thread_id ?? r.threadId)
  const runId = str(r.run_id ?? r.runId)
  const messageId = str(r.message_id ?? r.messageId)
  const toolCallId = str(r.tool_call_id ?? r.toolCallId)
  switch (type) {
    case 'RUN_STARTED':
      return { type: 'RUN_STARTED', thread_id: threadId, run_id: runId }
    case 'RUN_FINISHED':
      return { type: 'RUN_FINISHED', thread_id: threadId, run_id: runId }
    case 'TEXT_MESSAGE_START':
      return { type: 'TEXT_MESSAGE_START', message_id: messageId }
    case 'TEXT_MESSAGE_CONTENT':
      return { type: 'TEXT_MESSAGE_CONTENT', message_id: messageId, delta: str(r.delta) }
    case 'TEXT_MESSAGE_END':
      return { type: 'TEXT_MESSAGE_END', message_id: messageId }
    case 'TOOL_CALL_START':
      return {
        type: 'TOOL_CALL_START',
        tool_call_id: toolCallId,
        tool_name: str(r.tool_name ?? r.toolCallName ?? r.name),
        args: r.args,
      }
    case 'TOOL_CALL_END':
      return {
        type: 'TOOL_CALL_END',
        tool_call_id: toolCallId,
        summary: r.summary === undefined || r.summary === null ? undefined : str(r.summary),
      }
    case 'STATE_DELTA':
      return Array.isArray(r.delta) ? { type: 'STATE_DELTA', delta: r.delta } : null
    case 'RUN_ERROR':
      return { type: 'RUN_ERROR', message: str(r.message, '알 수 없는 오류') }
    default:
      return null
  }
}

/** Decode an SSE message into AG-UI events. Falls back to one JSON object per line. */
export function decodeAgUiMessage(message: SseMessage): AgUiEvent[] {
  const data = message.data.trim()
  if (!data || data === '[DONE]') return []
  const parsed: unknown[] = []
  try {
    parsed.push(JSON.parse(data))
  } catch {
    for (const line of data.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed) continue
      try {
        parsed.push(JSON.parse(trimmed))
      } catch {
        console.warn('[atlas] SSE data is not JSON:', trimmed.slice(0, 120))
      }
    }
  }
  return parsed.map((p) => normalizeAgUiEvent(p, message.event)).filter((e): e is AgUiEvent => e !== null)
}
