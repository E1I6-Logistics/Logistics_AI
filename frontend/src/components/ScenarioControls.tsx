import { useEffect, useState } from 'react'
import { getScenarios, getScenarioStatus, runScenario, stopScenario, resetScenario, downloadScenarioRecord, scenarioRecordUrl, type ScenarioItem, type ScenarioStatus } from '../api/fmsApi'
import { C } from '../constants/theme'

export default function ScenarioControls({ modeSwitching, onActiveChange }: {
  modeSwitching: boolean
  onActiveChange: (active: boolean) => void
}) {
  const [items, setItems] = useState<ScenarioItem[]>([])
  const [selected, setSelected] = useState('')
  const [status, setStatus] = useState<ScenarioStatus | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const active = status?.state === 'running' || status?.state === 'stopping'

  const refreshList = async () => {
    try {
      const list = await getScenarios()
      setItems(list)
      setSelected(current => list.some(item => item.name === current) ? current : list[0]?.name ?? '')
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : '시나리오 목록 조회 실패')
    }
  }

  useEffect(() => { void refreshList() }, [])
  useEffect(() => {
    let closed = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await getScenarioStatus()
        if (!closed) { setStatus(next); setConnectionError('') }
      } catch (e) {
        if (!closed) setConnectionError(e instanceof Error ? e.message : '실행 상태 조회 실패')
      } finally {
        if (!closed) timer = setTimeout(() => { void poll() }, 500)
      }
    }
    void poll()
    return () => { closed = true; clearTimeout(timer) }
  }, [])
  useEffect(() => { onActiveChange(active || submitting) }, [active, submitting, onActiveChange])

  const saveAndReset = async (next: ScenarioStatus) => {
    if (!next.run_id || !next.record_url || next.state === 'running' || next.state === 'stopping') return
    await downloadScenarioRecord(next.run_id)
    setStatus(await resetScenario(next.run_id))
  }
  const stop = async () => {
    setSubmitting(true)
    setError('')
    try {
      const next = await stopScenario()
      setStatus(next)
      await saveAndReset(next)
    }
    catch (e) { setError(e instanceof Error ? e.message : '시나리오 중지 실패') }
    finally { setSubmitting(false) }
  }
  const execute = async () => {
    setSubmitting(true)
    setError('')
    try { setStatus(await runScenario(selected)) }
    catch (e) { setError(e instanceof Error ? e.message : '시나리오 실행 실패') }
    finally { setSubmitting(false) }
  }
  const disabled = active || submitting || modeSwitching || !status || Boolean(connectionError)
  const message = error || connectionError || (status?.name && (status.name !== 'random_order_stress' || active) ? `${status.description} · ${status.message}` : '')

  return (
    <div style={{ position: 'absolute', top: 14, left: 56, zIndex: 21, maxWidth: 'calc(100% - 76px)' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <select aria-label="테스트 시나리오" value={selected} disabled={disabled}
          onFocus={() => { void refreshList() }} onChange={e => setSelected(e.target.value)}
          style={{ width: 250, maxWidth: '55vw', height: 32, border: `1px solid ${C.line}`, borderRadius: 6, background: '#fff', color: C.text, fontSize: 11, padding: '0 8px' }}>
          {!items.length && <option value="">시나리오 없음</option>}
          {items.map(item => <option key={item.name} value={item.name}>{item.description}</option>)}
        </select>
        <button disabled={disabled || !selected} onClick={() => { void execute() }}
          style={{ height: 32, padding: '0 12px', border: 0, borderRadius: 6, background: disabled || !selected ? '#DDE3EC' : C.primary, color: disabled ? C.muted : '#fff', fontSize: 11, fontWeight: 700, whiteSpace: 'nowrap', cursor: disabled ? 'not-allowed' : 'pointer' }}>
          {active || submitting ? '실행 중…' : '테스트 실행'}
        </button>
        {active && <button onClick={() => { void stop() }} disabled={submitting || status?.state === 'stopping'}
          style={{ height: 32, border: 0, borderRadius: 6, padding: '0 10px', background: C.danger, color: '#fff', fontSize: 11, whiteSpace: 'nowrap' }}>
          {status?.name === 'random_order_stress' ? '자동배차 취소' : '테스트 중지'}
        </button>}
      </div>
      {active && status?.name === 'random_order_stress' && <div style={{ marginTop: 6, padding: 8, background: '#fff', borderRadius: 6, fontSize: 11, maxWidth: 440 }}>
        <div>경과 {Math.floor((status.elapsed ?? 0) / 60)}분 {Math.floor((status.elapsed ?? 0) % 60)}초 · 주문당 1~5개 · 미완료 최대 6건 · 수동 중지까지 반복</div>
        {status.metrics && <>
          <div>총 주문 {status.metrics.created} · 성공 {status.metrics.completed} · 실패 {status.metrics.failed} · 진행 중 {status.metrics.open_orders}</div>
          {status.metrics.failures?.map(failure => <div key={failure.order_id}>
            실패 위치: {failure.robot_id} · {failure.phase} · {failure.node ? `N${failure.node}` : `간선 ${failure.edge ?? '미확인'}`} · {failure.reason}
          </div>)}
        </>}
        {status.run_id && <a href={scenarioRecordUrl(status.run_id)} download>시험 기록 내려받기 (JSON)</a>}
        <div>중지하면 시작~중지 구간의 기록을 다운로드하고 시험 카운터를 초기화합니다. 접수된 주문은 계속 처리합니다.</div>
      </div>}
      {error && !active && status?.name === 'random_order_stress' && status.run_id && <button
        onClick={() => { void saveAndReset(status).then(() => setError('')).catch(e => setError(e instanceof Error ? e.message : '기록 저장 실패')) }} disabled={submitting}>
        기록 다운로드 다시 시도
      </button>}
      {message && <div role="status" style={{ marginTop: 6, maxWidth: 440, maxHeight: 150, overflowY: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', padding: '7px 9px', borderRadius: 6, background: '#fff', color: error || connectionError || status?.state === 'failed' ? C.danger : C.text, fontSize: 11, boxShadow: '0 2px 6px #0001' }}>{message}</div>}
    </div>
  )
}
