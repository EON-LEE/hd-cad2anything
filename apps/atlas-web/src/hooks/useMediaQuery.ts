import { useEffect, useState } from 'react'

/** Subscribe to a CSS media query. Returns false where `matchMedia` is unavailable (SSR, jsdom). */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(query).matches : false,
  )

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia(query)
    const update = () => setMatches(mql.matches)
    update()
    mql.addEventListener('change', update)
    return () => mql.removeEventListener('change', update)
  }, [query])

  return matches
}

/** Width below which the single-column phone layout and collapsed floating cards apply (R1). */
export const COMPACT_MAX_WIDTH = 719.98

/** True on narrow (phone) viewports where the floating journey/route/flow cards collapse (R1). */
export function useIsCompact(): boolean {
  return useMediaQuery(`(max-width: ${COMPACT_MAX_WIDTH}px)`)
}

/** True on the stacked single-column layout where the map/side view toggle applies (R1, <= 900px). */
export function useIsStacked(): boolean {
  return useMediaQuery('(max-width: 900px)')
}
