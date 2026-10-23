import { useEffect, useState } from 'react'

/** Whole seconds since `active` became true (0 while inactive); for "대안을 만드는 중… 12초". */
export function useElapsedSeconds(active: boolean): number {
  const [seconds, setSeconds] = useState(0)

  useEffect(() => {
    if (!active) return
    const started = Date.now()
    setSeconds(0)
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [active])

  return active ? seconds : 0
}
