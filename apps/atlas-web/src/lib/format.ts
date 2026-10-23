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

/** Korean floor name from a floor code: "3F" -> "3층", "B1" -> "지하 1층". Unknown codes are returned as is. */
export function floorLabel(code: string | null | undefined): string {
  if (!code) return ''
  const upper = code.trim().toUpperCase()
  const basement = /^B(\d+)F?$/.exec(upper)
  if (basement) return `지하 ${Number(basement[1])}층`
  const above = /^(\d+)F$/.exec(upper)
  if (above) return `${Number(above[1])}층`
  if (upper === 'RF') return '옥상'
  return code
}

/** Particle "으로"/"로" after a word (Hangul final consonant rule; "(으)로" when unknown). */
export function withEuro(word: string): string {
  const last = word.trim().at(-1)
  const code = last ? last.charCodeAt(0) : 0
  if (code >= 0xac00 && code <= 0xd7a3) {
    const final = (code - 0xac00) % 28
    return `${word}${final === 0 || final === 8 ? '로' : '으로'}`
  }
  return `${word}(으)로`
}

/** 2.4만원 below 10만원, 51만원 up to 1억, 1.2억원 above; small amounts in 원. */
export function formatManwon(krw: number | null | undefined): string {
  if (krw === null || krw === undefined || !Number.isFinite(krw)) return '–'
  const sign = krw < 0 ? '−' : ''
  const abs = Math.abs(krw)
  const trim = (value: string) => value.replace(/\.0$/, '')
  if (abs >= 1e8) return `${sign}${trim((abs / 1e8).toFixed(1))}억원`
  if (abs >= 1e5) return `${sign}${numberFormat.format(Math.round(abs / 1e4))}만원`
  if (abs >= 1e4) return `${sign}${trim((abs / 1e4).toFixed(1))}만원`
  return `${sign}${numberFormat.format(Math.round(abs))}원`
}

/** 185 -> "3시간 5분", 45 -> "45분", 120 -> "2시간". */
export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) return '–'
  const total = Math.max(0, Math.round(minutes))
  const h = Math.floor(total / 60)
  const m = total % 60
  if (h === 0) return `${m}분`
  return m === 0 ? `${h}시간` : `${h}시간 ${m}분`
}

/** 640 -> "640m", 1250 -> "1.3km". */
export function formatWalk(meters: number | null | undefined): string {
  if (meters === null || meters === undefined || !Number.isFinite(meters)) return '–'
  if (meters < 1000) return `${Math.round(meters)}m`
  return `${(meters / 1000).toFixed(1).replace(/\.0$/, '')}km`
}

/** 3.2 -> "3.2%", signed: "+3.2%" / "−1.5%". */
export function formatPercent(value: number | null | undefined, signed = false, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '–'
  const text = formatNumber(Math.abs(value), digits).replace(/\.0+$/, '')
  const sign = value < 0 ? '−' : signed && value > 0 ? '+' : ''
  return `${sign}${text}%`
}

/** ISO date-time -> "2026-10-23 10:00" (local time); other strings are returned as is. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '–'
  const date = new Date(value)
  if (!/T\d{2}:\d{2}/.test(value) || Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export const REVISION_KIND_LABELS: Record<string, string> = {
  actual: '실제',
  hypothetical: '가상(what-if)',
}

/**
 * Markers of a revision in selectors: "업로드" for a model made from uploaded drawings and
 * "실적 없음" for an actual revision whose period has no sales (hypothetical ones already say 가상).
 */
export function revisionMarkers(revision: { kind: string; has_sales?: boolean; model_source?: string }): string[] {
  return [
    ...(revision.model_source === 'uploaded' ? ['업로드'] : []),
    ...(revision.kind === 'actual' && revision.has_sales === false ? ['실적 없음'] : []),
  ]
}

/** Efficiency percentile (0..100, low = inefficient) as "하위 2%"; the lowest unit reads "하위 1%". */
export function bottomPercentText(percentile: number | null | undefined): string {
  if (percentile === null || percentile === undefined || !Number.isFinite(percentile)) return '–'
  return `하위 ${Math.min(100, Math.max(1, Math.round(percentile)))}%`
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
