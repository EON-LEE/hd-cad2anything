import { replaceOp } from '../state/reducer'
import { useAppState, usePatchUi } from '../state/store'
import { heatmapActive } from '../state/uiState'

interface Props {
  onZoomIn: () => void
  onZoomOut: () => void
  onFit: () => void
}

const LAYER_LABELS: [key: 'fixtures' | 'walkways' | 'labels', label: string][] = [
  ['fixtures', '집기'],
  ['walkways', '통로'],
  ['labels', '라벨'],
]

export function LayerToolbar({ onZoomIn, onZoomOut, onFit }: Props) {
  const { ui } = useAppState()
  const patchUi = usePatchUi()
  const hasOverlays = ui.highlights.length > 0 || ui.edges.length > 0 || !!ui.focus

  return (
    <div className="layer-toolbar" role="toolbar" aria-label="도면 표시 도구">
      <fieldset className="toggle-group">
        <legend className="visually-hidden">레이어</legend>
        {LAYER_LABELS.map(([key, label]) => (
          <label key={key} className="toggle">
            <input
              type="checkbox"
              checked={ui.layers[key]}
              onChange={(e) => patchUi([replaceOp(`/layers/${key}`, e.target.checked)])}
            />
            {label}
          </label>
        ))}
      </fieldset>
      <div className="button-group">
        <button type="button" onClick={onZoomIn} aria-label="확대">
          +
        </button>
        <button type="button" onClick={onZoomOut} aria-label="축소">
          −
        </button>
        <button type="button" onClick={onFit} aria-label="도면 전체 보기">
          전체
        </button>
      </div>
      {heatmapActive(ui) && (
        <button type="button" onClick={() => patchUi([replaceOp('/heatmap', null)])}>
          heatmap 끄기
        </button>
      )}
      {hasOverlays && (
        <button
          type="button"
          onClick={() => patchUi([replaceOp('/highlights', []), replaceOp('/edges', []), replaceOp('/focus', null)])}
        >
          강조·연결 지우기
        </button>
      )}
    </div>
  )
}
