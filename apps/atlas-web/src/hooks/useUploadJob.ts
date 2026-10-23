import { useEffect, useRef, useState } from 'react'
import { ApiError, errorText } from '../api/client'
import { useData } from '../api/source'
import { isJobSettled } from '../lib/uploads'
import type { UploadJob } from '../types/api'

export const JOB_POLL_MS = 2000
const MAX_POLL_FAILURES = 5

interface Tracked {
  jobId: string | null
  job: UploadJob | null
  error: string | null
}

/**
 * Follow one upload job: GET it, then poll every `pollMs` until it is done, failed or needs review.
 * Polling stops when the job id changes or the component unmounts. `onSettled` runs once each time
 * the job is seen settled after (re)starting.
 */
export function useUploadJob(jobId: string | null, pollMs = JOB_POLL_MS, onSettled?: (job: UploadJob) => void) {
  const { source } = useData()
  const [tracked, setTracked] = useState<Tracked>({ jobId: null, job: null, error: null })
  const [restart, setRestart] = useState(0)
  const settledRef = useRef(onSettled)

  useEffect(() => {
    settledRef.current = onSettled
  })

  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const poll = async () => {
      try {
        const job = await source.getUploadJob(jobId)
        if (cancelled) return
        failures = 0
        setTracked({ jobId, job, error: null })
        if (isJobSettled(job.status)) {
          settledRef.current?.(job)
          return
        }
      } catch (error) {
        if (cancelled) return
        failures += 1
        setTracked((prev) => ({ jobId, job: prev.jobId === jobId ? prev.job : null, error: errorText(error) }))
        if ((error instanceof ApiError && error.status === 404) || failures >= MAX_POLL_FAILURES) return
      }
      timer = setTimeout(() => void poll(), pollMs)
    }
    void poll()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [jobId, pollMs, restart, source])

  const current = tracked.jobId === jobId ? tracked : { jobId, job: null, error: null }
  return {
    job: current.job,
    error: current.error,
    /** Show a job state returned by another call (e.g. the layer mapping) and poll it again. */
    resume(job: UploadJob) {
      setTracked({ jobId: job.job_id, job, error: null })
      setRestart((n) => n + 1)
    },
  }
}
