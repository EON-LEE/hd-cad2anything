// Minimal RFC 6902 JSON Patch applier with RFC 6901 JSON Pointers.
//
// - Immutable: the input document is never mutated. Containers along each patched path are
//   shallow-copied (copy-on-write), so untouched branches keep their identity (cheap React updates).
// - Atomic: applyPatch either applies every operation or throws a JsonPatchError.

export type PatchOperation =
  | { op: 'add'; path: string; value: unknown }
  | { op: 'replace'; path: string; value: unknown }
  | { op: 'remove'; path: string }
  | { op: 'move'; from: string; path: string }
  | { op: 'copy'; from: string; path: string }
  | { op: 'test'; path: string; value: unknown }

export class JsonPatchError extends Error {
  readonly operationIndex: number | undefined

  constructor(message: string, operationIndex?: number) {
    super(message)
    this.name = 'JsonPatchError'
    this.operationIndex = operationIndex
  }
}

type Container = Record<string, unknown> | unknown[]

const ARRAY_INDEX = /^(0|[1-9][0-9]*)$/

export function parsePointer(pointer: string): string[] {
  if (pointer === '') return []
  if (typeof pointer !== 'string' || pointer[0] !== '/') {
    throw new JsonPatchError(`Invalid JSON Pointer "${String(pointer)}": must be empty or start with "/"`)
  }
  return pointer
    .slice(1)
    .split('/')
    .map((token) => {
      if (/~(?![01])/.test(token)) throw new JsonPatchError(`Invalid escape in JSON Pointer "${pointer}"`)
      const decoded = token.replace(/~1/g, '/').replace(/~0/g, '~')
      if (decoded === '__proto__') throw new JsonPatchError(`Forbidden JSON Pointer token "__proto__"`)
      return decoded
    })
}

export function escapePointerToken(token: string): string {
  return token.replace(/~/g, '~0').replace(/\//g, '~1')
}

export function toPointer(tokens: readonly string[]): string {
  return tokens.map((t) => `/${escapePointerToken(t)}`).join('')
}

function isContainer(value: unknown): value is Container {
  return typeof value === 'object' && value !== null
}

function hasOwn(obj: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key)
}

function shallowClone<T extends Container>(value: T): T {
  return (Array.isArray(value) ? value.slice() : { ...value }) as T
}

function arrayIndex(token: string, length: number, allowEnd: boolean, pointer: string): number {
  if (token === '-') {
    if (allowEnd) return length
    throw new JsonPatchError(`"-" is not a valid index here: "${pointer}"`)
  }
  if (!ARRAY_INDEX.test(token)) throw new JsonPatchError(`Invalid array index "${token}" in "${pointer}"`)
  const index = Number(token)
  if (allowEnd ? index > length : index >= length) {
    throw new JsonPatchError(`Array index ${index} out of bounds in "${pointer}"`)
  }
  return index
}

/** Resolve a JSON Pointer; throws when any segment is missing. */
export function getValueAt(doc: unknown, pointer: string): unknown {
  let current = doc
  for (const token of parsePointer(pointer)) {
    if (Array.isArray(current)) {
      current = current[arrayIndex(token, current.length, false, pointer)]
    } else if (isContainer(current) && hasOwn(current, token)) {
      current = (current as Record<string, unknown>)[token]
    } else {
      throw new JsonPatchError(`Path not found: "${pointer}"`)
    }
  }
  return current
}

/** Copy the containers from the root to the parent of the target and run `mutate` on the copy. */
function updateParent(
  doc: unknown,
  tokens: string[],
  pointer: string,
  mutate: (parent: Container, key: string) => void,
): unknown {
  if (!isContainer(doc)) throw new JsonPatchError(`Path not found: "${pointer}"`)
  const root = shallowClone(doc)
  let current: Container = root
  for (let i = 0; i < tokens.length - 1; i += 1) {
    const token = tokens[i]
    let next: unknown
    let key: string | number
    if (Array.isArray(current)) {
      key = arrayIndex(token, current.length, false, pointer)
      next = current[key]
    } else {
      if (!hasOwn(current, token)) throw new JsonPatchError(`Path not found: "${pointer}"`)
      key = token
      next = current[token]
    }
    if (!isContainer(next)) throw new JsonPatchError(`Path not found: "${pointer}"`)
    const copy = shallowClone(next)
    ;(current as Record<string | number, unknown>)[key] = copy
    current = copy
  }
  mutate(current, tokens[tokens.length - 1])
  return root
}

function addValue(doc: unknown, pointer: string, value: unknown): unknown {
  const tokens = parsePointer(pointer)
  if (tokens.length === 0) return value
  return updateParent(doc, tokens, pointer, (parent, key) => {
    if (Array.isArray(parent)) parent.splice(arrayIndex(key, parent.length, true, pointer), 0, value)
    else parent[key] = value
  })
}

function removeValue(doc: unknown, pointer: string): unknown {
  const tokens = parsePointer(pointer)
  if (tokens.length === 0) throw new JsonPatchError('Cannot remove the document root')
  return updateParent(doc, tokens, pointer, (parent, key) => {
    if (Array.isArray(parent)) {
      parent.splice(arrayIndex(key, parent.length, false, pointer), 1)
    } else {
      if (!hasOwn(parent, key)) throw new JsonPatchError(`Path not found: "${pointer}"`)
      delete parent[key]
    }
  })
}

function replaceValue(doc: unknown, pointer: string, value: unknown): unknown {
  const tokens = parsePointer(pointer)
  if (tokens.length === 0) return value
  return updateParent(doc, tokens, pointer, (parent, key) => {
    if (Array.isArray(parent)) {
      parent[arrayIndex(key, parent.length, false, pointer)] = value
    } else {
      if (!hasOwn(parent, key)) throw new JsonPatchError(`Path not found: "${pointer}"`)
      parent[key] = value
    }
  })
}

export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (!isContainer(a) || !isContainer(b)) return false
  if (Array.isArray(a) !== Array.isArray(b)) return false
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => deepEqual(item, b[i]))
  }
  const ka = Object.keys(a)
  const kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  return ka.every((k) => hasOwn(b, k) && deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]))
}

function requireValue(op: Record<string, unknown>): unknown {
  if (!('value' in op) || op.value === undefined) {
    throw new JsonPatchError(`Operation "${String(op.op)}" requires a "value"`)
  }
  return op.value
}

function requireString(op: Record<string, unknown>, field: 'path' | 'from'): string {
  const value = op[field]
  if (typeof value !== 'string') throw new JsonPatchError(`Operation "${String(op.op)}" requires a string "${field}"`)
  return value
}

export function applyOperation(doc: unknown, operation: PatchOperation): unknown {
  if (!isContainer(operation) || Array.isArray(operation)) throw new JsonPatchError('Operation must be an object')
  const op = operation as unknown as Record<string, unknown>
  const path = requireString(op, 'path')
  switch (op.op) {
    case 'add':
      return addValue(doc, path, requireValue(op))
    case 'remove':
      return removeValue(doc, path)
    case 'replace':
      return replaceValue(doc, path, requireValue(op))
    case 'move': {
      const from = requireString(op, 'from')
      if (from === path) return doc
      if (path.startsWith(`${from}/`)) throw new JsonPatchError(`Cannot move "${from}" into its own child "${path}"`)
      const value = getValueAt(doc, from)
      return addValue(removeValue(doc, from), path, value)
    }
    case 'copy': {
      const from = requireString(op, 'from')
      return addValue(doc, path, getValueAt(doc, from))
    }
    case 'test': {
      const expected = requireValue(op)
      if (!deepEqual(getValueAt(doc, path), expected)) throw new JsonPatchError(`Test failed at "${path}"`)
      return doc
    }
    default:
      throw new JsonPatchError(`Unknown operation "${String(op.op)}"`)
  }
}

/** Apply all operations atomically; returns a new document or throws JsonPatchError. */
export function applyPatch<T>(doc: T, operations: readonly PatchOperation[]): T {
  if (!Array.isArray(operations)) throw new JsonPatchError('Patch must be an array of operations')
  let result: unknown = doc
  operations.forEach((operation, index) => {
    try {
      result = applyOperation(result, operation)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      throw new JsonPatchError(`Operation ${index} failed: ${message}`, index)
    }
  })
  return result as T
}

/** Top-level keys touched by a patch (e.g. "/heatmap/values/3F-N05" -> "heatmap"; the root is "/"). */
export function touchedKeys(ops: readonly PatchOperation[]): string[] {
  const keys = new Set<string>()
  for (const op of ops) {
    for (const pointer of [op?.path, op && 'from' in op ? op.from : undefined]) {
      if (typeof pointer !== 'string') continue
      try {
        const tokens = parsePointer(pointer)
        keys.add(tokens[0] ?? '/')
      } catch {
        keys.add(pointer)
      }
    }
  }
  return [...keys]
}
