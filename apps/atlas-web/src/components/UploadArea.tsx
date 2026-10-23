// Upload area of the 개정 검토 tab: drawing uploads with job tracking, and metadata uploads.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { errorText } from '../api/client'
import { cacheKeys, revisionCachePrefixes } from '../api/keys'
import { useData } from '../api/source'
import { invalidateCache, useCachedResource } from '../hooks/useCachedResource'
import { JOB_POLL_MS, useUploadJob } from '../hooks/useUploadJob'
import { formatDateTime } from '../lib/format'
import { JOB_STATUS_LABELS, JOB_STATUS_TONE, jobHasRevision } from '../lib/uploads'
import type { UploadJob, UploadJobSummary } from '../types/api'
import { DrawingUploadForm } from './DrawingUploadForm'
import { MetadataUploads } from './MetadataUploads'
import { UploadJobDetail } from './UploadJobDetail'

/** A request to open the upload area (agent `panel = {type: "uploads", payload: {job_id?}}`). */
export interface UploadRequest {
  jobId: string | null
  seq: number
}

type UploadTab = 'drawings' | 'metadata'

const TABS: { id: UploadTab; label: string }[] = [
  { id: 'drawings', label: '도면 올리기' },
  { id: 'metadata', label: '메타데이터' },
]

function DrawingUploads({ request, pollMs }: { request: UploadRequest | null; pollMs: number }) {
  const { source, refreshMeta } = useData()
  const profiles = useCachedResource(cacheKeys.uploadProfiles(source.mode), () => source.listDrawingProfiles())
  const [jobs, setJobs] = useState<{ data: UploadJobSummary[] | null; error: string | null }>({ data: null, error: null })
  const [jobsSeq, setJobsSeq] = useState(0)
  const [selectedJobId, setSelectedJobId] = useState<string | null>(request?.jobId ?? null)

  useEffect(() => {
    if (request?.jobId) setSelectedJobId(request.jobId)
  }, [request?.jobId, request?.seq])

  useEffect(() => {
    let cancelled = false
    source.listUploadJobs().then(
      (data) => !cancelled && setJobs({ data, error: null }),
      (e: unknown) => !cancelled && setJobs((prev) => ({ data: prev.data, error: errorText(e) })),
    )
    return () => {
      cancelled = true
    }
  }, [source, jobsSeq])

  // A finished job may have created (or replaced) a revision: reload /api/meta and drop cached models.
  const onSettled = useCallback(
    (job: UploadJob) => {
      setJobsSeq((n) => n + 1)
      if (jobHasRevision(job.status) && job.rev_id) {
        for (const prefix of revisionCachePrefixes(source.mode, job.rev_id)) invalidateCache(prefix)
        refreshMeta?.().catch(() => {
          // keep the current revision list; the job detail still offers the revision
        })
      }
      if (job.relayout_check) invalidateCache(cacheKeys.relayoutBriefs(source.mode))
    },
    [refreshMeta, source],
  )

  const tracked = useUploadJob(selectedJobId, pollMs, onSettled)
  const list = jobs.data ?? []

  return (
    <div className="drawing-uploads">
      <DrawingUploadForm
        profiles={profiles.data}
        profilesError={profiles.error ? errorText(profiles.error) : null}
        onCreated={(jobId) => {
          setSelectedJobId(jobId)
          setJobsSeq((n) => n + 1)
        }}
      />

      <div className="jobs-block">
        <div className="block-head">
          <h4 id="upload-jobs-title">최근 작업</h4>
          <button type="button" onClick={() => setJobsSeq((n) => n + 1)}>
            새로 고침
          </button>
        </div>
        {jobs.error && <p className="error-text">작업 목록을 불러오지 못했습니다: {jobs.error}</p>}
        {!jobs.data && !jobs.error && <p className="muted">작업 목록을 불러오는 중…</p>}
        {jobs.data && list.length === 0 && <p className="muted">아직 올린 도면이 없습니다.</p>}
        {list.length > 0 && (
          <ul className="job-list" aria-labelledby="upload-jobs-title">
            {list.map((j) => {
              const current = j.job_id === selectedJobId
              const status = current && tracked.job ? tracked.job.status : j.status
              return (
                <li key={j.job_id}>
                  <button
                    type="button"
                    className={`link-button${current ? ' active' : ''}`}
                    aria-current={current ? 'true' : undefined}
                    onClick={() => setSelectedJobId(j.job_id)}
                  >
                    <strong>{j.rev_id ?? '–'}</strong>
                    <span className={`badge ${JOB_STATUS_TONE[status] ?? ''}`}>{JOB_STATUS_LABELS[status] ?? status}</span>
                    <span className="muted small">
                      {' '}
                      {j.job_id} · {formatDateTime(j.created_at)}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {selectedJobId && !tracked.job && !tracked.error && <p className="muted">작업 상태를 불러오는 중…</p>}
      {selectedJobId && !tracked.job && tracked.error && (
        <p className="error-text" role="alert">
          작업 상태를 불러오지 못했습니다: {tracked.error}
        </p>
      )}
      {tracked.job && <UploadJobDetail job={tracked.job} error={tracked.error} onResumed={tracked.resume} />}
    </div>
  )
}

export function UploadArea({ request, pollMs = JOB_POLL_MS }: { request: UploadRequest | null; pollMs?: number }) {
  const [tab, setTab] = useState<UploadTab>('drawings')
  const [metadataVisited, setMetadataVisited] = useState(false)
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])

  useEffect(() => {
    if (request) setTab('drawings')
  }, [request?.seq])

  useEffect(() => {
    if (tab === 'metadata') setMetadataVisited(true)
  }, [tab])

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % TABS.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + TABS.length) % TABS.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = TABS.length - 1
    else return
    event.preventDefault()
    setTab(TABS[next].id)
    tabRefs.current[next]?.focus()
  }

  return (
    <div className="upload-area">
      <div role="tablist" aria-label="올리기 종류" className="sub-tabs">
        {TABS.map((t, i) => {
          const selected = tab === t.id
          return (
            <button
              key={t.id}
              ref={(el) => {
                tabRefs.current[i] = el
              }}
              id={`upload-tab-${t.id}`}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={`upload-tabpanel-${t.id}`}
              tabIndex={selected ? 0 : -1}
              className={selected ? 'active' : undefined}
              onClick={() => setTab(t.id)}
              onKeyDown={(e) => onKeyDown(e, i)}
            >
              {t.label}
            </button>
          )
        })}
      </div>
      <div role="tabpanel" id="upload-tabpanel-drawings" aria-labelledby="upload-tab-drawings" hidden={tab !== 'drawings'}>
        <DrawingUploads request={request} pollMs={pollMs} />
      </div>
      <div role="tabpanel" id="upload-tabpanel-metadata" aria-labelledby="upload-tab-metadata" hidden={tab !== 'metadata'}>
        {metadataVisited && <MetadataUploads />}
      </div>
    </div>
  )
}
