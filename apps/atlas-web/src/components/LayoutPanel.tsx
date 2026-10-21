import { useEffect, useMemo, useState } from 'react'
import { useData } from '../api/source'
import { CHANGE_COLORS } from '../lib/colors'
import { formatKrw } from '../lib/format'
import { floorOfUnitCode } from '../lib/geometry'
import { formatObjective, normalizeAlternatives } from '../lib/layout'
import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { sortedFloors } from '../state/uiState'
import type { GraphEdge, Highlight, LayoutAlternative } from '../types/api'

const DEPARTURE_COLOR = '#757575'

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
  const { ui, panelSeq } = useAppState()
  const patchUi = usePatchUi()
  const floors = sortedFloors(meta)
  const [selectedFloors, setSelectedFloors] = useState<string[]>([ui.floor])
  const [maxMoves, setMaxMoves] = useState(3)
  const [keepAnchor, setKeepAnchor] = useState(true)
  const [result, setResult] = useState<{ alternatives: LayoutAlternative[]; origin: 'agent' | 'request'; rev: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [previewId, setPreviewId] = useState<string | null>(null)

  // Alternatives pushed by the agent through `panel = {type: "layout", payload: {alternatives}}`.
  useEffect(() => {
    if (ui.panel?.type !== 'layout') return
    const payload = ui.panel.payload
    if (Array.isArray(payload.alternatives)) {
      setResult({
        alternatives: normalizeAlternatives(payload.alternatives),
        origin: 'agent',
        rev: typeof payload.rev === 'string' ? payload.rev : ui.rev,
      })
      setPreviewId(null)
    }
    if (Array.isArray(payload.floors)) setSelectedFloors(payload.floors.map(String))
    // Intentionally keyed on panelSeq only: react to new panel requests, not to every UI change.
  }, [panelSeq])

  const generate = async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await source.getLayoutAlternatives({
        rev: ui.rev,
        floors: selectedFloors.length ? selectedFloors : [ui.floor],
        constraints: { max_moves: maxMoves, keep_anchors: keepAnchor },
      })
      setResult({ alternatives: normalizeAlternatives(response.alternatives), origin: 'request', rev: ui.rev })
      setPreviewId(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  const preview = (alt: LayoutAlternative) => {
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
        <p className="muted">제약 기반 배치 대안(what-if)을 만들고 KPI 영향 범위를 비교합니다.</p>
      </header>
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
            <input type="number" min={1} max={10} value={maxMoves} onChange={(e) => setMaxMoves(Math.max(1, Number(e.target.value) || 1))} />
          </label>
          <label className="toggle">
            <input type="checkbox" checked={keepAnchor} onChange={(e) => setKeepAnchor(e.target.checked)} />
            앵커 브랜드 고정
          </label>
        </div>
        <button type="submit" className="primary" disabled={loading}>
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
      {result?.alternatives.length === 0 && <p className="muted">조건을 만족하는 대안이 없습니다.</p>}
      {result?.alternatives.map((alt) => {
        const failed = alt.constraints.filter((c) => !c.ok)
        const active = previewId === alt.id
        return (
          <article key={alt.id} className={`alt-card${active ? ' active' : ''}`} aria-labelledby={`alt-${alt.id}`}>
            <header>
              <h3 id={`alt-${alt.id}`}>{alt.id}</h3>
              <span className={`badge ${failed.length ? 'danger' : 'ok'}`}>{failed.length ? `제약 위반 ${failed.length}` : '제약 충족'}</span>
              <button type="button" aria-pressed={active} onClick={() => preview(alt)}>
                {active ? '미리보는 중' : '미리보기'}
              </button>
            </header>
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
              <dt>목적함수</dt>
              <dd>
                {formatObjective(alt.objective_before)} → {formatObjective(alt.objective_after)} (
                {formatObjective(alt.objective_after - alt.objective_before, true)})
              </dd>
              <dt>매출 영향</dt>
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
          </article>
        )
      })}
    </div>
  )
}
