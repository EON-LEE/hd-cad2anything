import { describe, expect, it } from 'vitest'
import { decodeAgUiMessage, normalizeAgUiEvent, readSseStream, SseParser, type SseMessage } from './sse'

function collect(chunks: string[], options?: ConstructorParameters<typeof SseParser>[1]): SseMessage[] {
  const messages: SseMessage[] = []
  const parser = new SseParser((m) => messages.push(m), options)
  for (const chunk of chunks) parser.push(chunk)
  parser.end()
  return messages
}

function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
}

describe('SseParser', () => {
  it('parses complete events separated by blank lines', () => {
    const messages = collect(['data: {"type":"RUN_STARTED"}\n\ndata: {"type":"RUN_FINISHED"}\n\n'])
    expect(messages.map((m) => m.data)).toEqual(['{"type":"RUN_STARTED"}', '{"type":"RUN_FINISHED"}'])
    expect(messages[0].event).toBe('message')
  })

  it('reassembles events split at arbitrary chunk boundaries', () => {
    const text = 'data: {"type":"TEXT_MESSAGE_CONTENT","delta":"안녕"}\n\ndata: {"type":"RUN_FINISHED"}\n\n'
    for (const size of [1, 2, 3, 7, 13]) {
      const chunks: string[] = []
      for (let i = 0; i < text.length; i += size) chunks.push(text.slice(i, i + size))
      const messages = collect(chunks)
      expect(messages.map((m) => JSON.parse(m.data).type)).toEqual(['TEXT_MESSAGE_CONTENT', 'RUN_FINISHED'])
    }
  })

  it('handles a split field name, CRLF and a CR-only terminator across chunks', () => {
    const messages = collect(['da', 'ta: {"a":1}\r', '\n\r', '\ndata: {"b":2}\r\r'])
    expect(messages.map((m) => m.data)).toEqual(['{"a":1}', '{"b":2}'])
  })

  it('joins multi-line data fields with newlines', () => {
    const messages = collect(['data: {"type":"STATE_DELTA",\n', 'data: "delta":[]}\n\n'])
    expect(messages).toHaveLength(1)
    expect(messages[0].data).toBe('{"type":"STATE_DELTA",\n"delta":[]}')
    expect(JSON.parse(messages[0].data)).toEqual({ type: 'STATE_DELTA', delta: [] })
  })

  it('ignores comments and unknown fields, keeps event names and ids', () => {
    const messages = collect([': keep-alive\n', 'retry: 1000\nevent: custom\nid: 7\ndata: x\n\n', 'data:y\n\n'])
    expect(messages).toEqual([
      { event: 'custom', data: 'x', id: '7' },
      { event: 'message', data: 'y', id: '7' },
    ])
  })

  it('flushes a trailing event without a blank line at end of stream', () => {
    expect(collect(['data: {"a":1}']).map((m) => m.data)).toEqual(['{"a":1}'])
  })

  it('splits back-to-back JSON data lines without blank-line separators (lenient mode)', () => {
    const messages = collect(['data: {"type":"RUN_STARTED"}\ndata: {"type":"TEXT_MESSAGE_START"}\n', 'data: {"type":"RUN_FINISHED"}\n'])
    expect(messages.map((m) => JSON.parse(m.data).type)).toEqual(['RUN_STARTED', 'TEXT_MESSAGE_START', 'RUN_FINISHED'])
  })

  it('keeps spec behaviour when lenient mode is off', () => {
    const messages = collect(['data: {"a":1}\ndata: {"b":2}\n\n'], { splitJsonLines: false })
    expect(messages).toHaveLength(1)
    expect(messages[0].data).toBe('{"a":1}\n{"b":2}')
  })
})

describe('readSseStream', () => {
  it('decodes UTF-8 sequences split across byte chunks', async () => {
    const bytes = new TextEncoder().encode('data: {"type":"TEXT_MESSAGE_CONTENT","message_id":"m","delta":"평효율 분석"}\n\n')
    const cut = bytes.indexOf(0xed) + 1 // inside the first Hangul syllable (3-byte sequence)
    const messages: SseMessage[] = []
    await readSseStream(streamOf([bytes.slice(0, cut), bytes.slice(cut, cut + 2), bytes.slice(cut + 2)]), (m) => messages.push(m))
    expect(messages).toHaveLength(1)
    expect(JSON.parse(messages[0].data).delta).toBe('평효율 분석')
  })

  it('stops when aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(readSseStream(streamOf([new TextEncoder().encode('data: x\n\n')]), () => undefined, controller.signal)).rejects.toThrow(
      /Abort/,
    )
  })
})

describe('AG-UI decoding', () => {
  it('normalizes snake_case and camelCase fields', () => {
    expect(normalizeAgUiEvent({ type: 'RUN_STARTED', thread_id: 't', run_id: 'r' })).toEqual({ type: 'RUN_STARTED', thread_id: 't', run_id: 'r' })
    expect(normalizeAgUiEvent({ type: 'TOOL_CALL_START', toolCallId: 'c1', toolCallName: 'analytics.productivity', args: { floor: '3F' } })).toEqual({
      type: 'TOOL_CALL_START',
      tool_call_id: 'c1',
      tool_name: 'analytics.productivity',
      args: { floor: '3F' },
    })
    expect(normalizeAgUiEvent({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'm', delta: 'hi' })).toEqual({
      type: 'TEXT_MESSAGE_CONTENT',
      message_id: 'm',
      delta: 'hi',
    })
  })

  it('drops unknown or malformed events', () => {
    expect(normalizeAgUiEvent({ type: 'SOMETHING_ELSE' })).toBeNull()
    expect(normalizeAgUiEvent({ type: 'STATE_DELTA', delta: 'not-an-array' })).toBeNull()
    expect(normalizeAgUiEvent('text')).toBeNull()
  })

  it('uses the SSE event name when the payload has no type', () => {
    expect(decodeAgUiMessage({ event: 'RUN_ERROR', data: '{"message":"boom"}' })).toEqual([{ type: 'RUN_ERROR', message: 'boom' }])
  })

  it('falls back to one JSON object per line and ignores [DONE]', () => {
    const events = decodeAgUiMessage({ event: 'message', data: '{"type":"RUN_STARTED","thread_id":"t","run_id":"r"}\n{"type":"RUN_FINISHED","thread_id":"t","run_id":"r"}' })
    expect(events.map((e) => e.type)).toEqual(['RUN_STARTED', 'RUN_FINISHED'])
    expect(decodeAgUiMessage({ event: 'message', data: '[DONE]' })).toEqual([])
  })
})
