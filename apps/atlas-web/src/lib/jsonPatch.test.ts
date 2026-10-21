import { describe, expect, it } from 'vitest'
import { applyPatch, escapePointerToken, getValueAt, JsonPatchError, parsePointer, type PatchOperation } from './jsonPatch'

const baseState = () => ({
  rev: 'R2',
  compareRev: null as string | null,
  floor: '3F',
  layers: { fixtures: false, walkways: true, labels: true },
  heatmap: null as null | { metric: string; label: string; values: Record<string, number> },
  highlights: [{ unit_code: '3F-N05', color: '#e53935', reason: '저효율' }],
  edges: [] as { from: string; to: string }[],
  focus: null,
  panel: { type: 'none', payload: {} },
})

describe('JSON Pointer', () => {
  it('parses and unescapes tokens (RFC 6901)', () => {
    expect(parsePointer('')).toEqual([])
    expect(parsePointer('/a/b')).toEqual(['a', 'b'])
    expect(parsePointer('/a~1b/c~0d/~01')).toEqual(['a/b', 'c~d', '~1'])
    expect(parsePointer('/')).toEqual([''])
  })

  it('rejects malformed pointers and prototype keys', () => {
    expect(() => parsePointer('a/b')).toThrow(JsonPatchError)
    expect(() => parsePointer('/a~2')).toThrow(JsonPatchError)
    expect(() => parsePointer('/__proto__/polluted')).toThrow(JsonPatchError)
  })

  it('escapes tokens', () => {
    expect(escapePointerToken('a/b~c')).toBe('a~1b~0c')
  })

  it('reads values', () => {
    const doc = { a: [{ b: 1 }], 'x/y': 2 }
    expect(getValueAt(doc, '/a/0/b')).toBe(1)
    expect(getValueAt(doc, '/x~1y')).toBe(2)
    expect(() => getValueAt(doc, '/a/1')).toThrow(JsonPatchError)
  })
})

describe('applyPatch: objects', () => {
  it('replaces an existing member', () => {
    const next = applyPatch(baseState(), [{ op: 'replace', path: '/floor', value: '1F' }])
    expect(next.floor).toBe('1F')
  })

  it('replaces a null member with an object and adds into it', () => {
    const next = applyPatch(baseState(), [
      { op: 'replace', path: '/heatmap', value: { metric: 'sales_per_m2', label: '평효율(원/㎡)', values: {} } },
      { op: 'add', path: '/heatmap/values/3F-N05', value: 0.82 },
    ])
    expect(next.heatmap).toEqual({ metric: 'sales_per_m2', label: '평효율(원/㎡)', values: { '3F-N05': 0.82 } })
  })

  it('adds a new member and replaces an existing one with add', () => {
    const next = applyPatch({ a: 1 } as Record<string, unknown>, [
      { op: 'add', path: '/b', value: 2 },
      { op: 'add', path: '/a', value: 3 },
    ])
    expect(next).toEqual({ a: 3, b: 2 })
  })

  it('removes a member', () => {
    const next = applyPatch({ a: 1, b: 2 } as Record<string, unknown>, [{ op: 'remove', path: '/a' }])
    expect(next).toEqual({ b: 2 })
  })

  it('replaces nested layer flags', () => {
    const next = applyPatch(baseState(), [{ op: 'replace', path: '/layers/fixtures', value: true }])
    expect(next.layers).toEqual({ fixtures: true, walkways: true, labels: true })
  })

  it('replaces the whole document with an empty path', () => {
    expect(applyPatch({ a: 1 }, [{ op: 'replace', path: '', value: { b: 2 } }])).toEqual({ b: 2 })
  })
})

describe('applyPatch: arrays', () => {
  it('appends with "-" and inserts at an index', () => {
    const next = applyPatch(baseState(), [
      { op: 'add', path: '/edges/-', value: { from: '3F-N05', to: '2F-C03' } },
      { op: 'add', path: '/edges/0', value: { from: 'A', to: 'B' } },
      { op: 'add', path: '/edges/2', value: { from: 'C', to: 'D' } },
    ])
    expect(next.edges.map((e) => e.from)).toEqual(['A', '3F-N05', 'C'])
  })

  it('replaces and removes elements by index', () => {
    const next = applyPatch({ list: [1, 2, 3] }, [
      { op: 'replace', path: '/list/1', value: 20 },
      { op: 'remove', path: '/list/0' },
    ])
    expect(next.list).toEqual([20, 3])
  })

  it('patches a member of an array element', () => {
    const next = applyPatch(baseState(), [{ op: 'replace', path: '/highlights/0/color', value: '#1e88e5' }])
    expect(next.highlights[0]).toEqual({ unit_code: '3F-N05', color: '#1e88e5', reason: '저효율' })
  })

  it('rejects out-of-range and malformed indexes', () => {
    expect(() => applyPatch({ list: [1] }, [{ op: 'replace', path: '/list/1', value: 0 }])).toThrow(JsonPatchError)
    expect(() => applyPatch({ list: [1] }, [{ op: 'add', path: '/list/2', value: 0 }])).toThrow(JsonPatchError)
    expect(() => applyPatch({ list: [1] }, [{ op: 'remove', path: '/list/-' }])).toThrow(JsonPatchError)
    expect(() => applyPatch({ list: [1, 2] }, [{ op: 'remove', path: '/list/01' }])).toThrow(JsonPatchError)
  })
})

describe('applyPatch: move, copy, test', () => {
  it('moves and copies values', () => {
    const next = applyPatch({ a: { x: 1 }, list: [1, 2, 3] } as Record<string, unknown>, [
      { op: 'move', from: '/a', path: '/b' },
      { op: 'copy', from: '/b', path: '/c' },
      { op: 'move', from: '/list/0', path: '/list/-' },
    ])
    expect(next).toEqual({ b: { x: 1 }, c: { x: 1 }, list: [2, 3, 1] })
  })

  it('refuses to move a value into its own child', () => {
    expect(() => applyPatch({ a: { b: {} } }, [{ op: 'move', from: '/a', path: '/a/b/c' }])).toThrow(JsonPatchError)
  })

  it('test passes on deep equality and fails otherwise', () => {
    const doc = { a: { list: [1, { b: 'x' }] } }
    expect(applyPatch(doc, [{ op: 'test', path: '/a', value: { list: [1, { b: 'x' }] } }])).toBe(doc)
    expect(() => applyPatch(doc, [{ op: 'test', path: '/a/list/1/b', value: 'y' }])).toThrow(JsonPatchError)
  })
})

describe('applyPatch: errors, atomicity and immutability', () => {
  it('fails on missing paths for replace/remove and on unknown ops', () => {
    expect(() => applyPatch({ a: 1 }, [{ op: 'replace', path: '/b', value: 1 }])).toThrow(/Path not found/)
    expect(() => applyPatch({ a: 1 }, [{ op: 'remove', path: '/b' }])).toThrow(/Path not found/)
    expect(() => applyPatch({ a: 1 }, [{ op: 'add', path: '/x/y', value: 1 }])).toThrow(/Path not found/)
    expect(() => applyPatch({ a: 1 }, [{ op: 'nope', path: '/a' } as unknown as PatchOperation])).toThrow(/Unknown operation/)
    expect(() => applyPatch({ a: 1 }, [{ op: 'add', path: '/a' } as unknown as PatchOperation])).toThrow(/requires a "value"/)
  })

  it('reports the failing operation index and leaves the input untouched', () => {
    const doc = baseState()
    const snapshot = structuredClone(doc)
    let error: unknown
    try {
      applyPatch(doc, [
        { op: 'replace', path: '/floor', value: '1F' },
        { op: 'remove', path: '/does-not-exist' },
      ])
    } catch (e) {
      error = e
    }
    expect(error).toBeInstanceOf(JsonPatchError)
    expect((error as JsonPatchError).operationIndex).toBe(1)
    expect(doc).toEqual(snapshot)
  })

  it('never mutates the input and shares untouched branches', () => {
    const doc = baseState()
    const snapshot = structuredClone(doc)
    const next = applyPatch(doc, [
      { op: 'replace', path: '/highlights/0/color', value: '#000000' },
      { op: 'add', path: '/edges/-', value: { from: 'a', to: 'b' } },
    ])
    expect(doc).toEqual(snapshot)
    expect(next).not.toBe(doc)
    expect(next.highlights).not.toBe(doc.highlights)
    expect(next.layers).toBe(doc.layers)
    expect(next.panel).toBe(doc.panel)
  })
})
