import { useState } from 'react'
import { cancelReplenishment, type ReplenishmentDto } from '../api/fmsApi'
import { C } from '../constants/theme'

const ACTIVITY: Record<string, string> = {
  SENDING: '액션 접수 확인 중', RUNNING: '재고 보충 가동 중', CANCELLING: '취소 처리 중',
  READY: '액션 서버 대기 가능', UNKNOWN: '가동 상태 확인 필요',
}
const JOB: Record<string, string> = { SUCCEEDED: '보충 동작 완료', FAILED: '실패', CANCELLED: '취소 완료', REJECTED: '접수 거절' }
const at = (value?: number | null) => value == null ? '미수신' : new Date(value * 1000).toLocaleTimeString('ko-KR')

export default function WafPanel({ data, error, onClose }: {
  data: ReplenishmentDto | null; error: string | null; onClose: () => void
}) {
  const robot = data?.robot
  const job = data?.job
  const feedback = robot?.feedback
  const [notice, setNotice] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const cancel = async () => {
    if (!job || cancelling || !window.confirm('WAF 재고 보충 액션을 취소할까요? 실제 종료 결과까지 기다립니다.')) return
    setCancelling(true)
    try { await cancelReplenishment(job.request_id); setNotice('취소 요청 전송 · 종료 결과 확인 중') }
    catch (e) { setNotice(e instanceof Error ? e.message : '취소 결과 확인 실패') }
    finally { setCancelling(false) }
  }
  const connection = error || robot?.connection_state === 'UNKNOWN' ? '연결 확인 필요'
    : robot?.connected ? 'Zenoh 연결됨' : 'Zenoh 연결 끊김'
  const ownActive = job && ['SENDING', 'RUNNING', 'CANCELLING', 'UNKNOWN'].includes(job.state)
  const freshFeedback = !job?.started_at || (feedback?.received_at ?? 0) >= job.started_at
  const stage = ownActive ? job.stage || (freshFeedback ? feedback?.stage : null) : feedback?.stage || job?.stage
  return <aside className="fms-robot-panel" aria-label="WAF robot4 상태"
    style={{ width: 390, flexShrink: 0, padding: 24, boxSizing: 'border-box', overflowY: 'auto', background: C.surface }}>
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
      <h2 style={{ margin: 0, fontSize: 19 }}>R-04 · WAF</h2>
      <button onClick={onClose} aria-label="WAF 상세 닫기">×</button>
    </div>
    <p style={{ color: C.muted }}>재고 보충 로봇 · 액션 전용</p>
    <strong style={{ color: robot?.connected && !error ? C.success : C.warning }}>{connection}</strong>
    <p style={{ fontSize: 12 }}>IP: {robot?.ip || '미수신'} · 최근 연결 확인: {at(robot?.last_seen)}</p>
    <div style={{ padding: 14, background: C.subtle, borderRadius: 8 }}>
      <strong>{error ? '상태 조회 실패' : ACTIVITY[robot?.activity ?? 'UNKNOWN'] ?? '상태 확인 필요'}</strong>
      <p>배터리: {robot?.battery == null ? '잔량 미수신' : `${robot.battery.toFixed(0)}%`}
        {robot?.battery_voltage != null && ` · ${robot.battery_voltage.toFixed(2)}V`}</p>
      <p style={{ color: C.muted, fontSize: 12 }}>배터리 수신: {at(robot?.battery_received_at)}{robot && !robot.battery_fresh ? ' · 최신 값 대기' : ''}</p>
      <p>보충 액션: {data?.available ? '연결됨' : data?.reason ?? '확인 중'}</p>
      <p style={{ color: C.muted, fontSize: 12, overflowWrap: 'anywhere' }}>{job?.action_name || data?.action_name}</p>
    </div>
    <h3>실제 수신 피드백</h3>
    <p style={{ fontWeight: 700, overflowWrap: 'anywhere' }}>{stage || '아직 수신된 단계 없음'}</p>
    <p style={{ color: C.muted, fontSize: 12 }}>피드백 수신: {at(feedback?.received_at || job?.updated_at)}</p>
    {job && <>
      <p>요청 품목: {job.item ?? '미확인'} · 목적지 {job.destination ?? '미확인'}</p>
      <p>{JOB[job.state] ?? ACTIVITY[job.state] ?? job.state}</p>
      <p style={{ overflowWrap: 'anywhere' }}>{job.message}</p>
    </>}
    {data?.busy && data.can_cancel && <button disabled={cancelling || job?.cancel_requested} onClick={() => void cancel()}>
      {cancelling || job?.cancel_requested ? '액션 종료 대기' : '보충 액션 취소'}
    </button>}
    {notice && <p role="status">{notice}</p>}
    <h3>수신 위치</h3>
    <p>{robot?.pose ? `x=${robot.pose.x.toFixed(2)}, y=${robot.pose.y.toFixed(2)}, 방향=${(robot.pose.yaw * 180 / Math.PI).toFixed(0)}°` : '최신 위치 미수신'}</p>
    <p style={{ color: C.muted, fontSize: 12 }}>{robot?.pose?.source} · {robot?.pose?.frame_id} · {at(robot?.pose_received_at)}</p>
    <p style={{ color: C.muted, fontSize: 12 }}>A·B·C·D 재고 보충 버튼에서 액션을 요청합니다.</p>
    {error && <p role="alert" style={{ color: C.danger }}>{error}</p>}
  </aside>
}
