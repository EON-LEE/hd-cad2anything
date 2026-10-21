// Color utilities: hex parsing, category colors, heatmap scale and change-type colors.

export type Rgba = [number, number, number, number]

export const NO_DATA_COLOR: Rgba = [214, 217, 222, 255]
export const VACANT_COLOR: Rgba = [241, 243, 244, 255]
export const DEFAULT_CATEGORY_COLOR: Rgba = [217, 217, 217, 255]

const HEX = /^#?([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i

/** Parse `#rgb`, `#rgba`, `#rrggbb` or `#rrggbbaa`; invalid input yields `fallback`. */
export function hexToRgba(hex: string | null | undefined, alpha?: number, fallback: Rgba = DEFAULT_CATEGORY_COLOR): Rgba {
  const match = typeof hex === 'string' ? HEX.exec(hex.trim()) : null
  if (!match) return alpha === undefined ? [...fallback] : [fallback[0], fallback[1], fallback[2], alpha]
  let body = match[1]
  if (body.length <= 4) body = [...body].map((c) => c + c).join('')
  const r = parseInt(body.slice(0, 2), 16)
  const g = parseInt(body.slice(2, 4), 16)
  const b = parseInt(body.slice(4, 6), 16)
  const a = body.length === 8 ? parseInt(body.slice(6, 8), 16) : 255
  return [r, g, b, alpha ?? a]
}

export function rgbaToCss([r, g, b, a]: Rgba): string {
  return `rgba(${r}, ${g}, ${b}, ${Math.round((a / 255) * 1000) / 1000})`
}

export function rgbToHex([r, g, b]: Rgba | [number, number, number]): string {
  return `#${[r, g, b].map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('')}`
}

export function withAlpha(color: Rgba, alpha: number): Rgba {
  return [color[0], color[1], color[2], alpha]
}

/** Mix a color toward white (amount 0..1) for de-emphasis. */
export function tint(color: Rgba, amount: number): Rgba {
  const k = Math.max(0, Math.min(1, amount))
  return [
    Math.round(color[0] + (255 - color[0]) * k),
    Math.round(color[1] + (255 - color[1]) * k),
    Math.round(color[2] + (255 - color[2]) * k),
    color[3],
  ]
}

// ---------------------------------------------------------------------------------------------
// Category colors. Mirrors cad2anything.generators.spatial._category_color (golden-ratio hues in HLS)
// so that fallback colors match the backend's floor directory when /api/meta omits a color.
// ---------------------------------------------------------------------------------------------

function hlsChannel(m1: number, m2: number, hue: number): number {
  const h = ((hue % 1) + 1) % 1
  if (h < 1 / 6) return m1 + (m2 - m1) * h * 6
  if (h < 0.5) return m2
  if (h < 2 / 3) return m1 + (m2 - m1) * (2 / 3 - h) * 6
  return m1
}

export function hlsToRgb(h: number, l: number, s: number): [number, number, number] {
  if (s === 0) return [l, l, l]
  const m2 = l <= 0.5 ? l * (1 + s) : l + s - l * s
  const m1 = 2 * l - m2
  return [hlsChannel(m1, m2, h + 1 / 3), hlsChannel(m1, m2, h), hlsChannel(m1, m2, h - 1 / 3)]
}

export function categoryColorByIndex(index: number): string {
  const hue = (index * 0.618033988749895) % 1
  const [r, g, b] = hlsToRgb(hue, 0.78, 0.55)
  return `#${[r, g, b].map((v) => Math.floor(v * 255).toString(16).padStart(2, '0')).join('')}`
}

// ---------------------------------------------------------------------------------------------
// Heatmap: sequential scale for normalized values 0..1 (YlGnBu-like; light = low, dark = high).
// ---------------------------------------------------------------------------------------------

export const HEATMAP_STOPS: readonly string[] = ['#ffffcc', '#a1dab4', '#41b6c4', '#2c7fb8', '#253494']
const HEATMAP_RGBA: readonly Rgba[] = HEATMAP_STOPS.map((hex) => hexToRgba(hex))

/** Map a normalized value (0..1, clamped) to a color; null/undefined/NaN gives NO_DATA_COLOR. */
export function heatmapColor(value: number | null | undefined, alpha = 255): Rgba {
  if (value === null || value === undefined || typeof value !== 'number' || Number.isNaN(value)) {
    return withAlpha(NO_DATA_COLOR, alpha)
  }
  const t = Math.max(0, Math.min(1, value))
  const scaled = t * (HEATMAP_RGBA.length - 1)
  const i = Math.min(Math.floor(scaled), HEATMAP_RGBA.length - 2)
  const f = scaled - i
  const a = HEATMAP_RGBA[i]
  const b = HEATMAP_RGBA[i + 1]
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
    alpha,
  ]
}

/** CSS gradient that matches heatmapColor, for the legend. */
export function heatmapGradientCss(direction = 'to right'): string {
  const stops = HEATMAP_STOPS.map((c, i) => `${c} ${Math.round((i / (HEATMAP_STOPS.length - 1)) * 100)}%`)
  return `linear-gradient(${direction}, ${stops.join(', ')})`
}

// Text on dark heatmap cells needs a light color.
export function isDark([r, g, b]: Rgba): boolean {
  return 0.299 * r + 0.587 * g + 0.114 * b < 128
}

// ---------------------------------------------------------------------------------------------
// Revision comparison colors (add=green, remove=red, move/reassign=orange, resize=blue).
// ---------------------------------------------------------------------------------------------

export const CHANGE_COLORS: Record<string, string> = {
  add: '#2e7d32',
  remove: '#d32f2f',
  move: '#ef6c00',
  reassign: '#ef6c00',
  resize: '#1565c0',
  split: '#8e24aa',
  merge: '#8e24aa',
  fixture_change: '#00897b',
  walkway_change: '#6d4c41',
}

export const CHANGE_LABELS: Record<string, string> = {
  add: '추가',
  remove: '철거',
  move: '이전',
  reassign: '브랜드 교체',
  resize: '면적 변경',
  split: '분할',
  merge: '병합',
  fixture_change: '집기 변경',
  walkway_change: '통로 변경',
}

export const CHANGE_ORDER: readonly string[] = [
  'add',
  'remove',
  'move',
  'reassign',
  'resize',
  'split',
  'merge',
  'fixture_change',
  'walkway_change',
]

export function changeColor(changeType: string): string {
  return CHANGE_COLORS[changeType] ?? '#757575'
}

export const LINK_COLORS: Record<string, string> = {
  escalator: '#4a6fa5',
  elevator: '#6a4c93',
  stair: '#7f7f7f',
}

export const DEFAULT_HIGHLIGHT_COLOR = '#e53935'
export const DEFAULT_EDGE_COLOR = '#7b1fa2'
