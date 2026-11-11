// One drawing upload job: step progress, parse/review queue (layer mapping), changes, QA, relayout check.
import { useId, useState } from 'react'
import { errorText } from '../api/client'
import { useData } from '../api/source'
import { CHANGE_LABELS } from '../lib/colors'
import { formatDateTime, formatPercent, RISK_LABELS, ARTIFACT_STATUS_LABELS } from '../lib/format'
import {
  JOB_STATUS_LABELS,
  JOB_STATUS_TONE,
  LAYER_SEMANTICS,
  LAYER_SEMANTIC_LABELS,
  QA_CODE_LABELS,
  SEVERITY_LABELS,
  SEVERITY_TONE,
  STEP_LABELS,
  STEP_STATUS_LABELS,
  jobRevisionAvailable,
} from '../lib/uploads'
import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import type { LayerReviewItem, UploadJob } from '../types/api'

const STEP_ICONS: Record<string, string> = { done: '✔', running: '●', failed: '✖', skipped: '–', pending: '○' }

const itemKey = (item: LayerReviewItem) => `${item.floor ?? ''}|${item.layer}`

/** Short text for the job's aria-live line. */
export function jobLiveText(job: UploadJob): string {
  const name = job.rev_id ?? job.job_id
  if (job.status === 'queued') return `${name} 도면 처리를 기다리는 중입니다.`
  if (job.status === 'running') {
    const index = job.steps.findIndex((s) => s.status === 'running')
    const step = job.steps[index]
    return step ? `${name} 처리 중: ${step.label} (${index + 1}/${job.steps.length}단계)` : `${name} 처리 중입니다.`
  }
  if (job.status === 'done') return `${name} 처리를 마쳤습니다.`
  if (job.status === 'partial') return `${name} 모델은 저장했지만 일부 단계가 실패했습니다.`
  if (job.status === 'needs_review') return `${name} 처리를 마쳤습니다. 레이어 의미를 확인해 주세요.`
  if (job.status === 'failed') return `${name} 처리에 실패했습니다.`
  return `${name}: ${JOB_STATUS_LABELS[job.status] ?? job.status}`
}

function ReviewQueue({ job, onResumed }: { job: UploadJob; onResumed: (job: UploadJob) => void }) {
  const { source } = useData()
  const queue = job.parse?.review_queue ?? []
  const [choices, setChoices] = useState<Record<string, string>>(() => Object.fromEntries(queue.map((q) => [itemKey(q), q.semantic ?? ''])))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    const mappings = queue.map((q) => ({ layer: q.layer, semantic: choices[itemKey(q)] ?? '', ...(q.floor ? { floor: q.floor } : {}) }))
    if (mappings.some((m) => !m.semantic)) {
      setError('모든 레이어의 의미를 골라 주세요.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      onResumed(await source.submitLayerMapping(job.job_id, mappings))
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="review-queue">
      <h5>확인할 레이어 {queue.length}개</h5>
      <p className="muted small">의미를 추정해 모델을 만들었습니다. 맞는 의미를 고르고 다시 처리하면 매핑이 저장됩니다.</p>
      <div className="table-wrap">
        <table>
          <caption className="visually-hidden">의미를 확인할 레이어</caption>
          <thead>
            <tr>
              <th scope="col">레이어</th>
              <th scope="col">추정</th>
              <th scope="col">질문·예시</th>
              <th scope="col">의미</th>
            </tr>
          </thead>
          <tbody>
            {queue.map((q) => (
              <tr key={itemKey(q)}>
                <td>
                  <code>{q.layer}</code>
                  {q.floor && <div className="muted small">{q.floor}</div>}
                </td>
                <td>
                  {q.semantic ? (LAYER_SEMANTIC_LABELS[q.semantic] ?? q.semantic) : '–'}
                  {q.confidence !== null && <div className="muted small">확신 {formatPercent(q.confidence * 100, false, 0)}</div>}
                  {q.alternatives.length > 0 && (
                    <div className="muted small">후보 {q.alternatives.map((a) => LAYER_SEMANTIC_LABELS[a] ?? a).join(', ')}</div>
                  )}
                </td>
                <td>
                  {q.question ?? '–'}
                  {q.samples.length > 0 && <div className="muted small">예: {q.samples.join(', ')}</div>}
                </td>
                <td>
                  <select
                    aria-label={`${q.layer}${q.floor ? ` (${q.floor})` : ''} 레이어 의미`}
                    value={choices[itemKey(q)] ?? ''}
                    onChange={(e) => setChoices((prev) => ({ ...prev, [itemKey(q)]: e.target.value }))}
                  >
                    <option value="">고르세요</option>
                    {LAYER_SEMANTICS.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error && (
        <p className="error-text" role="alert">
          매핑을 저장하지 못했습니다: {error}
        </p>
      )}
      <button type="button" className="primary" disabled={busy} onClick={() => void submit()}>
        {busy ? '저장하는 중…' : '매핑 저장 후 다시 처리'}
      </button>
    </div>
  )
}

type QaIssue = NonNullable<UploadJob['qa']>['issues'][number]

function QaTable({ id, issues, caption }: { id?: string; issues: QaIssue[]; caption: string }) {
  return (
    <table id={id} className="compact-table qa-table">
      <caption className="visually-hidden">{caption}</caption>
      <thead>
        <tr>
          <th scope="col">유형</th>
          <th scope="col">매장</th>
          <th scope="col">내용</th>
        </tr>
      </thead>
      <tbody>
        {issues.map((issue, i) => (
          <tr key={`${issue.code}-${issue.unit_code ?? ''}-${i}`}>
            <td>
              <span className={`badge cell-chip ${SEVERITY_TONE[issue.severity] ?? ''}`} title={issue.code}>
                {SEVERITY_LABELS[issue.severity] ?? issue.severity} · {QA_CODE_LABELS[issue.code] ?? issue.code}
              </span>
            </td>
            <td className="unit-code">{issue.unit_code ?? '–'}</td>
            <td className="wrap">{issue.message}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Lease QA: errors and warnings listed, infos ("참고") behind a toggle. */
function QaSection({ qa }: { qa: NonNullable<UploadJob['qa']> }) {
  const [showInfos, setShowInfos] = useState(false)
  const infosId = useId()
  const main = qa.issues.filter((i) => i.severity !== 'info')
  const infos = qa.issues.filter((i) => i.severity === 'info')
  const total = Math.max(qa.errors + qa.warnings + qa.infos, qa.issues.length)
  return (
    <div className="qa-block">
      <h5>
        계약 정보 대조 · 오류 {qa.errors} · 경고 {qa.warnings} · 참고 {qa.infos}
      </h5>
      {main.length > 0 ? (
        <QaTable issues={main} caption="계약 정보와 다른 점" />
      ) : (
        <p className="muted">{qa.errors + qa.warnings > 0 ? '오류·경고 목록을 받지 못했습니다.' : '계약 정보와 다른 오류·경고가 없습니다.'}</p>
      )}
      {infos.length > 0 && (
        <>
          <button
            type="button"
            className="text-button disclosure-toggle"
            aria-expanded={showInfos}
            aria-controls={showInfos ? infosId : undefined}
            onClick={() => setShowInfos((open) => !open)}
          >
            참고 {infos.length}건 보기
          </button>
          {showInfos && <QaTable id={infosId} issues={infos} caption="참고 사항" />}
        </>
      )}
      {total > qa.issues.length && (
        <p className="muted small">
          전체 {total}건 중 {qa.issues.length}건만 받았습니다.
        </p>
      )}
    </div>
  )
}

export function UploadJobDetail({
  job,
  error,
  onResumed,
  onOpenJob,
}: {
  job: UploadJob
  error: string | null
  onResumed: (job: UploadJob) => void
  /** Open another job (the one that replaced this job's model). */
  onOpenJob?: (jobId: string) => void
}) {
  const { ui } = useAppState()
  const patchUi = usePatchUi()
  // The revision can be opened when this job's model is the one saved (a later upload of the same id replaces it).
  const available = jobRevisionAvailable(job)
  const viewing = ui.rev === job.rev_id
  const stageErrors = job.stage_errors
  const check = job.relayout_check

  const viewRevision = () => {
    if (!job.rev_id) return
    const ops = [replaceOp('/rev', job.rev_id)]
    if (ui.compareRev === job.rev_id) ops.push(replaceOp('/compareRev', job.parent_rev ?? null))
    patchUi(ops)
  }

  return (
    <section className="job-detail" aria-labelledby="upload-job-title">
      <header className="block-head">
        <h4 id="upload-job-title">
          {job.rev_id ?? '–'} <span className="muted small">{job.job_id}</span>
        </h4>
        <span className={`badge ${JOB_STATUS_TONE[job.status] ?? ''}`}>{JOB_STATUS_LABELS[job.status] ?? job.status}</span>
        {job.superseded_by && <span className="badge">대체됨</span>}
      </header>
      <p className="job-live" role="status">
        {jobLiveText(job)}
      </p>
      <dl className="kv">
        <dt>부모 개정</dt>
        <dd>{job.parent_rev ?? '–'}</dd>
        <dt>적용일</dt>
        <dd>{job.effective_date ?? '–'}</dd>
        <dt>프로필</dt>
        <dd>{job.profile ?? '–'}</dd>
        <dt>올린 시각</dt>
        <dd>{formatDateTime(job.created_at)}</dd>
        {job.files.length > 0 && (
          <>
            <dt>파일</dt>
            <dd>{job.files.map((f) => f.name).join(', ')}</dd>
          </>
        )}
      </dl>

      <ol className="step-progress" aria-label="처리 단계">
        {job.steps.map((step) => (
          <li key={step.name} className={`step ${step.status}`}>
            <span className="step-mark" aria-hidden="true">
              {STEP_ICONS[step.status] ?? '○'}
            </span>
            <span className="step-name">{step.label}</span>
            <span className="step-state">{STEP_STATUS_LABELS[step.status] ?? step.status}</span>
            {step.message && <span className="step-message muted">{step.message}</span>}
          </li>
        ))}
      </ol>

      {job.error && (
        <p className="error-text" role="alert">
          {job.error}
        </p>
      )}
      {error && <p className="error-text">작업 상태를 불러오지 못했습니다: {error}</p>}

      {(job.status === 'partial' || stageErrors.length > 0) && (
        <div className="notice warn job-stage-errors">
          <strong>{job.status === 'partial' ? '일부 단계가 실패했어요' : '실패한 단계가 있어요'}</strong>
          {job.status === 'partial' && (
            <p>도면 모델은 저장했어요. 이 개정은 열 수 있지만, 실패한 단계의 결과(변경 비교, 계약 정보 대조, 영향 분석, 산출물, 개편안 대조)는 비어 있거나 불완전할 수 있어요.</p>
          )}
          {stageErrors.length > 0 && (
            <ul className="issue-list" aria-label="실패한 단계">
              {stageErrors.map((e, i) => (
                <li key={`${e.step}-${i}`} className="issue warning">
                  <strong>{STEP_LABELS[e.step] ?? (e.step || '단계')}</strong> {e.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {job.superseded_by && (
        <div className="notice warn job-superseded" role="status">
          <strong>이 작업의 모델은 더 나중 작업이 대체했어요</strong>
          <p>
            같은 개정 ID({job.rev_id ?? '–'})를 {job.superseded_by} 작업이 다시 올렸어요. 지금 저장된 모델은 그 작업의 것이에요.
          </p>
          {onOpenJob && (
            <button type="button" onClick={() => onOpenJob(job.superseded_by!)}>
              {job.superseded_by} 작업 보기
            </button>
          )}
        </div>
      )}

      {(job.floors_uploaded.length > 0 || job.floors_inherited.length > 0) && (
        <p>
          올린 층 <strong>{job.floors_uploaded.join(', ') || '–'}</strong>
          {job.floors_inherited.length > 0 && <span className="muted"> · 부모 개정에서 가져온 층 {job.floors_inherited.join(', ')}</span>}
        </p>
      )}
      {job.parse && (
        <p>
          도면 읽기: 매장 {job.parse.units ?? '–'}개 · 오류 {job.parse.issues.error} · 경고 {job.parse.issues.warning} · 참고 {job.parse.issues.info}
        </p>
      )}
      {job.parse && job.parse.review_queue.length > 0 && isSettledForMapping(job) && (
        <ReviewQueue key={`${job.job_id}-${job.updated_at}`} job={job} onResumed={onResumed} />
      )}
      {job.review_reason === 'layer_review_queue' && !(job.parse && job.parse.review_queue.length > 0) && (
        <p className="muted">레이어의 의미를 추정해 모델을 만들었어요. 의미를 확인해야 이 개정을 믿고 쓸 수 있어요.</p>
      )}

      {job.changes && (
        <div>
          <h5>변경 요약 {job.changes.total}건</h5>
          {job.changes.total > 0 && (
            <ul className="chip-list" aria-label="변경 유형별 건수">
              {Object.entries(job.changes.by_type).map(([type, count]) => (
                <li key={type} className="chip">
                  {CHANGE_LABELS[type] ?? type} {count}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {job.impact.length > 0 && (
        <p>
          영향 산출물: {job.impact.map((i) => `${i.title_ko}(${RISK_LABELS[i.risk_tier] ?? i.risk_tier})`).join(', ')}
        </p>
      )}
      {job.artifacts.length > 0 && (
        <p className="muted">
          산출물:{' '}
          {job.artifacts
            .map((a) => `${job.impact.find((i) => i.generator === a.generator)?.title_ko ?? a.generator} ${ARTIFACT_STATUS_LABELS[a.status] ?? a.status}`)
            .join(', ')}
        </p>
      )}

      {job.qa && <QaSection qa={job.qa} />}

      {check && check.skipped && (
        <p className="muted job-check-skipped">개편안 대조는 건너뛰었어요{check.reason ? `: ${check.reason}` : ''} (실패가 아니에요)</p>
      )}
      {check && !check.skipped && (
        <div className="notice">
          <strong>
            개편안 대조: {check.brief_id} {check.alt_id ?? ''} · 일치율 {formatPercent(check.conformance_pct)}
          </strong>
          {check.summary && <p>{check.summary}</p>}
          <button type="button" onClick={() => patchUi([replaceOp('/panel', { type: 'layout', payload: { brief_id: check.brief_id } })])}>
            시즌 개편에서 보기
          </button>
        </div>
      )}

      {available && job.rev_id && (
        <div className="card-actions">
          <button type="button" className="primary" disabled={viewing} onClick={viewRevision}>
            {viewing ? `${job.rev_id} 보는 중` : '이 개정 보기'}
          </button>
          {job.status === 'partial' && <span className="muted small">일부 단계가 실패해 변경 목록·산출물이 불완전할 수 있어요.</span>}
        </div>
      )}
    </section>
  )
}

/** Layer mapping can be changed once processing stopped with a saved model. */
function isSettledForMapping(job: UploadJob): boolean {
  return job.status === 'needs_review' || job.status === 'done' || job.status === 'partial'
}
