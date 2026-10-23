// Cache keys for useCachedResource / cachedFetch (always prefixed by the data-source mode).
export const cacheKeys = {
  floor: (mode: string, rev: string, floor: string, fixtures = false) => `${mode}:floor:${rev}:${floor}:${fixtures ? 'fixtures' : 'base'}`,
  changes: (mode: string, revFrom: string, revTo: string) => `${mode}:changes:${revFrom}:${revTo}`,
  artifacts: (mode: string, rev: string) => `${mode}:artifacts:${rev}`,
  productivity: (mode: string, rev: string, floor: string) => `${mode}:prod:${rev}:${floor}`,
  relayoutBriefs: (mode: string) => `${mode}:relayout:briefs`,
  uploadProfiles: (mode: string) => `${mode}:uploads:profiles`,
}

/** Cached resources built from one revision's model (dropped after that revision is uploaded again). */
export function revisionCachePrefixes(mode: string, rev: string): string[] {
  // Change sets are keyed by both ends, so all of them are dropped (they are cheap to reload).
  return [`${mode}:floor:${rev}:`, `${mode}:artifacts:${rev}`, `${mode}:prod:${rev}:`, `${mode}:changes:`]
}
