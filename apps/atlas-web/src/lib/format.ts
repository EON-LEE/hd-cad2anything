// Korean display formatting helpers.

export const M2_PER_PYEONG = 3.305785

const numberFormat = new Intl.NumberFormat('ko-KR')

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '–'
  return new Intl.NumberFormat('ko-KR', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value)
}

/** 1.2억 원 / 3,400만 원 / 9,800원. `signed` adds +/- for deltas. */
export function formatKrw(value: number | null | undefined, signed = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '–'
  const sign = value < 0 ? '−' : signed && value > 0 ? '+' : ''
  const abs = Math.abs(value)
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(abs >= 1e10 ? 0 : 1)}억 원`
  if (abs >= 1e4) return `${sign}${numberFormat.format(Math.round(abs / 1e4))}만 원`
  return `${sign}${numberFormat.format(Math.round(abs))}원`
}

export function formatArea(m2: number | null | undefined): string {
  if (m2 === null || m2 === undefined || Number.isNaN(m2)) return '–'
  return `${formatNumber(m2, 1)}㎡ (${formatNumber(m2 / M2_PER_PYEONG, 1)}평)`
}

/** Percentile may arrive as 0..1 or 0..100; display as 0..100. */
export function formatPercentile(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '–'
  const pct = value <= 1 ? value * 100 : value
  return `${Math.round(pct)}백분위`
}

export const REVISION_KIND_LABELS: Record<string, string> = {
  actual: '실제',
  hypothetical: '가상(what-if)',
}

export const RISK_LABELS: Record<string, string> = { high: '고위험', medium: '중위험', low: '저위험' }

export const APPROVAL_MODE_LABELS: Record<string, string> = {
  pre_publish: '게시 전 승인',
  post_publish: '게시 후 검토',
}

export const ARTIFACT_STATUS_LABELS: Record<string, string> = {
  awaiting_approval: '승인 대기',
  published: '게시됨',
  rejected: '반려됨',
  qa_failed: 'QA 실패',
  pending: '대기',
  running: '생성 중',
}

export const LINK_TYPE_LABELS: Record<string, string> = {
  escalator: '에스컬레이터',
  elevator: '엘리베이터',
  stair: '계단',
}

export const AMENITY_LABELS: Record<string, string> = {
  RESTROOM: '화장실',
  INFO: '안내데스크',
  ATM: 'ATM',
  NURSING: '수유실',
  LOUNGE: '고객라운지',
}

/** Human-readable summary of an artifact `summary` value (object, string or scalar). */
export function summarizeValue(value: unknown, maxEntries = 6): string[] {
  if (value === null || value === undefined || value === '') return []
  if (typeof value === 'string') return [value]
  if (typeof value !== 'object') return [String(value)]
  if (Array.isArray(value)) return value.slice(0, maxEntries).map((v) => (typeof v === 'string' ? v : JSON.stringify(v)))
  return Object.entries(value as Record<string, unknown>)
    .slice(0, maxEntries)
    .map(([k, v]) => `${k}: ${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`)
}
