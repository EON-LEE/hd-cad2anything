import type { KeyboardEvent } from 'react'
import { useData } from '../api/source'
import { floorLabel } from '../lib/format'
import { ROUTE_FLOOR_ROLE_LABELS, routeFloorRoles, routeFloors, routeSummaryChips } from '../lib/route'
import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { sortedFloors } from '../state/uiState'
import type { Route } from '../types/api'

/** Compact box over the canvas while a route is drawn: summary, the floors on the way and a clear button. */
export function RouteCard({ route }: { route: Route }) {
  const { meta } = useData()
  const { ui } = useAppState()
  const patchUi = usePatchUi()
  const floors = sortedFloors(meta)
  const known = new Set(floors.map((f) => f.code))
  const roles = routeFloorRoles(route)
  const chips = routeSummaryChips(route, floors)

  const clear = () => patchUi([replaceOp('/route', null)])
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') clear()
  }

  return (
    <section className="route-card" aria-labelledby="route-card-title" onKeyDown={onKeyDown}>
      <p className="route-eyebrow">
        <span className="route-eyebrow-line" aria-hidden="true" />
        길 찾기
      </p>
      <h2 id="route-card-title">{route.title}</h2>
      {chips.length > 0 && (
        <ul className="route-chips" aria-label="경로 요약">
          {chips.map((chip) => (
            <li key={chip}>{chip}</li>
          ))}
        </ul>
      )}
      {route.rev && route.rev !== ui.rev && (
        <p className="route-rev-note">
          {route.rev} 도면으로 계산한 경로예요. 지금은 {ui.rev} 도면을 보고 있어요.
        </p>
      )}
      <ol className="route-floors" aria-label="경로가 지나는 층 (누르면 그 층 도면으로 이동)">
        {routeFloors(route).map((floor, i) => {
          const role = roles.get(floor)
          const caption = role ? ROUTE_FLOOR_ROLE_LABELS[role] : ''
          const current = floor === ui.floor
          return (
            <li key={floor}>
              {i > 0 && (
                <span className="route-floor-arrow" aria-hidden="true">
                  →
                </span>
              )}
              <button
                type="button"
                className="route-floor"
                aria-current={current ? 'true' : undefined}
                aria-label={`${floorLabel(floor)}${caption ? ` ${caption}` : ''}, 도면 보기`}
                disabled={known.size > 0 && !known.has(floor)}
                onClick={() => {
                  if (!current) patchUi([replaceOp('/floor', floor)])
                }}
              >
                {floorLabel(floor)}
                {caption && <span className="route-floor-role">{caption}</span>}
              </button>
            </li>
          )
        })}
      </ol>
      {route.caveat && <p className="route-caveat">{route.caveat}</p>}
      <div className="route-actions">
        <button type="button" className="danger" onClick={clear}>
          경로 지우기
        </button>
      </div>
    </section>
  )
}
