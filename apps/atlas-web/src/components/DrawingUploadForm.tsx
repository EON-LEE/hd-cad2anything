// Drawing upload form (POST /api/uploads/drawings) with the contract's checks done before sending.
import { useRef, useState, type FormEvent } from 'react'
import { errorText } from '../api/client'
import { useData } from '../api/source'
import { REVISION_KIND_LABELS, revisionMarkers } from '../lib/format'
import { floorFromFileName, formatBytes, validateDrawingUpload, type DrawingField, type FieldError } from '../lib/uploads'
import { pickDefaultRevision } from '../state/uiState'
import type { DrawingProfile } from '../types/api'

const FIELD_IDS: Record<DrawingField, string> = {
  files: 'upload-files',
  rev_id: 'upload-rev-id',
  effective_date: 'upload-effective-date',
}

export function DrawingUploadForm({
  profiles,
  profilesError,
  onCreated,
}: {
  profiles: DrawingProfile[] | undefined
  profilesError: string | null
  onCreated: (jobId: string) => void
}) {
  const { source, meta } = useData()
  const fileInput = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [revId, setRevId] = useState('')
  const [parentChoice, setParentChoice] = useState<string | null>(null)
  const [effectiveDate, setEffectiveDate] = useState('')
  const [units, setUnits] = useState<'' | 'm' | 'mm'>('')
  const [profile, setProfile] = useState('default')
  const [runGenerators, setRunGenerators] = useState(true)
  const [errors, setErrors] = useState<FieldError[]>([])
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const parents = meta.revisions.filter((r) => r.kind !== 'hypothetical')
  const defaultParent = pickDefaultRevision(meta)
  const parent =
    parentChoice && parents.some((r) => r.rev_id === parentChoice)
      ? parentChoice
      : parents.some((r) => r.rev_id === defaultParent)
        ? defaultParent
        : (parents.at(-1)?.rev_id ?? '')
  const profileList = profiles?.length ? profiles : [{ profile_id: 'default', title: '기본 도면 규약', description: null, units: null }]
  const selectedProfile = profileList.find((p) => p.profile_id === profile) ?? profileList[0]
  const existing = meta.revisions.find((r) => r.rev_id === revId.trim())
  const fieldErrors = (field: DrawingField) => errors.filter((e) => e.field === field)

  const describedBy = (field: DrawingField, hint?: string) =>
    [hint, fieldErrors(field).length ? `${FIELD_IDS[field]}-error` : null].filter(Boolean).join(' ') || undefined

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const found = validateDrawingUpload({ files, revId, effectiveDate, revisions: meta.revisions })
    setErrors(found)
    setSubmitError(null)
    if (found.length) {
      document.getElementById(FIELD_IDS[found[0].field])?.focus()
      return
    }
    setBusy(true)
    try {
      const created = await source.uploadDrawings({
        files,
        rev_id: revId.trim(),
        parent_rev: parent || null,
        effective_date: effectiveDate,
        units: units || null,
        profile: selectedProfile.profile_id,
        run_generators: runGenerators,
      })
      setFiles([])
      if (fileInput.current) fileInput.current.value = ''
      onCreated(created.job_id)
    } catch (error) {
      setSubmitError(errorText(error))
    } finally {
      setBusy(false)
    }
  }

  const errorList = (field: DrawingField) =>
    fieldErrors(field).length > 0 && (
      <ul id={`${FIELD_IDS[field]}-error`} className="field-errors">
        {fieldErrors(field).map((e) => (
          <li key={e.message}>{e.message}</li>
        ))}
      </ul>
    )

  return (
    <form className="upload-form" noValidate onSubmit={(e) => void submit(e)} aria-labelledby="drawing-upload-title">
      <h4 id="drawing-upload-title">새 도면 올리기</h4>
      <div className="form-field">
        <label htmlFor={FIELD_IDS.files}>도면 파일</label>
        <input
          ref={fileInput}
          id={FIELD_IDS.files}
          type="file"
          multiple
          accept=".dxf,.zip"
          required
          aria-invalid={fieldErrors('files').length > 0}
          aria-describedby={describedBy('files', 'upload-files-hint')}
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
        />
        <p id="upload-files-hint" className="hint">
          층마다 DXF 하나(파일 이름에 층 코드, 예: R3_4F.dxf·6F.dxf·B1.dxf·4층.dxf) 또는 ZIP 하나. 파일 하나 30MB, 전체 100MB까지 올릴 수 있습니다.
        </p>
        {files.length > 0 && (
          <ul className="file-picks" aria-label="고른 파일">
            {files.map((f) => (
              <li key={`${f.name}-${f.size}`}>
                {f.name} <span className="muted">· {f.name.toLowerCase().endsWith('.zip') ? 'ZIP' : (floorFromFileName(f.name) ?? '층 코드 없음')} · {formatBytes(f.size)}</span>
              </li>
            ))}
          </ul>
        )}
        {errorList('files')}
      </div>

      <div className="form-grid">
        <div className="form-field">
          <label htmlFor={FIELD_IDS.rev_id}>새 개정 ID</label>
          <input
            id={FIELD_IDS.rev_id}
            type="text"
            value={revId}
            required
            maxLength={16}
            autoComplete="off"
            spellCheck={false}
            placeholder="예: R3"
            pattern="[A-Z0-9_\-]{1,16}"
            aria-invalid={fieldErrors('rev_id').length > 0}
            aria-describedby={describedBy('rev_id', 'upload-rev-hint')}
            onChange={(e) => setRevId(e.target.value.toUpperCase())}
          />
          <p id="upload-rev-hint" className="hint">
            영문 대문자·숫자·-·_ 1~16자
            {existing?.model_source === 'uploaded' ? ` · 업로드한 ${existing.rev_id}를 새 도면으로 바꿉니다.` : ''}
          </p>
          {errorList('rev_id')}
        </div>
        <div className="form-field">
          <label htmlFor={FIELD_IDS.effective_date}>적용일</label>
          <input
            id={FIELD_IDS.effective_date}
            type="date"
            value={effectiveDate}
            required
            aria-invalid={fieldErrors('effective_date').length > 0}
            aria-describedby={describedBy('effective_date')}
            onChange={(e) => setEffectiveDate(e.target.value)}
          />
          {errorList('effective_date')}
        </div>
        <div className="form-field">
          <label htmlFor="upload-parent">부모 개정</label>
          <select id="upload-parent" value={parent} aria-describedby="upload-parent-hint" onChange={(e) => setParentChoice(e.target.value)}>
            {parents.map((r) => (
              <option key={r.rev_id} value={r.rev_id}>
                {r.rev_id} · {REVISION_KIND_LABELS[r.kind] ?? r.kind}
                {r.effective_date ? ` · ${r.effective_date}` : ''}
                {revisionMarkers(r).map((marker) => ` · ${marker}`).join('')}
              </option>
            ))}
          </select>
          <p id="upload-parent-hint" className="hint">
            올리지 않은 층은 부모 개정에서 가져옵니다.
          </p>
        </div>
        <div className="form-field">
          <label htmlFor="upload-units">단위</label>
          <select id="upload-units" value={units} onChange={(e) => setUnits(e.target.value === 'm' || e.target.value === 'mm' ? e.target.value : '')}>
            <option value="">프로필 기본값{selectedProfile.units ? ` (${selectedProfile.units})` : ''}</option>
            <option value="m">m</option>
            <option value="mm">mm</option>
          </select>
        </div>
        <div className="form-field">
          <label htmlFor="upload-profile">도면 규약 프로필</label>
          <select id="upload-profile" value={selectedProfile.profile_id} aria-describedby="upload-profile-hint" onChange={(e) => setProfile(e.target.value)}>
            {profileList.map((p) => (
              <option key={p.profile_id} value={p.profile_id}>
                {p.title} ({p.profile_id})
              </option>
            ))}
          </select>
          <p id="upload-profile-hint" className="hint">
            {profilesError ? `프로필 목록을 불러오지 못해 기본 프로필만 보여 드립니다: ${profilesError}` : (selectedProfile.description ?? '')}
          </p>
        </div>
      </div>
      <label className="toggle">
        <input type="checkbox" checked={runGenerators} onChange={(e) => setRunGenerators(e.target.checked)} />
        산출물 재생성
      </label>
      {errors.length > 0 && (
        <p className="error-text" role="alert">
          입력 내용을 확인해 주세요 ({errors.length}건).
        </p>
      )}
      {submitError && (
        <p className="error-text" role="alert">
          도면을 올리지 못했습니다: {submitError}
        </p>
      )}
      <button type="submit" className="primary" disabled={busy}>
        {busy ? '올리는 중…' : '올리기'}
      </button>
    </form>
  )
}
