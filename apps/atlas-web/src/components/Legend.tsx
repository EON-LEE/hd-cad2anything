import { useMemo } from 'react'
import { useData } from '../api/source'
import { CHANGE_LABELS, CHANGE_ORDER, NO_DATA_COLOR, VACANT_COLOR, changeColor, heatmapColor, heatmapGradientCss, rgbaToCss } from '../lib/colors'
import type { FloorModel } from '../lib/geometry'
import { routeLegendText } from '../lib/route'
import type { HighlightRender } from '../map/prepare'
import { replaceOp } from '../state/reducer'
import { usePatchUi } from '../state/store'
import type { Heatmap, Journey, Route } from '../types/api'

interface Props {
  model: FloorModel
  heatmap: Heatmap | null
  changeMarks: Map<string, string> | null
  compareRev: string | null
  rev: string
  highlights: HighlightRender[]
  edgeCount: number
  journey: Journey | null
  journeyStopsOnFloor: number
  journeyCardOpen: boolean
  route: Route | null
}

export function Legend({
  model,
  heatmap,
  changeMarks,
  compareRev,
  rev,
  highlights,
  edgeCount,
  journey,
  journeyStopsOnFloor,
  journeyCardOpen,
  route,
}: Props) {
  const { meta } = useData()
  const patchUi = usePatchUi()
  const categories = useMemo(() => {
    const present = new Set<string>()
    let vacant = false
    for (const unit of model.units.values()) {
      if (unit.props.vacant) vacant = true
      else if (unit.props.category) present.add(String(unit.props.category))
    }
    const known = meta.categories.filter((c) => present.has(c.code))
    const unknown = [...present].filter((code) => !meta.categories.some((c) => c.code === code))
    return { known, unknown, vacant }
  }, [model, meta.categories])

  const changeCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const type of changeMarks?.values() ?? []) counts.set(type, (counts.get(type) ?? 0) + 1)
    return CHANGE_ORDER.filter((t) => counts.has(t)).map((t) => [t, counts.get(t)!] as const)
  }, [changeMarks])

  return (
    <aside className="legend" aria-label="범례">
      <details open>
        <summary>범례</summary>
        <div className="legend-body">
        {route && (
          <section className="legend-route">
            <h3>경로</h3>
            <ul>
              <li>
                <span className="swatch route-line" aria-hidden="true" />
                이동 경로
              </li>
              <li>
                <span className="swatch route-marker origin" aria-hidden="true" />
                출발
              </li>
              <li>
                <span className="swatch route-marker destination" aria-hidden="true" />
                도착
              </li>
            </ul>
            <p className="hint">{routeLegendText(route)}</p>
          </section>
        )}
        {journey && (
          <section className="legend-journey">
            <h3>고객 동선</h3>
            <ul>
              <li>
                <span className="swatch journey-line" aria-hidden="true" />
                이동 경로
              </li>
              <li>
                <span className="swatch journey-marker" aria-hidden="true">
                  1
                </span>
                결제 순서
              </li>
            </ul>
            <p className="hint">
              결제 순서 기반 추정 경로 · 이 층 결제 {journeyStopsOnFloor}곳 / 전체 {journey.stops.length}곳
            </p>
            {!journeyCardOpen && (
              <button type="button" className="legend-action" onClick={() => patchUi([replaceOp('/panel', { type: 'journey', payload: {} })])}>
                동선 목록 보기
              </button>
            )}
          </section>
        )}
        {compareRev && (
          <section>
            <h3>
              개정 비교 {compareRev} → {rev}
            </h3>
            {changeCounts.length === 0 && <p className="hint">이 층에는 변경된 매장이 없습니다.</p>}
            <ul>
              {changeCounts.map(([type, count]) => (
                <li key={type}>
                  <span className="swatch" style={{ background: changeColor(type) }} aria-hidden="true" />
                  {CHANGE_LABELS[type] ?? type} {count}개
                </li>
              ))}
              <li>
                <span className="swatch line" style={{ borderColor: '#5e35b1' }} aria-hidden="true" />
                {compareRev} 매장 경계선
              </li>
            </ul>
          </section>
        )}
        {heatmap ? (
          <section>
            <h3>{heatmap.label || heatmap.metric}</h3>
            {heatmap.legend?.binary ? (
              <ul>
                <li>
                  <span className="swatch" style={{ background: rgbaToCss(heatmapColor(1)) }} aria-hidden="true" />
                  {heatmap.legend.high ?? '해당'}
                </li>
                <li>
                  <span className="swatch" style={{ background: rgbaToCss(heatmapColor(0)) }} aria-hidden="true" />
                  {heatmap.legend.low ?? '해당 없음'}
                </li>
              </ul>
            ) : (
              <>
                <div className="gradient" style={{ background: heatmapGradientCss() }} aria-hidden="true" />
                <div className="ticks">
                  <span>{heatmap.legend?.low ?? '0 낮음'}</span>
                  <span>0.5</span>
                  <span>{heatmap.legend?.high ?? '1 높음'}</span>
                </div>
              </>
            )}
            {(heatmap.legend?.note || !heatmap.legend?.binary) && (
              <p className="hint">{heatmap.legend?.note ?? '진할수록 높음 (0~1 정규화 값)'}</p>
            )}
            <ul>
              <li>
                <span className="swatch" style={{ background: rgbaToCss(NO_DATA_COLOR) }} aria-hidden="true" />
                데이터 없음
              </li>
            </ul>
          </section>
        ) : (
          !compareRev && (
            <section>
              <h3>카테고리</h3>
              <ul>
                {categories.known.map((c) => (
                  <li key={c.code}>
                    <span className="swatch" style={{ background: c.color }} aria-hidden="true" />
                    {c.name_ko}
                  </li>
                ))}
                {categories.unknown.map((code) => (
                  <li key={code}>
                    <span className="swatch" aria-hidden="true" />
                    {code}
                  </li>
                ))}
                {categories.vacant && (
                  <li>
                    <span className="swatch" style={{ background: rgbaToCss(VACANT_COLOR) }} aria-hidden="true" />
                    공실
                  </li>
                )}
              </ul>
            </section>
          )
        )}
        {highlights.length > 0 && (
          <section>
            <h3>강조 매장 {highlights.length}개</h3>
            <ul className="highlight-list">
              {highlights.slice(0, 6).map((h) => (
                <li key={h.unit.code}>
                  <span className="swatch ring" style={{ borderColor: rgbaToCss(h.color) }} aria-hidden="true" />
                  <strong>{h.unit.code}</strong> {h.reason}
                </li>
              ))}
            </ul>
          </section>
        )}
        {edgeCount > 0 && (
          <section>
            <h3>연결선 {edgeCount}개</h3>
            <p className="hint">굵기 = weight · 다른 층 매장은 오른쪽 가장자리의 층 배지로 연결</p>
          </section>
        )}
        </div>
      </details>
    </aside>
  )
}
