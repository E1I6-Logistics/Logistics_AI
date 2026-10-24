import { useEffect, useState } from 'react'
import { getRobotMode, setOrderTrafficPolicy, type OrderTrafficPolicy, type RobotModeDto } from '../api/fmsApi'
import { C } from '../constants/theme'

const labels: Record<OrderTrafficPolicy, string> = {
  KEV: '기존 · 만든 경로 중 KEV 선택',
  MIN_EXPECTED_TIME: '코드 · 예상 시간이 짧은 경로',
  KEV_STRATEGY_ORDER: '새 방식 · KEV가 전략 순서 제안',
}

export default function OrderTrafficControls({ disabled }: { disabled: boolean }) {
  const [mode, setMode] = useState<RobotModeDto | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let closed = false
    const refresh = async () => {
      try { const next = await getRobotMode(); if (!closed) setMode(next) }
      catch (e) { if (!closed) setError(e instanceof Error ? e.message : '교통조정 설정 조회 실패') }
    }
    void refresh()
    const timer = setInterval(() => { void refresh() }, 2000)
    return () => { closed = true; clearInterval(timer) }
  }, [])
  const traffic = mode?.order_traffic
  if (mode?.mode !== 'simulation' || mode.operation_mode !== 'ai' || !traffic?.enabled) return null
  const change = async (policy: OrderTrafficPolicy) => {
    setBusy(true); setError('')
    try { setMode(await setOrderTrafficPolicy(policy)) }
    catch (e) { setError(e instanceof Error ? e.message : '교통조정 설정 변경 실패') }
    finally { setBusy(false) }
  }
  return <div style={{ marginTop: 6, maxWidth: 335, fontSize: 11, color: C.text }}>
    <label>교통조정{' '}
      <select aria-label="일반 주문 교통조정 방식" value={traffic.selection_policy ?? 'KEV'}
        disabled={disabled || busy} onChange={e => { void change(e.target.value as OrderTrafficPolicy) }}
        style={{ maxWidth: '100%', height: 32, border: `1px solid ${C.line}`, borderRadius: 6, background: '#fff', color: C.text, fontSize: 11 }}>
        {(traffic.available_policies ?? ['KEV', 'MIN_EXPECTED_TIME']).map(policy =>
          <option key={policy} value={policy}>{labels[policy]}</option>)}
      </select>
    </label>
    <div style={{ marginTop: 4 }}>경로 생성: {traffic.route_source === 'EXISTING_SHORTEST_PATH' ? 'A*'
      : traffic.route_source === 'KEV' ? 'KEV' : '운영 모드 설정 따름'}
      {mode.simulation_preview && ' · 시뮬 전용 · 실기체 연결 없음'}</div>
    <details style={{ marginTop: 4, color: C.muted }}>
      <summary style={{ cursor: 'pointer' }}>방식 설명·변경 조건</summary>
      <p>주문·복귀가 없을 때 변경해요. 다른 로봇의 주행과 예약은 유지해요.</p>
      {traffic.selection_policy === 'KEV_STRATEGY_ORDER' && <p>
        새 이동 요청 하나의 경로 전략을 제안해요. 여러 로봇의 통과 순서 탐색은 별도 시험이에요.
      </p>}
    </details>
    {error && <div role="alert" style={{ marginTop: 4, color: C.danger }}>{error}</div>}
  </div>
}
