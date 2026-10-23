// Aggregates drawn on the walkways ("flow", docs/specs/atlas-api.md §3): estimated traffic, popup
// candidates and the walkway width / egress check. Defensive normalization of agent and REST payloads,
// colors and widths by weight, and Korean display text. Pure functions (unit-tested without a DOM).
import type { Flow, FlowFloor, FlowLegend, FlowMarker, FlowSegment, FlowSummary } from '../types/api'
import { isRecord, num, optText, texts, type Rec } from './coerce'
import { hexToRgba, rgbToHex, type Rgba } from './colors'
import { formatNumber } from './format'
import { floorOfUnitCode } from './geometry'

export const FLOW_MODE_LABELS: Readonly<Record<string, string>> = {
  traffic: '추정 유동',
  popup: '팝업 후보',
  egress: '통로 폭·피난 점검',
}

/** Eyebrow of an unknown mode (drawn like `traffic`). */
export const DEFAULT_FLOW_MODE_LABEL = '통로 집계'

export function flowModeLabel(mode: string | null | undefined): string {
  return FLOW_MODE_LABELS[mode ?? ''] ?? DEFAULT_FLOW_MODE_LABEL
}

/** Marker fills: amber for spots, teal for units, red for warnings (white numbers stay readable). */
export const FLOW_MARKER_COLORS: Readonly<Record<string, string>> = {
  spot: '#b45309',
  unit: '#0f766e',
  warning: '#c62828',
}

export const FLOW_MARKER_KIND_LABELS: Readonly<Record<string, string>> = {
  spot: '통로 위 후보 공간',
  unit: '공실·팝업 매장',
  warning: '피난 거리 초과 지점',
}

const DEFAULT_MARKER_KIND = 'spot'

export function flowMarkerColor(kind: string | null | undefined): string {
  return FLOW_MARKER_COLORS[kind ?? ''] ?? FLOW_MARKER_COLORS[DEFAULT_MARKER_KIND]
}

// --- colors and widths by weight -----------------------------------------------------------------

/** Light blue (low) to deep violet (high) for walkway weights 0..1. */
export const FLOW_STOPS: readonly string[] = ['#a9c6ff', '#7f9cf7', '#6366f1', '#6d3fd0', '#3f1786']
const FLOW_RGBA: readonly Rgba[] = FLOW_STOPS.map((hex) => hexToRgba(hex))

/** Weight used when a segment has none (drawn at the middle of the scale). */
export const DEFAULT_FLOW_WEIGHT = 0.5
export const FLOW_WIDTH_MIN_PX = 2
export const FLOW_WIDTH_MAX_PX = 10

const clamp01 = (value: number) => Math.min(1, Math.max(0, value))

/** Color of a weight (0..1, clamped; null = the middle of the scale). */
export function flowColor(weight: number | null | undefined, alpha = 240): Rgba {
  const t = clamp01(typeof weight === 'number' && Number.isFinite(weight) ? weight : DEFAULT_FLOW_WEIGHT)
  const scaled = t * (FLOW_RGBA.length - 1)
  const i = Math.min(Math.floor(scaled), FLOW_RGBA.length - 2)
  const f = scaled - i
  const [a, b] = [FLOW_RGBA[i], FLOW_RGBA[i + 1]]
  return [Math.round(a[0] + (b[0] - a[0]) * f), Math.round(a[1] + (b[1] - a[1]) * f), Math.round(a[2] + (b[2] - a[2]) * f), alpha]
}

/** CSS gradient that matches flowColor, for the legend. */
export function flowGradientCss(direction = 'to right'): string {
  const stops = FLOW_STOPS.map((c, i) => `${c} ${Math.round((i / (FLOW_STOPS.length - 1)) * 100)}%`)
  return `linear-gradient(${direction}, ${stops.join(', ')})`
}

/** Line width in pixels: 2 px for weight 0 up to 10 px for weight 1 (null = the middle). */
export function flowWidthPx(weight: number | null | undefined): number {
  const t = clamp01(typeof weight === 'number' && Number.isFinite(weight) ? weight : DEFAULT_FLOW_WEIGHT)
  return FLOW_WIDTH_MIN_PX + (FLOW_WIDTH_MAX_PX - FLOW_WIDTH_MIN_PX) * t
}

const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i

/** The backend color when it is a hex color; null otherwise (the line is then colored by weight). */
function hexColor(value: unknown): string | null {
  const color = optText(value)?.trim() ?? null
  return color && HEX.test(color) ? color : null
}

// --- normalization -------------------------------------------------------------------------------

function toPoint(value: unknown): [number, number] | null {
  if (Array.isArray(value)) {
    const x = num(value[0])
    const y = num(value[1])
    return x === null || y === null ? null : [x, y]
  }
  if (isRecord(value)) {
    const x = num(value.x)
    const y = num(value.y)
    return x === null || y === null ? null : [x, y]
  }
  return null
}

const trimmed = (value: unknown): string | null => optText(value)?.trim() ?? null

/** Segments with at least two valid points; weights are clamped, or derived from value / max_value. */
function normalizeSegments(value: unknown, maxValue: number | null, deriveWeight: boolean): FlowSegment[] {
  if (!Array.isArray(value)) return []
  const segments: FlowSegment[] = []
  for (const item of value) {
    if (!isRecord(item) || !Array.isArray(item.points)) continue
    const points = item.points.map(toPoint).filter((p): p is [number, number] => p !== null)
    if (points.length < 2) continue
    const segmentValue = num(item.value)
    let weight = num(item.weight)
    if (weight === null && deriveWeight && segmentValue !== null && maxValue !== null && maxValue > 0) weight = segmentValue / maxValue
    segments.push({
      ...item,
      points,
      weight: weight === null ? null : clamp01(weight),
      value: segmentValue,
      color: hexColor(item.color),
      label: trimmed(item.label),
    })
  }
  return segments
}

function normalizeFloor(item: Rec, deriveWeight: boolean): FlowFloor {
  const maxValue = num(item.max_value)
  return { ...item, segments: normalizeSegments(item.segments, maxValue, deriveWeight), max_value: maxValue, total: num(item.total) }
}

/** `{floor: {segments, ...}}` as in the contract; a list of `{floor, segments, ...}` is accepted too. */
function normalizeFloors(value: unknown, deriveWeight: boolean): Record<string, FlowFloor> {
  const floors: Record<string, FlowFloor> = {}
  if (Array.isArray(value)) {
    for (const item of value) {
      const floor = isRecord(item) ? trimmed(item.floor) : null
      if (floor) floors[floor] = normalizeFloor(item as Rec, deriveWeight)
    }
  } else if (isRecord(value)) {
    for (const [floor, item] of Object.entries(value)) {
      if (floor.trim() && isRecord(item)) floors[floor.trim()] = normalizeFloor(item, deriveWeight)
    }
  }
  return floors
}

function normalizeMarkers(value: unknown): FlowMarker[] {
  if (!Array.isArray(value)) return []
  const markers: FlowMarker[] = []
  for (const item of value) {
    if (!isRecord(item)) continue
    const x = num(item.x)
    const y = num(item.y)
    const unitCode = trimmed(item.unit_code)
    const floor = trimmed(item.floor) ?? floorOfUnitCode(unitCode)
    if (x === null || y === null || !floor) continue
    const rank = num(item.rank)
    markers.push({
      ...item,
      floor,
      x,
      y,
      rank: rank === null ? null : Math.round(rank),
      kind: trimmed(item.kind) ?? DEFAULT_MARKER_KIND,
      label: trimmed(item.label) ?? (rank === null ? null : String(Math.round(rank))),
      title: trimmed(item.title),
      unit_code: unitCode,
    })
  }
  return markers
}

function normalizeLegend(value: unknown): FlowLegend | null {
  if (!isRecord(value)) return null
  return { low: trimmed(value.low), high: trimmed(value.high), unit: trimmed(value.unit), note: trimmed(value.note) }
}

/**
 * Coerce an agent- or REST-provided flow into a renderable one. Returns null unless the value is an
 * object that looks like a flow (a mode, floors or markers). Segments with fewer than two valid points
 * and markers without a position or floor are dropped; an empty result is kept (e.g. no narrow walkway).
 */
export function normalizeFlow(raw: unknown): Flow | null {
  if (!isRecord(raw)) return null
  const hasFloors = isRecord(raw.floors) || Array.isArray(raw.floors)
  const mode = trimmed(raw.mode)
  if (!mode && !hasFloors && !Array.isArray(raw.markers)) return null
  const resolvedMode = mode ?? 'traffic'
  // Egress values are widths: a weight from value / max_value would mean nothing there.
  const floors = normalizeFloors(raw.floors, resolvedMode !== 'egress')
  const markers = normalizeMarkers(raw.markers)
  const summary: Rec = isRecord(raw.summary) ? raw.summary : {}
  const normalizedSummary: FlowSummary = {
    ...summary,
    customer_days: num(summary.customer_days),
    trips: num(summary.trips),
    floors: Array.isArray(summary.floors) ? texts(summary.floors) : flowDataFloors({ floors, markers }),
  }
  return {
    ...raw,
    mode: resolvedMode,
    title: trimmed(raw.title) ?? flowModeLabel(resolvedMode),
    rev: trimmed(raw.rev),
    query: isRecord(raw.query) ? raw.query : null,
    floors,
    markers,
    legend: normalizeLegend(raw.legend),
    summary: normalizedSummary,
    caveat: trimmed(raw.caveat),
  }
}

// --- floors ------------------------------------------------------------------------------------

export interface FlowFloorStat {
  /** Drawable segments on the floor. */
  segments: number
  markers: number
}

/** floor -> what the flow draws there (floors without segments or markers are left out). */
export function flowFloorStats(flow: Pick<Flow, 'floors' | 'markers'> | null | undefined): Map<string, FlowFloorStat> {
  const stats = new Map<string, FlowFloorStat>()
  if (!flow) return stats
  const stat = (floor: string) => {
    let entry = stats.get(floor)
    if (!entry) {
      entry = { segments: 0, markers: 0 }
      stats.set(floor, entry)
    }
    return entry
  }
  for (const [floor, data] of Object.entries(flow.floors)) if (data.segments.length) stat(floor).segments += data.segments.length
  for (const marker of flow.markers) stat(marker.floor).markers += 1
  return stats
}

/** Floors with something to draw, in the order they first appear. */
export function flowDataFloors(flow: Pick<Flow, 'floors' | 'markers'> | null | undefined): string[] {
  return [...flowFloorStats(flow).keys()]
}

/** Floor tab note: "추정 유동 12구간", "팝업 후보 2곳", "좁은 통로 3곳 · 피난 거리 초과 2곳". */
export function flowFloorText(mode: string, stat: FlowFloorStat): string {
  if (mode === 'egress') {
    const parts = [stat.segments ? `좁은 통로 ${stat.segments}곳` : '', stat.markers ? `피난 거리 초과 ${stat.markers}곳` : '']
    return `${flowModeLabel(mode)}: ${parts.filter(Boolean).join(' · ')}`
  }
  if (mode === 'popup' && stat.markers) return `팝업 후보 ${stat.markers}곳`
  // Popup lines are the same estimated traffic: name them so, not "팝업 후보 N구간".
  if (stat.segments) return `${flowModeLabel(mode === 'popup' ? 'traffic' : mode)} ${stat.segments}구간`
  return `${flowModeLabel(mode)} 표시 ${stat.markers}곳`
}

// --- markers -------------------------------------------------------------------------------------

/** Markers by rank (unranked ones last, in their order). */
export function sortedFlowMarkers(flow: Pick<Flow, 'markers'>): FlowMarker[] {
  return flow.markers
    .map((marker, index) => ({ marker, index }))
    .sort((a, b) => (a.marker.rank ?? Infinity) - (b.marker.rank ?? Infinity) || a.index - b.index)
    .map(({ marker }) => marker)
}

/** Text inside a marker: its label, else its rank, else "!" for a warning. */
export function flowMarkerText(marker: Pick<FlowMarker, 'label' | 'rank' | 'kind'>): string {
  if (marker.label) return marker.label
  if (marker.rank !== null) return String(marker.rank)
  return marker.kind === 'warning' ? '!' : ''
}

/** Heading of the card's marker list. */
export function flowMarkerListTitle(mode: string): string {
  if (mode === 'popup') return '팝업 후보'
  if (mode === 'egress') return '피난 거리 초과 지점'
  return '표시한 지점'
}

/** Marker title for lists and tooltips: the title, else the kind and the unit code. */
export function flowMarkerTitle(marker: Pick<FlowMarker, 'title' | 'kind' | 'unit_code'>): string {
  if (marker.title) return marker.title
  const kind = FLOW_MARKER_KIND_LABELS[marker.kind] ?? '표시 지점'
  return marker.unit_code ? `${kind} ${marker.unit_code}` : kind
}

const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/**
 * Spoken / tooltip name of a marker: "1. 3F 에스컬레이터 A 앞" — the marker text is left out when the
 * title already starts with it ("후보 1 · …", "1위 …", "#1 …") and for the bare "!" of a warning.
 */
export function flowMarkerName(marker: Pick<FlowMarker, 'title' | 'kind' | 'unit_code' | 'label' | 'rank'>): string {
  const text = flowMarkerText(marker)
  const title = flowMarkerTitle(marker)
  if (!text || text === '!') return title
  // "1F …" and "B1 …" are floor codes, not the marker number.
  const numbered = new RegExp(`^(?:[가-힣]{1,4}\\s*|#)?${escapeRegExp(text)}(?![0-9A-Za-z])`)
  return numbered.test(title) ? title : `${text}. ${title}`
}

// --- display text --------------------------------------------------------------------------------

const countText = (value: number) => formatNumber(value)

/** Tooltip value of a segment: "1,234명(추정)" for traffic, "폭 2.4m" for egress widths. */
export function flowValueText(flow: Pick<Flow, 'mode' | 'legend'>, value: number | null): string {
  if (value === null) return ''
  const unit = flow.legend?.unit ?? ''
  if (flow.mode === 'egress') return `폭 ${formatNumber(value, 1).replace(/\.0$/, '')}${unit || 'm'}`
  return `${countText(value)}${unit}`
}

/** All drawable segments of the flow. */
export function flowSegmentCount(flow: Pick<Flow, 'floors'>): number {
  return Object.values(flow.floors).reduce((sum, floor) => sum + floor.segments.length, 0)
}

const criterion = (flow: Pick<Flow, 'query' | 'summary'>, key: string): number | null =>
  num(flow.summary[key]) ?? (flow.query ? num(flow.query[key]) : null)

const meters = (value: number) => `${formatNumber(value, 1).replace(/\.0$/, '')}m`

/**
 * Summary chips of the card. Traffic and popup: "고객-일 1,234", "이동 4,567건", "2개 층" (and "후보 4곳").
 * Egress: "좁은 통로 3곳", "피난 거리 초과 2곳" and the criteria when known ("기준 폭 4m", "기준 거리 50m").
 */
export function flowSummaryChips(flow: Flow): string[] {
  const chips: string[] = []
  const floors = flow.summary.floors.length || flowDataFloors(flow).length
  if (flow.mode === 'egress') {
    chips.push(`좁은 통로 ${flowSegmentCount(flow)}곳`)
    chips.push(`피난 거리 초과 ${flow.markers.filter((m) => m.kind === 'warning').length}곳`)
    const width = criterion(flow, 'min_width_m')
    const travel = criterion(flow, 'max_travel_m')
    if (width !== null) chips.push(`기준 폭 ${meters(width)}`)
    if (travel !== null) chips.push(`기준 거리 ${meters(travel)}`)
    return chips
  }
  if (flow.summary.customer_days !== null) chips.push(`고객-일 ${countText(flow.summary.customer_days)}`)
  if (flow.summary.trips !== null) chips.push(`이동 ${countText(flow.summary.trips)}건`)
  if (floors > 1) chips.push(`${floors}개 층`)
  if (flow.mode === 'popup') chips.push(`후보 ${flow.markers.length}곳`)
  return chips
}

export interface FlowLegendInfo {
  /** Some segments are colored by weight: show the gradient. */
  gradient: boolean
  /** Backend colors (e.g. the egress warning color) with their meaning. */
  lines: { color: string; label: string }[]
  /** Marker kinds present, in display order. */
  markerKinds: string[]
}

/** What the legend explains for a flow. */
export function flowLegendInfo(flow: Pick<Flow, 'mode' | 'floors' | 'markers' | 'legend'>): FlowLegendInfo {
  const segments = Object.values(flow.floors).flatMap((floor) => floor.segments)
  const colors = [...new Set(segments.map((s) => s.color).filter((c): c is string => !!c).map((c) => rgbToHex(hexToRgba(c))))]
  const lineLabel = flow.mode === 'egress' ? '기준보다 좁은 통로' : (flow.legend?.high ?? '강조 구간')
  const kinds = new Set(flow.markers.map((m) => m.kind))
  const known = Object.keys(FLOW_MARKER_KIND_LABELS)
  return {
    gradient: segments.some((s) => !s.color) || (segments.length === 0 && flow.mode !== 'egress'),
    lines: colors.slice(0, 3).map((color) => ({ color, label: lineLabel })),
    markerKinds: [...known.filter((k) => kinds.has(k)), ...[...kinds].filter((k) => !known.includes(k))],
  }
}
