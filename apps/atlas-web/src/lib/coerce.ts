// Small coercion helpers for normalizing loosely typed API responses.

export type Rec = Record<string, unknown>

export function isRecord(value: unknown): value is Rec {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** String (numbers are stringified); `fallback` otherwise. */
export function text(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return fallback
}

/** Non-empty string (numbers are stringified) or null. */
export function optText(value: unknown): string | null {
  if (typeof value === 'string') return value.trim() === '' ? null : value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return null
}

/** Finite number (numeric strings are converted) or null. */
export function num(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

export function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

export function records(value: unknown): Rec[] {
  return list(value).filter(isRecord)
}

/** Strings of a list; `{message}` objects become their message, other values are dropped. */
export function texts(value: unknown): string[] {
  return list(value).flatMap((item) => {
    if (typeof item === 'string') return item ? [item] : []
    if (typeof item === 'number' && Number.isFinite(item)) return [String(item)]
    if (isRecord(item) && typeof item.message === 'string' && item.message) return [item.message]
    return []
  })
}

/** Count given as a number or as a list. */
export function count(value: unknown): number {
  if (Array.isArray(value)) return value.length
  return num(value) ?? 0
}

export function bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null
}
