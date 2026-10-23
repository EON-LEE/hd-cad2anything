// Metadata uploads (POST /api/uploads/metadata) and the current status table (GET /api/uploads/metadata).
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { errorText } from '../api/client'
import { cacheKeys } from '../api/keys'
import { useData } from '../api/source'
import { invalidateCache } from '../hooks/useCachedResource'
import { formatDateTime, formatNumber } from '../lib/format'
import { METADATA_KINDS, METADATA_KIND_LABELS, metadataKindInfo, validateMetadataFile } from '../lib/uploads'
import { replaceOp } from '../state/reducer'
import { usePatchUi } from '../state/store'
import type { MetadataKind, MetadataStatus, MetadataUploadResult } from '../types/api'

function MetadataResult({ result }: { result: MetadataUploadResult }) {
  const patchUi = usePatchUi()
  const accepted = result.status === 'accepted'
  const label = METADATA_KIND_LABELS[result.kind] ?? result.kind
  return (
    <section className={`result-box ${accepted ? 'ok' : 'danger'}`} aria-labelledby="metadata-result-title">
      <header className="block-head">
        <h5 id="metadata-result-title">{label} 업로드 결과</h5>
        <span className={`badge ${accepted ? 'ok' : 'danger'}`}>{accepted ? '반영됨' : '거부됨'}</span>
      </header>
      <dl className="kv">
        <dt>행 수</dt>
        <dd>{formatNumber(result.rows)}</dd>
        {result.columns.length > 0 && (
          <>
            <dt>열</dt>
            <dd>{result.columns.join(', ')}</dd>
          </>
        )}
        {result.id && (
          <>
            <dt>ID</dt>
            <dd>{result.id}</dd>
          </>
        )}
        {result.backup && (
          <>
            <dt>이전 파일</dt>
            <dd>
              <code>{result.backup}</code>
            </dd>
          </>
        )}
      </dl>
      {result.errors.length > 0 && (
        <ul className="issue-list" aria-label="오류">
          {result.errors.map((e) => (
            <li key={e} className="issue error">
              <strong>오류</strong> {e}
            </li>
          ))}
        </ul>
      )}
      {result.warnings.length > 0 && (
        <ul className="issue-list" aria-label="경고">
          {result.warnings.map((w) => (
            <li key={w} className="issue warning">
              <strong>경고</strong> {w}
            </li>
          ))}
        </ul>
      )}
      {!accepted && <p className="muted small">오류가 있어 받지 않았습니다. 기존 데이터는 바뀌지 않았습니다.</p>}
      {accepted && result.kind === 'relayout_brief' && result.id && (
        <button type="button" onClick={() => patchUi([replaceOp('/panel', { type: 'layout', payload: { brief_id: result.id } })])}>
          시즌 개편에서 열기
        </button>
      )}
    </section>
  )
}

function StatusTable({ status }: { status: MetadataStatus[] }) {
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">종류별 메타데이터 현재 상태</caption>
        <thead>
          <tr>
            <th scope="col">종류</th>
            <th scope="col">상태</th>
            <th scope="col" className="nowrap">
              행 수
            </th>
            <th scope="col">갱신</th>
            <th scope="col">항목</th>
          </tr>
        </thead>
        <tbody>
          {status.map((s) => (
            <tr key={s.kind}>
              <td title={s.required_columns.length ? `필수: ${s.required_columns.join(', ')}` : undefined}>{METADATA_KIND_LABELS[s.kind] ?? s.kind}</td>
              <td>
                <span className={`badge ${s.present ? 'ok' : 'warn'}`}>{s.present ? '있음' : '없음'}</span>
              </td>
              <td>{formatNumber(s.rows)}</td>
              <td className="nowrap">{formatDateTime(s.updated_at)}</td>
              <td>{s.items.join(', ') || '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function MetadataUploads() {
  const { source } = useData()
  const fileInput = useRef<HTMLInputElement>(null)
  const [kind, setKind] = useState<MetadataKind>('brands')
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<MetadataUploadResult | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const [status, setStatus] = useState<{ data: MetadataStatus[] | null; error: string | null }>({ data: null, error: null })
  const [statusSeq, setStatusSeq] = useState(0)
  const info = metadataKindInfo(kind) ?? METADATA_KINDS[0]

  useEffect(() => {
    let cancelled = false
    source.getMetadataStatus().then(
      (data) => !cancelled && setStatus({ data, error: null }),
      (e: unknown) => !cancelled && setStatus((prev) => ({ data: prev.data, error: errorText(e) })),
    )
    return () => {
      cancelled = true
    }
  }, [source, statusSeq])

  const clearFile = () => {
    setFile(null)
    if (fileInput.current) fileInput.current.value = ''
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const invalid = validateMetadataFile(kind, file)
    setError(invalid)
    if (invalid || !file) {
      fileInput.current?.focus()
      return
    }
    setBusy(true)
    setAnnouncement('')
    try {
      const uploaded = await source.uploadMetadata(kind, file)
      setResult(uploaded)
      setStatusSeq((n) => n + 1)
      const label = METADATA_KIND_LABELS[uploaded.kind] ?? uploaded.kind
      if (uploaded.status === 'accepted') {
        setAnnouncement(`${label} 파일을 반영했습니다.`)
        if (uploaded.kind === 'relayout_brief') invalidateCache(cacheKeys.relayoutBriefs(source.mode))
        if (uploaded.kind === 'drawing_profile') invalidateCache(cacheKeys.uploadProfiles(source.mode))
        clearFile()
      } else {
        setAnnouncement(`${label} 파일을 받지 않았습니다. 오류 ${uploaded.errors.length}건`)
      }
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="metadata-uploads">
      <form className="upload-form" noValidate onSubmit={(e) => void submit(e)} aria-labelledby="metadata-upload-title">
        <h4 id="metadata-upload-title">메타데이터 올리기</h4>
        <div className="form-field">
          <label htmlFor="metadata-kind">종류</label>
          <select
            id="metadata-kind"
            value={kind}
            aria-describedby="metadata-kind-hint"
            onChange={(e) => {
              setKind(e.target.value as MetadataKind)
              setError(null)
              clearFile()
            }}
          >
            {METADATA_KINDS.map((k) => (
              <option key={k.kind} value={k.kind}>
                {k.label} ({k.kind})
              </option>
            ))}
          </select>
          <p id="metadata-kind-hint" className="hint">
            {info.formats} · 필수 {info.required.join(', ')}
            {info.optional.length ? ` (선택: ${info.optional.join(', ')})` : ''} · {info.storage}
          </p>
        </div>
        <div className="form-field">
          <label htmlFor="metadata-file">파일</label>
          <input
            ref={fileInput}
            id="metadata-file"
            type="file"
            accept={info.accept}
            required
            aria-invalid={!!error}
            aria-describedby={error ? 'metadata-file-error' : undefined}
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null)
              setError(null)
            }}
          />
          {error && (
            <p id="metadata-file-error" className="error-text" role="alert">
              {error}
            </p>
          )}
        </div>
        <button type="submit" className="primary" disabled={busy}>
          {busy ? '올리는 중…' : '올리기'}
        </button>
      </form>
      <p className="visually-hidden" role="status">
        {announcement}
      </p>
      {result && <MetadataResult result={result} />}

      <div className="status-block">
        <div className="block-head">
          <h4 id="metadata-status-title">현재 메타데이터</h4>
          <button type="button" onClick={() => setStatusSeq((n) => n + 1)}>
            새로 고침
          </button>
        </div>
        {status.error && <p className="error-text">상태를 불러오지 못했습니다: {status.error}</p>}
        {!status.data && !status.error && <p className="muted">상태를 불러오는 중…</p>}
        {status.data && <StatusTable status={status.data} />}
      </div>
    </div>
  )
}
