// Brief summary and diagnosis view of a seasonal relayout project.
import { useId, useState } from 'react'
import { bottomPercentText, formatArea, formatDateTime, formatKrw, formatNumber, formatPercent } from '../lib/format'
import { MOVABLE_LABELS, OBJECTIVE_LABELS, RELAYOUT_STATUS_LABELS, RELAYOUT_STATUS_TONE, pastDiagnosis, seasonExtremes, seasonText } from '../lib/relayout'
import type { RelayoutCandidate, RelayoutDiagnosis, RelayoutEntry, RelayoutExit, RelayoutProject } from '../types/api'

function areaText(range: [number, number] | null): string | null {
  if (!range) return null
  return range[0] === range[1] ? `${formatNumber(range[0])}㎡` : `${formatNumber(range[0])}~${formatNumber(range[1])}㎡`
}

function entryFacts(entry: RelayoutEntry): string[] {
  return [
    entry.category_name,
    areaText(entry.target_area_m2) && `목표 ${areaText(entry.target_area_m2)}`,
    entry.preferred_floors.length ? `선호 ${entry.preferred_floors.join('·')}` : null,
    entry.commission_rate !== null ? `수수료율 ${formatPercent(entry.commission_rate * 100, false, 0)}` : null,
    entry.attractiveness !== null ? `집객력 ${formatNumber(entry.attractiveness, 2)}` : null,
  ].filter((fact): fact is string => !!fact)
}

function ExitList({ exits }: { exits: RelayoutExit[] }) {
  if (!exits.length) return <p className="muted">퇴점 브랜드가 없습니다.</p>
  return (
    <ul className="plain-list">
      {exits.map((e) => (
        <li key={`${e.brand_id}-${e.unit_code ?? ''}`}>
          <strong>{e.brand_name}</strong>
          {e.unit_code && <span className="unit-code"> {e.unit_code}</span>}
          {e.reason && <span className="muted"> · {e.reason}</span>}
        </li>
      ))}
    </ul>
  )
}

const LONG_NOTES = 120

/** Brief notes; long ones fold into a disclosure once alternatives exist. */
function BriefNotes({ notes, defaultOpen }: { notes: string; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  if (notes.length <= LONG_NOTES) return <p className="note">{notes}</p>
  return (
    <details className="note" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>메모{open ? '' : ` · ${notes.slice(0, 40).trim()}…`}</summary>
      <p>{notes}</p>
    </details>
  )
}

export function BriefSummary({ project }: { project: RelayoutProject }) {
  const { brief } = project
  const season = brief.season_label ? `${brief.year ?? ''} ${brief.season_label}`.trim() : seasonText(brief.season, brief.year)
  const conditions = [
    brief.max_moves !== null ? `최대 이동 ${brief.max_moves}건` : null,
    brief.downtime_weeks !== null ? `공사 ${brief.downtime_weeks}주` : null,
    brief.movable ? `이동 대상 ${MOVABLE_LABELS[brief.movable] ?? brief.movable}` : null,
    brief.keep_anchors === true ? '앵커 고정' : brief.keep_anchors === false ? '앵커 이동 허용' : null,
    brief.area_tolerance !== null ? `면적 허용 오차 ±${formatPercent(brief.area_tolerance * 100, false, 0)}` : null,
  ].filter((c): c is string => !!c)
  return (
    <section className="relayout-block" aria-labelledby="relayout-brief-title">
      <header className="block-head">
        <h3 id="relayout-brief-title">{brief.title}</h3>
        <span className={`badge ${RELAYOUT_STATUS_TONE[project.status] ?? ''}`}>{RELAYOUT_STATUS_LABELS[project.status] ?? project.status}</span>
      </header>
      <dl className="kv">
        <dt>시즌</dt>
        <dd>{season}</dd>
        <dt>적용일</dt>
        <dd>{brief.effective_date ?? '–'}</dd>
        <dt>기준 개정</dt>
        <dd>{brief.base_rev}</dd>
        <dt>대상 층</dt>
        <dd>{brief.floors.join(', ') || '–'}</dd>
        <dt>목적</dt>
        <dd>{brief.objective_label ?? OBJECTIVE_LABELS[brief.objective] ?? brief.objective}</dd>
        {conditions.length > 0 && (
          <>
            <dt>조건</dt>
            <dd>{conditions.join(' · ')}</dd>
          </>
        )}
        <dt>입점·퇴점</dt>
        <dd>
          입점 {brief.entries.length}개 · 퇴점 {brief.exits.length}개
        </dd>
        {project.updated_at && (
          <>
            <dt>갱신</dt>
            <dd>{formatDateTime(project.updated_at)}</dd>
          </>
        )}
      </dl>
      {brief.notes && <BriefNotes key={`${brief.brief_id}-${project.status}`} notes={brief.notes} defaultOpen={!pastDiagnosis(project.status)} />}
      {(brief.entries.length > 0 || brief.exits.length > 0) && (
        <details className="brief-brands">
          <summary>입점·퇴점 브랜드 보기</summary>
          <h4>입점</h4>
          {brief.entries.length ? (
            <ul className="plain-list">
              {brief.entries.map((e) => (
                <li key={e.brand_id}>
                  <strong>{e.brand_name}</strong>
                  {e.new_brand && <span className="badge">신규 브랜드</span>}
                  <span className="muted"> · {entryFacts(e).join(' · ')}</span>
                  {e.note && <div className="muted">{e.note}</div>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">입점 브랜드가 없습니다.</p>
          )}
          <h4>퇴점</h4>
          <ExitList exits={brief.exits} />
        </details>
      )}
    </section>
  )
}

function FactorChips({ label, factors }: { label: string; factors: RelayoutDiagnosis['season_factors'] }) {
  if (!factors.length) return null
  return (
    <div className="factor-row">
      <span className="factor-label">{label}</span>
      <ul className="chip-list" aria-label={`${label} 카테고리`}>
        {factors.map((f) => (
          <li key={f.category} className={`chip ${f.factor >= 1 ? 'up' : 'down'}`}>
            {f.category_name} ×{formatNumber(f.factor, 2)}
          </li>
        ))}
      </ul>
    </div>
  )
}

const CANDIDATE_ROWS = 8

/** Candidates: the first rows with a "모두 보기" toggle; brand and category share one cell. */
function CandidateTable({ candidates }: { candidates: RelayoutCandidate[] }) {
  const [showAll, setShowAll] = useState(false)
  const tableId = useId()
  const rows = showAll ? candidates : candidates.slice(0, CANDIDATE_ROWS)
  return (
    <>
      <div className="table-wrap">
        <table id={tableId} className="compact-table">
          <caption className="visually-hidden">계약 만료 또는 저효율 후보 매장</caption>
          <thead>
            <tr>
              <th scope="col">매장</th>
              <th scope="col">브랜드</th>
              <th scope="col">계약 만료</th>
              <th scope="col">효율</th>
              <th scope="col">사유</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.unit_code}>
                <td className="unit-code">{c.unit_code}</td>
                <td className="wrap">
                  {c.brand_name ?? '–'}
                  {c.category_name && <div className="muted small">{c.category_name}</div>}
                </td>
                <td>{c.lease_end ?? '–'}</td>
                <td title={c.percentile === null ? undefined : `효율 백분위 ${formatNumber(c.percentile, 1)}`}>{bottomPercentText(c.percentile)}</td>
                <td>
                  <ul className="chip-list" aria-label="사유">
                    {c.reasons.map((reason) => (
                      <li key={reason} className={`chip ${reason.includes('저효율') ? 'down' : 'warn'}`}>
                        {reason}
                      </li>
                    ))}
                  </ul>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {candidates.length > CANDIDATE_ROWS && (
        <button type="button" className="text-button" aria-expanded={showAll} aria-controls={tableId} onClick={() => setShowAll((all) => !all)}>
          {showAll ? `처음 ${CANDIDATE_ROWS}곳만 보기` : `모두 보기 (${candidates.length})`}
        </button>
      )}
    </>
  )
}

/** Short line for the collapsed diagnosis: candidates and the strongest categories of the season. */
export function diagnosisSummary(diagnosis: RelayoutDiagnosis): string {
  const total = diagnosis.candidate_total ?? diagnosis.candidates.length
  const { strong } = seasonExtremes(diagnosis.season_factors, 2)
  return [`후보 매장 ${total}곳`, strong.length ? `강세 ${strong.map((f) => f.category_name).join('·')}` : null, `공실 ${diagnosis.vacancies.length}곳`]
    .filter(Boolean)
    .join(' · ')
}

/**
 * Diagnosis behind a disclosure: open while the project is being diagnosed, closed once
 * alternatives exist (the parent remounts it with a new `defaultOpen` when the status changes).
 */
export function DiagnosisDisclosure({ diagnosis, defaultOpen }: { diagnosis: RelayoutDiagnosis; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <details className="disclosure-block" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        진단 결과 <span className="muted small">{diagnosisSummary(diagnosis)}</span>
      </summary>
      {open && <DiagnosisView diagnosis={diagnosis} />}
    </details>
  )
}

export function DiagnosisView({ diagnosis }: { diagnosis: RelayoutDiagnosis }) {
  const { strong, weak } = seasonExtremes(diagnosis.season_factors)
  const { season } = diagnosis
  const total = diagnosis.candidate_total ?? diagnosis.candidates.length
  return (
    <div className="diagnosis">
      <p className="muted">
        {season.year ? `${season.year} ` : ''}
        {season.label}
        {season.last && ` · 최근 시즌 ${season.last.start}~${season.last.end}`}
        {season.previous && ` · 비교 ${season.previous.start}~${season.previous.end}`}
        {diagnosis.period && ` · 효율 기준 ${diagnosis.period.start}~${diagnosis.period.end}`}
      </p>

      <h4>시즌 강세·약세 카테고리</h4>
      <FactorChips label="강세" factors={strong} />
      <FactorChips label="약세" factors={weak} />
      {!strong.length && !weak.length && <p className="muted">시즌 가중치가 없습니다.</p>}

      <h4>층별 전년 대비</h4>
      {diagnosis.floors.length ? (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">층별 ㎡당 매출과 전년 같은 시즌 대비 증감</caption>
            <thead>
              <tr>
                <th scope="col">층</th>
                <th scope="col">㎡당 매출</th>
                <th scope="col">전년 대비</th>
                <th scope="col">저효율 매장</th>
              </tr>
            </thead>
            <tbody>
              {diagnosis.floors.map((f) => (
                <tr key={f.floor}>
                  <td>{f.floor}</td>
                  <td>{formatKrw(f.sales_per_m2)}</td>
                  <td className={f.yoy_pct !== null && f.yoy_pct < 0 ? 'negative' : undefined}>{formatPercent(f.yoy_pct, true)}</td>
                  <td>{f.low_efficiency === null ? '–' : `${f.low_efficiency}곳`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">층별 값이 없습니다.</p>
      )}

      <h4>
        바꿀 후보 매장 <span className="count">{diagnosis.candidates.length < total ? `${total}곳 중 ${diagnosis.candidates.length}곳` : `${total}곳`}</span>
      </h4>
      {diagnosis.candidates.length ? <CandidateTable candidates={diagnosis.candidates} /> : <p className="muted">계약 만료나 저효율로 바꿀 후보가 없습니다.</p>}

      <h4>공실</h4>
      {diagnosis.vacancies.length ? (
        <ul className="plain-list">
          {diagnosis.vacancies.map((v) => (
            <li key={v.unit_code}>
              <span className="unit-code">{v.unit_code}</span> <span className="muted">{formatArea(v.area_m2)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">대상 층에 공실이 없습니다.</p>
      )}

      <h4>입점 브랜드</h4>
      {diagnosis.entries.length ? (
        <ul className="plain-list">
          {diagnosis.entries.map((e) => (
            <li key={e.brand_id}>
              <strong>{e.brand_name}</strong>
              <span className="muted"> · {entryFacts(e).join(' · ')}</span>
              {e.season_factor !== null && <span className="muted"> · 시즌 가중치 ×{formatNumber(e.season_factor, 2)}</span>}
              <div className={e.feasible_units ? undefined : 'warn-text'}>
                {e.feasible_units ? `들어갈 수 있는 매장 ${e.feasible_units}곳` : '들어갈 수 있는 매장이 없습니다'}
                {e.feasible_unit_codes.length > 0 && <span className="muted"> ({e.feasible_unit_codes.join(', ')})</span>}
              </div>
              {e.note && <div className="muted">{e.note}</div>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">입점 브랜드가 없습니다.</p>
      )}

      <h4>퇴점 브랜드</h4>
      <ExitList exits={diagnosis.exits} />

      {diagnosis.notes.length > 0 && (
        <ul className="notes">
          {diagnosis.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
