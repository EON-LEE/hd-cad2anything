import { createContext, useCallback, useContext, useReducer, type Dispatch, type ReactNode } from 'react'
import type { PatchOperation } from '../lib/jsonPatch'
import { appReducer, type AppAction, type AppState } from './reducer'

const StateContext = createContext<AppState | null>(null)
const DispatchContext = createContext<Dispatch<AppAction> | null>(null)

export function StoreProvider({ initialState, children }: { initialState: AppState; children: ReactNode }) {
  const [state, dispatch] = useReducer(appReducer, initialState)
  return (
    <DispatchContext value={dispatch}>
      <StateContext value={state}>{children}</StateContext>
    </DispatchContext>
  )
}

export function useAppState(): AppState {
  const state = useContext(StateContext)
  if (!state) throw new Error('useAppState must be used inside <StoreProvider>')
  return state
}

export function useAppDispatch(): Dispatch<AppAction> {
  const dispatch = useContext(DispatchContext)
  if (!dispatch) throw new Error('useAppDispatch must be used inside <StoreProvider>')
  return dispatch
}

/** User-driven UI changes go through the same JSON Patch path as agent STATE_DELTA events. */
export function usePatchUi(): (ops: PatchOperation[]) => void {
  const dispatch = useAppDispatch()
  return useCallback((ops: PatchOperation[]) => dispatch({ type: 'ui/patch', ops }), [dispatch])
}
