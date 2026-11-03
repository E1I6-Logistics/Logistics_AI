import { useRef, useState } from 'react'
import { restartOmx, type OmxDeviceDto } from '../api/fmsApi'
import { C } from '../constants/theme'
import PalletReplenishment from './PalletReplenishment'

const STATES: Record<string, string> = {
  IDLE: '주문 대기', READY: '준비 완료', WORKING: '작업 중', COMMAND_SENT: '접수 대기',
  ERROR: '오류 · 복구 대기', FAILED: '작업 실패', REJECTED: '명령 거절', SUCCESS: '작업 완료', OFFLINE: '연결 끊김',
}
const STOCK = { PRESENT: '물건 있음', EMPTY: '비어 있음', UNKNOWN: '확인 불가' }
const colorFor = (state: string) => state === 'PRESENT' ? C.success : state === 'EMPTY' ? C.warning : C.muted

export default function OmxPanel({ device, queryError, realMode, blocked, onClose }: {
  device: OmxDeviceDto; queryError: string | null; realMode: boolean; blocked: boolean; onClose: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null)
  const submitting = useRef(false)
  const queryStale = !!queryError || !!device.observation_stale
  const online = device.connected && !queryStale
  const working = ['WORKING', 'COMMAND_SENT'].includes(device.state)
  const restart = async () => {
    if (submitting.current) return
    if (!window.confirm(`${device.omx_id.toUpperCase()}의 OMX 서비스를 재시작할까요? 홈 복귀 후 대기하며 이전 작업은 자동 재실행하지 않습니다.`)) return
    submitting.current = true
    setBusy(true)
    setNotice(null)
    try {
      const result = await restartOmx(device.omx_id)
      setNotice({ error: false, text: result.message })
    } catch (error) {
      setNotice({ error: true, text: error instanceof Error ? error.message : '재시작 요청 결과를 확인하지 못했습니다. 장치 상태를 확인하세요.' })
    } finally { submitting.current = false; setBusy(false) }
  }
  return <aside className="fms-robot-panel" aria-label={`${device.omx_id.toUpperCase()} 상태`}
    style={{ width: 390, flexShrink: 0, borderLeft: `1px solid ${C.line}`, background: C.surface, padding: 24, boxSizing: 'border-box', overflowY: 'auto' }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <h2 style={{ margin: 0, fontSize: 19 }}>{device.omx_id.toUpperCase()}</h2>
      <button onClick={onClose} aria-label="OMX 상세 닫기" style={{ border: 0, background: 'none', cursor: 'pointer', fontSize: 22 }}>×</button>
    </div>
    <p style={{ color: queryStale ? C.warning : online ? C.success : C.danger, fontWeight: 700 }}>{queryStale ? '● 상태 조회 지연' : online ? '● 연결됨' : '● 연결 확인 필요'}</p>
    <div style={{ padding: 14, borderRadius: 8, background: C.subtle }}>
      <strong>{queryError ? '상태 조회 실패' : STATES[online ? device.state : 'OFFLINE'] ?? device.state}</strong>
      {device.detail && <p style={{ overflowWrap: 'anywhere', color: C.muted }}>{device.detail === 'RECOVERY_WAIT' ? '복구 대기 · 현장 상태 확인 필요' : device.detail}</p>}
      <p style={{ fontSize: 12, color: C.muted, marginBottom: 0 }}>최근 장치 수신: {device.last_seen ? new Date(device.last_seen * 1000).toLocaleTimeString('ko-KR') : '아직 없음'}</p>
    </div>
    {queryError && <p role="alert" style={{ color: C.danger, fontSize: 12 }}>{queryError} 재연결 후 자동 갱신합니다.</p>}
    <h3 style={{ marginTop: 24, fontSize: 14 }}>담당 팔레트</h3>
    {device.stock?.supported ? <>
      {Object.entries(device.stock.palettes).map(([item, row]) => {
        const state = online && device.stock.fresh ? row.state : 'UNKNOWN'
        return <div key={item} style={{ border: `1px solid ${C.line}`, borderRadius: 8, marginBottom: 8, padding: 13 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}><strong>{item} 팔레트</strong><strong style={{ color: colorFor(state) }}>{STOCK[state]}</strong></div>
          {state === 'UNKNOWN' && <p style={{ color: C.muted, fontSize: 12, marginBottom: 0 }}>{queryError ? 'FMS 상태 수신 대기' : row.reason}</p>}
          <PalletReplenishment item={item} realMode={realMode} blocked={blocked || working} />
        </div>
      })}
      <p style={{ color: C.muted, fontSize: 12 }}>존재 여부만 표시합니다. 실제 개수는 계산하지 않습니다.</p>
    </> : <p style={{ color: C.muted, fontSize: 13 }}>하차 담당 장비입니다. 팔레트 재고 검사는 사용하지 않습니다.</p>}
    <h3 style={{ marginTop: 24, fontSize: 14 }}>최근 작업</h3>
    <p style={{ fontSize: 13 }}>{device.count_kind === 'motion_cycles' ? '동작 횟수' : '진행'}: {device.current_count} / {device.total_count}</p>
    {device.job_id && <p style={{ color: C.muted, fontSize: 12, overflowWrap: 'anywhere' }}>작업: {device.job_id}</p>}
    {device.count_kind === 'motion_cycles' && <p style={{ color: C.warning, fontSize: 12 }}>동작 완료 횟수이며 실제 물건 전달 성공은 검증하지 않습니다.</p>}
    {device.message && <p style={{ color: C.muted, fontSize: 12, overflowWrap: 'anywhere' }}>{device.message}</p>}
    <h3 style={{ marginTop: 24, fontSize: 14 }}>서비스 관리</h3>
    <button disabled={queryStale || !device.restart_available || !realMode || blocked || working || busy} onClick={() => void restart()}
      style={{ border: `1px solid ${C.line}`, borderRadius: 7, padding: '9px 14px', fontWeight: 700 }}>
      {busy ? '재시작 요청 중…' : 'OMX 서비스 재시작'}
    </button>
    <p style={{ color: C.muted, fontSize: 12 }}>{!device.restart_available ? 'SSH 설정 후 사용할 수 있습니다.' : !realMode ? '실제 로봇 모드에서 사용할 수 있습니다.' : working ? '작업 중에는 재시작할 수 없습니다.' : 'Pi 전원 재부팅 없이 OMX 프로그램을 다시 시작합니다.'}</p>
    {notice && <p role="status" style={{ color: notice.error ? C.danger : C.primary, fontSize: 13 }}>{notice.text}</p>}
  </aside>
}
