import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { cacheKeys } from '../api/keys'
import { useData } from '../api/source'
import { cachedFetch, useCachedResource } from '../hooks/useCachedResource'
import { DEFAULT_EDGE_COLOR } from '../lib/colors'
import { computeGraphLayout, type GraphNode } from '../lib/graphLayout'
import { replaceOp } from '../state/reducer'
import { useAppDispatch, useAppState } from '../state/store'

const WIDTH = 420
const HEIGHT = 380

interface UnitLabel {
  name: string
  category: string | null
}

export function GraphPanel() {
  const { source, meta } = useData()
  const { ui } = useAppState()
  const dispatch = useAppDispatch()
  const layout = useMemo(() => computeGraphLayout(ui.edges, WIDTH, HEIGHT), [ui.edges])
  const [dragged, setDragged] = useState<Map<string, { x: number; y: number }>>(new Map())
  const drag = useRef<{ id: string; moved: boolean; startX: number; startY: number } | null>(null)
  const suppressClick = useRef(false)
  const svgRef = useRef<SVGSVGElement>(null)

  useEffect(() => setDragged(new Map()), [layout])

  const knownFloors = useMemo(() => new Set(meta.floors.map((f) => f.code)), [meta.floors])
  const floorsNeeded = useMemo(
    () => [...new Set(layout.nodes.map((n) => n.floor).filter((f): f is string => !!f && knownFloors.has(f)))].sort(),
    [layout, knownFloors],
  )
  const index = useCachedResource(
    floorsNeeded.length ? `${source.mode}:unit-index:${ui.rev}:${floorsNeeded.join(',')}` : null,
    async () => {
      const results = await Promise.allSettled(
        floorsNeeded.map((f) => cachedFetch(cacheKeys.floor(source.mode, ui.rev, f), () => source.getFloor(ui.rev, f, false))),
      )
      const labels = new Map<string, UnitLabel>()
      for (const result of results) {
        if (result.status !== 'fulfilled') continue
        for (const feature of result.value.features) {
          const p = feature.properties
          if (p?.kind !== 'unit' || !p.unit_code) continue
          labels.set(p.unit_code, { name: p.brand_name ?? (p.vacant ? '(공실)' : p.unit_code), category: p.category ?? null })
        }
      }
      return labels
    },
  )
  const colors = useMemo(() => new Map(meta.categories.map((c) => [c.code, c.color])), [meta.categories])
  const label = (id: string) => index.data?.get(id)?.name ?? id
  const position = (n: GraphNode) => dragged.get(n.id) ?? { x: n.x, y: n.y }

  const goTo = (node: GraphNode) => {
    if (!node.floor || !knownFloors.has(node.floor)) return
    if (node.floor !== ui.floor) dispatch({ type: 'ui/patch', ops: [replaceOp('/floor', node.floor)] })
    dispatch({ type: 'selection/set', unitCodes: [node.id] })
  }

  const toSvgPoint = (event: PointerEvent) => {
    const svg = svgRef.current
    const matrix = svg?.getScreenCTM?.()
    if (!svg || !matrix) return null
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse())
    return { x: Math.min(WIDTH - 12, Math.max(12, point.x)), y: Math.min(HEIGHT - 12, Math.max(12, point.y)) }
  }

  const onPointerDown = (event: PointerEvent<SVGGElement>, node: GraphNode) => {
    drag.current = { id: node.id, moved: false, startX: event.clientX, startY: event.clientY }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }

  const onPointerMove = (event: PointerEvent<SVGGElement>) => {
    const state = drag.current
    if (!state) return
    if (Math.hypot(event.clientX - state.startX, event.clientY - state.startY) > 3) state.moved = true
    if (!state.moved) return
    const point = toSvgPoint(event)
    if (point) setDragged((prev) => new Map(prev).set(state.id, point))
  }

  const onPointerUp = (event: PointerEvent<SVGGElement>) => {
    const state = drag.current
    drag.current = null
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    // A drag must not also count as a click on the node.
    suppressClick.current = !!state?.moved
  }

  const onNodeClick = (node: GraphNode) => {
    if (suppressClick.current) {
      suppressClick.current = false
      return
    }
    goTo(node)
  }

  const onNodeKey = (event: KeyboardEvent<SVGGElement>, node: GraphNode) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      goTo(node)
    }
  }

  if (layout.nodes.length === 0) {
    return (
      <div className="graph-panel">
        <header className="panel-header">
          <h2>관계 그래프</h2>
        </header>
        <p className="muted">
          표시할 관계가 없습니다. 에이전트에게 “40대 VIP 교차구매 보여줘”처럼 요청하면 매장 간 연결(state.edges)이 여기와 도면에 함께 표시됩니다.
        </p>
      </div>
    )
  }

  return (
    <div className="graph-panel">
      <header className="panel-header">
        <h2>관계 그래프</h2>
        <p className="muted">
          노드 {layout.nodes.length}개 · 연결 {layout.links.length}개 · 선 굵기 = weight · 노드를 끌어 옮기거나 눌러 도면에서 볼 수 있습니다.
        </p>
      </header>
      <svg
        ref={svgRef}
        className="graph-svg"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="group"
        aria-label="매장 관계 그래프"
      >
        <g>
          {layout.links.map((link, i) => {
            const s = position(link.source)
            const t = position(link.target)
            return (
              <g key={`${link.edge.from}-${link.edge.to}-${i}`}>
                <line
                  x1={s.x}
                  y1={s.y}
                  x2={t.x}
                  y2={t.y}
                  stroke={link.edge.color ?? DEFAULT_EDGE_COLOR}
                  strokeOpacity={0.7}
                  strokeWidth={link.width}
                  strokeLinecap="round"
                />
                {link.edge.label && (
                  <text className="graph-edge-label" x={(s.x + t.x) / 2} y={(s.y + t.y) / 2 - 5} textAnchor="middle">
                    {link.edge.label}
                  </text>
                )}
              </g>
            )
          })}
        </g>
        <g>
          {layout.nodes.map((node) => {
            const p = position(node)
            const info = index.data?.get(node.id)
            const current = node.floor === ui.floor
            const fill = info?.category ? (colors.get(info.category) ?? '#9e9e9e') : '#b0bec5'
            const radius = 10 + Math.min(node.degree, 5) * 2
            const name = label(node.id)
            const navigable = !!node.floor && knownFloors.has(node.floor)
            return (
              <g
                key={node.id}
                className={`graph-node${current ? ' current' : ''}${navigable ? '' : ' disabled'}`}
                transform={`translate(${p.x}, ${p.y})`}
                role="button"
                tabIndex={0}
                aria-label={`${node.id} ${name}${node.floor ? ` (${node.floor})` : ''}, 연결 ${node.degree}개${navigable ? ', 도면에서 보기' : ''}`}
                onPointerDown={(e) => onPointerDown(e, node)}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onPointerCancel={() => {
                  drag.current = null
                }}
                onClick={() => onNodeClick(node)}
                onKeyDown={(e) => onNodeKey(e, node)}
              >
                <circle r={radius} fill={fill} stroke={current ? '#111827' : '#ffffff'} strokeWidth={current ? 2.5 : 1.5} />
                <text className="graph-node-floor" y={4} textAnchor="middle">
                  {node.floor ?? ''}
                </text>
                <text className="graph-node-label" y={radius + 13} textAnchor="middle">
                  {name.length > 14 ? `${name.slice(0, 13)}…` : name}
                </text>
              </g>
            )
          })}
        </g>
      </svg>
      <table className="edge-table">
        <caption>연결 목록</caption>
        <thead>
          <tr>
            <th scope="col">출발</th>
            <th scope="col">도착</th>
            <th scope="col">weight</th>
            <th scope="col">라벨</th>
          </tr>
        </thead>
        <tbody>
          {ui.edges.map((edge, i) => (
            <tr key={`${edge.from}-${edge.to}-${i}`}>
              <td>
                {label(edge.from)} <span className="muted">{edge.from}</span>
              </td>
              <td>
                {label(edge.to)} <span className="muted">{edge.to}</span>
              </td>
              <td>{edge.weight ?? '–'}</td>
              <td>{edge.label ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
