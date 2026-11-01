import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { getFmsWebStatus, restartFmsWeb, type FmsWebStatus } from '../api/fmsApi'
import { C } from '../constants/theme'

const KEY = 'fms-web-restart'
type Pending = { at: number; previous: string; token?: string }
function readPending(): Pending | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(KEY) ?? 'null') as Pending | null
    return value && typeof value.at === 'number' && typeof value.previous === 'string' && value.at <= Date.now() && Date.now() - value.at < 900000 ? value : null
  } catch { return null }
}
function storePending(value: Pending | null) {
  try { if (value) sessionStorage.setItem(KEY, JSON.stringify(value)); else sessionStorage.removeItem(KEY) } catch { /* 저장이 막혀도 현재 화면에서 확인한다. */ }
}

export default function FmsRestartButton() {
  const [status, setStatus] = useState<FmsWebStatus | null>(null)
  const [pending, setPending] = useState<Pending | null>(readPending)
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const sending = useRef(false)

  useEffect(() => {
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const next = await getFmsWebStatus()
        if (stopped) return
        setStatus(next)
        if (pending) {
          const matching = !pending.token || next.restart.token === pending.token
          if (matching && next.restart.phase === 'FAILED') {
            setFailed(true); setMessage(`재시작 실패: ${next.restart.message ?? 'logs/web_restart.log를 확인하세요.'}`)
          } else if (matching && next.instance_id !== pending.previous && next.restart.phase === 'COMPLETE') {
            const front = await fetch(new URL('./', window.location.href), { cache: 'no-store', signal: AbortSignal.timeout(3000) })
            if (front.ok && !stopped) { storePending(null); window.location.reload(); return }
          } else {
            setMessage(next.restart.phase === 'STOPPING' ? '전체 FMS 종료 중…' : '전체 FMS 시작 중… 서버 복귀를 기다립니다.')
          }
        }
      } catch {
        if (!stopped && pending) setMessage('서버 연결이 끊겼습니다. 자동 시작 후 다시 연결합니다…')
        else if (!stopped) setStatus(null)
      } finally {
        if (!stopped) timer = window.setTimeout(poll, pending ? 1000 : 5000)
      }
    }
    void poll()
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [pending])

  useEffect(() => {
    if (!pending) return
    const remaining = Math.max(0, 900000 - (Date.now() - pending.at))
    const timer = window.setTimeout(() => {
      setFailed(true); setMessage('서버 복귀 확인 시간이 초과됐습니다. FMS PC의 logs/web_restart.log를 확인하고 start_fms.sh를 실행하세요.')
    }, remaining)
    return () => window.clearTimeout(timer)
  }, [pending])

  const restart = async () => {
    if (sending.current || pending || !status?.available) return
    setConfirming(false)
    sending.current = true
    const waiting: Pending = { at: Date.now(), previous: status.instance_id }
    setFailed(false); setMessage('FMS 전체 재시작을 요청합니다…')
    storePending(waiting); setPending(waiting)
    try {
      const result = await restartFmsWeb()
      const next = { ...waiting, token: result.token, previous: result.previous_instance }
      storePending(next); setPending(next)
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error)
      if (/^\d{3} /.test(text)) {
        storePending(null); setPending(null); setFailed(true); setMessage(text)
      } else setMessage('응답을 받지 못했습니다. 재시작 진행 여부를 자동 확인합니다…')
    } finally { sending.current = false }
  }

  return <>
    <button type="button" disabled={!!pending || !status?.available || status.restarting}
      title={status?.reason ?? '재시작 API를 확인 중입니다. 백엔드 업데이트 후 사용할 수 있습니다.'}
      onClick={() => setConfirming(true)} style={{ border: `1px solid ${C.line}`, borderRadius: 7, padding: '7px 10px', background: C.surface, fontWeight: 700, fontSize: 11 }}>
      FMS 전체 재시작
    </button>
    {!pending && failed && <span role="alert" style={{ color: C.danger, fontSize: 11 }}>{message}</span>}
    {confirming && createPortal(<div role="dialog" aria-modal="true" aria-label="FMS 전체 재시작 확인"
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,.65)', zIndex: 10000, display: 'grid', placeItems: 'center', padding: 24 }}>
      <section style={{ background: '#fff', borderRadius: 14, padding: 30, maxWidth: 480, color: C.text }}>
        <h2 style={{ fontSize: 20, marginTop: 0 }}>전체 FMS를 재시작할까요?</h2>
        <p>작업 여부와 관계없이 stop_fms.sh → start_fms.sh를 실행합니다. 메모리에 있던 주문·운영 상태는 초기화될 수 있습니다.</p>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button onClick={() => setConfirming(false)}>취소</button>
          <button onClick={() => void restart()} style={{ background: C.danger, color: '#fff', border: 0, borderRadius: 5, padding: '8px 12px' }}>전체 종료 후 재시작</button>
        </div>
      </section>
    </div>, document.body)}
    {pending && createPortal(<div role="dialog" aria-modal="true" aria-label="FMS 전체 재시작 진행"
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,.82)', zIndex: 10000, display: 'grid', placeItems: 'center', padding: 24 }}>
      <section style={{ background: '#fff', borderRadius: 14, padding: 30, maxWidth: 480, color: C.text }}>
        <h2 style={{ fontSize: 20, marginTop: 0 }}>FMS 전체 재시작</h2>
        <p role="status" style={{ color: failed ? C.danger : C.primary }}>{message || '종료 후 자동으로 시작합니다…'}</p>
        <p style={{ color: C.muted, fontSize: 13 }}>서버가 준비되면 자동으로 새로고침합니다. 이 화면을 열어두세요.</p>
        {failed && <button onClick={() => { storePending(null); setPending(null) }}>닫기</button>}
      </section>
    </div>, document.body)}
  </>
}
