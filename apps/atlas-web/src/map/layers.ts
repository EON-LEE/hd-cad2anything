// deck.gl layer construction for the floor canvas (OrthographicView, local meter coordinates).
import type { Layer } from '@deck.gl/core'
import { PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers'
import { LINK_COLORS, changeColor, hexToRgba, withAlpha, type Rgba } from '../lib/colors'
import type { FloorModel, LineItem, Point2, PolygonItem, UnitInfo } from '../lib/geometry'
import { AMENITY_LABELS, LINK_TYPE_LABELS } from '../lib/format'
import {
  unitFillColor,
  unitLabels,
  type EdgeRender,
  type HighlightRender,
  type LabelItem,
  type UnitStyleContext,
} from './prepare'

export const FONT_FAMILY = '"Pretendard", "Noto Sans KR", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif'
const FONT_SETTINGS = { sdf: true, fontSize: 64, buffer: 8, radius: 12 }

export interface UnitPart extends PolygonItem {
  unit: UnitInfo
}

export interface LayerInput {
  model: FloorModel
  compareModel: FloorModel | null
  removed: UnitInfo[]
  changeMarks: Map<string, string> | null
  style: UnitStyleContext
  /** Cache key for style-dependent accessors (heatmap/compare/category state). */
  styleKey: string
  layers: { fixtures: boolean; walkways: boolean; labels: boolean }
  highlights: HighlightRender[]
  selection: UnitInfo[]
  edges: EdgeRender[]
  zoom: number
}

const rings = (d: PolygonItem) => d.rings

function unitParts(units: Iterable<UnitInfo>): UnitPart[] {
  const parts: UnitPart[] = []
  for (const unit of units) for (const part of unit.parts) parts.push({ ...part, unit })
  return parts
}

function outlinePaths(units: Iterable<UnitInfo>): { path: Point2[]; unit: UnitInfo }[] {
  const paths: { path: Point2[]; unit: UnitInfo }[] = []
  for (const unit of units) for (const part of unit.parts) for (const ring of part.rings) paths.push({ path: ring, unit })
  return paths
}

function poiLabels(model: FloorModel): LabelItem[] {
  const labels: LabelItem[] = []
  for (const item of model.polygons.vertical_link) {
    const linkType = String(item.props.link_type ?? '')
    const text = `${item.props.link_id ?? ''}\n${LINK_TYPE_LABELS[linkType] ?? linkType}`.trim()
    const xs = item.rings[0].map((p) => p[0])
    const ys = item.rings[0].map((p) => p[1])
    labels.push({
      key: item.id,
      position: [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2],
      text,
      color: [30, 41, 59, 255],
      size: 10,
    })
  }
  for (const item of model.polygons.amenity) {
    const xs = item.rings[0].map((p) => p[0])
    const ys = item.rings[0].map((p) => p[1])
    const type = String(item.props.amenity_type ?? '')
    const text = AMENITY_LABELS[type] ?? String(item.props.name_ko ?? item.props.label ?? type)
    if (!text) continue
    labels.push({
      key: item.id,
      position: [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2],
      text,
      color: [19, 78, 74, 255],
      size: 10,
    })
  }
  const center = model.bbox ? [(model.bbox[0] + model.bbox[2]) / 2, (model.bbox[1] + model.bbox[3]) / 2] : null
  for (const line of model.lines.filter((l) => l.kind === 'opening')) {
    const text = String(line.props.label ?? line.props.name_ko ?? '')
    const a = line.path[Math.floor((line.path.length - 1) / 2)]
    const b = line.path[Math.ceil((line.path.length - 1) / 2)]
    if (!text || !a || !b) continue
    const mid: Point2 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
    // Openings sit on the exterior wall: nudge their labels inside so they are not clipped.
    let position: Point2 = mid
    if (center) {
      const dx = center[0] - mid[0]
      const dy = center[1] - mid[1]
      const length = Math.hypot(dx, dy) || 1
      position = [mid[0] + (dx / length) * 4, mid[1] + (dy / length) * 4]
    }
    labels.push({
      key: line.id,
      position,
      text,
      color: [27, 94, 32, 255],
      size: 10,
    })
  }
  return labels
}

function textLayer(id: string, data: LabelItem[], extra: Record<string, unknown> = {}) {
  return new TextLayer<LabelItem>({
    id,
    data,
    getPosition: (d) => d.position,
    getText: (d) => d.text,
    getColor: (d) => d.color,
    getSize: (d) => d.size,
    sizeUnits: 'pixels',
    getTextAnchor: 'middle',
    getAlignmentBaseline: 'center',
    characterSet: 'auto',
    fontFamily: FONT_FAMILY,
    fontWeight: 600,
    lineHeight: 1.15,
    fontSettings: FONT_SETTINGS,
    outlineWidth: 5,
    outlineColor: [255, 255, 255, 220],
    ...extra,
  })
}

export function buildLayers(input: LayerInput): Layer[] {
  const { model, style, styleKey } = input
  const units = [...model.units.values()]
  const result: Layer[] = []

  result.push(
    new PolygonLayer<PolygonItem>({
      id: 'exterior',
      data: model.polygons.exterior,
      getPolygon: rings,
      getFillColor: [255, 255, 255, 255],
      getLineColor: [71, 85, 105, 255],
      getLineWidth: 2,
      lineWidthUnits: 'pixels',
    }),
  )
  if (input.layers.walkways) {
    result.push(
      new PolygonLayer<PolygonItem>({
        id: 'walkways',
        data: model.polygons.walkway,
        getPolygon: rings,
        getFillColor: [228, 231, 235, 255],
        stroked: false,
      }),
    )
  }
  result.push(
    new PolygonLayer<PolygonItem>({
      id: 'amenities',
      data: model.polygons.amenity,
      getPolygon: rings,
      getFillColor: [204, 235, 229, 255],
      getLineColor: [94, 158, 146, 255],
      getLineWidth: 1,
      lineWidthUnits: 'pixels',
    }),
    new PolygonLayer<PolygonItem>({
      id: 'vertical-links',
      data: model.polygons.vertical_link,
      getPolygon: rings,
      getFillColor: (d) => hexToRgba(LINK_COLORS[String(d.props.link_type)] ?? '#7f7f7f', 225),
      getLineColor: [30, 41, 59, 255],
      getLineWidth: 1,
      lineWidthUnits: 'pixels',
    }),
    new PolygonLayer<UnitPart>({
      id: 'units',
      data: unitParts(units),
      getPolygon: rings,
      getFillColor: (d) => unitFillColor(d.unit, style),
      getLineColor: [255, 255, 255, 255],
      getLineWidth: 1.5,
      lineWidthUnits: 'pixels',
      pickable: true,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 90],
      updateTriggers: { getFillColor: styleKey },
    }),
  )
  if (input.layers.fixtures && model.polygons.fixture.length) {
    result.push(
      new PolygonLayer<PolygonItem>({
        id: 'fixtures',
        data: model.polygons.fixture,
        getPolygon: rings,
        getFillColor: [55, 65, 81, 170],
        getLineColor: [255, 255, 255, 160],
        getLineWidth: 0.5,
        lineWidthUnits: 'pixels',
      }),
    )
  }
  const walls = model.lines.filter((l) => l.kind !== 'opening')
  if (walls.length) {
    result.push(
      new PathLayer<LineItem>({
        id: 'walls',
        data: walls,
        getPath: (d) => d.path,
        getColor: [71, 85, 105, 255],
        getWidth: 1.5,
        widthUnits: 'pixels',
      }),
    )
  }
  result.push(
    new PathLayer<LineItem>({
      id: 'openings',
      data: model.lines.filter((l) => l.kind === 'opening'),
      getPath: (d) => d.path,
      getColor: [46, 125, 50, 255],
      getWidth: 5,
      widthUnits: 'pixels',
      capRounded: true,
    }),
  )

  if (input.compareModel) {
    result.push(
      new PolygonLayer<UnitPart>({
        id: 'compare-removed',
        data: unitParts(input.removed),
        getPolygon: rings,
        getFillColor: (d) => hexToRgba(changeColor(input.changeMarks?.get(d.unit.code) ?? 'remove'), 120),
        getLineColor: (d) => hexToRgba(changeColor(input.changeMarks?.get(d.unit.code) ?? 'remove'), 255),
        getLineWidth: 2,
        lineWidthUnits: 'pixels',
        pickable: true,
        updateTriggers: { getFillColor: styleKey, getLineColor: styleKey },
      }),
      new PathLayer<{ path: Point2[]; unit: UnitInfo }>({
        id: 'compare-outlines',
        data: outlinePaths(input.compareModel.units.values()),
        getPath: (d) => d.path,
        getColor: [94, 53, 177, 230],
        getWidth: 1.5,
        widthUnits: 'pixels',
      }),
    )
  }

  if (input.highlights.length) {
    result.push(
      new PolygonLayer<HighlightRender>({
        id: 'highlight-halo',
        data: input.highlights,
        getPolygon: (d) => d.unit.parts[0].rings,
        filled: false,
        getLineColor: (d) => withAlpha(d.color, 80),
        getLineWidth: 10,
        lineWidthUnits: 'pixels',
      }),
      new PolygonLayer<HighlightRender>({
        id: 'highlights',
        data: input.highlights,
        getPolygon: (d) => d.unit.parts[0].rings,
        filled: false,
        getLineColor: (d) => d.color,
        getLineWidth: 3,
        lineWidthUnits: 'pixels',
      }),
    )
  }
  if (input.selection.length) {
    result.push(
      new PolygonLayer<UnitPart>({
        id: 'selection',
        data: unitParts(input.selection),
        getPolygon: rings,
        filled: false,
        getLineColor: [17, 24, 39, 255],
        getLineWidth: 3.5,
        lineWidthUnits: 'pixels',
      }),
    )
  }

  if (input.layers.labels) {
    const labels = unitLabels(units, input.zoom, style)
    const triggers = { updateTriggers: { getText: [input.zoom, styleKey], getColor: styleKey } }
    result.push(textLayer('unit-labels', labels.filter((l) => !l.onDark), triggers))
    result.push(textLayer('unit-labels-on-dark', labels.filter((l) => l.onDark), { ...triggers, outlineColor: [17, 24, 39, 230] }))
    result.push(textLayer('poi-labels', poiLabels(model)))
  }

  if (input.edges.length) {
    result.push(
      new PathLayer<EdgeRender>({
        id: 'edges',
        data: input.edges,
        getPath: (d) => d.path,
        getColor: (d) => d.color,
        getWidth: (d) => d.width,
        widthUnits: 'pixels',
        capRounded: true,
        jointRounded: true,
        pickable: true,
      }),
      new ScatterplotLayer<{ position: Point2; color: Rgba }>({
        id: 'edge-ends',
        data: input.edges.flatMap((e) => [
          { position: e.source, color: e.color },
          { position: e.target, color: e.color },
        ]),
        getPosition: (d) => d.position,
        getFillColor: (d) => d.color,
        getLineColor: [255, 255, 255, 255],
        stroked: true,
        getRadius: 5,
        radiusUnits: 'pixels',
        lineWidthUnits: 'pixels',
        getLineWidth: 1.5,
      }),
      textLayer(
        'edge-labels',
        input.edges
          .filter((e) => e.label)
          .map((e) => ({ key: e.key, position: e.mid, text: e.label, color: [76, 29, 149, 255] as Rgba, size: 11 })),
        { background: true, getBackgroundColor: [255, 255, 255, 235], backgroundPadding: [4, 2], outlineWidth: 0 },
      ),
    )
  }
  return result
}
