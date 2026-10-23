// "시즌 개편" section of the layout tab: brief → 진단 → 대안 → 사람 선택 → 도면 대조 → 제안서.
import { useEffect, useRef, useState } from 'react'
import { errorText } from '../api/client'
import { cacheKeys } from '../api/keys'
import { useData } from '../api/source'
import { useCachedResource } from '../hooks/useCachedResource'
import { useElapsedSeconds } from '../hooks/useElapsedSeconds'
import { formatPercent } from '../lib/format'
import { RELAYOUT_STATUS_LABELS, pastDiagnosis } from '../lib/relayout'
import type { RelayoutObjective, RelayoutMovable, RelayoutPlanOptions, RelayoutProject } from '../types/api'
import { RelayoutAlternatives, type RelayoutBusy } from './RelayoutAlternatives'
import { BriefSummary, DiagnosisDisclosure } from './RelayoutBrief'
import { RelayoutCheckPanel } from './RelayoutCheck'

/** A request to open one brief (agent `panel = {type: "layout", payload: {brief_id}}` or an upload job). */
export interface RelayoutRequest {
  briefId: string
  seq: number
}

interface PlanForm {
  objective: RelayoutObjective
  maxMoves: number
  downtimeWeeks: number
  movable: RelayoutMovable
}

function planDefaults(project: RelayoutProject): PlanForm {
  const params = project.plan?.params ?? {}
  const objective = params.objective ?? project.brief.objective
  const movable = params.movable ?? project.brief.movable
  const number = (value: unknown, fallback: number) => (typeof value === 'number' && Number.isFinite(value) ? value : fallback)
  return {
    objective: objective === 'sales' ? 'sales' : 'commission',
    maxMoves: number(params.max_moves ?? project.brief.max_moves, 6),
    downtimeWeeks: number(params.downtime_weeks ?? project.brief.downtime_weeks, 2),
    movable: movable === 'all' ? 'all' : 'expiring',
  }
}

const clampInt = (value: string, min: number, max: number, fallback: number) => {
  const parsed = Math.round(Number(value))
  return Number.isFinite(parsed) && value.trim() !== '' ? Math.min(max, Math.max(min, parsed)) : fallback
}

export function RelayoutSection({ request }: { request: RelayoutRequest | null }) {
  const { source, refreshMeta } = useData()
  const briefs = useCachedResource(cacheKeys.relayoutBriefs(source.mode), () => source.listRelayoutBriefs())
  const [briefId, setBriefId] = useState<string | null>(request?.briefId ?? null)
  const [loaded, setLoaded] = useState<{ briefId: string; project: RelayoutProject } | null>(null)
  const [loadError, setLoadError] = useState<{ briefId: string; message: string } | null>(null)
  const [reloadSeq, setReloadSeq] = useState(0)
  const [busy, setBusy] = useState<RelayoutBusy>(null)
  const [actionError, setActionError] = useState<{ briefId: string; action: Exclude<RelayoutBusy, null>; message: string } | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const [form, setForm] = useState<{ briefId: string; values: PlanForm } | null>(null)
  // Diagnosis disclosure: follows the status until the user diagnoses (open) or plans (closed) again.
  const [diagnosisView, setDiagnosisView] = useState<{ briefId: string | null; seq: number; open: boolean }>({ briefId: null, seq: 0, open: false })
  const currentBrief = useRef(briefId)
  // The request the section was mounted with is already loaded through the initial brief.
  const handledRequest = useRef(request ? `${request.briefId}:${request.seq}` : null)
  const elapsed = useElapsedSeconds(busy === 'plan')

  useEffect(() => {
    currentBrief.current = briefId
  }, [briefId])

  // Open the requested brief and reload it and the brief list, even when it is already shown
  // (e.g. after an upload job checked the drawing against the decided plan).
  const reloadBriefs = briefs.reload
  useEffect(() => {
    if (!request) return
    const key = `${request.briefId}:${request.seq}`
    if (handledRequest.current === key) return
    handledRequest.current = key
    setBriefId(request.briefId)
    setReloadSeq((n) => n + 1)
    reloadBriefs()
  }, [request?.briefId, request?.seq, reloadBriefs])

  useEffect(() => {
    if (briefId === null && briefs.data?.length) setBriefId(briefs.data[0].brief_id)
  }, [briefId, briefs.data])

  useEffect(() => {
    if (!briefId) return
    let cancelled = false
    source.getRelayoutProject(briefId).then(
      (project) => {
        if (cancelled) return
        setLoaded({ briefId, project })
        setLoadError(null)
      },
      (error: unknown) => {
        if (!cancelled) setLoadError({ briefId, message: errorText(error) })
      },
    )
    return () => {
      cancelled = true
    }
  }, [briefId, reloadSeq, source])

  const project = loaded?.briefId === briefId ? loaded.project : null
  const loadMessage = loadError?.briefId === briefId ? loadError.message : null
  const values = form?.briefId === briefId ? form.values : project ? planDefaults(project) : null
  const errorFor = (action: Exclude<RelayoutBusy, null>) =>
    actionError?.briefId === briefId && actionError.action === action ? actionError.message : null
  // The last diagnose/plan action on this brief decides whether the diagnosis is open.
  const diagnosisAction = diagnosisView.briefId === briefId && diagnosisView.seq > 0

  /** Run one project action; the result is shown at once and the whole project is reloaded after it. */
  async function act<T>(
    action: Exclude<RelayoutBusy, null>,
    call: (id: string) => Promise<T>,
    apply: (project: RelayoutProject, result: T) => RelayoutProject,
    done: (result: T) => string,
  ): Promise<boolean> {
    const id = briefId
    if (!id || busy) return false
    setBusy(action)
    setActionError(null)
    try {
      const result = await call(id)
      if (currentBrief.current !== id) return false
      setLoaded((prev) => (prev && prev.briefId === id ? { briefId: id, project: apply(prev.project, result) } : prev))
      setAnnouncement(done(result))
      if (action === 'diagnose' || action === 'plan') {
        setDiagnosisView((prev) => ({ briefId: id, seq: prev.seq + 1, open: action === 'diagnose' }))
      }
      briefs.reload()
      setReloadSeq((n) => n + 1)
      return true
    } catch (error) {
      if (currentBrief.current === id) setActionError({ briefId: id, action, message: errorText(error) })
      return false
    } finally {
      setBusy(null)
    }
  }

  const diagnose = () =>
    act(
      'diagnose',
      (id) => source.diagnoseRelayout(id),
      (p, diagnosis) => ({ ...p, diagnosis, status: p.status === 'draft' ? 'diagnosed' : p.status }),
      () => '진단을 마쳤습니다.',
    )

  const plan = () => {
    if (!values) return
    const options: RelayoutPlanOptions = {
      objective: values.objective,
      max_moves: values.maxMoves,
      downtime_weeks: values.downtimeWeeks,
      movable: values.movable,
    }
    setAnnouncement('대안을 만드는 중입니다. 수십 초 걸릴 수 있습니다.')
    void act(
      'plan',
      (id) => source.planRelayout(id, options),
      (p, result) => ({
        ...p,
        plan: result,
        status: 'planned',
        selected_alt_id: result.alternatives.some((a) => a.alt_id === p.selected_alt_id) ? p.selected_alt_id : null,
      }),
      (result) => `대안 ${result.alternatives.length}개를 만들었습니다.`,
    ).then((ok) => {
      // The plan saved hypothetical revisions; list them in the revision selector.
      if (ok) refreshMeta?.().catch(() => undefined)
    })
  }

  const select = (altId: string, decidedBy: string) =>
    act(
      'select',
      (id) => source.selectRelayoutAlternative(id, { alt_id: altId, decided_by: decidedBy }),
      (_p, result) => result,
      () => `${altId} 대안으로 결정했습니다.`,
    )

  const check = (rev: string) =>
    void act(
      'check',
      (id) => source.checkRelayout(id, rev),
      (p, result) => ({ ...p, check: result, status: 'checked' }),
      (result) => `도면 대조를 마쳤습니다. 일치율 ${formatPercent(result.conformance_pct)}`,
    )

  const update = (patch: Partial<PlanForm>) => {
    if (briefId && values) setForm({ briefId, values: { ...values, ...patch } })
  }

  const reportUrl = project ? (project.report_url ?? source.relayoutReportUrl(project.brief.brief_id)) : null
  const options = briefs.data ?? []

  return (
    <div className="relayout" aria-busy={busy !== null}>
      <p className="muted">개편 브리프로 진단하고, 대안을 비교해 결정한 뒤 CAD팀이 올린 도면과 대조합니다.</p>
      <div className="relayout-picker">
        <label className="field stacked">
          <span>개편 브리프</span>
          <select value={briefId ?? ''} disabled={!options.length} onChange={(e) => setBriefId(e.target.value || null)}>
            {!briefId && <option value="">브리프를 고르세요</option>}
            {briefId && !options.some((b) => b.brief_id === briefId) && <option value={briefId}>{briefId}</option>}
            {options.map((b) => (
              <option key={b.brief_id} value={b.brief_id}>
                {b.title} ({b.brief_id}) · {RELAYOUT_STATUS_LABELS[b.status] ?? b.status}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => {
            briefs.reload()
            setReloadSeq((n) => n + 1)
          }}
        >
          새로 고침
        </button>
      </div>
      {briefs.error && (
        <p className="error-text" role="alert">
          브리프 목록을 불러오지 못했습니다: {errorText(briefs.error)}
        </p>
      )}
      {briefs.data && options.length === 0 && (
        <p className="muted">개편 브리프가 없습니다. 개정 검토 탭의 “도면·메타데이터 올리기”에서 개편 브리프(YAML·JSON)를 올려 주세요.</p>
      )}
      {loadMessage && (
        <p className="error-text" role="alert">
          개편 프로젝트를 불러오지 못했습니다: {loadMessage}
        </p>
      )}
      {briefId && !project && !loadMessage && <p className="muted">개편 프로젝트를 불러오는 중…</p>}
      <p className="visually-hidden" role="status">
        {announcement}
      </p>

      {project && values && (
        <>
          <BriefSummary project={project} />

          <section className="relayout-block" aria-labelledby="relayout-diagnosis-title">
            <header className="block-head">
              <h3 id="relayout-diagnosis-title">진단</h3>
              <button type="button" disabled={busy !== null} onClick={() => void diagnose()}>
                {busy === 'diagnose' ? '진단하는 중…' : project.diagnosis ? '다시 진단하기' : '진단하기'}
              </button>
            </header>
            {errorFor('diagnose') && (
              <p className="error-text" role="alert">
                진단하지 못했습니다: {errorFor('diagnose')}
              </p>
            )}
            {project.diagnosis ? (
              <DiagnosisDisclosure
                key={`${project.brief.brief_id}-${pastDiagnosis(project.status)}-${diagnosisAction ? diagnosisView.seq : 0}`}
                diagnosis={project.diagnosis}
                defaultOpen={diagnosisAction ? diagnosisView.open : !pastDiagnosis(project.status)}
              />
            ) : (
              <p className="muted">진단하면 시즌 강세 카테고리, 층별 전년 대비 매출, 바꿀 후보 매장, 입점 가능한 매장을 보여 드립니다.</p>
            )}
          </section>

          <section className="relayout-block" aria-labelledby="relayout-plan-title">
            <h3 id="relayout-plan-title">대안 만들기</h3>
            <form
              className="plan-form"
              onSubmit={(event) => {
                event.preventDefault()
                plan()
              }}
            >
              <div className="form-grid">
                <label className="field stacked">
                  <span>목적</span>
                  <select value={values.objective} onChange={(e) => update({ objective: e.target.value === 'sales' ? 'sales' : 'commission' })}>
                    <option value="commission">수수료 수익</option>
                    <option value="sales">매출</option>
                  </select>
                </label>
                <label className="field stacked">
                  <span>최대 이동 수</span>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    step={1}
                    value={values.maxMoves}
                    onChange={(e) => update({ maxMoves: clampInt(e.target.value, 1, 20, values.maxMoves) })}
                  />
                </label>
                <label className="field stacked">
                  <span>공사 기간(주)</span>
                  <input
                    type="number"
                    min={0}
                    max={26}
                    step={1}
                    value={values.downtimeWeeks}
                    onChange={(e) => update({ downtimeWeeks: clampInt(e.target.value, 0, 26, values.downtimeWeeks) })}
                  />
                </label>
                <fieldset className="radio-group">
                  <legend>이동 대상</legend>
                  <label className="toggle">
                    <input type="radio" name="relayout-movable" value="expiring" checked={values.movable === 'expiring'} onChange={() => update({ movable: 'expiring' })} />
                    계약 만료 매장만
                  </label>
                  <label className="toggle">
                    <input type="radio" name="relayout-movable" value="all" checked={values.movable === 'all'} onChange={() => update({ movable: 'all' })} />
                    전체
                  </label>
                </fieldset>
              </div>
              <button type="submit" className="primary" disabled={busy !== null}>
                {busy === 'plan' ? `대안을 만드는 중… ${elapsed}초` : project.plan ? '대안 다시 만들기' : '대안 만들기'}
              </button>
              {busy === 'plan' && <p className="muted small">대안 계산은 수십 초 걸릴 수 있습니다. 다른 탭을 보셔도 계산은 계속됩니다.</p>}
              {!project.diagnosis && busy !== 'plan' && <p className="muted small">진단 전이면 먼저 진단한 뒤 대안을 만듭니다.</p>}
            </form>
            {errorFor('plan') && (
              <p className="error-text" role="alert">
                대안을 만들지 못했습니다: {errorFor('plan')}
              </p>
            )}
          </section>

          <RelayoutAlternatives key={`alts-${project.brief.brief_id}`} project={project} busy={busy} error={errorFor('select')} onSelect={select} />
          <RelayoutCheckPanel key={`check-${project.brief.brief_id}`} project={project} busy={busy} error={errorFor('check')} onCheck={check} />

          <section className="relayout-block" aria-labelledby="relayout-report-title">
            <h3 id="relayout-report-title">제안서</h3>
            <p className="muted">진단, 대안 비교, 선택안, 도면 대조, 가정과 한계를 한 문서로 봅니다.</p>
            {reportUrl ? (
              <a className="button-link" href={reportUrl} target="_blank" rel="noopener noreferrer">
                제안서 열기
              </a>
            ) : (
              <p className="muted">이 환경에서는 제안서를 열 수 없습니다.</p>
            )}
          </section>
        </>
      )}
    </div>
  )
}
