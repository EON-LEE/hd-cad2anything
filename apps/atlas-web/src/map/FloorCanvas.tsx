import { LinearInterpolator, OrthographicView, type PickingInfo } from '@deck.gl/core'
import DeckGL from '@deck.gl/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { cacheKeys } from '../api/keys'
import { useData } from '../api/source'
import { Legend } from '../components/Legend'
import { LayerToolbar } from '../components/LayerToolbar'
import { TablePanel } from '../components/TablePanel'
import { UnitDetailsCard } from '../components/UnitDetailsCard'
import { useCachedResource } from '../hooks/useCachedResource'
import { useElementSize } from '../hooks/useElementSize'
import { CHANGE_LABELS } from '../lib/colors'
import { formatArea } from '../lib/format'
import { buildFloorModel, fitViewState, unionBBox, MAX_ZOOM, MIN_ZOOM, type OrthoViewState, type UnitInfo } from '../lib/geometry'
import { useAppDispatch, useAppState } from '../state/store'
import { heatmapActive, sortedFloors } from '../state/uiState'
import { replaceOp } from '../state/reducer'
import { buildLayers, type UnitPart } from './layers'
import {
  categoryColorMap,
  changeMarksForFloor,
  crossFloorTargets,
  layoutFloorBadges,
  removedUnits,
  resolveEdges,
  resolveHighlights,
  unitDisplayName,
  type EdgeRender,
  type UnitStyleContext,
} from './prepare'

type ViewState = OrthoViewState & { transitionDuration?: number; transitionInterpolator?: LinearInterpolator }

const VIEW = new OrthographicView({ id: 'floor', flipY: false })
const INTERPOLATOR = new LinearInterpolator({ transitionProps: ['target', 'zoom'] })
const CONTROLLER = { doubleClickZoom: true, keyboard: true, inertia: true, scrollZoom: { speed: 0.02, smooth: false } }

export function FloorCanvas() {
  const { source, meta } = useData()
  const { ui, selection, focusSeq } = useAppState()
  const dispatch = useAppDispatch()
  const [containerRef, size] = useElementSize<HTMLDivElement>()
  const mode = source.mode
  const floors = useMemo(() => sortedFloors(meta), [meta])

  const base = useCachedResource(cacheKeys.floor(mode, ui.rev, ui.floor), () => source.getFloor(ui.rev, ui.floor, false))
  const withFixtures = useCachedResource(ui.layers.fixtures ? cacheKeys.floor(mode, ui.rev, ui.floor, true) : null, () =>
    source.getFloor(ui.rev, ui.floor, true),
  )
  const compareRev = ui.compareRev && ui.compareRev !== ui.rev ? ui.compareRev : null
  const compareFloor = useCachedResource(compareRev ? cacheKeys.floor(mode, compareRev, ui.floor) : null, () =>
    source.getFloor(compareRev!, ui.floor, false),
  )
  const changes = useCachedResource(compareRev ? cacheKeys.changes(mode, compareRev, ui.rev) : null, () =>
    source.getChanges(compareRev!, ui.rev),
  )

  const model = useMemo(() => buildFloorModel(withFixtures.data ?? base.data), [withFixtures.data, base.data])
  const compareModel = useMemo(() => (compareRev && compareFloor.data ? buildFloorModel(compareFloor.data) : null), [compareRev, compareFloor.data])
  const changeMarks = useMemo(
    () => (compareRev ? changeMarksForFloor(changes.data?.change_set.items, ui.floor) : null),
    [compareRev, changes.data, ui.floor],
  )
  const categoryColors = useMemo(() => categoryColorMap(meta.categories), [meta.categories])
  const heatValues = heatmapActive(ui) ? (ui.heatmap?.values ?? null) : null
  const style: UnitStyleContext = useMemo(() => ({ categoryColors, heatValues, changeMarks }), [categoryColors, heatValues, changeMarks])
  const styleKey = `${heatValues ? JSON.stringify(heatValues) : 'cat'}|${compareRev ?? ''}|${changeMarks?.size ?? 0}|${changes.data ? 1 : 0}`

  // --- view state: fit on floor change, zoom to state.focus when it changes ----------------------
  const [viewState, setViewState] = useState<ViewState | null>(null)
  const fittedFloor = useRef<string | null>(null)
  const appliedFocus = useRef(0)
  const ready = size.width > 0 && size.height > 0 && !!base.data && !!model.bbox

  useEffect(() => {
    if (!ready || !model.bbox) return
    const codes = (ui.focus?.unit_codes ?? []).filter((c) => model.units.has(c))
    if (focusSeq !== appliedFocus.current && codes.length) {
      appliedFocus.current = focusSeq
      fittedFloor.current = ui.floor
      const bbox = unionBBox(codes.map((c) => model.units.get(c)!.bbox))!
      setViewState({
        ...fitViewState(bbox, size.width, size.height, { padding: 90, maxZoom: 4.5, minSpan: 20 }),
        transitionDuration: 700,
        transitionInterpolator: INTERPOLATOR,
      })
      return
    }
    if (fittedFloor.current !== ui.floor) {
      fittedFloor.current = ui.floor
      setViewState(fitViewState(model.bbox, size.width, size.height, { padding: 28 }))
    }
  }, [ready, model, focusSeq, ui.focus, ui.floor, size.width, size.height])

  const fitAll = useCallback(() => {
    if (model.bbox && size.width) {
      setViewState({ ...fitViewState(model.bbox, size.width, size.height, { padding: 28 }), transitionDuration: 400, transitionInterpolator: INTERPOLATOR })
    }
  }, [model.bbox, size.width, size.height])

  const zoomBy = useCallback((delta: number) => {
    setViewState((prev) =>
      prev
        ? { ...prev, zoom: Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev.zoom + delta)), transitionDuration: 250, transitionInterpolator: INTERPOLATOR }
        : prev,
    )
  }, [])

  // --- derived render data -------------------------------------------------------------------
  const zoom = viewState?.zoom ?? 2
  const labelZoom = Math.round(zoom * 4) / 4
  const highlights = useMemo(() => resolveHighlights(ui.highlights, model.units), [ui.highlights, model.units])
  const selectedUnits = useMemo(
    () => selection.map((c) => model.units.get(c)).filter((u): u is UnitInfo => !!u),
    [selection, model.units],
  )
  const removed = useMemo(() => (compareModel ? removedUnits(model, compareModel) : []), [model, compareModel])
  const badgeTargets = useMemo(() => crossFloorTargets(ui.edges, ui.floor, model.units, floors), [ui.edges, ui.floor, model.units, floors])
  const badges = useMemo(
    () => (viewState && size.width ? layoutFloorBadges(badgeTargets, floors, viewState, size.width, size.height) : []),
    [badgeTargets, floors, viewState, size.width, size.height],
  )
  const edges: EdgeRender[] = useMemo(() => resolveEdges(ui.edges, ui.floor, model.units, badges), [ui.edges, ui.floor, model.units, badges])

  const layers = useMemo(
    () =>
      buildLayers({
        model,
        compareModel,
        removed,
        changeMarks,
        style,
        styleKey,
        layers: ui.layers,
        highlights,
        selection: selectedUnits,
        edges,
        zoom: labelZoom,
      }),
    [model, compareModel, removed, changeMarks, style, styleKey, ui.layers, highlights, selectedUnits, edges, labelZoom],
  )

  const onClick = useCallback(
    (info: PickingInfo) => {
      if (info.layer?.id === 'units' && info.object) {
        dispatch({ type: 'selection/set', unitCodes: [(info.object as UnitPart).unit.code] })
      } else if (!info.object) {
        dispatch({ type: 'selection/set', unitCodes: [] })
      }
    },
    [dispatch],
  )

  const categoryNames = useMemo(() => new Map(meta.categories.map((c) => [c.code, c.name_ko])), [meta.categories])
  const getTooltip = useCallback(
    (info: PickingInfo) => {
      const object = info.object as Record<string, unknown> | null
      if (!object) return null
      const style = { backgroundColor: 'rgba(17,24,39,0.92)', color: '#fff', fontSize: '12px', padding: '6px 8px', borderRadius: '6px', whiteSpace: 'pre-line' }
      if (info.layer?.id === 'units' || info.layer?.id === 'compare-removed') {
        const unit = (object as unknown as UnitPart).unit
        const lines = [unitDisplayName(unit), `${unit.code} · ${categoryNames.get(String(unit.props.category)) ?? unit.props.category ?? '-'}`]
        lines.push(formatArea(Number(unit.props.area_m2 ?? unit.area)))
        if (info.layer.id === 'compare-removed') lines.unshift(`[${CHANGE_LABELS[changeMarks?.get(unit.code) ?? 'remove'] ?? '철거'}] ${compareRev} 기준`)
        const change = changeMarks?.get(unit.code)
        if (change && info.layer.id === 'units') lines.push(`변경: ${CHANGE_LABELS[change] ?? change}`)
        if (heatValues && heatValues[unit.code] !== undefined) lines.push(`${ui.heatmap?.label ?? 'heatmap'}: ${heatValues[unit.code].toFixed(2)}`)
        const reason = ui.highlights.find((h) => h.unit_code === unit.code)?.reason
        if (reason) lines.push(`강조: ${reason}`)
        return { text: lines.join('\n'), style }
      }
      if (info.layer?.id === 'edges') {
        const edge = (object as unknown as EdgeRender).edge
        return { text: `${edge.from} → ${edge.to}${edge.label ? `\n${edge.label}` : ''}`, style }
      }
      return null
    },
    [categoryNames, changeMarks, compareRev, heatValues, ui.heatmap?.label, ui.highlights],
  )

  const floorName = floors.find((f) => f.code === ui.floor)?.name ?? ''
  const selectedUnit = selectedUnits[0] ?? null
  const loadingError = base.error
  const [deckError, setDeckError] = useState<string | null>(null)
  const onDeckError = useCallback((error: Error) => {
    console.error('[atlas] deck.gl error', error)
    setDeckError(error.message || String(error))
  }, [])

  return (
    <section className="canvas-wrap" id="floor-canvas" aria-label={`도면 캔버스: ${ui.floor} ${floorName}, 개정 ${ui.rev}`}>
      <LayerToolbar onZoomIn={() => zoomBy(0.5)} onZoomOut={() => zoomBy(-0.5)} onFit={fitAll} />
      <div className="canvas-stage">
        <div className="canvas" ref={containerRef}>
          {viewState && (
            <DeckGL
              views={VIEW}
              viewState={viewState}
              onViewStateChange={({ viewState: next }) => setViewState(next as ViewState)}
              controller={CONTROLLER}
              layers={layers}
              onClick={onClick}
              getTooltip={getTooltip}
              getCursor={({ isDragging, isHovering }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'grab')}
              onError={onDeckError}
            />
          )}
        </div>
        {deckError && (
          <div className="canvas-alert" role="alert">
            <span>도면을 그리지 못했습니다(WebGL): {deckError}</span>
            <button type="button" className="icon-button" aria-label="오류 알림 닫기" onClick={() => setDeckError(null)}>
              ×
            </button>
          </div>
        )}
        <p className="visually-hidden" aria-live="polite">
          {`${ui.floor} 도면: 매장 ${model.units.size}개, 강조 ${highlights.length}개, 연결선 ${edges.length}개${heatValues ? ', heatmap 표시 중' : ''}${compareRev ? `, ${compareRev} 대비 비교 중` : ''}`}
        </p>
        {badges.map((badge) => (
          <button
            key={badge.floor}
            type="button"
            className="floor-badge"
            style={{ left: badge.screen[0], top: badge.screen[1] }}
            disabled={!badge.known}
            onClick={() => dispatch({ type: 'ui/patch', ops: [replaceOp('/floor', badge.floor)] })}
            aria-label={badge.known ? `${badge.floor} ${badge.name} 층으로 이동 (다른 층과의 연결)` : `${badge.floor} (도면 없음)`}
            title={badge.known ? `${badge.floor} ${badge.name}로 이동` : `${badge.floor} 도면이 없습니다`}
          >
            {badge.floor}
          </button>
        ))}
        {selectedUnit && <UnitDetailsCard unit={selectedUnit} changeType={changeMarks?.get(selectedUnit.code) ?? null} />}
        {ui.panel?.type === 'table' && <TablePanel payload={ui.panel.payload} />}
        {(base.loading || !viewState) && !loadingError && <div className="canvas-status">도면을 불러오는 중…</div>}
        {loadingError && (
          <div className="canvas-status error" role="alert">
            <p>도면을 불러오지 못했습니다: {loadingError.message}</p>
            <button type="button" onClick={base.reload}>
              다시 시도
            </button>
          </div>
        )}
      </div>
      <Legend
        model={model}
        heatmap={heatValues ? ui.heatmap : null}
        changeMarks={changeMarks}
        compareRev={compareRev}
        rev={ui.rev}
        highlights={highlights}
        edgeCount={ui.edges.length}
      />
    </section>
  )
}
