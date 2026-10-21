// Static force-directed layout for the relationship graph (d3-force, run synchronously).
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force'
import type { GraphEdge } from '../types/api'
import { floorOfUnitCode } from './geometry'

export interface GraphNode extends SimulationNodeDatum {
  id: string
  floor: string | null
  degree: number
  x: number
  y: number
}

export interface GraphLink extends SimulationLinkDatum<GraphNode> {
  source: GraphNode
  target: GraphNode
  edge: GraphEdge
  width: number
}

export interface GraphLayout {
  nodes: GraphNode[]
  links: GraphLink[]
}

export function linkWidth(weight: number | undefined): number {
  const w = typeof weight === 'number' && Number.isFinite(weight) ? weight : 1
  return Math.min(9, Math.max(1, w * 1.8))
}

/** Compute node positions for `edges` inside a width x height box (deterministic for equal input). */
export function computeGraphLayout(edges: readonly GraphEdge[], width: number, height: number, margin = 36): GraphLayout {
  const nodes = new Map<string, GraphNode>()
  const node = (id: string) => {
    let n = nodes.get(id)
    if (!n) {
      n = { id, floor: floorOfUnitCode(id), degree: 0, x: 0, y: 0 }
      nodes.set(id, n)
    }
    n.degree += 1
    return n
  }
  const raw = edges
    .filter((edge) => edge.from !== edge.to)
    .map((edge) => ({ source: node(edge.from), target: node(edge.to), edge, width: linkWidth(edge.weight) }))
  const list = [...nodes.values()]
  if (list.length === 0) return { nodes: [], links: [] }

  // Seed positions on a circle so results do not depend on d3's default phyllotaxis around (0, 0).
  list.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / list.length
    n.x = width / 2 + Math.cos(angle) * Math.min(width, height) * 0.3
    n.y = height / 2 + Math.sin(angle) * Math.min(width, height) * 0.3
  })
  const simulation = forceSimulation<GraphNode>(list)
    .force(
      'link',
      forceLink<GraphNode, GraphLink>(raw as GraphLink[])
        .id((d) => d.id)
        .distance(120)
        .strength(0.5),
    )
    .force('charge', forceManyBody<GraphNode>().strength(-380))
    .force('center', forceCenter(width / 2, height / 2))
    .force('collide', forceCollide<GraphNode>(40))
    .force('x', forceX<GraphNode>(width / 2).strength(0.06))
    .force('y', forceY<GraphNode>(height / 2).strength(0.06))
    .stop()
  for (let i = 0; i < 300; i += 1) simulation.tick()

  for (const n of list) {
    n.x = Math.min(width - margin, Math.max(margin, n.x))
    n.y = Math.min(height - margin, Math.max(margin, n.y))
  }
  return { nodes: list, links: raw as GraphLink[] }
}
