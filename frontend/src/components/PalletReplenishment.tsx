import { useEffect, useRef, useState } from 'react'
import { getReplenishment, startReplenishment, cancelReplenishment, clearStoppedReplenishment, type ReplenishmentDto } from '../api/fmsApi'
import { C } from '../constants/theme'
import { replenishmentRequestId } from './palletReplenishmentId'

const STATES: Record<string, string> = {
  SENDING: '접수 확인 중', RUNNING: '보충 중', CANCELLING: '취소 처리 중',
  SUCCEEDED: '보충 동작 완료', FAILED: '보충 실패', REJECTED: '접수 거절',
  CANCELLED: '취소 완료', UNKNOWN: '실제 상태 확인 필요',
  ENDED: '작업 종료 · 상세 결과 미수신',
}
const STAGES: Record<string, string> = {
  NAV_TO_PICK: '해당 팔레트로 이동', ALIGN: '정렬·삽입·들어올리기·후진',
  NAV_TO_DROP: '반납 위치로 이동', LIFT_DOWN: '팔레트 내려놓기', BACKUP: '후진',
  NAV_TO_PICK_2: '보충 팔레트로 이동', ALIGN_2: '보충 팔레트 정렬·인수',
  NAV_TO_PLACE: '원래 위치로 이동', PLACE: '위치 보정·팔레트 배치',
  LIFT_DOWN_2: '보충 팔레트 내려놓기', BACKUP_2: '배치 후 후진', NAV_TO_HOME: 'WAF 홈 복귀',
}

export default function PalletReplenishment({ item, realMode, blocked }: {
  item: string; realMode: boolean; blocked: boolean
}) {
  const [data, setData] = useState<ReplenishmentDto | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const lock = useRef(false)
  const version = useRef(0)
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    const poll = async () => {
      const current = version.current
      try {
        const result = await getReplenishment(controller.signal)
        if (live && current === version.current) { setData(result); setError(null) }
      } catch (exc) {
        if (live && current === version.current) setError(exc instanceof Error ? exc.message : 'WAF 상태 조회 실패')
      } finally { if (live) timer = setTimeout(() => void poll(), 2000) }
    }
    void poll()
    return () => { live = false; controller.abort(); clearTimeout(timer) }
  }, [])
  const job = data?.job
  const own = job?.item === item
  const submit = async (action: 'start' | 'cancel' | 'clear' = 'start') => {
    if (lock.current) return
    const text = action === 'clear' ? 'WAF가 실제로 정지했고 이전 보충 작업도 종료됐는지 확인했나요? 이전 기록만 정리하며 로봇을 움직이거나 새 작업을 보내지 않습니다.'
      : action === 'cancel' ? '진행 중인 WAF 보충 작업을 취소할까요? 종료 결과를 확인할 때까지 기다립니다.'
      : `${item} 팔레트 재고 보충을 시작할까요? WAF가 실제로 이동합니다. 목적지: ${data?.destinations[item]}`
    if (!window.confirm(text)) return
    lock.current = true; version.current += 1; setSending(true); setError(null)
    try {
      const result = action === 'clear' && job ? await clearStoppedReplenishment(job.request_id)
        : action === 'cancel' && job ? await cancelReplenishment(job.request_id)
        : await startReplenishment(item, replenishmentRequestId())
      setData(result)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '요청 결과 확인 실패 · WAF 상태를 확인하세요')
    } finally { lock.current = false; setSending(false); version.current += 1 }
  }
  return <div style={{ marginTop: 12, fontSize: 12 }} aria-label={`${item} 재고 보충`}>
    <button disabled={!realMode || blocked || sending || !!error || !data?.available || data.busy}
      onClick={() => void submit()} style={{ padding: '7px 12px', border: `1px solid ${C.line}`, borderRadius: 6 }}>
      {sending ? '요청 중…' : '재고 보충'}
    </button>
    {!realMode ? <p style={{ color: C.muted }}>실제 로봇 모드에서 사용</p>
      : data?.busy && !own ? <p style={{ color: C.warning }}>WAF: {job?.item ?? '이전'} 보충 작업 처리 중</p>
      : !data?.available && <p style={{ color: C.muted }}>{data?.reason ?? 'WAF 연결 확인 중'}</p>}
    {own && job && <div role="status" style={{ marginTop: 8 }}>
      <strong style={{ color: ['FAILED', 'REJECTED', 'UNKNOWN'].includes(job.state) ? C.danger : C.primary }}>{STATES[job.state] ?? job.state}</strong>
      {job.stage && <p>{STAGES[job.stage] ?? job.stage}</p>}
      {job.message && <p style={{ overflowWrap: 'anywhere', color: C.muted }}>{job.message}</p>}
      {data?.busy && data.can_cancel && <button disabled={sending} onClick={() => void submit('cancel')}>
        {job.cancel_requested ? 'WAF 종료 확인 대기' : '보충 취소'}
      </button>}
      {data?.can_clear && <button disabled={sending} onClick={() => void submit('clear')}>이전 작업 종료 확인·기록 정리</button>}
      {data?.busy && !data.can_cancel && !data.can_clear && <p style={{ color: C.warning }}>
        {job.cancel_requested ? '취소 요청 접수 · WAF 실제 종료 확인 중' : 'WAF에서 실제 작업 종료를 확인하세요.'}
      </p>}
      {job.state === 'SUCCEEDED' && <p style={{ color: C.muted }}>팔레트 유무는 기존 OMX 검사 결과로 다시 확인합니다.</p>}
    </div>}
    {error && <p role="alert" style={{ color: C.danger }}>{error}</p>}
  </div>
}
