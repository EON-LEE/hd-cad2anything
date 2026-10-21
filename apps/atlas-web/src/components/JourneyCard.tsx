import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { ApiError } from '../api/client'
import { useData } from '../api/source'
import { floorLabel, formatManwon } from '../lib/format'
import { gapText, journeySubtitle, journeySummaryChips, transitionText } from '../lib/journey'
import type { PatchOperation } from '../lib/jsonPatch'
import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { sortedFloors } from '../state/uiState'
import type { Journey, JourneyStop } from '../types/api'

const CLOSED_PANEL = { type: 'none', payload: {} }

/** Floating card over the canvas for `panel.type = "journey"`: summary, purchase timeline and actions. */
export function JourneyCard({ journey }: { journey: Journey }) {
  const { source, meta } = useData()
  const { ui } = useAppState()
  const patchUi = usePatchUi()
  const floors = sortedFloors(meta)
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [activeOrder, setActiveOrder] = useState<number | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    setActiveOrder(null)
    setMessage(null)
  }, [journey])

  const showStop = (stop: JourneyStop) => {
    setActiveOrder(stop.order)
    const ops: PatchOperation[] = []
    if (stop.floor && stop.floor !== ui.floor) ops.push(replaceOp('/floor', stop.floor))
    if (stop.unit_code) ops.push(replaceOp('/focus', { unit_codes: [stop.unit_code] }))
    if (ops.length) patchUi(ops)
  }

  const showNextCustomer = async () => {
    const segment = journey.query?.segment ?? null
    const rank = (journey.query?.rank ?? 1) + 1
    const rev = journey.query?.rev ?? null
    setLoading(true)
    setMessage(null)
    try {
      const next = await source.getJourney({ segment, rank, rev })
      if (!alive.current) return
      const ops = [replaceOp('/journey', { ...next, query: next.query ?? { segment, rank, rev } })]
      // Stops and paths follow the drawing of the purchase day's revision.
      if (next.rev && next.rev !== ui.rev) ops.push(replaceOp('/rev', next.rev))
      const firstFloor = next.stops[0]?.floor
      if (firstFloor) ops.push(replaceOp('/floor', firstFloor))
      patchUi(ops)
    } catch (error) {
      if (!alive.current) return
      if (error instanceof ApiError && error.status === 404) setMessage('더 보여 줄 고객이 없어요.')
      else setMessage(`동선을 불러오지 못했어요. ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      if (alive.current) setLoading(false)
    }
  }

  const close = () => patchUi([replaceOp('/panel', CLOSED_PANEL)])
  const clear = () => patchUi([replaceOp('/journey', null), replaceOp('/panel', CLOSED_PANEL)])
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') close()
  }

  const subtitle = journeySubtitle(journey)
  const chips = journeySummaryChips(journey)

  return (
    <section className="journey-card" aria-labelledby="journey-card-title" onKeyDown={onKeyDown}>
      <header className="journey-head">
        <p className="journey-eyebrow">
          <span className="journey-eyebrow-line" aria-hidden="true" />
          고객 동선
        </p>
        <h2 id="journey-card-title">{journey.title}</h2>
        {subtitle && <p className="journey-sub">{subtitle}</p>}
        <ul className="journey-chips" aria-label="동선 요약">
          {chips.map((chip) => (
            <li key={chip}>{chip}</li>
          ))}
        </ul>
      </header>
      <ol className="journey-timeline" aria-label="결제 순서 (누르면 도면에서 매장으로 이동)">
        {journey.stops.map((stop) => {
          const move = journey.transitions.find((t) => t.to_order === stop.order)
          const gap = gapText(stop.gap_min)
          const place = floorLabel(stop.floor)
          const name = stop.brand_name ?? stop.unit_code
          const amount = stop.amount_krw !== null ? formatManwon(stop.amount_krw) : ''
          return (
            <li key={`${stop.order}-${stop.unit_code}`}>
              {move && <div className="journey-move">{transitionText(move, floors)}</div>}
              {gap && <div className="journey-gap">{gap}</div>}
              <button
                type="button"
                className={`journey-stop${activeOrder === stop.order ? ' active' : ''}${stop.floor === ui.floor ? ' on-floor' : ''}`}
                aria-current={activeOrder === stop.order ? 'true' : undefined}
                aria-label={`${stop.order}번째 결제 ${stop.time} ${place} ${name}${stop.category_name ? ` ${stop.category_name}` : ''}${amount ? ` ${amount}` : ''}, 도면에서 보기`}
                onClick={() => showStop(stop)}
              >
                <span className="journey-num" aria-hidden="true">
                  {stop.order}
                </span>
                <span className="journey-stop-body">
                  <span className="journey-stop-top">
                    <span className="journey-time">{stop.time}</span>
                    <span className="journey-floor">{place}</span>
                    {amount && <span className="journey-amount">{amount}</span>}
                  </span>
                  <span className="journey-brand">
                    {name}
                    {stop.category_name && <span className="journey-category"> · {stop.category_name}</span>}
                  </span>
                </span>
              </button>
            </li>
          )
        })}
      </ol>
      {journey.caveat && <p className="journey-caveat">{journey.caveat}</p>}
      {message && (
        <p className="journey-message" role="status">
          {message}
        </p>
      )}
      <footer className="journey-actions">
        <button type="button" onClick={() => void showNextCustomer()} disabled={loading} aria-busy={loading}>
          {loading ? '불러오는 중…' : '다른 고객 보기'}
        </button>
        <button type="button" onClick={close}>
          닫기
        </button>
        <button type="button" className="danger" onClick={clear}>
          동선 지우기
        </button>
      </footer>
    </section>
  )
}
