import { useEffect, useState } from 'react'
import { getScenarios, getScenarioStatus, runScenario, stopOrderSupply, type ScenarioItem, type ScenarioStatus, type ScenarioRouteSource } from '../api/fmsApi'
import { C } from '../constants/theme'
import OrderTrafficControls from './OrderTrafficControls'

const checkResultLabels: Record<string, string> = {
  VALID: '예약 검사 통과',
  NO_FEASIBLE_RESERVATION: '다른 로봇의 점유·예약 때문에 이동 불가',
  BLOCKED_NODE: '차단된 노드를 지나므로 제외',
  BLOCKED_EDGE: '차단된 통로를 지나므로 제외',
  ORIGINAL_GOAL_NOT_RESTORED: '목적지 도착·양보 복귀가 끝나지 않아 제외',
}

const scenarioGroupHelp: Record<string, string> = {
  '일반 주문': '주문을 계속 넣어 배차·픽업·작업·복귀를 확인해요. 여러 대 양보 자동 복구는 별도 시험이에요.',
  '경로 연결 확인': '주문 없이 정해진 위치에서 시작해요. 경로 생성 결과가 교통조정·이동으로 이어지는지 확인해요.',
  '교통조정만 시험': '주문 없이 정해진 경로로 시험해요. 완성된 조정안 중 선택하거나, 이동 불가 시 출발하지 않는지 확인해요.',
  '여러 대 양보 비교': '주문 없는 연구 비교예요. 두 로봇이 비켜주는 순서를 코드·KEV로 찾아요. 일반 주문 자동 복구와 별개예요.',
}

export default function ScenarioControls({ modeSwitching, onActiveChange }: {
  modeSwitching: boolean
  onActiveChange: (active: boolean) => void
}) {
  const [items, setItems] = useState<ScenarioItem[]>([])
  const [selected, setSelected] = useState('')
  const [routeSource, setRouteSource] = useState<ScenarioRouteSource>('EXISTING_SHORTEST_PATH')
  const [status, setStatus] = useState<ScenarioStatus | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const active = status?.state === 'running' || status?.state === 'stopping'
  const selectedItem = items.find(item => item.name === selected)
  const routeChoices = selectedItem?.route_source_options ?? []
  const groups = [...new Set([
    '일반 주문', '경로 연결 확인', '교통조정만 시험', '여러 대 양보 비교',
    ...items.map(item => item.group ?? '기존 관제'),
  ])].filter(group => items.some(item => (item.group ?? '기존 관제') === group))

  useEffect(() => {
    setRouteSource(selectedItem?.route_source ?? 'EXISTING_SHORTEST_PATH')
  }, [selected, selectedItem?.route_source])

  const refreshList = async () => {
    try {
      // 예전 시험은 기존 API·CLI와 자동 검사에서 유지하고 화면 목록만 정리한다.
      const list = (await getScenarios()).filter(item => item.group !== '개발용 보관')
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

  const execute = async () => {
    setSubmitting(true)
    setError('')
    try { setStatus(await runScenario(selected, routeChoices.length ? routeSource : undefined)) }
    catch (e) { setError(e instanceof Error ? e.message : '시나리오 실행 실패') }
    finally { setSubmitting(false) }
  }
  const stopSupply = async () => {
    setSubmitting(true)
    setError('')
    try { setStatus(await stopOrderSupply()) }
    catch (e) { setError(e instanceof Error ? e.message : '주문 공급 중지 실패') }
    finally { setSubmitting(false) }
  }
  const disabled = active || submitting || modeSwitching || !status || Boolean(connectionError)
  const statusMatchesSelection = status?.name === selected
  const message = error || connectionError || (status?.name
    ? `${statusMatchesSelection ? '' : '마지막 실행: '}${status.description} · ${status.message}` : '')
  const coordination = statusMatchesSelection ? status?.coordination : undefined
  const measured = coordination?.actual_metrics
  const seconds = (value?: number | null) => value == null ? '미완료·값 없음' : `${value.toFixed(2)}초`

  return (
    <div style={{ position: 'absolute', top: 14, left: 56, zIndex: 21, maxWidth: 'calc(100% - 76px)' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <select aria-label="테스트 시나리오" value={selected} disabled={disabled}
          onFocus={() => { void refreshList() }} onChange={e => setSelected(e.target.value)}
          style={{ width: 250, maxWidth: '100%', minWidth: 0, height: 32, border: `1px solid ${C.line}`, borderRadius: 6, background: '#fff', color: C.text, fontSize: 11, padding: '0 8px' }}>
          {!items.length && <option value="">시나리오 없음</option>}
          {groups.map(group => <optgroup key={group} label={group}>
            {items.filter(item => (item.group ?? '기존 관제') === group).map(item =>
              <option key={item.name} value={item.name}>{item.description}</option>)}
          </optgroup>)}
        </select>
        <button disabled={disabled || !selected} onClick={() => { void execute() }}
          style={{ height: 32, padding: '0 12px', border: 0, borderRadius: 6, background: disabled || !selected ? '#DDE3EC' : C.primary, color: disabled ? C.muted : '#fff', fontSize: 11, fontWeight: 700, whiteSpace: 'nowrap', cursor: disabled ? 'not-allowed' : 'pointer' }}>
          {active || submitting ? '실행 중…' : '테스트 실행'}
        </button>
        {active && status?.kind === 'order_stream' && <button
          disabled={submitting || Boolean(connectionError)} onClick={() => { void stopSupply() }}
          style={{ height: 32, padding: '0 12px', border: 0, borderRadius: 6, background: C.danger, color: '#fff', fontSize: 11, fontWeight: 700, whiteSpace: 'nowrap', cursor: submitting ? 'not-allowed' : 'pointer' }}>
          주문 공급 중지
        </button>}
      </div>
      {selectedItem && <div style={{ marginTop: 6, maxWidth: 335, color: C.muted, fontSize: 11 }}>
        {scenarioGroupHelp[selectedItem.group ?? ''] ?? '정해진 배치의 별도 시험이에요. 일반 주문 처리 시험과 구분해요.'}
      </div>}
      {selectedItem?.group === '일반 주문' && <OrderTrafficControls disabled={disabled} />}
      {selectedItem?.traffic_selector && <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginTop: 6, maxWidth: 250 }}>
        {routeChoices.length > 0 && <select aria-label="시나리오 경로 생성" value={routeSource} disabled={disabled}
          onChange={e => setRouteSource(e.target.value as ScenarioRouteSource)}
          style={{ maxWidth: '100%', height: 32, border: `1px solid ${C.line}`, borderRadius: 6, background: '#fff', color: C.text, fontSize: 11, padding: '0 8px' }}>
          {routeChoices.map(source => <option key={source} value={source}>
            {source === 'KEV' ? '경로 생성: KEV' : '경로 생성: A*'}
          </option>)}
        </select>}
        {!routeChoices.length && <span style={{ color: C.text, fontSize: 11 }}>경로: 시험용 고정 경로</span>}
        <span style={{ color: C.text, fontSize: 11 }}>{selectedItem.traffic_selector === 'guided'
          ? '교통조정: KEV가 다음 동작 제안 + 코드 검사'
          : selectedItem.traffic_selector === 'search'
          ? '교통조정: 코드 자동 탐색 (KEV 호출 없음)'
          : selectedItem.traffic_selector === 'fixed' ? '교통조정: 고정 계획 (KEV 호출 없음)' : '교통조정: KEV'}</span>
      </div>}
      {selectedItem?.test_goal && <details style={{ marginTop: 6, maxWidth: 335, padding: '6px 8px', background: '#fff', color: C.text, fontSize: 12, overflowWrap: 'anywhere' }}>
        <summary style={{ cursor: 'pointer' }}>이 시험에서 확인하는 것</summary>
        <p>{selectedItem.test_goal}</p>
      </details>}
      {message && <div role="status" style={{ marginTop: 6, maxWidth: 440, maxHeight: 150, overflowY: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', padding: '7px 9px', borderRadius: 6, background: '#fff', color: error || connectionError || status?.state === 'failed' ? C.danger : C.text, fontSize: 11, boxShadow: '0 2px 6px #0001' }}>{message}</div>}
      {coordination?.recovery_search && <div style={{ marginTop: 6, maxWidth: 440, padding: '6px 8px', background: '#fff', color: C.text, fontSize: 11 }}>
        탐색 검사 {coordination.recovery_search.checks}회 / 최대 {coordination.recovery_search.limits.max_checks}회
        {' · '}{coordination.recovery_search.found_steps == null ? '해결 순서 미발견' : `${coordination.recovery_search.found_steps}단계 계획 발견`}
        {' · '}{coordination.recovery_search.elapsed_seconds.toFixed(3)}초
        {' · '}KEV 제안 {coordination.recovery_search.model_calls ?? 0}회
        {coordination.recovery_search.motion_estimates_enabled && <> · 거리·이동 시간 정보 포함</>}
        {Boolean(coordination.recovery_search.code_after_model_budget_decisions) &&
          <> · 제안 한도 뒤 코드 보완 {coordination.recovery_search.code_after_model_budget_decisions}회</>}
        {coordination.recovery_search.branch_policy === 'DEFER' &&
          <> · 나중에 검토하도록 보관 {coordination.recovery_search.deferred_branches ?? 0}개 / 다시 검토 {coordination.recovery_search.resumed_branches ?? 0}개</>}
      </div>}
      {Boolean(coordination?.recovery_search?.guidance_calls?.length) && <details style={{ marginTop: 6, maxWidth: 440, maxHeight: 190, overflow: 'auto', padding: '7px 9px', background: '#fff', color: C.text, fontSize: 11 }}>
        <summary style={{ cursor: 'pointer' }}>KEV가 제안한 동작 보기</summary>
        <p>계획을 찾으며 검사할 동작을 제안한 기록이에요. 실제 출발은 완성 계획 검사 후에 해요.</p>
        {coordination?.recovery_search?.motion_estimates_enabled &&
          <p>거리와 이동 예상 시간도 KEV에 줘요. 거리÷0.2m/s 기준이며 회전·예약 대기·모델 응답 시간은 제외해요. 짧은 이동이 전체 최단 시간을 보장하지는 않아요.</p>}
        {coordination?.recovery_search?.guidance_calls?.map(call => <p key={call.call}>
          {call.call}회: {call.proposed_move
            ? `${call.proposed_move.robot_id.replace(/^robot(\d+)$/, 'R$1')} ${call.proposed_move.action === 'YIELD' ? '양보 검토' : call.proposed_move.purpose === 'RESTORE_ORIGINAL_GOAL' ? '복귀 검토' : '진행 검토'} · ${call.proposed_move.path.map(n => `N${n}`).join(' → ')}`
            : call.proposed_control === 'BACKTRACK'
              ? coordination.recovery_search?.branch_policy === 'DEFER' ? '이 순서는 보관하고 다른 순서 먼저 검토' : '이 순서를 포기하고 다른 순서 검토'
              : call.status}
          {call.check_result && ` · ${checkResultLabels[call.check_result] ?? '이동 조건을 충족하지 못해 제외'}`}
          {call.proposed_move?.estimated_motion &&
            ` · ${call.proposed_move.estimated_motion.distance_m.toFixed(2)}m / 이동 예상 ${call.proposed_move.estimated_motion.travel_seconds.toFixed(2)}초`}
        </p>)}
      </details>}
      {coordination?.candidate_estimates && <details style={{ marginTop: 6, maxWidth: 440, maxHeight: 260, overflow: 'auto', padding: '7px 9px', borderRadius: 6, background: '#fff', color: C.text, fontSize: 11 }}>
        <summary style={{ cursor: 'pointer' }}>예상 시간과 실제 시간 보기</summary>
        <p>예상: 거리÷속도, 한 단계씩 이동. 판단·회전·예약 지연은 제외해요.</p>
        <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
          <thead><tr><th>방법</th><th>평균 도착 예상</th><th>총 이동 거리</th></tr></thead>
          <tbody>{Object.entries(coordination.candidate_estimates).map(([id, estimate]) => <tr key={id}>
            <td style={{ padding: '4px 6px 4px 0' }}>{id === coordination.decision.joint_action_id ? '✓ ' : ''}{coordination.candidate_descriptions?.[id] ?? id}</td>
            <td>{seconds(estimate.mean_goal_completion_seconds)}</td>
            <td>{estimate.total_distance_m.toFixed(2)}m</td>
          </tr>)}</tbody>
        </table>
        {measured && <>
          <p>선택한 방법의 실제 평균 도착: {seconds(measured.mean_goal_completion_seconds)}<br />
            준비·판단 포함 평균: {seconds(measured.mean_request_to_goal_seconds)}<br />
            준비: {seconds(measured.setup_seconds)} (선택 요청: {seconds(measured.selection_seconds)})<br />
            가장 오래 출발 차례를 기다린 시간: {seconds(measured.max_turn_wait_seconds)}</p>
          {measured.unfinished_robot_ids.length > 0 && <p>아직 목적지에 도착하지 않은 로봇: {measured.unfinished_robot_ids.join(', ')}</p>}
        </>}
        <p>평균은 원래 이동할 일이 있는 로봇만 계산해요. 실제 이동 단계에는 예약 정지·도착 확인 시간도 포함돼요.</p>
      </details>}
    </div>
  )
}
