// Construction order ("공사 순서") of a relayout alternative: conditions, busy state and the result
// (total weeks, lost sales against the plan's assumption, a week-by-week table, entries, cycles).
import { useId, useState, type FormEvent } from 'react'
import { formatKrw } from '../lib/format'
import {
  PHASING_ACTION_FULL_LABELS,
  PHASING_ACTION_LABELS,
  PHASING_ACTION_TONE,
  defaultExpandedAlternative,
  phasingCycleText,
  phasingDefaults,
  phasingStepRemark,
  phasingWeekText,
  weeksText,
  type PhasingForm,
} from '../lib/relayout'
import type { RelayoutPhasing, RelayoutPhasingRequest, RelayoutProject } from '../types/api'
import type { RelayoutBusy } from './RelayoutAlternatives'

/** Rows shown before "모두 보기". */
const STEP_ROWS = 8

const clampInput = (value: string, min: number, max: number, fallback: number) => {
  const parsed = Math.round(Number(value))
  return Number.isFinite(parsed) && value.trim() !== '' ? Math.min(max, Math.max(min, parsed)) : fallback
}

const weekNumber = (value: number | null) => (value === null ? '–' : weeksText(value).replace(/주$/, ''))

function PhasingResult({ phasing, targetAltId }: { phasing: RelayoutPhasing; targetAltId: string | null }) {
  const [open, setOpen] = useState(true)
  const [showNotes, setShowNotes] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const tableId = useId()
  const rows = showAll ? phasing.steps : phasing.steps.slice(0, STEP_ROWS)
  const stale = !!phasing.alt_id && !!targetAltId && phasing.alt_id !== targetAltId
  const delay = phasing.entry_delay_krw ?? 0
  const params = phasing.params
  const conditions = [params.crews !== null ? `동시 공사 ${params.crews}곳` : null, params.fitout_weeks !== null ? `공사 ${weeksText(params.fitout_weeks)}` : null]
    .filter(Boolean)
    .join(' · ')
  const headline = [phasing.alt_id, phasing.total_weeks !== null ? `총 ${weeksText(phasing.total_weeks)}` : null, `영업 손실 ${formatKrw(phasing.lost_krw)}`]
    .filter(Boolean)
    .join(' · ')

  return (
    <details className="disclosure-block phasing-result" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        계산 결과 <span className="muted small">{headline}</span>
      </summary>
      {open && (
        <div className="phasing-body">
          {stale && (
            <p className="notice warn">
              {phasing.alt_id} 기준으로 계산한 결과입니다. 지금 대상은 {targetAltId}이니 다시 계산해 주세요.
            </p>
          )}
          <dl className="kv phasing-kpi">
            <dt>대상 대안</dt>
            <dd>
              {phasing.alt_id ?? '–'}
              {phasing.rev_id && <span className="muted"> ({phasing.rev_id})</span>}
            </dd>
            <dt>전체 기간</dt>
            <dd>
              <strong>{weeksText(phasing.total_weeks)}</strong>
              {conditions && <span className="muted"> ({conditions})</span>}
            </dd>
            <dt>영업 손실(추정)</dt>
            <dd>
              <strong>{formatKrw(phasing.lost_krw)}</strong>
              {phasing.naive_lost_krw !== null && <span className="muted"> (모두 공사 기간만큼 휴점하면 {formatKrw(phasing.naive_lost_krw)})</span>}
            </dd>
            {delay > 0 && (
              <>
                <dt>입점 지연 기회 손실</dt>
                <dd>
                  {formatKrw(delay)} <span className="muted">(공사팀이 모자라 늦게 여는 기간)</span>
                </dd>
              </>
            )}
          </dl>
          {phasing.summary && <p className="small phasing-summary">{phasing.summary}</p>}

          {phasing.steps.length ? (
            <>
              <div className="block-head phasing-table-head">
                <h4>주차별 순서 {phasing.steps.length}단계</h4>
                <button type="button" className="text-button" aria-pressed={showNotes} onClick={() => setShowNotes((value) => !value)}>
                  작업 설명 보기
                </button>
              </div>
              <div className="table-wrap">
                <table id={tableId} className="compact-table phasing-table">
                  <caption className="visually-hidden">주차별 공사 순서 (0주차가 공사 시작)</caption>
                  <thead>
                    <tr>
                      <th scope="col">주차</th>
                      <th scope="col">매장</th>
                      <th scope="col">작업</th>
                      <th scope="col">브랜드/비고</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((step) => {
                      const full = step.action_label ?? PHASING_ACTION_FULL_LABELS[step.action] ?? step.action
                      const remark = phasingStepRemark(step)
                      return (
                        <tr key={step.step}>
                          <td>{phasingWeekText(step)}</td>
                          <td className="unit-code">{step.unit_code ?? '–'}</td>
                          <td>
                            <span className={`chip ${PHASING_ACTION_TONE[step.action] ?? ''}`} title={full}>
                              {PHASING_ACTION_LABELS[step.action] ?? full}
                            </span>
                          </td>
                          <td className="wrap">
                            <span className="muted">{step.step}. </span>
                            {step.brand_name}
                            {remark && <div className="muted small">{remark}</div>}
                            {showNotes && step.note && <div className="small phasing-note">{step.note}</div>}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {phasing.steps.length > STEP_ROWS && (
                <button type="button" className="text-button" aria-expanded={showAll} aria-controls={tableId} onClick={() => setShowAll((all) => !all)}>
                  {showAll ? `처음 ${STEP_ROWS}단계만 보기` : `모두 보기 (${phasing.steps.length})`}
                </button>
              )}
            </>
          ) : (
            <p className="muted">공사할 매장이 없습니다.</p>
          )}

          {phasing.entries.length > 0 && (
            <>
              <h4>신규 입점</h4>
              <ul className="plain-list">
                {phasing.entries.map((entry, i) => {
                  const late = entry.open_week !== null && entry.earliest_week !== null && entry.open_week > entry.earliest_week
                  return (
                    <li key={`${entry.brand_id ?? entry.brand_name}-${i}`}>
                      <strong>{entry.brand_name}</strong>
                      {entry.unit_code && <span className="unit-code"> {entry.unit_code}</span>} · {weekNumber(entry.open_week)}주차 개점
                      {late && <span className="warn-text"> (가장 빠르면 {weekNumber(entry.earliest_week)}주차)</span>}
                    </li>
                  )
                })}
              </ul>
            </>
          )}

          {phasing.cycles.length > 0 && (
            <div className="notice warn">
              <strong>서로 자리를 바꾸는 이전 {phasing.cycles.length}건</strong>
              <ul className="plain-list">
                {phasing.cycles.map((cycle, i) => (
                  <li key={i}>{phasingCycleText(cycle)}</li>
                ))}
              </ul>
            </div>
          )}

          {phasing.caveats.length > 0 && (
            <ul className="notes" aria-label="공사 순서의 가정과 한계">
              {phasing.caveats.map((caveat) => (
                <li key={caveat}>{caveat}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </details>
  )
}

export function RelayoutPhasingPanel({
  project,
  busy,
  error,
  onPhase,
}: {
  project: RelayoutProject
  busy: RelayoutBusy
  error: string | null
  onPhase: (body: RelayoutPhasingRequest) => Promise<boolean>
}) {
  const [form, setForm] = useState<PhasingForm | null>(null)
  const [runs, setRuns] = useState(0)
  const plan = project.plan
  if (!plan) {
    return (
      <section className="relayout-block" aria-labelledby="relayout-phasing-title">
        <h3 id="relayout-phasing-title">공사 순서</h3>
        <p className="muted">대안을 만들면 퇴점·이전·입점 공사의 순서와 휴점 손실을 계산할 수 있습니다.</p>
      </section>
    )
  }
  const values = form ?? phasingDefaults(project)
  const targetAltId = defaultExpandedAlternative(plan, project.selected_alt_id)
  const phasing = project.phasing
  const update = (patch: Partial<PhasingForm>) => setForm({ ...values, ...patch })
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy !== null) return
    if (await onPhase({ crews: values.crews, fitout_weeks: values.fitoutWeeks, use_temporary: values.useTemporary })) setRuns((n) => n + 1)
  }

  return (
    <section className="relayout-block" aria-labelledby="relayout-phasing-title" aria-busy={busy === 'phasing'}>
      <h3 id="relayout-phasing-title">공사 순서</h3>
      <p className="muted small">
        {project.selected_alt_id
          ? `결정한 대안 ${project.selected_alt_id}의 공사 순서를 영업 손실이 가장 적도록 정합니다.`
          : `아직 결정한 대안이 없어 순효과가 가장 큰 ${targetAltId ?? '대안'}의 공사 순서를 정합니다.`}
      </p>
      <form className="phasing-form" onSubmit={(event) => void submit(event)}>
        <details className="phasing-options">
          <summary>
            계산 조건{' '}
            <span className="muted small">
              동시 공사 {values.crews}곳 · 공사 {values.fitoutWeeks}주 · 임시 매장 {values.useTemporary ? '사용' : '안 씀'}
            </span>
          </summary>
          <div className="form-grid">
            <label className="field stacked">
              <span>동시 공사 수</span>
              <input
                type="number"
                name="phasing-crews"
                min={1}
                max={6}
                step={1}
                value={values.crews}
                onChange={(e) => update({ crews: clampInput(e.target.value, 1, 6, values.crews) })}
              />
            </label>
            <label className="field stacked">
              <span>공사 기간(주)</span>
              <input
                type="number"
                name="phasing-fitout-weeks"
                min={1}
                max={12}
                step={1}
                value={values.fitoutWeeks}
                onChange={(e) => update({ fitoutWeeks: clampInput(e.target.value, 1, 12, values.fitoutWeeks) })}
              />
            </label>
          </div>
          <label className="toggle">
            <input type="checkbox" name="phasing-use-temporary" checked={values.useTemporary} onChange={(e) => update({ useTemporary: e.target.checked })} />
            임시 매장 사용
          </label>
          <p className="hint">서로 자리를 바꾸는 이전이 있으면 비어 있는 매장을 임시 매장으로 씁니다. 끄면 그 브랜드가 휴점합니다.</p>
        </details>
        <button type="submit" className="primary" disabled={busy !== null}>
          {busy === 'phasing' ? '계산하는 중…' : phasing ? '공사 순서 다시 계산' : '공사 순서 계산'}
        </button>
      </form>
      {error && (
        <p className="error-text" role="alert">
          공사 순서를 계산하지 못했습니다: {error}
        </p>
      )}
      {phasing ? (
        <PhasingResult key={`${phasing.alt_id ?? ''}-${runs}`} phasing={phasing} targetAltId={targetAltId} />
      ) : (
        <p className="muted small">계산하면 동시 공사 수에 맞춰 퇴점·이전·입점 공사를 주 단위로 늘어놓고, 휴점 손실을 추정합니다.</p>
      )}
    </section>
  )
}
