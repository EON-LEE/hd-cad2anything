import { createContext, useContext } from 'react'
import type { Meta } from '../types/api'
import { createHttpSource, normalizeMeta, type AtlasDataSource } from './client'

export interface DataContextValue {
  source: AtlasDataSource
  meta: Meta
  /** Why mock mode is active (null in live mode). */
  mockReason: string | null
  /**
   * Reload `/api/meta` (e.g. after an upload created a revision) and publish it to the app.
   * Provided by <AppProviders>; absent on hand-built context values.
   */
  refreshMeta?: () => Promise<Meta>
}

export const DataContext = createContext<DataContextValue | null>(null)

export function useData(): DataContextValue {
  const value = useContext(DataContext)
  if (!value) throw new Error('useData must be used inside <DataContext>')
  return value
}

/** Mock mode is forced by VITE_MOCK=1 (build/dev env) or `?mock=1` in the page URL. */
export function mockRequested(): boolean {
  const env = String(import.meta.env.VITE_MOCK ?? '').toLowerCase()
  if (env === '1' || env === 'true') return true
  if (typeof window !== 'undefined') {
    const query = new URLSearchParams(window.location.search).get('mock')
    if (query === '1' || query === 'true') return true
  }
  return false
}

/**
 * Whether mock data may take the place of a backend that does not answer: only in mock mode (`vite --mode mock`,
 * VITE_MOCK=1 or `?mock=1`). A production build never shows made-up data in place of the real data.
 */
export function mockFallbackAllowed(): boolean {
  return import.meta.env.MODE === 'mock' || mockRequested()
}

export async function createMockContext(reason: string): Promise<DataContextValue> {
  const { createMockSource } = await import('../mock/mockSource')
  const source = createMockSource()
  return { source, meta: normalizeMeta(await source.getMeta()), mockReason: reason }
}

/** `/api/meta` could not be loaded and mock data was not asked for: the app cannot start (see <BootError>). */
export class BackendUnavailableError extends Error {
  /** Why the request failed, for the "자세히 보기" text. */
  readonly detail: string

  constructor(detail: string) {
    super(`서버에 연결하지 못했어요 (${detail})`)
    this.name = 'BackendUnavailableError'
    this.detail = detail
  }
}

function failureDetail(error: unknown): string {
  if (typeof DOMException !== 'undefined' && error instanceof DOMException && (error.name === 'TimeoutError' || error.name === 'AbortError')) {
    return '8초 안에 응답하지 않았어요.'
  }
  return error instanceof Error ? error.message : String(error)
}

/**
 * Use the live backend when /api/meta answers. When it does not, mock fixtures stand in only in mock mode
 * (see `mockFallbackAllowed`); otherwise this rejects with a `BackendUnavailableError`.
 */
export async function resolveDataSource(): Promise<DataContextValue> {
  if (mockRequested()) return createMockContext('VITE_MOCK=1 또는 ?mock=1 설정으로 내장 모의 데이터를 사용합니다.')
  const http = createHttpSource()
  try {
    const meta = normalizeMeta(await http.getMeta())
    return { source: http, meta, mockReason: null }
  } catch (error) {
    const detail = failureDetail(error)
    if (mockFallbackAllowed()) return createMockContext(`백엔드 /api/meta에 연결하지 못해 내장 모의 데이터를 사용합니다. (${detail})`)
    throw new BackendUnavailableError(detail)
  }
}
