// Check of the decided relayout alternative against an actual drawing the CAD team uploaded.
import { useState } from 'react'
import { useData } from '../api/source'
import { formatNumber, formatPercent, revisionMarkers } from '../lib/format'
import { CHECK_STATUS_LABELS, CHECK_STATUS_TONE, RELAYOUT_CHANGE_LABELS, brandDisplay, checkRevisionOptions, defaultCheckRevision } from '../lib/relayout'
import type { RelayoutCheck, RelayoutCheckItem, RelayoutProject } from '../types/api'
import type { RelayoutBusy } from './RelayoutAlternatives'

function Unit({ code }: { code: string }) {
  return <span className="unit-code">{code}</span>
}

/** Planned position: the target unit, or "퇴점" for an exit. */
function Planned({ item }: { item: RelayoutCheckItem }) {
  if (item.type === 'exit') return <>퇴점</>
  return item.planned_unit ? <Unit code={item.planned_unit} /> : <>–</>
}

/** Where the drawing has the brand ("도면에 없음" when absent; an exit that stayed "…에 남음"). */
function Drawn({ item }: { item: RelayoutCheckItem }) {
  if (!item.actual_unit) return <>도면에 없음</>
  return item.type === 'exit' ? (
    <>
      <Unit code={item.actual_unit} />에 남음
    </>
  ) : (
    <Unit code={item.actual_unit} />
  )
}

/** Muted line under the brand: the change type and where it came from. */
function Origin({ item }: { item: RelayoutCheckItem }) {
  const label = item.type_label ?? RELAYOUT_CHANGE_LABELS[item.type] ?? item.type
  if (!item.from_unit || item.type === 'entry') return <>{label}</>
  return (
    <>
      {label} · <span className="nowrap">{item.type === 'exit' ? item.from_unit : `${item.from_unit}에서`}</span>
    </>
  )
}

export function CheckResult({ check }: { check: RelayoutCheck }) {
  const pct = check.conformance_pct
  return (
    <div className="check-result">
      <p className="conformance">
        <span className="conformance-value">{formatPercent(pct)}</span> 일치
        <span className="muted">
          {' '}
          · {check.rev} 도면 ↔ {check.alt_id ?? '선택안'}
          {check.plan_rev ? `(${check.plan_rev})` : ''}
        </span>
      </p>
      <div className="meter" aria-hidden="true">
        <span style={{ width: `${Math.max(0, Math.min(100, pct ?? 0))}%` }} />
      </div>
      {check.summary && <p>{check.summary}</p>}

      <h4>계획 항목 {check.items.length}건</h4>
      {check.items.length ? (
        <table className="compact-table check-items">
          <caption className="visually-hidden">계획한 입점·퇴점·이전과 도면의 대조</caption>
          <thead>
            <tr>
              <th scope="col">결과</th>
              <th scope="col">브랜드</th>
              <th scope="col">계획 → 도면</th>
            </tr>
          </thead>
          <tbody>
            {check.items.map((item, i) => (
              <tr key={`${item.brand_name}-${i}`} className={item.status === 'match' ? undefined : 'mismatch'}>
                <td>
                  <span className={`badge cell-chip ${CHECK_STATUS_TONE[item.status] ?? ''}`}>
                    {item.status_label ?? CHECK_STATUS_LABELS[item.status] ?? item.status}
                  </span>
                </td>
                <td className="wrap">
                  {item.brand_name}
                  <div className="muted small">
                    <Origin item={item} />
                  </div>
                </td>
                <td className="wrap">
                  <Planned item={item} /> → <Drawn item={item} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">대조할 계획 항목이 없습니다.</p>
      )}

      <h4>계획에 없는 변경 {check.unexpected.length}건</h4>
      {check.unexpected.length ? (
        <ul className="plain-list" aria-label="계획에 없던 브랜드 변경">
          {check.unexpected.map((u) => (
            <li key={u.unit_code}>
              <span className="unit-code">{u.unit_code}</span> {brandDisplay(u.before_brand_name, u.before_brand)} →{' '}
              <strong>{brandDisplay(u.after_brand_name, u.after_brand)}</strong>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">계획에 없는 브랜드 변경이 없습니다.</p>
      )}
      <h4>면적 변경 {check.area_changes.length}건</h4>
      {check.area_changes.length ? (
        <table className="compact-table">
          <caption className="visually-hidden">기준 개정 대비 매장 면적 변경</caption>
          <thead>
            <tr>
              <th scope="col">매장</th>
              <th scope="col">이전</th>
              <th scope="col">도면</th>
              <th scope="col">변화</th>
            </tr>
          </thead>
          <tbody>
            {check.area_changes.map((a) => (
              <tr key={a.unit_code}>
                <td className="unit-code">{a.unit_code}</td>
                <td>{a.before_m2 === null ? '없음' : `${formatNumber(a.before_m2, 1)}㎡`}</td>
                <td>{a.after_m2 === null ? '없음' : `${formatNumber(a.after_m2, 1)}㎡`}</td>
                <td>{formatPercent(a.delta_pct, true)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">면적이 바뀐 매장이 없습니다.</p>
      )}
    </div>
  )
}

export function RelayoutCheckPanel({
  project,
  busy,
  error,
  onCheck,
}: {
  project: RelayoutProject
  busy: RelayoutBusy
  error: string | null
  onCheck: (rev: string) => void
}) {
  const { meta } = useData()
  const base = project.brief.base_rev
  const options = checkRevisionOptions(meta, base)
  const [choice, setChoice] = useState<string | null>(null)
  const rev = choice && options.some((r) => r.rev_id === choice) ? choice : defaultCheckRevision(meta, base)

  return (
    <section className="relayout-block" aria-labelledby="relayout-check-title">
      <h3 id="relayout-check-title">도면 대조</h3>
      {!project.selected_alt_id ? (
        <p className="muted">대안을 결정하면 CAD팀이 그려 올린 실제 개정과 대조할 수 있습니다.</p>
      ) : options.length === 0 ? (
        <p className="muted">대조할 실제 개정이 없습니다. 개정 검토 탭의 “도면·메타데이터 올리기”에서 도면을 올려 주세요.</p>
      ) : (
        <form
          className="inline-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (rev) onCheck(rev)
          }}
        >
          <label className="field stacked">
            <span>대조할 실제 개정</span>
            <select value={rev ?? ''} onChange={(e) => setChoice(e.target.value)}>
              {options.map((r) => (
                <option key={r.rev_id} value={r.rev_id}>
                  {r.rev_id}
                  {r.effective_date ? ` · ${r.effective_date}` : ''}
                  {r.parent_rev_id ? ` · 부모 ${r.parent_rev_id}` : ''}
                  {revisionMarkers(r)
                    .map((m) => ` · ${m}`)
                    .join('')}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" className="primary" disabled={busy !== null || !rev}>
            {busy === 'check' ? '대조하는 중…' : '도면 대조'}
          </button>
        </form>
      )}
      {error && (
        <p className="error-text" role="alert">
          도면을 대조하지 못했습니다: {error}
        </p>
      )}
      {project.check && <CheckResult check={project.check} />}
    </section>
  )
}
