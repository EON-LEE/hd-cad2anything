import './chatState.css'

/** `?mock=1` on the current page: how a developer opens the built-in mock data without a backend. */
function mockHref(): string {
  if (typeof window === 'undefined') return '?mock=1'
  const url = new URL(window.location.href)
  url.searchParams.set('mock', '1')
  return `${url.pathname}${url.search}`
}

/**
 * Blocking start-up error: the server did not answer /api/meta. A production build never shows made-up data in its
 * place; a developer build offers the mock data as an explicit choice (`allowMock`, default: Vite dev server).
 */
export function BootError({
  message,
  onRetry,
  retrying = false,
  allowMock = import.meta.env.DEV,
}: {
  message: string
  onRetry: () => void
  retrying?: boolean
  allowMock?: boolean
}) {
  return (
    <div className="boot error boot-error" role="alert">
      <div className="boot-error-card">
        <h1>서버에 연결하지 못했어요</h1>
        <p>Atlas 서버가 응답하지 않아 화면을 열 수 없어요. 서버가 켜져 있는지 확인한 뒤 다시 시도해 주세요.</p>
        <div className="boot-error-actions">
          <button type="button" className="primary" disabled={retrying} onClick={onRetry}>
            {retrying ? '연결하는 중…' : '다시 시도'}
          </button>
          {allowMock && (
            <a className="button-link" href={mockHref()}>
              모의 데이터로 보기
            </a>
          )}
        </div>
        <details>
          <summary>자세히 보기</summary>
          <p className="boot-error-detail">{message}</p>
        </details>
      </div>
    </div>
  )
}
