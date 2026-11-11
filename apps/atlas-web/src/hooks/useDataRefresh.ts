import { useCallback } from 'react'
import { useData } from '../api/source'
import { useAppDispatch } from '../state/store'
import { invalidateCache } from './useCachedResource'

/**
 * Call after something outside a panel's own actions changed the server's data (an agent tool made a revision,
 * ran a generator or changed a relayout project): the revision list is reloaded, cached resources of this data
 * source are dropped and open panels reload (`dataEpoch`).
 */
export function useDataRefresh(): () => void {
  const { source, refreshMeta } = useData()
  const dispatch = useAppDispatch()
  return useCallback(() => {
    invalidateCache(`${source.mode}:`)
    dispatch({ type: 'data/changed' })
    refreshMeta?.().catch(() => {
      // keep the revision list we have; the next refresh tries again
    })
  }, [dispatch, refreshMeta, source.mode])
}
