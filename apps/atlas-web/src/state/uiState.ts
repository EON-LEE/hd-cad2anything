// UI state model (the target of STATE_DELTA patches): defaults and defensive normalization.
import type { GraphEdge, HeatmapLegend, Highlight, LayerToggles, Meta, PanelType, UiState } from '../types/api'

export const DEFAULT_LAYERS: LayerToggles = { fixtures: false, walkways: true, labels: true }

export const PANEL_TYPES: readonly PanelType[] = ['changes', 'artifact', 'layout', 'table', 'none']

export function sortedFloors(meta: Meta | null | undefined) {
  return [...(meta?.floors ?? [])].sort((a, b) => a.ordinal - b.ordinal)
}

/** Latest actual revision (by effective date, then list order); falls back to the last revision. */
export function pickDefaultRevision(meta: Meta | null | undefined): string {
  const revisions = meta?.revisions ?? []
  const actual = revisions
    .map((r, index) => ({ r, index }))
    .filter(({ r }) => r.kind !== 'hypothetical')
    .sort((a, b) => (a.r.effective_date ?? '').localeCompare(b.r.effective_date ?? '') || a.index - b.index)
  return actual.at(-1)?.r.rev_id ?? revisions.at(-1)?.rev_id ?? 'R0'
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
  }
}

/** True when the heatmap should replace category colors. */
export function heatmapActive(ui: UiState): boolean {
  return !!ui.heatmap && !!ui.heatmap.values
}
