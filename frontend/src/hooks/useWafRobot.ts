import { useEffect, useState } from 'react'
import { getReplenishment, type ReplenishmentDto } from '../api/fmsApi'

export function useWafRobot(enabled: boolean) {
  const [data, setData] = useState<ReplenishmentDto | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!enabled) { setData(null); setError(null); return }
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    const poll = async () => {
      try {
        const next = await getReplenishment(controller.signal)
        if (live) { setData(next); setError(null) }
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : 'WAF 상태 수신 실패')
      } finally { if (live) timer = setTimeout(() => void poll(), 1000) }
    }
    void poll()
    return () => { live = false; controller.abort(); clearTimeout(timer) }
  }, [enabled])
  return { data, error }
}
