import { useEffect, useMemo, useRef, useState } from 'react'
import { cacheKeys } from '../api/keys'
import { useData } from '../api/source'
import { invalidateCache, useCachedResource } from '../hooks/useCachedResource'
import { DEFAULT_REVIEW_WINDOW, REVIEW_WINDOW_OPTIONS, canReviewRelayout } from '../lib/benchmark'
import { CHANGE_LABELS, CHANGE_ORDER, changeColor } from '../lib/colors'
import { APPROVAL_MODE_LABELS, ARTIFACT_STATUS_LABELS, RISK_LABELS, summarizeValue } from '../lib/format'
import { floorOfUnitCode } from '../lib/geometry'
import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { panelPayloadString, parentRevision } from '../state/uiState'
import type { Artifact, ChangeItem } from '../types/api'
import { UploadArea, type UploadRequest } from './UploadArea'

const STATUS_TONE: Record<string, string> = {
  awaiting_approval: 'warn',
  published: 'ok',
  rejected: 'danger',
  qa_failed: 'danger',
}

function ArtifactCard({
  artifact,
  rev,
  reviewer,
  focused,
  onReviewed,
}: {
  artifact: Artifact
  rev: string
  reviewer: string
  focused: boolean
  onReviewed: (artifact: Artifact) => void
}) {
  const { source } = useData()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLElement>(null)
  const canReview = artifact.status === 'awaiting_approval'

  useEffect(() => {
    if (focused) ref.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  }, [focused])

  const review = async (decision: 'approve' | 'reject') => {
    setBusy(true)
    setError(null)
    try {
      const updated = await source.reviewArtifact(rev, artifact.generator, { decision, reviewer: reviewer.trim() || '검토자', note })
      // The response may be the raw artifact record (no title/urls, different generator id): merge review fields only.
      onReviewed({
        ...artifact,
        status: updated.status ?? artifact.status,
        reviewed_by: updated.reviewed_by ?? reviewer,
        review_note: updated.review_note ?? note,
        issues: Array.isArray(updated.issues) ? updated.issues : artifact.issues,
      })
      setNote('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const summary = summarizeValue(artifact.summary)
  const titleId = `artifact-${artifact.generator}`
  return (
    <article ref={ref} className={`artifact-card${focused ? ' focused' : ''}`} aria-labelledby={titleId}>
      <header>
        <h4 id={titleId}>{artifact.title_ko || artifact.generator}</h4>
        <span className={`badge ${STATUS_TONE[artifact.status] ?? ''}`}>{ARTIFACT_STATUS_LABELS[artifact.status] ?? artifact.status}</span>
      </header>
      <p className="muted">
        <code>{artifact.generator}</code> · {APPROVAL_MODE_LABELS[artifact.approval_mode] ?? artifact.approval_mode}
        {artifact.reviewed_by ? ` · 검토: ${artifact.reviewed_by}` : ''}
        {artifact.review_note ? ` (${artifact.review_note})` : ''}
      </p>
      {artifact.files.length > 0 && (
        <ul className="file-list" aria-label="파일">
          {artifact.files.map((file) => {
            const url = source.artifactFileUrl(rev, artifact.generator, file)
            const name = file.path.split('/').pop() ?? file.path
            return (
              <li key={file.path}>
                {url ? (
                  <a href={url} target="_blank" rel="noopener noreferrer" download={url.startsWith('data:') ? name : undefined}>
                    {name}
                  </a>
                ) : (
                  <span className="muted" title="모의 모드에서는 이 형식의 파일을 제공하지 않습니다.">
                    {name} (미제공)
                  </span>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {summary.length > 0 && (
        <ul className="summary-list">
          {summary.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {artifact.issues.length > 0 && (
        <ul className="issue-list" aria-label="QA 이슈">
          {artifact.issues.map((issue, i) => {
            const severity = typeof issue === 'string' ? 'info' : issue.severity
            const message = typeof issue === 'string' ? issue : issue.message
            return (
              <li key={i} className={`issue ${severity}`}>
                <strong>{severity === 'error' ? '오류' : severity === 'warning' ? '경고' : '정보'}</strong> {message}
              </li>
            )
          })}
        </ul>
      )}
      {canReview && (
        <div className="review-actions">
          <label className="visually-hidden" htmlFor={`note-${artifact.generator}`}>
            {artifact.title_ko} 검토 의견
          </label>
          <input
            id={`note-${artifact.generator}`}
            type="text"
            placeholder="검토 의견 (선택)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button type="button" className="primary" disabled={busy} onClick={() => void review('approve')} aria-label={`${artifact.title_ko} 승인`}>
            승인
          </button>
          <button type="button" className="danger" disabled={busy} onClick={() => void review('reject')} aria-label={`${artifact.title_ko} 반려`}>
            반려
          </button>
        </div>
      )}
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
    </article>
  )
}

export function ReviewPanel() {
  const { source, meta } = useData()
  const { ui, panelSeq } = useAppState()
  const patchUi = usePatchUi()
  const mode = source.mode
  const payload = ui.panel && (ui.panel.type === 'changes' || ui.panel.type === 'artifact') ? ui.panel.payload : {}
  const revTo = typeof payload.rev_to === 'string' ? payload.rev_to : typeof payload.rev === 'string' ? payload.rev : ui.rev
  const revFrom =
    typeof payload.rev_from === 'string'
      ? payload.rev_from
      : ui.compareRev && ui.compareRev !== revTo
        ? ui.compareRev
        : parentRevision(meta, revTo)
  const focusGenerator = ui.panel?.type === 'artifact' && typeof payload.generator === 'string' ? payload.generator : null

  const changes = useCachedResource(revFrom ? cacheKeys.changes(mode, revFrom, revTo) : null, () => source.getChanges(revFrom!, revTo))
  const artifacts = useCachedResource(cacheKeys.artifacts(mode, revTo), () => source.getArtifacts(revTo))
  const [overrides, setOverrides] = useState<Record<string, Artifact>>({})
  const [reviewer, setReviewer] = useState('공간기획 검토자')
  // Upload area: collapsed until opened by the user or a `panel = {type: "uploads"}` request.
  const [uploadsOpen, setUploadsOpen] = useState(false)
  const [uploadsMounted, setUploadsMounted] = useState(false)
  const [uploadRequest, setUploadRequest] = useState<UploadRequest | null>(null)
  const uploadsRef = useRef<HTMLElement>(null)
  const [reviewDays, setReviewDays] = useState(DEFAULT_REVIEW_WINDOW)
  // Sales before and after an actual revision took effect (not for the first one or one without sales).
  const reviewable = canReviewRelayout(meta, revTo)
  const reviewUrl = reviewable ? source.relayoutReviewReportUrl(revTo, reviewDays) : null

  useEffect(() => {
    if (ui.panel?.type !== 'uploads') return
    setUploadRequest({ jobId: panelPayloadString(ui.panel, 'uploads', 'job_id'), seq: panelSeq })
    setUploadsOpen(true)
    setUploadsMounted(true)
    // Intentionally keyed on panelSeq only (like the other panel requests).
  }, [panelSeq])

  useEffect(() => {
    if (uploadRequest) uploadsRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
  }, [uploadRequest])

  const groups = useMemo(() => {
    const items = changes.data?.change_set.items ?? []
    const known = CHANGE_ORDER.map((type) => [type, items.filter((i) => i.change_type === type)] as const)
    const other = items.filter((i) => !CHANGE_ORDER.includes(i.change_type))
    return [...known, ...(other.length ? [['other', other] as const] : [])].filter(([, list]) => list.length > 0)
  }, [changes.data])

  const showItem = (item: ChangeItem) => {
    const codes = [...new Set([...item.unit_codes_after, ...item.unit_codes_before])]
    const floor = item.floor || floorOfUnitCode(codes[0]) || ui.floor
    patchUi([
      replaceOp('/rev', revTo),
      replaceOp('/compareRev', revFrom),
      replaceOp('/floor', floor),
      replaceOp('/heatmap', null),
      replaceOp(
        '/highlights',
        codes.map((code) => ({ unit_code: code, color: changeColor(item.change_type), reason: item.summary })),
      ),
      replaceOp(
        '/edges',
        item.change_type === 'move' && item.unit_codes_before[0] && item.unit_codes_after[0]
          ? [{ from: item.unit_codes_before[0], to: item.unit_codes_after[0], weight: 2, label: CHANGE_LABELS.move, color: changeColor('move') }]
          : [],
      ),
      replaceOp('/focus', codes.length ? { unit_codes: codes } : null),
    ])
  }

  const artifactList = (artifacts.data ?? []).map((a) => overrides[`${revTo}/${a.generator}`] ?? a)
  const pending = artifactList.filter((a) => a.status === 'awaiting_approval').length

  return (
    <div className="review-panel" data-panel-seq={panelSeq}>
      <header className="panel-header">
        <h2>개정 검토</h2>
        <p className="muted">{revFrom ? `${revFrom} → ${revTo}` : `${revTo} (이전 개정 없음)`}</p>
        {reviewable && (
          <div className="review-report">
            <label className="field">
              <span>비교 기간</span>
              <select name="review-window-days" value={reviewDays} onChange={(e) => setReviewDays(Number(e.target.value))}>
                {REVIEW_WINDOW_OPTIONS.map((days) => (
                  <option key={days} value={days}>
                    {days}일
                  </option>
                ))}
              </select>
            </label>
            {reviewUrl ? (
              <a href={reviewUrl} target="_blank" rel="noopener noreferrer" title={`${revTo} 적용일 앞뒤 ${reviewDays}일의 일평균 매출 비교`}>
                개편 성과 보고서({reviewDays}일) 열기
              </a>
            ) : (
              <span className="muted small">이 환경에서는 개편 성과 보고서를 열 수 없습니다.</span>
            )}
          </div>
        )}
      </header>

      <section ref={uploadsRef} className="upload-section" aria-labelledby="uploads-title">
        <h3 id="uploads-title">
          <button
            type="button"
            className="disclosure"
            aria-expanded={uploadsOpen}
            aria-controls="uploads-body"
            onClick={() => {
              setUploadsOpen((open) => !open)
              setUploadsMounted(true)
            }}
          >
            <span className="disclosure-mark" aria-hidden="true">
              ▸
            </span>
            도면·메타데이터 올리기
          </button>
        </h3>
        <div id="uploads-body" hidden={!uploadsOpen}>
          {uploadsMounted && <UploadArea request={uploadRequest} />}
        </div>
      </section>

      <section aria-labelledby="changes-title">
        <h3 id="changes-title">변경 목록{changes.data ? ` (${changes.data.change_set.items.length}건)` : ''}</h3>
        {!revFrom && <p className="muted">비교할 이전 개정이 없습니다.</p>}
        {changes.loading && <p className="muted">변경 사항을 불러오는 중…</p>}
        {changes.error && (
          <p className="error-text" role="alert">
            변경 사항을 불러오지 못했습니다: {changes.error.message}
          </p>
        )}
        {changes.data && groups.length === 0 && <p className="muted">변경 사항이 없습니다.</p>}
        {groups.map(([type, list]) => (
          <details key={type} open className="change-group">
            <summary>
              <span className="swatch" style={{ background: changeColor(type) }} aria-hidden="true" />
              {CHANGE_LABELS[type] ?? '기타'} <span className="count">{list.length}</span>
            </summary>
            <ul>
              {list.map((item, i) => (
                <li key={`${type}-${i}`}>
                  <button type="button" className="link-button" onClick={() => showItem(item)} title="도면에서 보기">
                    <span className="floor-chip">{item.floor}</span>
                    {item.summary || `${item.change_type} ${[...item.unit_codes_before, ...item.unit_codes_after].join(', ')}`}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        ))}
      </section>

      <section aria-labelledby="impact-title">
        <h3 id="impact-title">영향 분석 · 재생성 계획</h3>
        {changes.data && changes.data.impact.length === 0 && <p className="muted">재생성 대상이 없습니다.</p>}
        <ul className="impact-list">
          {(changes.data?.impact ?? []).map((entry) => (
            <li key={entry.generator} className="impact-item">
              <div className="impact-head">
                <strong>{entry.title_ko}</strong>
                <span className={`badge risk-${entry.risk_tier}`}>{RISK_LABELS[entry.risk_tier] ?? entry.risk_tier}</span>
                <span className="badge">{APPROVAL_MODE_LABELS[entry.approval_mode] ?? entry.approval_mode}</span>
              </div>
              <p className="muted">
                <code>{entry.generator}</code> · 층 {entry.floors.join(', ') || '–'}
              </p>
              {entry.reasons.length > 0 && (
                <details>
                  <summary>사유 {entry.reasons.length}건</summary>
                  <ul>
                    {entry.reasons.map((reason, i) => (
                      <li key={i}>{reason}</li>
                    ))}
                  </ul>
                </details>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="artifacts-title">
        <h3 id="artifacts-title">
          산출물 · {revTo}
          {pending ? <span className="badge warn">승인 대기 {pending}</span> : null}
        </h3>
        <label className="field inline">
          <span>검토자</span>
          <input type="text" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
        </label>
        {artifacts.loading && <p className="muted">산출물을 불러오는 중…</p>}
        {artifacts.error && (
          <p className="error-text" role="alert">
            산출물을 불러오지 못했습니다: {artifacts.error.message}
          </p>
        )}
        {artifacts.data && artifactList.length === 0 && <p className="muted">산출물이 없습니다.</p>}
        {artifactList.map((artifact) => (
          <ArtifactCard
            key={artifact.generator}
            artifact={artifact}
            rev={revTo}
            reviewer={reviewer}
            focused={focusGenerator === artifact.generator}
            onReviewed={(updated) => {
              invalidateCache(cacheKeys.artifacts(mode, revTo))
              setOverrides((prev) => ({ ...prev, [`${revTo}/${updated.generator}`]: updated }))
            }}
          />
        ))}
      </section>
    </div>
  )
}
