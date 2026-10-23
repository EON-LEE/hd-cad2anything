import type { KeyboardEvent } from 'react'
import { useData } from '../api/source'
import { flowDataFloors, flowFloorStats, flowFloorText, flowMarkerListTitle, flowMarkerName, flowMarkerText, flowMarkerTitle, flowModeLabel, flowSummaryChips, sortedFlowMarkers } from '../lib/flow'
import { floorLabel } from '../lib/format'
import type { PatchOperation } from '../lib/jsonPatch'
import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { sortedFloors } from '../state/uiState'
import type { Flow, FlowMarker } from '../types/api'

/** Markers listed in the box; the rest are on the map. */
const LISTED_MARKERS = 5

/** Box over the canvas while walkway aggregates are drawn: summary, floors with data, top markers, caveat and a clear button. */
export function FlowCard({ flow }: { flow: Flow }) {
  const { meta } = useData()
  const { ui } = useAppState()
  const patchUi = usePatchUi()
  const known = sortedFloors(meta).map((f) => f.code)
  const stats = flowFloorStats(flow)
  // Floors in building order (from /api/meta), then floors the meta does not list.
  const dataFloors = [...known.filter((code) => stats.has(code)), ...flowDataFloors(flow).filter((code) => !known.includes(code))]
  const markers = sortedFlowMarkers(flow)
  const chips = flowSummaryChips(flow)
  const mode = flowModeLabel(flow.mode)

  const clear = () => patchUi([replaceOp('/flow', null)])
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') clear()
  }
  const showFloor = (floor: string) => {
    if (floor !== ui.floor) patchUi([replaceOp('/floor', floor)])
  }
  const showMarker = (marker: FlowMarker) => {
    const ops: PatchOperation[] = []
    if (marker.floor !== ui.floor) ops.push(replaceOp('/floor', marker.floor))
    if (marker.unit_code) ops.push(replaceOp('/focus', { unit_codes: [marker.unit_code] }))
    if (ops.length) patchUi(ops)
  }

  return (
    <section className={`flow-card${flow.mode === 'egress' ? ' egress' : ''}`} aria-labelledby="flow-card-title" onKeyDown={onKeyDown}>
      <p className="flow-eyebrow">
        <span className="flow-eyebrow-line" aria-hidden="true" />
        {mode}
      </p>
      <h2 id="flow-card-title">{flow.title}</h2>
      {chips.length > 0 && (
        <ul className="flow-chips" aria-label={`${mode} 요약`}>
          {chips.map((chip) => (
            <li key={chip}>{chip}</li>
          ))}
        </ul>
      )}
      {flow.rev && flow.rev !== ui.rev && (
        <p className="flow-rev-note">
          {flow.rev} 도면으로 계산했어요. 지금은 {ui.rev} 도면을 보고 있어요.
        </p>
      )}
      {dataFloors.length > 0 && (
        <ul className="flow-floors" aria-label="표시가 있는 층 (누르면 그 층 도면으로 이동)">
          {dataFloors.map((floor) => {
            const current = floor === ui.floor
            const stat = stats.get(floor)
            return (
              <li key={floor}>
                <button
                  type="button"
                  className="flow-floor"
                  aria-current={current ? 'true' : undefined}
                  aria-label={`${floorLabel(floor)}${stat ? ` ${flowFloorText(flow.mode, stat)}` : ''}, 도면 보기`}
                  disabled={known.length > 0 && !known.includes(floor)}
                  onClick={() => showFloor(floor)}
                >
                  {floorLabel(floor)}
                </button>
              </li>
            )
          })}
        </ul>
      )}
      {markers.length > 0 && (
        <div className="flow-marker-block">
          <h3 id="flow-markers-title">
            {flowMarkerListTitle(flow.mode)} {markers.length}곳
          </h3>
          <ol className="flow-markers" aria-labelledby="flow-markers-title">
            {markers.slice(0, LISTED_MARKERS).map((marker, i) => {
              const text = flowMarkerText(marker)
              const title = flowMarkerTitle(marker)
              return (
                <li key={`${marker.rank ?? 'x'}-${i}`}>
                  <button
                    type="button"
                    className={`flow-marker${marker.floor === ui.floor ? ' on-floor' : ''}`}
                    aria-label={`${flowMarkerName(marker)}, ${floorLabel(marker.floor)} 도면에서 보기`}
                    onClick={() => showMarker(marker)}
                  >
                    <span className={`flow-num kind-${marker.kind}`} aria-hidden="true">
                      {text}
                    </span>
                    <span className="flow-marker-title">{title}</span>
                    <span className="flow-marker-floor" aria-hidden="true">
                      {marker.floor}
                    </span>
                  </button>
                </li>
              )
            })}
          </ol>
          {markers.length > LISTED_MARKERS && <p className="flow-more">나머지 {markers.length - LISTED_MARKERS}곳은 도면에서 볼 수 있어요.</p>}
        </div>
      )}
      {flow.caveat && <p className="flow-caveat">{flow.caveat}</p>}
      <div className="flow-actions">
        <button type="button" className="danger" onClick={clear}>
          표시 지우기
        </button>
      </div>
    </section>
  )
}
