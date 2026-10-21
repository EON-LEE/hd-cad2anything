import { cacheKeys } from '../api/keys'
import { useData } from '../api/source'
import { useCachedResource } from '../hooks/useCachedResource'
import { CHANGE_LABELS, changeColor } from '../lib/colors'
import { formatArea, formatKrw, formatNumber, formatPercentile } from '../lib/format'
import type { UnitInfo } from '../lib/geometry'
import { unitDisplayName } from '../map/prepare'
import { useAppDispatch, useAppState } from '../state/store'

interface Props {
  unit: UnitInfo
  changeType: string | null
}

export function UnitDetailsCard({ unit, changeType }: Props) {
  const { source, meta } = useData()
  const { ui } = useAppState()
  const dispatch = useAppDispatch()
  const productivity = useCachedResource(cacheKeys.productivity(source.mode, ui.rev, ui.floor), () => source.getProductivity(ui.rev, ui.floor))
  const row = productivity.data?.rows.find((r) => r.unit_code === unit.code)
  const category = meta.categories.find((c) => c.code === unit.props.category)
  const heat = ui.heatmap?.values?.[unit.code]
  const highlight = ui.highlights.find((h) => h.unit_code === unit.code)
  const name = unitDisplayName(unit)
  const area = typeof unit.props.area_m2 === 'number' ? unit.props.area_m2 : unit.area

  return (
    <aside className="details-card" aria-label={`${unit.code} 매장 상세`}>
      <header>
        <div>
          <h2>{name}</h2>
          <p className="muted">
            {unit.code}
            {unit.props.zone_code ? ` · ${unit.props.zone_code}` : ''}
          </p>
        </div>
        <button type="button" className="icon-button" aria-label="매장 상세 닫기" onClick={() => dispatch({ type: 'selection/set', unitCodes: [] })}>
          ×
        </button>
      </header>
      <dl className="kv">
        <dt>카테고리</dt>
        <dd>
          {category && <span className="swatch" style={{ background: category.color }} aria-hidden="true" />}
          {category?.name_ko ?? unit.props.category ?? '–'}
        </dd>
        <dt>면적</dt>
        <dd>{formatArea(area)}</dd>
        {unit.props.brand_id && (
          <>
            <dt>브랜드 ID</dt>
            <dd>{unit.props.brand_id}</dd>
          </>
        )}
        {changeType && (
          <>
            <dt>변경</dt>
            <dd>
              <span className="swatch" style={{ background: changeColor(changeType) }} aria-hidden="true" />
              {CHANGE_LABELS[changeType] ?? changeType} ({ui.compareRev} 대비)
            </dd>
          </>
        )}
        {heat !== undefined && (
          <>
            <dt>{ui.heatmap?.label || 'heatmap'}</dt>
            <dd>{heat.toFixed(2)} (0~1 정규화)</dd>
          </>
        )}
        {highlight?.reason && (
          <>
            <dt>강조 사유</dt>
            <dd>{highlight.reason}</dd>
          </>
        )}
      </dl>
      <section className="kpi" aria-label="KPI">
        <h3>KPI · {ui.rev}</h3>
        {productivity.loading && <p className="muted">KPI 불러오는 중…</p>}
        {productivity.error && <p className="muted">KPI를 불러오지 못했습니다.</p>}
        {row ? (
          <dl className="kv">
            <dt>매출</dt>
            <dd>{formatKrw(row.sales_krw)}</dd>
            <dt>평효율</dt>
            <dd>{formatNumber(row.sales_per_m2)}원/㎡</dd>
            <dt>평당 매출</dt>
            <dd>{formatNumber(row.sales_per_pyeong)}원/평</dd>
            <dt>순위</dt>
            <dd>
              {formatPercentile(row.percentile)}
              {row.low_efficiency && <span className="badge danger">저효율</span>}
            </dd>
            {row.driver && (
              <>
                <dt>요인</dt>
                <dd>{row.driver}</dd>
              </>
            )}
          </dl>
        ) : (
          !productivity.loading && !productivity.error && <p className="muted">KPI 없음 (공실이거나 데이터 없음)</p>
        )}
      </section>
      <button
        type="button"
        className="primary"
        onClick={() => dispatch({ type: 'chat/compose', text: `${unit.code} ${name} 매장의 성과와 개선 방안을 알려줘` })}
      >
        에이전트에게 이 매장 질문
      </button>
    </aside>
  )
}
