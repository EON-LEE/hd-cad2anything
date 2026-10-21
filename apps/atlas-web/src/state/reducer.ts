// Application reducer: UI state (patched by the agent or the user), chat transcript and selection.
import { closeRun, createRun, finishRun, foldRunEvent, isRunActive, type ChatRun } from '../lib/chatRun'
import { applyPatch, touchedKeys, type PatchOperation } from '../lib/jsonPatch'
import type { AgUiEvent, UiState } from '../types/api'
import { normalizeUiState } from './uiState'

export { touchedKeys }

/** A conversation is a list of user messages, each followed by one agent run (see lib/chatRun.ts). */
export type ChatItem = { kind: 'user'; id: string; text: string } | { kind: 'run'; id: string; run: ChatRun }

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
  /** `at` (ms) defaults to Date.now(); tests pass fixed times. */
  | { type: 'chat/user'; id: string; text: string; at?: number }
  | { type: 'chat/event'; event: AgUiEvent; at?: number }
  | { type: 'chat/closed'; aborted?: boolean; at?: number }
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

/** Index of the run that receives events: the last run item when it is still active. */
function activeRunIndex(items: readonly ChatItem[]): number {
  const last = items.length - 1
  const item = items[last]
  return item && item.kind === 'run' && isRunActive(item.run) ? last : -1
}

function withRun(chat: ChatState, index: number, run: ChatRun): ChatState {
  const items = chat.items.slice()
  items[index] = { kind: 'run', id: run.id, run }
  return { ...chat, items, running: isRunActive(run) }
}

// Events that may open a run when none is active (server-initiated output without a user message).
const OPENING_EVENTS = new Set<AgUiEvent['type']>(['RUN_STARTED', 'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TOOL_CALL_START'])

function reduceChatEvent(state: AppState, event: AgUiEvent, at: number): AppState {
  let next = state
  let patch: { keys: string[]; error?: string } | undefined
  if (event.type === 'STATE_DELTA') {
    const result = applyUiPatch(state, event.delta)
    if (result.error) console.warn('[atlas] STATE_DELTA rejected:', result.error)
    next = result.state
    patch = { keys: result.keys, error: result.error }
  }
  let chat = next.chat
  if (event.type === 'RUN_STARTED' || event.type === 'RUN_FINISHED') {
    chat = { ...chat, threadId: event.thread_id || chat.threadId, runId: event.run_id || chat.runId }
  }
  let index = activeRunIndex(chat.items)
  if (index < 0) {
    if (!OPENING_EVENTS.has(event.type)) return { ...next, chat }
    const run = createRun(`run-${chat.items.length}`, at)
    chat = { ...chat, items: [...chat.items, { kind: 'run', id: run.id, run }] }
    index = chat.items.length - 1
  }
  const current = (chat.items[index] as Extract<ChatItem, { kind: 'run' }>).run
  return { ...next, chat: withRun(chat, index, foldRunEvent(current, event, { at, patch })) }
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
    case 'chat/user': {
      const at = action.at ?? Date.now()
      const run = createRun(`run-${action.id}`, at)
      const items: ChatItem[] = [...state.chat.items, { kind: 'user', id: action.id, text: action.text }, { kind: 'run', id: run.id, run }]
      return { ...state, chat: { ...state.chat, running: true, items } }
    }
    case 'chat/event':
      return reduceChatEvent(state, action.event, action.at ?? Date.now())
    case 'chat/closed': {
      const index = activeRunIndex(state.chat.items)
      if (index < 0) return state.chat.running ? { ...state, chat: { ...state.chat, running: false } } : state
      const at = action.at ?? Date.now()
      const run = (state.chat.items[index] as Extract<ChatItem, { kind: 'run' }>).run
      return { ...state, chat: withRun(state.chat, index, action.aborted ? finishRun(run, 'stopped', at) : closeRun(run, at)) }
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
