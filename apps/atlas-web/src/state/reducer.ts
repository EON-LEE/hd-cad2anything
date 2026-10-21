// Application reducer: UI state (patched by the agent or the user), chat transcript and selection.
import { applyPatch, parsePointer, type PatchOperation } from '../lib/jsonPatch'
import type { AgUiEvent, UiState } from '../types/api'
import { normalizeUiState } from './uiState'

export interface ToolCallItem {
  kind: 'tool'
  id: string
  name: string
  args: unknown
  summary?: string
  status: 'running' | 'done'
}

export type ChatItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string; streaming: boolean }
  | ToolCallItem
  | { kind: 'state'; id: string; paths: string[]; error?: string }
  | { kind: 'error'; id: string; message: string }
  | { kind: 'notice'; id: string; text: string }

export interface ChatState {
  threadId: string | null
  runId: string | null
  running: boolean
  items: ChatItem[]
}

export interface AppState {
  ui: UiState
  chat: ChatState
  selection: string[]
  /** Incremented whenever `/panel` is patched, so the side panel can react even to identical values. */
  panelSeq: number
  /** Incremented whenever `/focus` is patched, so the canvas re-fits even to an identical focus. */
  focusSeq: number
  /** Text pushed into the chat input from elsewhere (e.g. the unit details card). */
  composer: { text: string; seq: number }
}

export type AppAction =
  | { type: 'ui/patch'; ops: PatchOperation[] }
  | { type: 'selection/set'; unitCodes: string[] }
  | { type: 'chat/user'; id: string; text: string }
  | { type: 'chat/event'; event: AgUiEvent }
  | { type: 'chat/closed'; aborted?: boolean }
  | { type: 'chat/compose'; text: string }
  | { type: 'chat/reset' }

export function createAppState(ui: UiState): AppState {
  return {
    ui,
    chat: { threadId: null, runId: null, running: false, items: [] },
    selection: [],
    panelSeq: 0,
    focusSeq: 0,
    composer: { text: '', seq: 0 },
  }
}

/** Top-level UI state keys touched by a patch (e.g. "/heatmap/values/3F-N05" -> "heatmap"). */
export function touchedKeys(ops: readonly PatchOperation[]): string[] {
  const keys = new Set<string>()
  for (const op of ops) {
    for (const pointer of [op.path, 'from' in op ? op.from : undefined]) {
      if (typeof pointer !== 'string') continue
      try {
        const tokens = parsePointer(pointer)
        keys.add(tokens[0] ?? '/')
      } catch {
        keys.add(pointer)
      }
    }
  }
  return [...keys]
}

type PatchResult = { state: AppState; keys: string[]; error?: string }

function applyUiPatch(state: AppState, ops: PatchOperation[]): PatchResult {
  const keys = touchedKeys(ops)
  let ui: UiState
  try {
    ui = normalizeUiState(applyPatch(state.ui, ops), state.ui)
  } catch (error) {
    return { state, keys, error: error instanceof Error ? error.message : String(error) }
  }
  const touched = new Set(keys)
  const rootReplaced = touched.has('/')
  return {
    keys,
    state: {
      ...state,
      ui,
      selection: ui.floor !== state.ui.floor || ui.rev !== state.ui.rev ? [] : state.selection,
      panelSeq: touched.has('panel') || rootReplaced ? state.panelSeq + 1 : state.panelSeq,
      focusSeq: touched.has('focus') || rootReplaced ? state.focusSeq + 1 : state.focusSeq,
    },
  }
}

function finalizeItems(items: ChatItem[]): ChatItem[] {
  return items.map((item) => {
    if (item.kind === 'assistant' && item.streaming) return { ...item, streaming: false }
    if (item.kind === 'tool' && item.status === 'running') return { ...item, status: 'done' }
    return item
  })
}

function updateItem<K extends ChatItem['kind']>(
  items: ChatItem[],
  kind: K,
  id: string,
  update: (item: Extract<ChatItem, { kind: K }>) => ChatItem,
): ChatItem[] | null {
  const index = items.findIndex((item) => item.kind === kind && item.id === id)
  if (index === -1) return null
  const next = items.slice()
  next[index] = update(items[index] as Extract<ChatItem, { kind: K }>)
  return next
}

function reduceChatEvent(state: AppState, event: AgUiEvent): AppState {
  const chat = state.chat
  const items = chat.items
  switch (event.type) {
    case 'RUN_STARTED':
      return { ...state, chat: { ...chat, threadId: event.thread_id || chat.threadId, runId: event.run_id, running: true } }
    case 'TEXT_MESSAGE_START': {
      if (items.some((item) => item.kind === 'assistant' && item.id === event.message_id)) return state
      return {
        ...state,
        chat: { ...chat, items: [...items, { kind: 'assistant', id: event.message_id, text: '', streaming: true }] },
      }
    }
    case 'TEXT_MESSAGE_CONTENT': {
      const updated = updateItem(items, 'assistant', event.message_id, (item) => ({ ...item, text: item.text + event.delta }))
      return {
        ...state,
        chat: {
          ...chat,
          items: updated ?? [...items, { kind: 'assistant', id: event.message_id, text: event.delta, streaming: true }],
        },
      }
    }
    case 'TEXT_MESSAGE_END': {
      const updated = updateItem(items, 'assistant', event.message_id, (item) => ({ ...item, streaming: false }))
      return updated ? { ...state, chat: { ...chat, items: updated } } : state
    }
    case 'TOOL_CALL_START': {
      const updated = updateItem(items, 'tool', event.tool_call_id, (item) => ({
        ...item,
        name: event.tool_name || item.name,
        args: event.args ?? item.args,
      }))
      const tool: ToolCallItem = { kind: 'tool', id: event.tool_call_id, name: event.tool_name, args: event.args, status: 'running' }
      return { ...state, chat: { ...chat, items: updated ?? [...items, tool] } }
    }
    case 'TOOL_CALL_END': {
      const updated = updateItem(items, 'tool', event.tool_call_id, (item) => ({
        ...item,
        status: 'done',
        summary: event.summary ?? item.summary,
      }))
      const tool: ToolCallItem = {
        kind: 'tool',
        id: event.tool_call_id,
        name: '도구',
        args: undefined,
        summary: event.summary,
        status: 'done',
      }
      return { ...state, chat: { ...chat, items: updated ?? [...items, tool] } }
    }
    case 'STATE_DELTA': {
      const result = applyUiPatch(state, event.delta)
      const item: ChatItem = { kind: 'state', id: `state-${items.length}`, paths: result.keys, error: result.error }
      return { ...result.state, chat: { ...result.state.chat, items: [...items, item] } }
    }
    case 'RUN_FINISHED':
      return { ...state, chat: { ...chat, running: false, threadId: event.thread_id || chat.threadId, items: finalizeItems(items) } }
    case 'RUN_ERROR':
      return {
        ...state,
        chat: {
          ...chat,
          running: false,
          items: [...finalizeItems(items), { kind: 'error', id: `error-${items.length}`, message: event.message }],
        },
      }
    default:
      return state
  }
}

export function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'ui/patch': {
      const result = applyUiPatch(state, action.ops)
      if (result.error) console.warn('[atlas] UI patch rejected:', result.error)
      return result.state
    }
    case 'selection/set':
      return { ...state, selection: action.unitCodes }
    case 'chat/user':
      return {
        ...state,
        chat: { ...state.chat, running: true, items: [...state.chat.items, { kind: 'user', id: action.id, text: action.text }] },
      }
    case 'chat/event':
      return reduceChatEvent(state, action.event)
    case 'chat/closed': {
      if (!state.chat.running && !action.aborted) return state
      const items = finalizeItems(state.chat.items)
      const notice: ChatItem[] = action.aborted ? [{ kind: 'notice', id: `notice-${items.length}`, text: '응답을 중지했습니다.' }] : []
      return { ...state, chat: { ...state.chat, running: false, items: [...items, ...notice] } }
    }
    case 'chat/compose':
      return { ...state, composer: { text: action.text, seq: state.composer.seq + 1 } }
    case 'chat/reset':
      return { ...state, chat: { threadId: null, runId: null, running: false, items: [] } }
    default:
      return state
  }
}

/** Convenience: build a replace operation for a top-level or nested UI state path. */
export function replaceOp(path: string, value: unknown): PatchOperation {
  return { op: 'replace', path, value }
}
