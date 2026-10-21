// Pure render-model preparation for the floor canvas (no deck.gl imports; unit-testable).
import {
  DEFAULT_CATEGORY_COLOR,
  DEFAULT_EDGE_COLOR,
  DEFAULT_HIGHLIGHT_COLOR,
  VACANT_COLOR,
  changeColor,
  heatmapColor,
  hexToRgba,
  isDark,
  tint,
  type Rgba,
} from '../lib/colors'
import {
  curvedPath,
  estimateTextWidth,
  floorOfUnitCode,
  screenToWorld,
  type FloorModel,
  type OrthoViewState,
  type Point2,
  type UnitInfo,
} from '../lib/geometry'
import type { CategoryInfo, ChangeItem, FloorInfo, GraphEdge, Highlight } from '../types/api'

export function categoryColorMap(categories: readonly CategoryInfo[]): Map<string, Rgba> {
  return new Map(categories.map((c) => [c.code, hexToRgba(c.color)]))
}

// Higher priority wins when one unit appears in several change items.
const CHANGE_PRIORITY = ['add', 'remove', 'merge', 'split', 'move', 'reassign', 'resize', 'fixture_change', 'walkway_change']

/** unit_code -> change_type for the given floor (both before and after codes). */
export function changeMarksForFloor(items: readonly ChangeItem[] | undefined, floor: string): Map<string, string> {
  const marks = new Map<string, string>()
  for (const item of items ?? []) {
    if (item.floor !== floor) continue
    for (const code of [...(item.unit_codes_before ?? []), ...(item.unit_codes_after ?? [])]) {
      const existing = marks.get(code)
      const rank = (t: string) => {
        const i = CHANGE_PRIORITY.indexOf(t)
        return i === -1 ? CHANGE_PRIORITY.length : i
      }
      if (!existing || rank(item.change_type) < rank(existing)) marks.set(code, item.change_type)
    }
  }
  return marks
}

export interface UnitStyleContext {
  categoryColors: Map<string, Rgba>
  /** Normalized heatmap values (0..1) or null when the heatmap is off. */
  heatValues: Record<string, number> | null
  /** Compare-mode change marks or null when compare mode is off. */
  changeMarks: Map<string, string> | null
}

export function unitBaseColor(unit: UnitInfo, ctx: UnitStyleContext): Rgba {
  if (ctx.heatValues) return heatmapColor(ctx.heatValues[unit.code])
  if (unit.props.vacant) return VACANT_COLOR
  return ctx.categoryColors.get(String(unit.props.category ?? '')) ?? DEFAULT_CATEGORY_COLOR
}

/** Fill color of a unit: change color in compare mode, else heatmap or category color. */
export function unitFillColor(unit: UnitInfo, ctx: UnitStyleContext): Rgba {
  const change = ctx.changeMarks?.get(unit.code)
  if (change && change !== 'walkway_change') return hexToRgba(changeColor(change), 235)
  const base = unitBaseColor(unit, ctx)
  return ctx.changeMarks ? tint(base, 0.6) : base
}

/** Units of the comparison revision that no longer exist in the current revision. */
export function removedUnits(current: FloorModel, previous: FloorModel | null): UnitInfo[] {
  if (!previous) return []
  return [...previous.units.values()].filter((u) => !current.units.has(u.code))
}

// --- labels ------------------------------------------------------------------------------------

export interface LabelItem {
  key: string
  position: Point2
  text: string
  color: Rgba
  size: number
  /** True when drawn on a dark fill (light text, dark outline). */
  onDark?: boolean
}

export function unitDisplayName(unit: UnitInfo): string {
  return String(unit.props.brand_name || (unit.props.vacant ? '(공실)' : unit.code))
}

/** Pick the longest label that fits the unit's on-screen width at `zoom`, or null. */
export function fitUnitLabel(unit: UnitInfo, zoom: number, fontPx = 12): string | null {
  const scale = 2 ** zoom
  const widthPx = (unit.bbox[2] - unit.bbox[0]) * scale - 6
  const heightPx = (unit.bbox[3] - unit.bbox[1]) * scale
  if (heightPx < fontPx + 2) return null
  const name = unitDisplayName(unit)
  const twoLines = `${name}\n${unit.code}`
  if (heightPx >= fontPx * 3.2 && estimateTextWidth(name, fontPx) <= widthPx && estimateTextWidth(unit.code, fontPx) <= widthPx) {
    return twoLines
  }
  for (const candidate of [name, unit.code]) {
    if (estimateTextWidth(candidate, fontPx) <= widthPx) return candidate
  }
  return null
}

export function unitLabels(units: Iterable<UnitInfo>, zoom: number, ctx: UnitStyleContext, fontPx = 12): LabelItem[] {
  const labels: LabelItem[] = []
  for (const unit of units) {
    const text = fitUnitLabel(unit, zoom, fontPx)
    if (!text) continue
    const dark = isDark(unitFillColor(unit, ctx))
    labels.push({
      key: unit.code,
      position: unit.centroid,
      text,
      color: dark ? [255, 255, 255, 255] : [31, 41, 55, 255],
      size: fontPx,
      onDark: dark,
    })
  }
  return labels
}

// --- highlights ---------------------------------------------------------------------------------

export interface HighlightRender {
  unit: UnitInfo
  color: Rgba
  reason: string
}

export function resolveHighlights(highlights: readonly Highlight[], units: Map<string, UnitInfo>): HighlightRender[] {
  return highlights.flatMap((h) => {
    const unit = units.get(h.unit_code)
    return unit ? [{ unit, color: hexToRgba(h.color ?? DEFAULT_HIGHLIGHT_COLOR, 255, hexToRgba(DEFAULT_HIGHLIGHT_COLOR)), reason: h.reason ?? '' }] : []
  })
}

// --- edges and floor badges --------------------------------------------------------------------

export interface FloorBadge {
  floor: string
  name: string
  /** Screen position (px) inside the canvas. */
  screen: Point2
  /** World position matching `screen` for the current view. */
  world: Point2
  known: boolean
}

/** Other floors that edges from the current floor connect to, ordered top floor first. */
export function crossFloorTargets(
  edges: readonly GraphEdge[],
  floor: string,
  units: Map<string, UnitInfo>,
  floors: readonly FloorInfo[],
): string[] {
  const targets = new Set<string>()
  for (const edge of edges) {
    const a = units.has(edge.from)
    const b = units.has(edge.to)
    if (a === b) continue
    const other = floorOfUnitCode(a ? edge.to : edge.from)
    if (other && other !== floor) targets.add(other)
  }
  const ordinal = (code: string) => floors.find((f) => f.code === code)?.ordinal ?? Number.NEGATIVE_INFINITY
  return [...targets].sort((x, y) => ordinal(y) - ordinal(x) || x.localeCompare(y))
}

/** Place floor badges along the right canvas edge, centered vertically. */
export function layoutFloorBadges(
  targets: readonly string[],
  floors: readonly FloorInfo[],
  view: Pick<OrthoViewState, 'target' | 'zoom'>,
  width: number,
  height: number,
): FloorBadge[] {
  const spacing = Math.min(72, Math.max(44, (height - 80) / Math.max(targets.length, 1)))
  return targets.map((floor, i) => {
    const y = Math.min(Math.max(height / 2 + (i - (targets.length - 1) / 2) * spacing, 36), Math.max(height - 36, 36))
    const screen: Point2 = [Math.max(width - 40, 40), y]
    const info = floors.find((f) => f.code === floor)
    return { floor, name: info?.name ?? '', screen, world: screenToWorld(view, width, height, screen), known: !!info }
  })
}

export interface EdgeRender {
  key: string
  edge: GraphEdge
  path: Point2[]
  mid: Point2
  source: Point2
  target: Point2
  width: number
  color: Rgba
  label: string
  crossFloor: string | null
}

export function edgeWidth(weight: number | undefined): number {
  const w = typeof weight === 'number' && Number.isFinite(weight) ? weight : 1
  return Math.min(10, Math.max(1.5, 1 + w * 1.6))
}

export function resolveEdges(
  edges: readonly GraphEdge[],
  floor: string,
  units: Map<string, UnitInfo>,
  badges: readonly FloorBadge[],
): EdgeRender[] {
  const badgeByFloor = new Map(badges.map((b) => [b.floor, b]))
  const endpoint = (code: string): { point: Point2; badge: string | null } | null => {
    const unit = units.get(code)
    if (unit) return { point: unit.centroid, badge: null }
    const other = floorOfUnitCode(code)
    const badge = other && other !== floor ? badgeByFloor.get(other) : undefined
    return badge ? { point: badge.world, badge: badge.floor } : null
  }
  const result: EdgeRender[] = []
  edges.forEach((edge, index) => {
    const a = endpoint(edge.from)
    const b = endpoint(edge.to)
    if (!a || !b || (a.badge && b.badge)) return
    const { path, mid } = curvedPath(a.point, b.point, a.badge || b.badge ? 0.06 : 0.18)
    result.push({
      key: `${index}:${edge.from}->${edge.to}`,
      edge,
      path,
      mid,
      source: a.point,
      target: b.point,
      width: edgeWidth(edge.weight),
      color: hexToRgba(edge.color ?? DEFAULT_EDGE_COLOR, 215, hexToRgba(DEFAULT_EDGE_COLOR, 215)),
      label: edge.label ?? (typeof edge.weight === 'number' ? edge.weight.toFixed(1) : ''),
      crossFloor: a.badge ?? b.badge,
    })
  })
  return result
}
