import { describe, expect, it } from 'vitest'
import { computeGraphLayout, linkWidth } from './graphLayout'

const edges = [
  { from: '3F-C01', to: '1F-C03', weight: 2.4, label: 'lift 2.4' },
  { from: '3F-S03', to: '1F-S01', weight: 2.1 },
  { from: '3F-N02', to: '3F-C06', weight: 1.9 },
  { from: '3F-C06', to: '1F-W01', weight: 1.7 },
  { from: '3F-C01', to: '3F-S03', weight: 1.6 },
  { from: '3F-C01', to: '3F-C01', weight: 9 },
]

describe('computeGraphLayout', () => {
  it('creates one node per unit and one link per edge (self-loops dropped)', () => {
    const layout = computeGraphLayout(edges, 420, 380)
    expect(layout.nodes.map((n) => n.id).sort()).toEqual(['1F-C03', '1F-S01', '1F-W01', '3F-C01', '3F-C06', '3F-N02', '3F-S03'])
    expect(layout.links).toHaveLength(5)
    expect(layout.nodes.find((n) => n.id === '3F-C01')).toMatchObject({ degree: 2, floor: '3F' })
  })

  it('keeps nodes inside the box and separated', () => {
    const { nodes } = computeGraphLayout(edges, 420, 380, 36)
    for (const n of nodes) {
      expect(n.x).toBeGreaterThanOrEqual(36)
      expect(n.x).toBeLessThanOrEqual(420 - 36)
      expect(n.y).toBeGreaterThanOrEqual(36)
      expect(n.y).toBeLessThanOrEqual(380 - 36)
    }
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        expect(Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y)).toBeGreaterThan(20)
      }
    }
  })

  it('is deterministic for the same input', () => {
    const a = computeGraphLayout(edges, 420, 380).nodes.map((n) => [n.id, Math.round(n.x), Math.round(n.y)])
    const b = computeGraphLayout(edges, 420, 380).nodes.map((n) => [n.id, Math.round(n.x), Math.round(n.y)])
    expect(a).toEqual(b)
  })

  it('handles empty input and scales link width with weight', () => {
    expect(computeGraphLayout([], 100, 100)).toEqual({ nodes: [], links: [] })
    expect(linkWidth(undefined)).toBe(1.8)
    expect(linkWidth(2)).toBeGreaterThan(linkWidth(1))
    expect(linkWidth(100)).toBe(9)
  })
})
