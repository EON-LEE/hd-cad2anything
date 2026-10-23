import { describe, expect, it } from 'vitest'
import type { RevisionInfo } from '../types/api'
import {
  MAX_FILE_BYTES,
  QA_CODE_LABELS,
  floorFromFileName,
  formatBytes,
  isJobSettled,
  jobHasRevision,
  normalizeUploadJob,
  validateDrawingUpload,
  validateMetadataFile,
} from './uploads'

const file = (name: string, size = 1000) => ({ name, size })
const MB = 1024 * 1024

describe('floorFromFileName', () => {
  it('reads the floor code the contract allows in drawing names', () => {
    expect(floorFromFileName('R3_4F.dxf')).toBe('4F')
    expect(floorFromFileName('6F.dxf')).toBe('6F')
    expect(floorFromFileName('B1.dxf')).toBe('B1')
    expect(floorFromFileName('R3-b2f.DXF')).toBe('B2')
    expect(floorFromFileName('4층.dxf')).toBe('4F')
    expect(floorFromFileName('지하1층 식품관.dxf')).toBe('B1')
    expect(floorFromFileName('R3_04F_rev2.dxf')).toBe('4F')
    expect(floorFromFileName('plan.dxf')).toBeNull()
    expect(floorFromFileName('R34F.dxf')).toBeNull()
  })
})

describe('validateDrawingUpload', () => {
  const ok = { files: [file('R3_4F.dxf'), file('R3_5F.dxf')], revId: 'R3', effectiveDate: '2026-10-01' }

  it('accepts DXF files per floor or one ZIP', () => {
    expect(validateDrawingUpload(ok)).toEqual([])
    expect(validateDrawingUpload({ ...ok, files: [file('R3.zip')] })).toEqual([])
    expect(validateDrawingUpload({ ...ok, revId: 'FW2026-A_1' })).toEqual([])
  })

  it('mirrors the backend 400 rules: files, floor codes, sizes, rev_id and date', () => {
    const messages = (input: Parameters<typeof validateDrawingUpload>[0]) => validateDrawingUpload(input).map((e) => `${e.field}: ${e.message}`)
    expect(messages({ ...ok, files: [] })).toEqual(['files: 도면 파일을 하나 이상 고르세요.'])
    expect(messages({ ...ok, files: [file('R3_4F.pdf')] })[0]).toContain('DXF 또는 ZIP 파일만')
    expect(messages({ ...ok, files: [file('a.zip'), file('R3_4F.dxf')] })[0]).toContain('ZIP 파일은 하나만')
    expect(messages({ ...ok, files: [file('plan.dxf')] })[0]).toContain('파일 이름에 층 코드가 없습니다: plan.dxf')
    expect(messages({ ...ok, files: [file('R3_4F.dxf'), file('4층.dxf')] })).toEqual(['files: 같은 층(4F) 파일이 둘 이상입니다: R3_4F.dxf, 4층.dxf'])
    expect(messages({ ...ok, files: [file('R3_4F.dxf', MAX_FILE_BYTES + 1)] })[0]).toContain('30MB까지')
    expect(messages({ ...ok, files: [1, 2, 3, 4].map((n) => file(`${n}F.dxf`, 26 * MB)) })).toEqual(['files: 전체 크기는 100MB까지입니다 (지금 104MB).'])
    expect(messages({ ...ok, revId: '' })).toEqual(['rev_id: 새 개정 ID를 입력하세요.'])
    expect(messages({ ...ok, revId: 'r3' })).toEqual(['rev_id: 개정 ID는 영문 대문자·숫자·-·_로 1~16자여야 합니다.'])
    expect(messages({ ...ok, revId: 'R3.1' })).toHaveLength(1)
    expect(messages({ ...ok, revId: 'A'.repeat(17) })).toHaveLength(1)
    expect(messages({ ...ok, effectiveDate: '' })).toEqual(['effective_date: 적용일을 입력하세요.'])
    expect(messages({ ...ok, effectiveDate: '2026-02-30' })).toEqual(['effective_date: 적용일은 YYYY-MM-DD 형식의 날짜여야 합니다.'])
  })

  it('blocks reusing an existing revision ID unless an upload made it (409)', () => {
    const revisions: RevisionInfo[] = [
      { rev_id: 'R2', kind: 'actual', parent_rev_id: 'R1', effective_date: '2025-09-01', model_source: 'parsed' },
      { rev_id: 'R3', kind: 'actual', parent_rev_id: 'R2', effective_date: '2026-10-01', model_source: 'uploaded' },
    ]
    expect(validateDrawingUpload({ ...ok, revId: 'R2', revisions })[0]).toMatchObject({ field: 'rev_id', message: expect.stringContaining('이미 있는 개정') })
    expect(validateDrawingUpload({ ...ok, revId: 'R3', revisions })).toEqual([])
  })
})

describe('validateMetadataFile', () => {
  it('checks the file type of each metadata kind', () => {
    expect(validateMetadataFile('brands', null)).toBe('올릴 파일을 고르세요.')
    expect(validateMetadataFile('brands', { name: 'brands.csv' })).toBeNull()
    expect(validateMetadataFile('leases', { name: 'leases.XLSX' })).toBeNull()
    expect(validateMetadataFile('brands', { name: 'brands.yaml' })).toBe('브랜드 종류는 CSV·XLSX 파일만 올릴 수 있습니다.')
    expect(validateMetadataFile('relayout_brief', { name: 'FW2026.yml' })).toBeNull()
    expect(validateMetadataFile('relayout_brief', { name: 'FW2026.csv' })).toBe('개편 브리프 종류는 YAML·JSON 파일만 올릴 수 있습니다.')
    expect(validateMetadataFile('unknown', { name: 'x.csv' })).toBe('메타데이터 종류를 고르세요.')
  })
})

describe('upload jobs', () => {
  it('normalizes a job and labels its steps', () => {
    const job = normalizeUploadJob({
      job_id: 'J1',
      status: 'running',
      steps: [{ name: 'merge', status: 'running' }, { name: 'custom', label: '사용자 단계', status: 'pending' }],
      changes: { total: '7', by_type: { move: 3, bad: 'x' } },
      relayout_check: { brief_id: 'FW2026', conformance_pct: 75 },
      error: { message: '파싱 실패' },
    })
    expect(job.steps).toEqual([
      { name: 'merge', label: '층 합치기', status: 'running', message: null },
      { name: 'custom', label: '사용자 단계', status: 'pending', message: null },
    ])
    expect(job.changes).toEqual({ total: 7, by_type: { move: 3 } })
    expect(job.relayout_check).toEqual({ brief_id: 'FW2026', alt_id: null, conformance_pct: 75, summary: '' })
    expect(job).toMatchObject({ kind: 'drawing', files: [], parse: null, qa: null, impact: [], error: '파싱 실패' })
    expect(() => normalizeUploadJob({ status: 'done' })).toThrow()
  })

  it('counts lease QA issues by severity, preferring the totals the backend sends', () => {
    const issues = [
      { severity: 'warning', code: 'area_mismatch', unit_code: '4F-C06', message: '면적' },
      { severity: 'info', code: 'unit_without_lease', unit_code: '4F-E02', message: '계약 없음' },
      { severity: 'info', code: 'unit_without_lease', unit_code: '4F-E03', message: '계약 없음' },
      { code: 'brand_mismatch', message: '브랜드' },
    ]
    expect(normalizeUploadJob({ job_id: 'J1', qa: { issues } }).qa).toMatchObject({ errors: 0, warnings: 2, infos: 2 })
    // Issues are capped at 50 by the backend; its counts cover all of them.
    const capped = normalizeUploadJob({ job_id: 'J2', qa: { errors: 1, warnings: 4, infos: 28, issues } }).qa
    expect(capped).toMatchObject({ errors: 1, warnings: 4, infos: 28 })
    expect(capped?.issues[3]).toEqual({ severity: 'warning', code: 'brand_mismatch', unit_code: null, message: '브랜드' })
    expect(['unit_missing_in_drawing', 'unit_without_lease', 'brand_mismatch', 'area_mismatch'].map((code) => QA_CODE_LABELS[code])).toEqual([
      '계약 매장 누락',
      '계약 없는 매장',
      '브랜드 불일치',
      '면적 불일치',
    ])
  })

  it('knows which states stop polling and which produced a revision', () => {
    expect(['queued', 'running'].map(isJobSettled)).toEqual([false, false])
    expect(['done', 'failed', 'needs_review'].map(isJobSettled)).toEqual([true, true, true])
    expect(['done', 'needs_review', 'failed'].map(jobHasRevision)).toEqual([true, true, false])
    expect([381220, 31 * MB, 512].map(formatBytes)).toEqual(['372KB', '31MB', '512B'])
  })
})
