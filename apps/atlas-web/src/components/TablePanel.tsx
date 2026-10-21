import { replaceOp } from '../state/reducer'
import { usePatchUi } from '../state/store'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString('ko-KR') : value.toFixed(3)
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

/** Renders `panel.type = "table"` payloads: `{title?, columns?: string[], rows: object[] | any[][]}`. */
export function TablePanel({ payload }: { payload: Record<string, unknown> }) {
  const patchUi = usePatchUi()
  const rows = Array.isArray(payload.rows) ? payload.rows : []
  const columns: string[] = Array.isArray(payload.columns)
    ? payload.columns.map(String)
    : [...new Set(rows.filter(isRecord).flatMap((r) => Object.keys(r)))]
  const title = typeof payload.title === 'string' ? payload.title : '데이터 표'

  return (
    <section className="table-panel" aria-label={title}>
      <header>
        <h2>{title}</h2>
        <button type="button" className="icon-button" aria-label="표 닫기" onClick={() => patchUi([replaceOp('/panel', { type: 'none', payload: {} })])}>
          ×
        </button>
      </header>
      {rows.length === 0 ? (
        <p className="muted">표시할 행이 없습니다.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c} scope="col">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 200).map((row, i) => (
                <tr key={i}>
                  {columns.map((c, j) => (
                    <td key={c}>{cell(Array.isArray(row) ? row[j] : isRecord(row) ? row[c] : row)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
