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
    expect(normalizeAgUiEvent({ type: 'THINKING_START' })).toBeNull()
    expect(normalizeAgUiEvent({ type: 'STATE_DELTA', delta: 'not-an-array' })).toBeNull()
    expect(normalizeAgUiEvent('text')).toBeNull()
  })

  it('decodes progress notes in snake_case and camelCase and drops unknown fields', () => {
    expect(normalizeAgUiEvent({ type: 'THINKING_TEXT_MESSAGE_START', message_id: 'n1', role: 'assistant' })).toStrictEqual({
      type: 'THINKING_TEXT_MESSAGE_START',
      message_id: 'n1',
    })
    expect(
      normalizeAgUiEvent({ type: 'THINKING_TEXT_MESSAGE_START', message_id: 'n2', parent_tool_call_id: 'c1', agent: ' space-productivity-analyst ' }),
    ).toStrictEqual({ type: 'THINKING_TEXT_MESSAGE_START', message_id: 'n2', parent_tool_call_id: 'c1', agent: 'space-productivity-analyst' })
    expect(normalizeAgUiEvent({ type: 'THINKING_TEXT_MESSAGE_START', messageId: 'n3', parentToolCallId: 'c1', agent: '' })).toStrictEqual({
      type: 'THINKING_TEXT_MESSAGE_START',
      message_id: 'n3',
      parent_tool_call_id: 'c1',
    })
    expect(normalizeAgUiEvent({ type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'n3', delta: '4층 매출부터 볼게요.', extra: 1 })).toStrictEqual({
      type: 'THINKING_TEXT_MESSAGE_CONTENT',
      message_id: 'n3',
      delta: '4층 매출부터 볼게요.',
    })
    expect(normalizeAgUiEvent({ type: 'THINKING_TEXT_MESSAGE_END', message_id: 'n3', parent_tool_call_id: 'c1' })).toStrictEqual({
      type: 'THINKING_TEXT_MESSAGE_END',
      message_id: 'n3',
    })
    expect(decodeAgUiMessage({ event: 'THINKING_TEXT_MESSAGE_CONTENT', data: '{"messageId":"n4","delta":"방법을 바꿔 볼게요."}' })).toEqual([
      { type: 'THINKING_TEXT_MESSAGE_CONTENT', message_id: 'n4', delta: '방법을 바꿔 볼게요.' },
    ])
  })

  it('keeps the method and result of tool calls', () => {
    const method = '결제 실적을 연 매출로 환산해 ㎡당 매출을 구하고 층 하위 10%를 저효율로 봐요'
    expect(
      normalizeAgUiEvent({ type: 'TOOL_CALL_START', toolCallId: 'c1', toolCallName: 'analytics_productivity', label: '4층 공간 생산성 계산', method: ` ${method} `, extra: true }),
    ).toStrictEqual({ type: 'TOOL_CALL_START', tool_call_id: 'c1', tool_name: 'analytics_productivity', args: undefined, label: '4층 공간 생산성 계산', method })
    expect(
      normalizeAgUiEvent({
        type: 'TOOL_CALL_END',
        tool_call_id: 'c1',
        summary: '{"ok": true}',
        method,
        result: '매장 50곳 중 저효율 5곳 · 가장 낮은 곳 Ravandre Nuit(4F-E05)',
        ok: true,
        extra: 'x',
      }),
    ).toStrictEqual({
      type: 'TOOL_CALL_END',
      tool_call_id: 'c1',
      summary: '{"ok": true}',
      method,
      result: '매장 50곳 중 저효율 5곳 · 가장 낮은 곳 Ravandre Nuit(4F-E05)',
      ok: true,
    })
    // Null, empty or non-text values mean "nothing to say".
    expect(normalizeAgUiEvent({ type: 'TOOL_CALL_START', tool_call_id: 'c2', tool_name: 'x', method: null })).toStrictEqual({
      type: 'TOOL_CALL_START',
      tool_call_id: 'c2',
      tool_name: 'x',
      args: undefined,
    })
    expect(normalizeAgUiEvent({ type: 'TOOL_CALL_END', toolCallId: 'c2', method: ' ', result: null, ok: false })).toStrictEqual({
      type: 'TOOL_CALL_END',
      tool_call_id: 'c2',
      summary: undefined,
      ok: false,
    })
    expect(normalizeAgUiEvent({ type: 'TOOL_CALL_END', tool_call_id: 'c3', result: { text: 'x' } })).toStrictEqual({
      type: 'TOOL_CALL_END',
      tool_call_id: 'c3',
      summary: undefined,
    })
  })

  it('keeps user-facing labels, nesting and outcome fields of tool calls', () => {
    expect(
      normalizeAgUiEvent({
        type: 'TOOL_CALL_START',
        tool_call_id: 'n1',
        tool_name: 'analytics_productivity',
        args: { floor: '4F' },
        label: ' 4층 공간 생산성 계산 ',
        parent_tool_call_id: 'c1',
        agent: 'space-productivity-analyst',
      }),
    ).toStrictEqual({
      type: 'TOOL_CALL_START',
      tool_call_id: 'n1',
      tool_name: 'analytics_productivity',
      args: { floor: '4F' },
      label: '4층 공간 생산성 계산',
      parent_tool_call_id: 'c1',
      agent: 'space-productivity-analyst',
    })
    expect(normalizeAgUiEvent({ type: 'TOOL_CALL_START', toolCallId: 'n2', toolCallName: 'x', parentToolCallId: 'c1', label: '' })).toStrictEqual({
      type: 'TOOL_CALL_START',
      tool_call_id: 'n2',
      tool_name: 'x',
      args: undefined,
      parent_tool_call_id: 'c1',
    })
    expect(normalizeAgUiEvent({ type: 'TOOL_CALL_END', tool_call_id: 'n1', summary: '{"ok":false}', label: '계산 실패', ok: false })).toStrictEqual({
      type: 'TOOL_CALL_END',
      tool_call_id: 'n1',
      summary: '{"ok":false}',
      label: '계산 실패',
      ok: false,
    })
    expect(normalizeAgUiEvent({ type: 'TOOL_CALL_END', tool_call_id: 'n1', ok: 'yes' })).toStrictEqual({
      type: 'TOOL_CALL_END',
      tool_call_id: 'n1',
      summary: undefined,
    })
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
