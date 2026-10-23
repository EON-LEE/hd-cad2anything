import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { facilityName } from '../api/client'
import { useData } from '../api/source'
import { REVISION_KIND_LABELS, revisionMarkers } from '../lib/format'
import { flowFloorStats, flowFloorText } from '../lib/flow'
import { journeyStopCounts } from '../lib/journey'
import { ROUTE_FLOOR_ROLE_LABELS, routeFloorRoles } from '../lib/route'
import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { parentRevision, sortedFloors } from '../state/uiState'

export function TopBar() {
  const { meta, source, mockReason } = useData()
  const { ui } = useAppState()
  const patchUi = usePatchUi()
  const floors = sortedFloors(meta)
  const journeyCounts = useMemo(() => journeyStopCounts(ui.journey), [ui.journey])
  const routeRoles = useMemo(() => routeFloorRoles(ui.route), [ui.route])
  const flowStats = useMemo(() => flowFloorStats(ui.flow), [ui.flow])
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [bannerOpen, setBannerOpen] = useState(true)

  const compareOn = !!ui.compareRev
  const knownRevisions = new Set(meta.revisions.map((r) => r.rev_id))
  const current = meta.revisions.find((r) => r.rev_id === ui.rev)
  const currentMarkers = current ? revisionMarkers(current) : []
  const defaultCompare = (rev: string) => parentRevision(meta, rev) ?? meta.revisions.find((r) => r.rev_id !== rev)?.rev_id ?? null

  const selectRevision = (rev: string) => {
    const ops = [replaceOp('/rev', rev)]
    if (ui.compareRev === rev) ops.push(replaceOp('/compareRev', defaultCompare(rev)))
    patchUi(ops)
  }

  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
    let next = index + delta
    if (event.key === 'Home') next = 0
    if (event.key === 'End') next = floors.length - 1
    if (next === index || next < 0 || next >= floors.length) return
    event.preventDefault()
    patchUi([replaceOp('/floor', floors[next].code)])
    tabRefs.current[next]?.focus()
  }

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <h1>CAD2Anything Atlas</h1>
          <span className="facility">{facilityName(meta.facility)}</span>
          {source.mode === 'mock' && (
            <span className="badge warn" title={mockReason ?? undefined}>
              모의 데이터
            </span>
          )}
        </div>
        <div className="controls">
          <label className="field">
            <span>개정</span>
            <select value={ui.rev} onChange={(e) => selectRevision(e.target.value)}>
              {/* Revisions created after start-up (e.g. a what-if set by the agent) are not in /api/meta yet. */}
              {!knownRevisions.has(ui.rev) && <option value={ui.rev}>{ui.rev} · 목록 외</option>}
              {meta.revisions.map((r) => (
                <option key={r.rev_id} value={r.rev_id}>
                  {r.rev_id} · {REVISION_KIND_LABELS[r.kind] ?? r.kind}
                  {r.effective_date ? ` · ${r.effective_date}` : ''}
                  {r.notes ? ` · ${r.notes}` : ''}
                  {revisionMarkers(r).map((marker) => ` · ${marker}`).join('')}
                </option>
              ))}
            </select>
          </label>
          {currentMarkers.includes('업로드') && (
            <span className="badge" title="사람이 올린 도면으로 만든 개정입니다.">
              업로드
            </span>
          )}
          {currentMarkers.includes('실적 없음') && (
            <span
              className="badge warn"
              title={`적용 기간에 거래 실적이 없습니다${meta.data_through ? `(거래 데이터는 ${meta.data_through}까지)` : ''}. 분석 값은 추정치입니다.`}
            >
              실적 없음
            </span>
          )}
          <label className="toggle">
            <input
              type="checkbox"
              checked={compareOn}
              disabled={meta.revisions.length < 2}
              onChange={(e) => patchUi([replaceOp('/compareRev', e.target.checked ? defaultCompare(ui.rev) : null)])}
            />
            개정 비교
          </label>
          {compareOn && (
            <label className="field">
              <span>비교 기준</span>
              <select value={ui.compareRev ?? ''} onChange={(e) => patchUi([replaceOp('/compareRev', e.target.value || null)])}>
                {ui.compareRev && !knownRevisions.has(ui.compareRev) && <option value={ui.compareRev}>{ui.compareRev} · 목록 외</option>}
                {meta.revisions
                  .filter((r) => r.rev_id !== ui.rev)
                  .map((r) => (
                    <option key={r.rev_id} value={r.rev_id}>
                      {r.rev_id}
                    </option>
                  ))}
              </select>
            </label>
          )}
        </div>
        <nav aria-label="층 선택" className="floor-nav">
          <div role="tablist" aria-label="층" className="floor-tabs">
            {floors.map((f, i) => {
              const selected = f.code === ui.floor
              const stops = journeyCounts.get(f.code) ?? 0
              const routeRole = routeRoles.get(f.code)
              const routeText = routeRole ? `길 찾기 경로: ${ROUTE_FLOOR_ROLE_LABELS[routeRole]} 층` : ''
              const flowStat = ui.flow ? flowStats.get(f.code) : undefined
              const flowText = ui.flow && flowStat ? flowFloorText(ui.flow.mode, flowStat) : ''
              return (
                <button
                  key={f.code}
                  ref={(el) => {
                    tabRefs.current[i] = el
                  }}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  aria-controls="floor-canvas"
                  tabIndex={selected ? 0 : -1}
                  title={`${f.code} ${f.name}${stops ? ` · 고객 동선 결제 ${stops}곳` : ''}${routeText ? ` · ${routeText}` : ''}${flowText ? ` · ${flowText}` : ''}`}
                  className={selected ? 'active' : undefined}
                  onClick={() => patchUi([replaceOp('/floor', f.code)])}
                  onKeyDown={(e) => onTabKey(e, i)}
                >
                  {f.code}
                  {stops > 0 && (
                    <>
                      <span className="journey-tab-count" aria-hidden="true">
                        {stops}
                      </span>
                      <span className="visually-hidden">(고객 동선 결제 {stops}곳)</span>
                    </>
                  )}
                  {routeRole && (
                    <>
                      <span className={`route-tab-mark ${routeRole}`} aria-hidden="true" />
                      <span className="visually-hidden">({routeText})</span>
                    </>
                  )}
                  {flowText && (
                    <>
                      <span className={`flow-tab-mark${ui.flow?.mode === 'egress' ? ' egress' : ''}`} aria-hidden="true" />
                      <span className="visually-hidden">({flowText})</span>
                    </>
                  )}
                </button>
              )
            })}
          </div>
        </nav>
      </header>
      {source.mode === 'mock' && bannerOpen && mockReason && (
        <div className="mock-banner" role="status">
          <span>{mockReason}</span>
          <button type="button" className="icon-button" aria-label="안내 닫기" onClick={() => setBannerOpen(false)}>
            ×
          </button>
        </div>
      )}
    </>
  )
}
