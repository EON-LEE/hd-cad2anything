// Cache keys for useCachedResource / cachedFetch (always prefixed by the data-source mode).
export const cacheKeys = {
  floor: (mode: string, rev: string, floor: string, fixtures = false) => `${mode}:floor:${rev}:${floor}:${fixtures ? 'fixtures' : 'base'}`,
  changes: (mode: string, revFrom: string, revTo: string) => `${mode}:changes:${revFrom}:${revTo}`,
  artifacts: (mode: string, rev: string) => `${mode}:artifacts:${rev}`,
  productivity: (mode: string, rev: string, floor: string) => `${mode}:prod:${rev}:${floor}`,
}
