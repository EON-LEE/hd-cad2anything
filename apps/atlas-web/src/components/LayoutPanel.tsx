import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useData } from '../api/source'
import { CHANGE_COLORS } from '../lib/colors'
import { formatKrw } from '../lib/format'
import { floorOfUnitCode } from '../lib/geometry'
import { formatObjective, normalizeAlternatives, optimalityGapNote, optimalityNote } from '../lib/layout'
import { replaceOp, type PanelRequest } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { payloadString, sortedFloors } from '../state/uiState'
import type { GraphEdge, Highlight, LayoutAlternative, UiState } from '../types/api'
import { RelayoutSection, type RelayoutRequest } from './RelayoutSection'
import { WritesNotice } from './WritesNotice'

const DEPARTURE_COLOR = '#757575'

type LayoutView = 'season' | 'whatif'

const VIEWS: { id: LayoutView; label: string }[] = [
  { id: 'season', label: '시즌 개편' },
  { id: 'whatif', label: '배치 대안' },
]

/**
 * View a `layout` payload opens: alternatives, floors or a revision → 배치 대안; a brief id → 시즌 개편. A request that
 * names nothing (the tool does not require a payload) opens 시즌 개편 with the first brief.
 */
export function layoutViewForPayload(payload: Record<string, unknown> | null | undefined): LayoutView {
  if (payloadString(payload, 'brief_id')) return 'season'
  if (Array.isArray(payload?.alternatives) || Array.isArray(payload?.floors) || payloadString(payload, 'rev')) return 'whatif'
  return 'season'
}

/** View a `layout` panel request opens; null when the panel is not a layout request. */
export function layoutViewFor(panel: UiState['panel']): LayoutView | null {
  return panel?.type === 'layout' ? layoutViewForPayload(panel.payload) : null
}

interface LayoutResult {
  alternatives: LayoutAlternative[]
  origin: 'agent' | 'request'
  /** The revision the alternatives were calculated for. */
  rev: string
}

/** Alternatives an agent request carries; their revision is the payload's `rev`, else the screen's when it was asked. */
function agentResult(request: PanelRequest | undefined): LayoutResult | null {
  if (!request || !Array.isArray(request.payload.alternatives)) return null
  return { alternatives: normalizeAlternatives(request.payload.alternatives), origin: 'agent', rev: payloadString(request.payload, 'rev') ?? request.rev }
}

function requestedFloors(request: PanelRequest | undefined): string[] | null {
  return request && Array.isArray(request.payload.floors) ? request.payload.floors.map(String) : null
}

function seasonRequestOf(request: PanelRequest | undefined): RelayoutRequest | null {
  if (!request || layoutViewForPayload(request.payload) !== 'season') return null
  return { briefId: payloadString(request.payload, 'brief_id'), seq: request.seq }
}

/** Highlights (arrival wins over departure) and from->to edges for previewing an alternative. */
export function previewPatch(alt: LayoutAlternative) {
  const highlights = new Map<string, Highlight>()
  for (const m of alt.moves) {
    highlights.set(m.from_unit, { unit_code: m.from_unit, color: DEPARTURE_COLOR, reason: `${alt.id}: ${m.brand_name} 출발` })
  }
  for (const m of alt.moves) {
    highlights.set(m.to_unit, { unit_code: m.to_unit, color: CHANGE_COLORS.move, reason: `${alt.id}: ${m.brand_name} 도착` })
  }
  const edges: GraphEdge[] = alt.moves.map((m) => ({ from: m.from_unit, to: m.to_unit, weight: 2, label: m.brand_name, color: CHANGE_COLORS.move }))
  const codes = [...highlights.keys()]
  const floor = floorOfUnitCode(alt.moves[0]?.to_unit) ?? null
  return { highlights: [...highlights.values()], edges, codes, floor }
}

function RangeBar({ low, mid, high, min, max }: { low: number; mid: number; high: number; min: number; max: number }) {
  const span = max - min || 1
  const pos = (v: number) => `${((v - min) / span) * 100}%`
  return (
    <div className="range-bar" aria-hidden="true">
      {min < 0 && max > 0 && <span className="zero" style={{ left: pos(0) }} />}
      <span className="range" style={{ left: pos(low), width: `calc(${pos(high)} - ${pos(low)})` }} />
      <span className="mid" style={{ left: pos(mid) }} />
    </div>
  )
}

export function LayoutPanel() {
  const { source, meta } = useData()
  const { ui, panels } = useAppState()
  // The agent's latest layout request lives in the store, so alternatives it prepared while this tab was not open
  // (or after a later panel such as a journey replaced `ui.panel`) are still here when the tab opens.
  const request = panels.layout
  const patchUi = usePatchUi()
  const floors = sortedFloors(meta)
  const [view, setView] = useState<LayoutView>(() => (request ? layoutViewForPayload(request.payload) : 'season'))
  const [seasonVisited, setSeasonVisited] = useState(view === 'season')
  const [seasonRequest, setSeasonRequest] = useState<RelayoutRequest | null>(() => seasonRequestOf(request))
  const viewRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [selectedFloors, setSelectedFloors] = useState<string[]>(() => requestedFloors(request) ?? [ui.floor])
  const [maxMoves, setMaxMoves] = useState('3')
  const movesValid = Number.isInteger(Number(maxMoves)) && Number(maxMoves) >= 1 && Number(maxMoves) <= 10
  const [keepAnchor, setKeepAnchor] = useState(true)
  const [result, setResult] = useState<LayoutResult | null>(() => agentResult(request))
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [previewId, setPreviewId] = useState<string | null>(null)
  // The initial state above already shows the request the panel was mounted with; later ones are applied once each.
  const appliedSeq = useRef(request?.seq ?? 0)
  // Only the newest calculation may fill the panel: an older answer arriving late (or after unmount) is dropped.
  const requestId = useRef(0)
  // Alternatives are calculated for one revision: after the screen moved to another one they no longer fit it.
  const stale = !!result && result.rev !== ui.rev

  useEffect(() => {
    if (view === 'season') setSeasonVisited(true)
  }, [view])

  useEffect(
    () => () => {
      requestId.current += 1
    },
    [],
  )

  // A new layout request from the agent: `{brief_id}` (or nothing) opens 시즌 개편; `{rev, floors, alternatives}` the what-if view.
  useEffect(() => {
    if (!request || request.seq === appliedSeq.current) return
    appliedSeq.current = request.seq
    setView(layoutViewForPayload(request.payload))
    const season = seasonRequestOf(request)
    if (season) setSeasonRequest(season)
    const agent = agentResult(request)
    if (agent) {
      // The agent's alternatives replace whatever was being calculated here.
      requestId.current += 1
      setLoading(false)
      setError(null)
      setResult(agent)
      setPreviewId(null)
    }
    const planned = requestedFloors(request)
    if (planned) setSelectedFloors(planned)
    // Intentionally keyed on the request's seq only: react to new requests, not to every UI change.
  }, [request?.seq])

  const onViewKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % VIEWS.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + VIEWS.length) % VIEWS.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = VIEWS.length - 1
    else return
    event.preventDefault()
    setView(VIEWS[next].id)
    viewRefs.current[next]?.focus()
  }

  const generate = async () => {
    const id = (requestId.current += 1)
    const rev = ui.rev
    setLoading(true)
    setError(null)
    setResult(null)
    setPreviewId(null)
    try {
      const response = await source.getLayoutAlternatives({
        rev,
        floors: selectedFloors,
        constraints: { max_moves: Number(maxMoves), keep_anchors: keepAnchor },
      })
      if (id !== requestId.current) return
      setResult({ alternatives: normalizeAlternatives(response.alternatives), origin: 'request', rev })
    } catch (e) {
      if (id === requestId.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }

  const preview = (alt: LayoutAlternative) => {
    // Moves of another revision's alternatives do not fit the units on this screen.
    if (stale || !result?.alternatives.some((a) => a.id === alt.id)) return
    const { highlights, edges, codes, floor } = previewPatch(alt)
    patchUi([
      ...(floor ? [replaceOp('/floor', floor)] : []),
      replaceOp('/heatmap', null),
      replaceOp('/highlights', highlights),
      replaceOp('/edges', edges),
      replaceOp('/focus', { unit_codes: codes }),
    ])
    setPreviewId(alt.id)
  }

  const backToResultRev = () => {
    if (!result) return
    patchUi([replaceOp('/rev', result.rev), ...(ui.compareRev === result.rev ? [replaceOp('/compareRev', null)] : [])])
  }

  const bounds = useMemo(() => {
    const values = (result?.alternatives ?? []).flatMap((a) => [a.kpi_delta.low, a.kpi_delta.high, 0])
    return { min: Math.min(...values, 0), max: Math.max(...values, 0) }
  }, [result])

  const toggleFloor = (code: string, checked: boolean) =>
    setSelectedFloors((prev) => (checked ? [...new Set([...prev, code])] : prev.filter((f) => f !== code)))

  return (
    <div className="layout-panel">
      <header className="panel-header">
        <h2>레이아웃 대안</h2>
        <div role="tablist" aria-label="레이아웃 작업" className="sub-tabs">
          {VIEWS.map((v, i) => {
            const selected = view === v.id
            return (
              <button
                key={v.id}
                ref={(el) => {
                  viewRefs.current[i] = el
                }}
                id={`layout-view-${v.id}`}
                type="button"
                role="tab"
                aria-selected={selected}
                aria-controls={`layout-view-panel-${v.id}`}
                tabIndex={selected ? 0 : -1}
                className={selected ? 'active' : undefined}
                onClick={() => setView(v.id)}
                onKeyDown={(e) => onViewKey(e, i)}
              >
                {v.label}
              </button>
            )
          })}
        </div>
      </header>
      <div role="tabpanel" id="layout-view-panel-season" aria-labelledby="layout-view-season" className="layout-view" hidden={view !== 'season'}>
        {seasonVisited && <RelayoutSection request={seasonRequest} />}
      </div>
      <div role="tabpanel" id="layout-view-panel-whatif" aria-labelledby="layout-view-whatif" className="layout-view" hidden={view !== 'whatif'}>
        <p className="muted">브랜드를 어느 매장으로 옮길지 비교해요. 예상 매출은 모델 계산값이며, 실제 매출 증가를 보장하지 않아요.</p>
        <WritesNotice />
        <form
          className="layout-form"
          onSubmit={(e) => {
            e.preventDefault()
            void generate()
          }}
        >
          <fieldset>
            <legend>대상 층</legend>
            <div className="checkbox-row">
              {floors.map((f) => (
                <label key={f.code} className="toggle">
                  <input type="checkbox" checked={selectedFloors.includes(f.code)} onChange={(e) => toggleFloor(f.code, e.target.checked)} />
                  {f.code}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="form-row">
            <label className="field inline">
              <span>최대 이동 수</span>
              <input
                type="number"
                name="max_moves"
                autoComplete="off"
                inputMode="numeric"
                required
                min={1}
                max={10}
                value={maxMoves}
                aria-invalid={!movesValid}
                onChange={(e) => setMaxMoves(e.target.value)}
              />
            </label>
            <label className="toggle">
              <input type="checkbox" checked={keepAnchor} onChange={(e) => setKeepAnchor(e.target.checked)} />
              앵커 브랜드 고정
            </label>
          </div>
          {!movesValid && <p className="error-text" role="alert">최대 이동 수는 1~10 사이의 정수로 입력해 주세요.</p>}
          {selectedFloors.length === 0 && <p className="error-text" role="alert">대상 층을 하나 이상 선택해 주세요.</p>}
          <button type="submit" className="primary" disabled={loading || !movesValid || selectedFloors.length === 0}>
            {loading ? '생성 중…' : `대안 생성 (${ui.rev})`}
          </button>
        </form>
        {error && (
          <p className="error-text" role="alert">
            대안을 생성하지 못했습니다: {error}
          </p>
        )}
        {result && (
          <p className="muted">
            {result.origin === 'agent' ? '에이전트가 제시한' : '요청한'} 대안 {result.alternatives.length}개 · 기준 개정 {result.rev}
          </p>
        )}
        {result && stale && (
          <div id="layout-stale-note" className="notice warn layout-stale" role="status">
            <p>
              이 대안은 {result.rev} 개정으로 계산했어요. 지금 화면의 개정은 {ui.rev}라서 이동할 매장이 맞지 않아 도면 미리보기를 쓸 수 없어요.
            </p>
            <div className="form-actions">
              <button type="button" onClick={backToResultRev}>
                {result.rev} 개정으로 돌아가기
              </button>
              <span className="muted">또는 위에서 {ui.rev} 기준으로 다시 생성해 주세요.</span>
            </div>
          </div>
        )}
        {loading && <p className="muted" role="status">이동 조건을 확인하고 배치 대안을 계산하고 있어요…</p>}
        {result?.alternatives.length === 0 && (
          <div className="layout-empty" role="status">
            <strong>현재 조건에서 개선 대안을 찾지 못했어요.</strong>
            <p>대안이 없다는 뜻이지, 현재 배치가 모든 조건에서 최적이라는 뜻은 아니에요. 이동 수나 대상 층을 조정해 다시 계산해 보세요.</p>
          </div>
        )}
        {result && result.alternatives.length > 0 && (
          <div className="layout-comparison">
            <table>
              <caption>대안 한눈에 비교 · 예상 연매출 변화</caption>
              <thead>
                <tr><th scope="col">대안</th><th scope="col">이동</th><th scope="col">예상 변화</th><th scope="col">도면</th></tr>
              </thead>
              <tbody>
                {result.alternatives.map((alt) => (
                  <tr key={alt.id} className={previewId === alt.id ? 'selected' : undefined}>
                    <th scope="row">
                      {alt.id}
                      <span className={`comparison-constraint ${alt.constraints.some((c) => !c.ok) ? 'fail-text' : 'muted'}`}>
                        {alt.constraints.some((c) => !c.ok) ? '제약 위반' : alt.constraints.length ? '검사한 제약 충족' : '제약 미확인'}
                      </span>
                    </th>
                    <td>{alt.moves.length}곳</td>
                    <td>{formatKrw(alt.kpi_delta.sales_krw, true)}</td>
                    <td>
                      <button
                        type="button"
                        aria-label={`${alt.id} 도면에서 보기`}
                        aria-pressed={previewId === alt.id}
                        disabled={stale}
                        aria-describedby={stale ? 'layout-stale-note' : undefined}
                        onClick={() => preview(alt)}
                      >
                        보기
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {result?.alternatives.map((alt, index) => {
          const failed = alt.constraints.filter((c) => !c.ok)
          const active = previewId === alt.id
          const searchNote = optimalityNote(alt.search_status, { first: index === 0, roundingKrw: alt.objective_rounding_krw })
          const gapNote = searchNote ? optimalityGapNote(alt.gap_krw) : null
          return (
            <article key={alt.id} className={`alt-card${active ? ' active' : ''}`} aria-labelledby={`alt-${alt.id}`}>
              <header>
                <h3 id={`alt-${alt.id}`}>{alt.id}</h3>
                <span className={`badge ${failed.length ? 'danger' : alt.constraints.length ? 'ok' : ''}`}>{failed.length ? `제약 위반 ${failed.length}` : alt.constraints.length ? '검사한 제약 충족' : '제약 검사 미확인'}</span>
                <button type="button" aria-pressed={active} disabled={stale} aria-describedby={stale ? 'layout-stale-note' : undefined} onClick={() => preview(alt)}>
                  {active ? '미리보는 중' : '미리보기'}
                </button>
              </header>
              <p className="alt-kpi"><strong>예상 연매출 {formatKrw(alt.kpi_delta.sales_krw, true)}</strong></p>
              {searchNote && (
                <p className="muted">
                  {searchNote}
                  {gapNote ? ` · ${gapNote}` : ''}
                </p>
              )}
              <details className="alt-details">
                <summary>이동 {alt.moves.length}곳 · 제약 · 계산 근거 보기</summary>
              <table className="moves">
                <caption className="visually-hidden">{alt.id} 이동 목록</caption>
                <thead>
                  <tr>
                    <th scope="col">브랜드</th>
                    <th scope="col">출발</th>
                    <th scope="col">도착</th>
                  </tr>
                </thead>
                <tbody>
                  {alt.moves.map((m, i) => (
                    <tr key={i}>
                      <td>{m.brand_name}</td>
                      <td>{m.from_unit}</td>
                      <td>{m.to_unit}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <dl className="kv">
                <dt>모델 평가값</dt>
                <dd>
                  {formatObjective(alt.objective_before)} → {formatObjective(alt.objective_after)} (
                  {formatObjective(alt.objective_after - alt.objective_before, true)})
                </dd>
                <dt>예상 연매출 변화</dt>
                <dd>
                  {formatKrw(alt.kpi_delta.sales_krw, true)}
                  <span className="muted">
                    {' '}
                    (범위 {formatKrw(alt.kpi_delta.low, true)} ~ {formatKrw(alt.kpi_delta.high, true)})
                  </span>
                  <RangeBar low={alt.kpi_delta.low} mid={alt.kpi_delta.sales_krw} high={alt.kpi_delta.high} min={bounds.min} max={bounds.max} />
                </dd>
              </dl>
              <ul className="constraint-list" aria-label="제약 조건">
                {alt.constraints.map((c) => (
                  <li key={c.name} className={c.ok ? 'ok' : 'fail'}>
                    <span aria-hidden="true">{c.ok ? '✔' : '✖'}</span> {c.name} <span className="visually-hidden">{c.ok ? '충족' : '위반'}</span>
                    {!c.ok && <strong className="fail-text"> 위반</strong>}
                    {!c.ok && c.detail && <span className="muted"> ({c.detail})</span>}
                  </li>
                ))}
              </ul>
              {alt.explanations.length > 0 && (
                <ul className="explanations">
                  {alt.explanations.map((text, i) => (
                    <li key={i}>{text}</li>
                  ))}
                </ul>
              )}
              {alt.rev_id && <p className="muted">가상 개정 {alt.rev_id}로 저장됨</p>}
              </details>
            </article>
          )
        })}
      </div>
    </div>
  )
}
