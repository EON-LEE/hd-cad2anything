// UI state model (the target of STATE_DELTA patches): defaults and defensive normalization.
import { normalizeFlow } from '../lib/flow'
import { normalizeJourney } from '../lib/journey'
import { parsePointer, touchedKeys, type PatchOperation } from '../lib/jsonPatch'
import { normalizeRoute } from '../lib/route'
import type { GraphEdge, HeatmapLegend, Highlight, LayerToggles, Meta, PanelType, UiState } from '../types/api'

export const DEFAULT_LAYERS: LayerToggles = { fixtures: false, walkways: true, labels: true }

export const PANEL_TYPES: readonly PanelType[] = ['changes', 'artifact', 'layout', 'table', 'journey', 'uploads', 'none']

export function sortedFloors(meta: Meta | null | undefined) {
  return [...(meta?.floors ?? [])].sort((a, b) => a.ordinal - b.ordinal)
}

/**
 * Latest actual revision with sales (`has_sales !== false`, by effective date, then list order).
 * Falls back to the latest actual revision, then to the last revision.
 */
export function pickDefaultRevision(meta: Meta | null | undefined): string {
  const revisions = meta?.revisions ?? []
  const actual = revisions
    .map((r, index) => ({ r, index }))
    .filter(({ r }) => r.kind !== 'hypothetical')
    .sort((a, b) => (a.r.effective_date ?? '').localeCompare(b.r.effective_date ?? '') || a.index - b.index)
  const withSales = actual.filter(({ r }) => r.has_sales !== false)
  return withSales.at(-1)?.r.rev_id ?? actual.at(-1)?.r.rev_id ?? revisions.at(-1)?.rev_id ?? 'R0'
}

/** Side panel tab that a `panel` request opens; null keeps the current tab. */
export function panelTab(panel: UiState['panel']): 'review' | 'layout' | null {
  switch (panel?.type) {
    case 'changes':
    case 'artifact':
    case 'uploads':
      return 'review'
    case 'layout':
      return 'layout'
    default:
      return null
  }
}

/** Non-empty string payload field of a panel request of the given type (e.g. `uploads` → `job_id`). */
export function panelPayloadString(panel: UiState['panel'], type: PanelType, key: string): string | null {
  if (panel?.type !== type) return null
  const value = panel.payload[key]
  return typeof value === 'string' && value.trim() !== '' ? value : null
}

export function pickDefaultFloor(meta: Meta | null | undefined): string {
  const floors = sortedFloors(meta)
  return floors.find((f) => f.code === '3F')?.code ?? floors.find((f) => f.ordinal === 0)?.code ?? floors[0]?.code ?? '1F'
}

export function parentRevision(meta: Meta | null | undefined, rev: string): string | null {
  return meta?.revisions.find((r) => r.rev_id === rev)?.parent_rev_id ?? null
}

export function createInitialUiState(meta: Meta | null | undefined, overrides: Partial<UiState> = {}): UiState {
  return {
    rev: pickDefaultRevision(meta),
    compareRev: null,
    floor: pickDefaultFloor(meta),
    layers: { ...DEFAULT_LAYERS },
    heatmap: null,
    highlights: [],
    edges: [],
    focus: null,
    panel: { type: 'none', payload: {} },
    // Must exist (as null) so that agent `replace /journey` patches apply.
    journey: null,
    // Same for `replace /route` and `replace /flow`.
    route: null,
    flow: null,
    ...overrides,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function normalizeHighlights(value: unknown): Highlight[] {
  if (!Array.isArray(value)) return []
  const result: Highlight[] = []
  for (const item of value) {
    if (typeof item === 'string' && item) result.push({ unit_code: item })
    else if (isRecord(item) && nonEmptyString(item.unit_code)) {
      result.push({ ...item, unit_code: String(item.unit_code), color: optionalString(item.color), reason: optionalString(item.reason) })
    }
  }
  return result
}

function normalizeEdges(value: unknown): GraphEdge[] {
  if (!Array.isArray(value)) return []
  const result: GraphEdge[] = []
  for (const item of value) {
    if (!isRecord(item)) continue
    const from = nonEmptyString(item.from)
    const to = nonEmptyString(item.to)
    if (!from || !to) continue
    const weight = typeof item.weight === 'number' && Number.isFinite(item.weight) ? item.weight : undefined
    result.push({ ...item, from, to, weight, label: optionalString(item.label), color: optionalString(item.color) })
  }
  return result
}

function normalizeHeatmap(value: unknown): UiState['heatmap'] {
  if (!isRecord(value)) return null
  let values: Record<string, number> | null = null
  if (isRecord(value.values)) {
    values = {}
    for (const [code, v] of Object.entries(value.values)) {
      if (typeof v === 'number' && Number.isFinite(v)) values[code] = v
    }
  }
  return {
    ...value,
    metric: String(value.metric ?? ''),
    label: String(value.label ?? value.metric ?? ''),
    values,
    legend: isRecord(value.legend) ? (value.legend as HeatmapLegend) : undefined,
  }
}

function normalizeFocus(value: unknown): UiState['focus'] {
  const codes = Array.isArray(value) ? value : isRecord(value) && Array.isArray(value.unit_codes) ? value.unit_codes : null
  if (!codes) return null
  return { unit_codes: codes.filter((c): c is string => typeof c === 'string' && c !== '') }
}

function normalizePanel(value: unknown): UiState['panel'] {
  if (!isRecord(value)) return null
  const type = PANEL_TYPES.includes(value.type as PanelType) ? (value.type as PanelType) : 'none'
  return { ...value, type, payload: isRecord(value.payload) ? value.payload : {} }
}

/**
 * Coerce an (agent-patched) state into a renderable UiState. Unknown extra keys are preserved for
 * forward compatibility; malformed known keys fall back to `fallback`/defaults.
 */
export function normalizeUiState(raw: unknown, fallback: UiState): UiState {
  if (!isRecord(raw)) return fallback
  const layers = isRecord(raw.layers) ? raw.layers : {}
  const baseLayers = fallback.layers ?? DEFAULT_LAYERS
  return {
    ...raw,
    rev: nonEmptyString(raw.rev) ?? fallback.rev,
    compareRev: nonEmptyString(raw.compareRev),
    floor: nonEmptyString(raw.floor) ?? fallback.floor,
    layers: {
      ...layers,
      fixtures: typeof layers.fixtures === 'boolean' ? layers.fixtures : baseLayers.fixtures,
      walkways: typeof layers.walkways === 'boolean' ? layers.walkways : baseLayers.walkways,
      labels: typeof layers.labels === 'boolean' ? layers.labels : baseLayers.labels,
    },
    heatmap: normalizeHeatmap(raw.heatmap),
    highlights: normalizeHighlights(raw.highlights),
    edges: normalizeEdges(raw.edges),
    focus: normalizeFocus(raw.focus),
    panel: normalizePanel(raw.panel),
    journey: (raw.journey === fallback.journey ? fallback.journey : normalizeJourney(raw.journey)) ?? null,
    route: (raw.route === fallback.route ? fallback.route : normalizeRoute(raw.route)) ?? null,
    flow: (raw.flow === fallback.flow ? fallback.flow : normalizeFlow(raw.flow)) ?? null,
  }
}

/** Overlays drawn alone (docs/specs/atlas-api.md §3): showing one of them clears the others. */
export const SOLO_OVERLAYS = ['journey', 'route', 'flow'] as const

export type SoloOverlay = (typeof SOLO_OVERLAYS)[number]

const isSoloOverlay = (key: string): key is SoloOverlay => (SOLO_OVERLAYS as readonly string[]).includes(key)

/**
 * The overlay a patch shows: the last operation that sets `/journey`, `/route` or `/flow` itself to a
 * value (a field inside it, null or a removal does not count). Null when the patch shows none.
 */
export function shownOverlay(ops: readonly PatchOperation[]): SoloOverlay | null {
  let shown: SoloOverlay | null = null
  for (const op of ops) {
    if (!op || typeof op.path !== 'string' || op.op === 'test') continue
    let tokens: string[]
    try {
      tokens = parsePointer(op.path)
    } catch {
      continue
    }
    if (tokens.length !== 1 || !isSoloOverlay(tokens[0])) continue
    const key = tokens[0]
    const clears = op.op === 'remove' || ((op.op === 'add' || op.op === 'replace') && (op.value === null || op.value === undefined))
    if (clears) {
      if (shown === key) shown = null
    } else {
      shown = key
    }
  }
  return shown
}

/**
 * Contract §3: a journey, a route and a flow are each drawn alone. When a patch shows one of them (and
 * it normalized to something drawable), the other two are cleared, and so are the analysis displays
 * it replaces (heatmap, highlights, edges, compareRev) unless the same patch sets them. The backend
 * sends these clears itself (`ui_show_journey`, `ui_show_route`, `ui_show_flow`); applying the rule
 * here as well keeps user actions and older backends to one overlay. Later analysis displays clearing
 * an overlay is left to the backend, which knows where one answer ends.
 */
export function applyOverlayRules(ui: UiState, ops: readonly PatchOperation[]): UiState {
  const shown = shownOverlay(ops)
  if (!shown || ui[shown] === null) return ui
  const touched = new Set(touchedKeys(ops))
  const next: UiState = { ...ui }
  let changed = false
  for (const key of SOLO_OVERLAYS) {
    if (key !== shown && next[key] !== null) {
      next[key] = null
      changed = true
    }
  }
  if (!touched.has('heatmap') && next.heatmap !== null) {
    next.heatmap = null
    changed = true
  }
  if (!touched.has('highlights') && next.highlights.length) {
    next.highlights = []
    changed = true
  }
  if (!touched.has('edges') && next.edges.length) {
    next.edges = []
    changed = true
  }
  if (!touched.has('compareRev') && next.compareRev !== null) {
    next.compareRev = null
    changed = true
  }
  return changed ? next : ui
}

/** True when the heatmap should replace category colors. */
export function heatmapActive(ui: UiState): boolean {
  return !!ui.heatmap && !!ui.heatmap.values
}
