import { useData } from '../api/source'
import './chatState.css'

/**
 * Non-blocking warning near the actions that save something (uploads, decisions, reviews, new alternatives):
 * `/api/meta` says the server cannot save changes right now (`writes_available: false`), so they may fail or get lost.
 */
export function WritesNotice() {
  const { meta } = useData()
  if (meta.writes_available !== false) return null
  return (
    <p className="notice warn writes-warning" role="status">
      <strong>지금은 서버가 변경을 저장하지 못해요.</strong> 올리기·결정·대안 만들기·승인은 실패하거나 저장되지 않을 수 있어요. 잠시 뒤 다시 시도해 주세요.
    </p>
  )
}
