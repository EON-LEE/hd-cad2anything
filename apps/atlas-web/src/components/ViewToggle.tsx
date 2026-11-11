import { useAppState } from '../state/store'

/** Which pane fills the screen on the stacked phone layout (R1). */
export type MobileView = 'map' | 'side'

/**
 * Sticky segmented control shown only on the stacked layout (<= 900px, via CSS). It swaps the map and
 * the side panel so the chat composer is reachable in one tap instead of far down the page (R1).
 */
export function ViewToggle({ value, onChange }: { value: MobileView; onChange: (view: MobileView) => void }) {
  const { chat } = useAppState()
  const busyOnSide = chat.running && value !== 'side'
  return (
    <div className="view-toggle" role="group" aria-label="화면 전환">
      <button
        type="button"
        className={`view-toggle-btn${value === 'map' ? ' active' : ''}`}
        aria-pressed={value === 'map'}
        onClick={() => onChange('map')}
      >
        지도
      </button>
      <button
        type="button"
        className={`view-toggle-btn${value === 'side' ? ' active' : ''}`}
        aria-pressed={value === 'side'}
        onClick={() => onChange('side')}
      >
        대화·패널
        {busyOnSide && (
          <>
            <span className="view-toggle-dot" aria-hidden="true" />
            <span className="visually-hidden">(응답을 작성하는 중)</span>
          </>
        )}
      </button>
    </div>
  )
}
