// Plan alternatives of a seasonal relayout: compact cards (KPI first, details on demand), map preview
// and the human decision.
import { useId, useState } from 'react'
import { useData } from '../api/source'
import { formatDateTime, formatKrw } from '../lib/format'
import {
  OBJECTIVE_LABELS,
  defaultExpandedAlternative,
  groupRelayoutChanges,
  isBaselineAlternative,
  relayoutPreview,
} from '../lib/relayout'
import { replaceOp } from '../state/reducer'
import { usePatchUi } from '../state/store'
import type { RelayoutAlternative, RelayoutChange, RelayoutProject } from '../types/api'

export type RelayoutBusy = 'diagnose' | 'plan' | 'select' | 'check' | null

function Unit({ code }: { code: string | null }) {
  return <span className="unit-code">{code ?? '–'}</span>
}

/** One line per change: floor chip, brand, unit (from → to for moves), category (explanations are behind "근거 보기"). */
function ChangeLine({ change }: { change: RelayoutChange }) {
  const crossFloor = change.type === 'move' && change.from_floor && change.to_floor && change.from_floor !== change.to_floor
  const floor = crossFloor ? `${change.from_floor}→${change.to_floor}` : (change.to_floor ?? change.from_floor)
  return (
    <li className="change-line">
      {floor && <span className="floor-chip">{floor}</span>}
      <strong>{change.brand_name}</strong>{' '}
      {change.type === 'move' ? (
        <>
          <Unit code={change.from_unit} /> → <Unit code={change.to_unit} />
        </>
      ) : (
        <Unit code={change.type === 'exit' ? change.from_unit : (change.to_unit ?? change.from_unit)} />
      )}
      {change.category_name && <span className="muted"> · {change.category_name}</span>}
    </li>
  )
}

function ConstraintList({ id, items, label }: { id?: string; items: RelayoutAlternative['constraints']; label: string }) {
  return (
    <ul id={id} className="constraint-list" aria-label={label}>
      {items.map((c) => (
        <li key={c.name} className={c.ok ? 'ok' : 'fail'}>
          <span aria-hidden="true">{c.ok ? '✔' : '✖'}</span> {c.name} <span className="visually-hidden">{c.ok ? '충족' : '위반'}</span>
          {c.detail && <span className="muted"> ({c.detail})</span>}
        </li>
      ))}
    </ul>
  )
}

/** "✔ 제약 9개 모두 충족", or the violations only; the full list on demand. */
function ConstraintSummary({ alt }: { alt: RelayoutAlternative }) {
  const [showAll, setShowAll] = useState(false)
  const listId = useId()
  if (!alt.constraints.length) return null
  const failed = alt.constraints.filter((c) => !c.ok)
  return (
    <div className="constraint-summary">
      {failed.length === 0 ? (
        <p className="constraint-line ok">
          <span aria-hidden="true">✔ </span>제약 {alt.constraints.length}개 모두 충족
        </p>
      ) : (
        <>
          <p className="constraint-line fail">
            <span aria-hidden="true">✖ </span>제약 위반 {failed.length}개
          </p>
          <ConstraintList items={failed} label={`${alt.alt_id} 제약 위반`} />
        </>
      )}
      <button
        type="button"
        className="text-button disclosure-toggle"
        aria-expanded={showAll}
        aria-controls={showAll ? listId : undefined}
        aria-label={`${alt.alt_id} 제약 전체 보기`}
        onClick={() => setShowAll((open) => !open)}
      >
        제약 전체 보기
      </button>
      {showAll && <ConstraintList id={listId} items={alt.constraints} label={`${alt.alt_id} 제약 조건`} />}
    </div>
  )
}

/** Per-change explanations and the alternative's extra notes ("참고"), behind one toggle. */
function Rationale({ alt }: { alt: RelayoutAlternative }) {
  const [open, setOpen] = useState(false)
  const regionId = useId()
  const explained = alt.changes.filter((c) => c.explanation)
  if (!explained.length && !alt.explanations.length) return null
  return (
    <div className="rationale">
      <button
        type="button"
        className="text-button disclosure-toggle"
        aria-expanded={open}
        aria-controls={open ? regionId : undefined}
        aria-label={`${alt.alt_id} 근거 보기`}
        onClick={() => setOpen((value) => !value)}
      >
        근거 보기
      </button>
      {open && (
        <div id={regionId} className="rationale-body">
          {explained.length > 0 && (
            <ul className="explanations">
              {explained.map((c, i) => (
                <li key={`${c.type}-${c.brand_id}-${i}`}>{c.explanation}</li>
              ))}
            </ul>
          )}
          {alt.explanations.length > 0 && (
            <>
              <h5>참고</h5>
              <ul className="explanations">
                {alt.explanations.map((text, i) => (
                  <li key={i}>{text}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  )
}

interface CardProps {
  alt: RelayoutAlternative
  project: RelayoutProject
  busy: RelayoutBusy
  expanded: boolean
  previewing: boolean
  onToggle: () => void
  onPreview: (alt: RelayoutAlternative) => void
  onSelect: (altId: string, decidedBy: string) => Promise<boolean>
}

function AlternativeCard({ alt, project, busy, expanded, previewing, onToggle, onPreview, onSelect }: CardProps) {
  const [deciding, setDeciding] = useState(false)
  const [decidedBy, setDecidedBy] = useState('MD팀')
  const baseline = isBaselineAlternative(alt)
  const selected = project.selected_alt_id === alt.alt_id
  const failed = alt.constraints.filter((c) => !c.ok)
  const objective = OBJECTIVE_LABELS[String(project.plan?.params.objective ?? project.brief.objective)] ?? '목표'
  const titleId = `relayout-alt-${alt.alt_id}`
  const detailsId = `${titleId}-details`
  const formId = `${titleId}-decision`
  const k = alt.kpi
  // A range is only worth showing when the estimate is uncertain (ALT-0 may send 0 ~ 0).
  const hasRange = k.sales_low_krw !== null && k.sales_high_krw !== null && k.sales_low_krw !== k.sales_high_krw

  return (
    <article className={`alt-card relayout-alt${selected ? ' selected' : ''}${previewing ? ' active' : ''}`} aria-labelledby={titleId}>
      <header>
        <h4 id={titleId}>
          {alt.alt_id}
          {baseline && <span className="alt-kind"> 기준안</span>}
        </h4>
        {selected && <span className="badge ok">결정됨</span>}
        <span className={`badge ${failed.length ? 'danger' : 'ok'}`}>{failed.length ? `제약 위반 ${failed.length}` : '제약 충족'}</span>
      </header>
      {alt.summary && <p className="alt-summary">{alt.summary}</p>}
      <dl className="kv kpi">
        <dt>순효과</dt>
        <dd>
          <strong className={k.net_krw !== null && k.net_krw < 0 ? 'negative' : undefined}>{formatKrw(k.net_krw, true)}</strong>
        </dd>
        <dt>목표 변화</dt>
        <dd>
          {formatKrw(k.objective_delta_krw, true)} <span className="muted">({objective})</span>
        </dd>
        <dt>이전 비용</dt>
        <dd>
          {formatKrw(k.move_cost_krw)} <span className="muted">(이전 {k.moves ?? 0}건)</span>
        </dd>
        <dt>기준안 대비 매출 변화</dt>
        <dd>
          {formatKrw(k.sales_delta_krw, true)}
          {hasRange && (
            <span className="muted">
              {' '}
              (범위 {formatKrw(k.sales_low_krw, true)} ~ {formatKrw(k.sales_high_krw, true)})
            </span>
          )}
        </dd>
        <dt>지금 대비 매출 변화</dt>
        <dd>{formatKrw(k.vs_current_sales_krw, true)}</dd>
        {k.vs_current_objective_krw !== null && (
          <>
            <dt>지금 대비 목표 변화</dt>
            <dd>{formatKrw(k.vs_current_objective_krw, true)}</dd>
          </>
        )}
      </dl>
      <button
        type="button"
        className="text-button disclosure-toggle"
        aria-expanded={expanded}
        aria-controls={expanded ? detailsId : undefined}
        aria-label={`${alt.alt_id} 자세히`}
        onClick={onToggle}
      >
        자세히
      </button>
      {expanded && (
        <div id={detailsId} className="alt-details">
          {baseline && <p className="muted small">기준안은 퇴점·입점만 반영합니다. 다른 대안의 KPI는 이 기준안 대비 변화입니다.</p>}
          <div className="change-groups">
            {groupRelayoutChanges(alt.changes).map((group) => (
              <div key={group.type} className="change-block">
                <h5>
                  <span className={`chip change-${group.type}`}>{group.label}</span> {group.items.length}건
                </h5>
                <ul className="plain-list">
                  {group.items.map((change, i) => (
                    <ChangeLine key={`${group.type}-${i}`} change={change} />
                  ))}
                </ul>
              </div>
            ))}
            {alt.changes.length === 0 && <p className="muted">바뀌는 매장이 없습니다.</p>}
          </div>
          <ConstraintSummary alt={alt} />
          <Rationale alt={alt} />
          <div className="card-actions">
            <button
              type="button"
              aria-pressed={previewing}
              aria-label={`${alt.alt_id} 도면에서 보기`}
              disabled={!alt.rev_id}
              title={alt.rev_id ? `${alt.rev_id}를 ${project.brief.base_rev}와 비교해 봅니다.` : '저장된 가상 개정이 없어 도면으로 볼 수 없습니다.'}
              onClick={() => onPreview(alt)}
            >
              도면에서 보기
            </button>
            {!selected && (
              <button
                type="button"
                aria-label={`${alt.alt_id} 이 대안으로 결정`}
                aria-expanded={deciding}
                aria-controls={deciding ? formId : undefined}
                disabled={busy !== null}
                onClick={() => setDeciding((open) => !open)}
              >
                이 대안으로 결정
              </button>
            )}
          </div>
          {deciding && !selected && (
            <form
              id={formId}
              className="decision-form"
              aria-label={`${alt.alt_id} 결정 기록`}
              onSubmit={async (event) => {
                event.preventDefault()
                if (await onSelect(alt.alt_id, decidedBy.trim())) setDeciding(false)
              }}
            >
              <label className="field stacked">
                <span>결정자</span>
                <input type="text" value={decidedBy} required autoFocus onChange={(e) => setDecidedBy(e.target.value)} />
              </label>
              <div className="form-actions">
                <button type="submit" className="primary" disabled={busy !== null || !decidedBy.trim()}>
                  {busy === 'select' ? '기록하는 중…' : '결정 기록'}
                </button>
                <button type="button" onClick={() => setDeciding(false)}>
                  취소
                </button>
              </div>
            </form>
          )}
        </div>
      )}
    </article>
  )
}

export function RelayoutAlternatives({
  project,
  busy,
  error,
  onSelect,
}: {
  project: RelayoutProject
  busy: RelayoutBusy
  error: string | null
  onSelect: (altId: string, decidedBy: string) => Promise<boolean>
}) {
  const { meta } = useData()
  const patchUi = usePatchUi()
  const [previewId, setPreviewId] = useState<string | null>(null)
  const plan = project.plan
  // Open the decided card (or the best one); reset when that default changes (new plan or decision).
  const defaultId = defaultExpandedAlternative(plan, project.selected_alt_id)
  const [expanded, setExpanded] = useState<{ base: string | null; ids: ReadonlySet<string> }>(() => ({
    base: defaultId,
    ids: new Set(defaultId ? [defaultId] : []),
  }))
  let openIds = expanded.ids
  if (expanded.base !== defaultId) {
    openIds = new Set(defaultId ? [defaultId] : [])
    setExpanded({ base: defaultId, ids: openIds })
  }
  if (!plan) return null
  const selected = plan.alternatives.find((a) => a.alt_id === project.selected_alt_id)

  const toggle = (altId: string) =>
    setExpanded((prev) => {
      const ids = new Set(prev.ids)
      if (ids.has(altId)) ids.delete(altId)
      else ids.add(altId)
      return { base: prev.base, ids }
    })

  const preview = (alt: RelayoutAlternative) => {
    const view = relayoutPreview(
      alt,
      project.brief.base_rev,
      meta.floors.map((f) => f.code),
    )
    if (!view.rev) return
    patchUi([
      replaceOp('/rev', view.rev),
      replaceOp('/compareRev', view.compareRev),
      ...(view.floor ? [replaceOp('/floor', view.floor)] : []),
      replaceOp('/heatmap', null),
      replaceOp('/journey', null),
      replaceOp('/route', null),
      replaceOp('/highlights', view.highlights),
      replaceOp('/edges', view.edges),
      replaceOp('/focus', view.codes.length ? { unit_codes: view.codes } : null),
    ])
    setPreviewId(alt.alt_id)
  }

  return (
    <section className="relayout-block" aria-labelledby="relayout-alts-title">
      <h3 id="relayout-alts-title">대안 비교</h3>
      <p className="muted">
        지금 매출 {formatKrw(plan.current.sales_krw)} · 기준안 {formatKrw(plan.baseline.sales_krw)}
        {plan.baseline.note ? `(${plan.baseline.note})` : ''}. 금액은 연간 추정치입니다.
      </p>
      {project.selected_alt_id && (
        <div className="decision-box">
          <strong>
            결정한 대안: {project.selected_alt_id}
            {selected?.rev_id ? ` (${selected.rev_id})` : ''}
          </strong>
          <span className="muted">
            결정자 {project.decided_by ?? '–'} · {formatDateTime(project.decided_at)}
          </span>
        </div>
      )}
      {error && (
        <p className="error-text" role="alert">
          대안을 결정하지 못했습니다: {error}
        </p>
      )}
      {plan.unplaced.length > 0 && (
        <div className="notice warn">
          <strong>자리를 찾지 못한 입점 브랜드 {plan.unplaced.length}개</strong>
          <ul className="plain-list">
            {plan.unplaced.map((u) => (
              <li key={u.brand_id}>
                {u.brand_name}: {u.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {plan.alternatives.length === 0 && <p className="muted">조건을 만족하는 대안이 없습니다. 최대 이동 수나 이동 대상을 바꿔 보세요.</p>}
      {plan.alternatives.map((alt) => (
        <AlternativeCard
          key={alt.alt_id}
          alt={alt}
          project={project}
          busy={busy}
          expanded={openIds.has(alt.alt_id)}
          previewing={previewId === alt.alt_id}
          onToggle={() => toggle(alt.alt_id)}
          onPreview={preview}
          onSelect={onSelect}
        />
      ))}
      {plan.caveats.length > 0 && (
        <ul className="notes" aria-label="가정과 한계">
          {plan.caveats.map((caveat) => (
            <li key={caveat}>{caveat}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
