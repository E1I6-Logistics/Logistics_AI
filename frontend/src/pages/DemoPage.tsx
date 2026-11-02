import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  getRobotMode, getRobots, getConnections, getScenarioStatus, runScenario, stopScenario, sendGoalNode, sendCharging,
  stopRobot, restartOmx, scenarioRecordUrl, getDockingSettings,
  type RobotMode, type OperationMode, type RouteModelDto, type ScenarioItem, type ScenarioStatus,
  type OmxDeviceDto, type DockingStationDto,
} from '../api/fmsApi'
import { createOrder, cancelOrder, getOrders, type OrderDto, type OrderItems, type PickupDto, type WorkstationDto } from '../api/orderApi'
import type { ManagedRobot } from '../hooks/useRobotFleet'
import { DEMOS, MARKERS, CHARGERS, TRAFFIC, RUN_LABEL, isActive, activeOrder, eventRun, loadRuns, saveRuns, downloadReport, type DemoId, type DemoRun } from '../demo/model'
import { observationSnapshot } from '../demo/observations'
import CommunicationPanel from '../demo/CommunicationPanel'
import '../demo/demo.css'

type Props = {
  mode: RobotMode; operation: OperationMode; switching: boolean; routeModel: RouteModelDto | null;
  robots: ManagedRobot[]; nodes: string[]; omx: OmxDeviceDto[]; orders: OrderDto[];
  ordersAt: number; ordersError: string; fleetError: string | null; omxError: string | null;
  scenario: ScenarioStatus | null; scenarioError: string; cases: ScenarioItem[];
  docks: DockingStationDto[]; pickups: Record<string, PickupDto>; workstations: Record<string, WorkstationDto>;
  onActiveChange: (active: boolean) => void; mapView: React.ReactNode;
  routeModels: RouteModelDto[]; onModelChange: (model: RouteModelDto) => void;
}
type Confirmation = { title: string; detail: string; danger?: boolean; allowDuringSend?: boolean; action: () => Promise<void> }
const strError = (e: unknown) => e instanceof Error ? e.message : String(e)
const nodeName = (id: string | null | undefined) => id == null ? '미확인' : `N${String(id).replace(/^N/i, '')}`
const normalized = (id: string | null | undefined) => id?.replace(/^N/i, '')
const formatMs = (value: number | null | undefined) => value == null ? '—' : `${value.toFixed(0)} ms`
const clock = (at: number) => new Date(at).toLocaleTimeString('ko-KR', { hour12: false })
const STATUS: Record<string, string> = { IDLE: '대기', MOVING: '이동', NAVIGATING: '이동', WAITING: '차례 대기', DOCKING: '도킹·정렬', CHARGING: '충전 상태', EMERGENCY_STOP: '비상정지', OFFLINE: '오프라인', PAUSED: '일시정지', RETURNING: '복귀', READY: '준비 완료', WORKING: '작업 중', COMMAND_SENT: '접수 대기', ERROR: '오류·복구 대기', INITIALIZING: '초기화' }
const PHASE: Record<string, string> = { PENDING: '배차 대기', WAITING_PICKUP: '집기 순서 대기', MOVING_TO_PICKUP: '집기 위치 이동', WORKING_AT_PICKUP: '집기 작업', WAITING_WORKSTATION: '하차 순서 대기', MOVING_TO_WORKSTATION: '하차 위치 이동', WORKING_AT_WORKSTATION: '하차 작업', RETURNING: '복귀', COMPLETED: '완료', FAILED: '실패', CANCELLED: '취소' }
function uuid() { return Array.from(crypto.getRandomValues(new Uint32Array(3)), n => n.toString(16)).join('') }
function Pill({ children, tone = '' }: { children: React.ReactNode; tone?: string }) { return <span className={`demo-pill ${tone}`}>{children}</span> }
function Metric({ label, value, note }: { label: string; value: React.ReactNode; note?: string }) { return <div className="demo-metric"><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</div> }
export default function DemoPage(p: Props) {
  const [selected, setSelected] = useState<DemoId>('D01')
  const [tab, setTab] = useState('시험')
  const [robotId, setRobotId] = useState('robot1')
  const [omxId, setOmxId] = useState('omx1')
  const [node, setNode] = useState('')
  const [markerNode, setMarkerNode] = useState('5')
  const [traffic, setTraffic] = useState('corridor_head_on')
  const [items, setItems] = useState<OrderItems>({ A: 1, B: 0, C: 0, D: 0 })
  const [runs, setRuns] = useState<DemoRun[]>(loadRuns)
  const [confirm, setConfirm] = useState<Confirmation | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [stats, setStats] = useState(observationSnapshot)
  const sending = useRef(false)
  const cancelIntent = useRef(new Set<string>())
  const cancelling = useRef(false)
  const abortPreparation = useRef(false)
  const [preparing, setPreparing] = useState(false)
  const [cancelBusy, setCancelBusy] = useState(false)
  const mounted = useRef(true)
  const run = runs[0] ?? null
  const active = isActive(run)
  const total = Object.values(items).reduce((a, b) => a + b, 0)
  const robot = p.robots.find(r => r.backendId === robotId)
  const chosenOmx = p.omx.find(o => o.omx_id === omxId)
  const charger = CHARGERS[robotId] ? { ...CHARGERS[robotId], station: p.docks.find(d => d.robot_id === robotId)?.station ?? CHARGERS[robotId].station } : undefined
  const receipt = stats.robots[robotId]
  const robotFresh = !!receipt && stats.at - receipt.at < 6000
  const ordersFresh = p.ordersAt > 0 && stats.at - p.ordersAt < 6000 && !p.ordersError
  const enabledCases = p.cases.filter(c => c.name === traffic + (p.operation === 'ai' ? '_ai' : ''))
  const busyRobots = p.robots.filter(r => !['IDLE', 'CHARGING', 'OFFLINE'].includes(r.status) || r.route)
  const blocked = p.switching || active || pending || !ordersFresh || !!p.fleetError || !!p.scenarioError || !p.scenario || p.scenario.state === 'running' || p.scenario.state === 'stopping'
  const switchDemo = (id: DemoId) => { setSelected(id); setError('') }
  const patch = (id: string, fn: (r: DemoRun) => DemoRun) => setRuns(prev => prev.map(r => r.id === id ? fn(r) : r))

  useEffect(() => { mounted.current = true; const timer = setInterval(() => setStats(observationSnapshot()), 1000); return () => { mounted.current = false; clearInterval(timer) } }, [])
  useEffect(() => { p.onActiveChange(active || pending) }, [active, pending, p.onActiveChange])
  useEffect(() => { saveRuns(runs) }, [runs])
  useEffect(() => { if (!p.nodes.includes(node)) setNode(p.nodes[0] ?? '') }, [p.nodes, node])

  // 소유한 서버 ID만 대조한다. 화면 이동·새로고침으로 명령을 다시 보내지 않는다.
  useEffect(() => {
    if (p.switching || run?.instance && !stats.serverInstance || !run || !isActive(run) || run.phase === 'REVIEW' && !['D01', 'D02'].includes(run.demo)) return
    let next = run
    if (p.mode !== run.mode || p.operation !== run.operation || (run.instance && stats.serverInstance && run.instance !== stats.serverInstance)) {
      next = eventRun(run, 'REVIEW', '운영 모드 또는 FMS 서버가 바뀌었습니다. 이전 명령을 재전송하지 않습니다. 상태를 확인하세요.')
    } else if (run.demo === 'D01' && ordersFresh) {
      const source = p.orders.find(o => run.sourceId ? o.order_id === run.sourceId : o.order_name === run.sourceName)
      if (source) {
        next = { ...next, sourceId: source.order_id }
        const message = `${source.robot_id ?? '배차 전'} · ${PHASE[source.phase] ?? source.phase} · ${source.error || source.recovery_error || source.dispatch_reason || source.recovery_status || source.status}`
        if (source.status === 'COMPLETED' && source.phase === 'COMPLETED') next = eventRun(next, run.cancelApplied ? 'CANCELLED' : 'COMPLETED', message)
        else if (['FAILED', 'CANCELLED'].includes(source.status) && !activeOrder(source)) next = eventRun(next, source.status === 'FAILED' ? 'FAILED' : 'CANCELLED', message)
        else next = eventRun(next, source.status === 'FAILED' ? 'REVIEW' : source.status === 'CANCELLED' || run.phase === 'CANCELLING' ? 'CANCELLING' : 'RUNNING', message)
      } else if (stats.at - run.started > 15000) next = eventRun(run, 'REVIEW', '서버에서 이 주문을 찾을 수 없습니다. 자동 재접수하지 않습니다.')
    } else if (run.demo === 'D02' && run.sourceId && p.scenario && !p.scenarioError) {
      const source = p.scenario
      if (source.run_id !== run.sourceId) next = eventRun(run, 'REVIEW', '서버 시나리오 ID가 달라졌습니다. 다른 실행을 중지하지 않습니다.')
      else next = eventRun(run, source.state === 'completed' ? 'COMPLETED' : source.state === 'failed' ? run.cancelApplied ? 'CANCELLED' : 'FAILED' : source.state === 'stopping' ? 'CANCELLING' : 'RUNNING', source.message)
    } else if (['D03', 'D05', 'D06'].includes(run.demo) && run.phase !== 'SENDING') {
      const r = p.robots.find(row => row.backendId === run.target)
      const received = stats.robots[run.target]
      if (r && received && stats.at - received.at < 6000 && received.at > run.started) {
        const moving = ['MOVING', 'NAVIGATING', 'DOCKING', 'UNDOCKING', 'TASK_ASSIGNED', 'WAITING'].includes(r.status) || !!r.route
        const seenBusy = run.seenBusy || moving
        const arrived = r.connected && r.hasPose && !r.route && normalized(r.occupiedNode) === normalized(run.node)
        const dock = p.docks.find(d => d.robot_id === run.target)
        const done = run.demo === 'D06' ? r.status === 'CHARGING' && arrived && !!dock?.docking_complete && dock.age_s != null && dock.age_s < 5
          : r.status === 'IDLE' && arrived && seenBusy
        const count = done ? (run.arrivedSamples ?? 0) + (received.at !== run.lastReceipt ? 1 : 0) : 0
        next = { ...run, seenBusy, lastReceipt: received.at, arrivedSamples: count }
        if (['EMERGENCY_STOP', 'PAUSED', 'OFFLINE'].includes(r.status)) next = eventRun(next, r.status === 'EMERGENCY_STOP' && run.phase === 'CANCELLING' ? 'CANCELLED' : 'REVIEW', `${STATUS[r.status]} · ${r.pauseReason || '로봇 상태를 확인하세요.'}`)
        else if (count >= 2) next = eventRun(next, 'COMPLETED', run.demo === 'D06' ? '충전소 도킹 완료 상태 확인. 실제 충전 전류는 API 미제공입니다.' : `${nodeName(run.node)} 도착${run.demo === 'D05' && run.mode === 'real' ? '·정렬 후 대기' : ' 후 대기'} 상태를 연속 확인했습니다.${run.demo === 'D05' && run.mode === 'simulation' ? ' 시뮬레이션에서는 실제 마커 인식·정렬을 검증하지 않습니다.' : ''} 독립 Action 결과는 API 미제공입니다.`)
        else next = eventRun(next, run.phase, `${STATUS[r.status] ?? r.status} · ${nodeName(r.occupiedNode)} → ${nodeName(run.node)}${r.pauseReason ? ` · ${r.pauseReason}` : ''}`)
      }
      if (stats.at - run.started > 180000 && next.phase === 'RUNNING') next = eventRun(next, 'REVIEW', '3분 동안 완료 상태를 확인하지 못했습니다. 동작을 자동 취소하거나 재실행하지 않습니다. 로봇 상태를 확인하세요.')
    }
    if (next !== run && JSON.stringify(next) !== JSON.stringify(run)) patch(run.id, () => next)
  }, [p.switching, p.robots, p.orders, p.scenario, p.mode, p.operation, p.scenarioError, p.docks, stats, ordersFresh, run])

  async function preflight(demo: DemoId) {
    const [mode, orders, scenario, robots, connections] = await Promise.all([getRobotMode(), getOrders(), getScenarioStatus(), getRobots(), getConnections()])
    if (mode.mode !== p.mode || mode.operation_mode !== p.operation) throw new Error('서버 모드가 화면과 다릅니다. 화면 갱신 후 다시 확인하세요.')
    if (['running', 'stopping'].includes(scenario.state)) throw new Error('다른 시나리오가 실행 중입니다. 기존 실행 상태를 먼저 확인하세요.')
    if (orders.orders.some(o => o.mode === mode.mode && activeOrder(o))) throw new Error('진행 중인 주문 또는 복구 작업이 있습니다. 완료 후 실행하세요.')
    if (demo !== 'D01' && demo !== 'D02') {
      const r = robots.find(row => row.robot_id === robotId)
      const live = mode.mode === 'simulation' ? r && !!r.updated_at && Date.now() - Date.parse(r.updated_at) < 5000 : connections.status !== 'unavailable' && connections.devices.some(d => d.name === robotId && d.connected && !d.blocked)
      if (!r || !live || !['IDLE', 'CHARGING'].includes(r.status) || r.route || !r.map_pose_received) throw new Error('선택 로봇의 연결·위치 수신·IDLE·빈 경로 조건을 확인하세요.')
      if (mode.mode === 'real' && !window.confirm('실제 로봇이 움직입니다. 선택한 로봇과 목표 위치, 주변 작업 공간을 현장에서 확인했나요?')) throw new Error('현장 확인을 취소했습니다. 명령은 보내지 않았습니다.')
    }
    if (p.operation === 'ai' && ['D01', 'D03', 'D05'].includes(demo) && !p.routeModel) throw new Error('AI 경로 모델을 통합 관제에서 먼저 선택하세요.')
    return { mode, scenario }
  }

  async function start() {
    if (sending.current || active || blocked) return
    sending.current = true; abortPreparation.current = false; setPreparing(true); setPending(true); setError(''); setNotice('')
    let started: DemoRun | null = null
    try {
      const { scenario } = await preflight(selected)
      if (abortPreparation.current) { setNotice('실행 준비를 취소했습니다. 로봇 동작 명령은 전송하지 않았습니다.'); return }
      if (!stats.serverInstance) throw new Error('FMS 서버 세션 정보를 확인 중입니다. 잠시 후 다시 실행하세요.')
      if (selected === 'D01' && (!Number.isInteger(total) || total < 1 || total > 5)) throw new Error('주문 수량은 A/B/C/D 합계 1~5개입니다.')
      if (selected === 'D02' && !enabledCases.length) throw new Error('현재 서버에 해당 모드의 시나리오가 없습니다.')
      if (selected === 'D03' && !p.nodes.includes(node)) throw new Error('유효한 목표 노드를 선택하세요.')
      if (selected === 'D05' && !p.nodes.includes(markerNode)) throw new Error('선택 마커의 노드가 현재 지도에 없습니다.')
      if (selected === 'D06' && (p.mode !== 'real' || !charger || !p.nodes.includes(charger.node))) throw new Error('충전 시험은 실제 모드의 robot1~3과 유효한 충전 노드가 필요합니다.')
      if (selected === 'D06') {
        const settings = await getDockingSettings()
        if (!settings.contact_verification_enabled) throw new Error('접점 검증이 꺼져 있습니다. 통합 관제에서 켠 뒤 실행하세요.')
      }
      if (abortPreparation.current) { setNotice('실행 준비를 취소했습니다. 로봇 동작 명령은 전송하지 않았습니다.'); return }
      setPreparing(false)
      const id = uuid(); const now = Date.now()
      const target = selected === 'D01' ? '자동 배차' : selected === 'D02' ? traffic + (p.operation === 'ai' ? '_ai' : '') : robotId
      started = { id, demo: selected, mode: p.mode, operation: p.operation, phase: 'SENDING', started: now, updated: now, target,
        node: selected === 'D05' ? markerNode : selected === 'D06' ? charger?.node : node,
        sourceName: `DEMO-${id}`, instance: stats.serverInstance, message: '접수 요청 전송 중', events: [{ at: now, text: `${selected} · ${target} 접수 요청` }] }
      setRuns(prev => [started!, ...prev].slice(0, 20))
      let sourceId: string | undefined
      if (selected === 'D01') sourceId = (await createOrder({ order_name: started.sourceName, items, total_quantity: total, operation_mode: p.operation, driving_mode: p.operation === 'ai' ? 'llm' : 'standard', selector: p.routeModel?.selector, model: p.routeModel?.model })).order_id
      else if (selected === 'D02') {
        const result = await runScenario(target)
        if (!result.run_id || result.run_id === scenario.run_id) throw new Error('새 시나리오 ID를 확인하지 못했습니다. 상태를 재확인하세요.')
        sourceId = result.run_id
      } else if (selected === 'D06') await sendCharging(robotId)
      else await sendGoalNode(robotId, started.node!, p.operation === 'ai' ? 'llm' : 'standard', p.routeModel ?? undefined)
      const cancellingNow = cancelIntent.current.has(id)
      patch(id, r => r.phase === 'CANCELLED' ? r : eventRun({ ...r, sourceId }, cancellingNow ? 'CANCELLING' : 'RUNNING', cancellingNow ? '접수 응답 확인. 요청한 취소를 적용하고 종료 상태를 확인합니다.' : '서버 접수 응답 확인. 실제 진행·완료 상태를 기다립니다.'))
      if (cancellingNow) await requestCancellation({ ...started, sourceId, cancelRequested: true })
    } catch (e) {
      const message = strError(e)
      if (started && cancelIntent.current.has(started.id)) {
        patch(started.id, r => r.phase === 'CANCELLED' ? r : eventRun(r, 'CANCELLING', `취소 요청 후 접수 응답: ${message}. 실제 종료 상태를 확인합니다.`))
      } else {
        setError(message)
        if (started) patch(started.id, r => eventRun(r, 'REVIEW', `접수·실행 여부 확인 필요: ${message}. 자동 재전송하지 않습니다.`))
      }
    } finally { sending.current = false; if (mounted.current) { setPending(false); setPreparing(false) } }
  }

  async function requestCancellation(target: DemoRun) {
    let applied = false
    if (target.demo === 'D01') {
      const current = (await getOrders()).orders.find(o => target.sourceId ? o.order_id === target.sourceId : o.order_name === target.sourceName)
      if (!current) return false // 접수 응답이 오면 동일 작업에 취소를 다시 적용한다.
      patch(target.id, r => ({ ...r, sourceId: current.order_id }))
      if (current.status === 'COMPLETED') {
        if (current.phase !== 'COMPLETED' && current.robot_id) { await stopRobot(current.robot_id); applied = true }
      } else if (['PENDING', 'IN_PROGRESS'].includes(current.status)) {
        const result = await cancelOrder(current.order_id); applied = result.status === 'CANCELLED'
      }
    } else if (target.demo === 'D02') {
      if (!target.sourceId) return false
      const current = await getScenarioStatus()
      if (current.run_id !== target.sourceId) throw new Error('서버 실행 ID가 달라 중지하지 않았습니다.')
      if (['running', 'stopping'].includes(current.state)) { await stopScenario(); applied = true }
    } else { await stopRobot(target.target); applied = true }
    if (applied) { setError(''); patch(target.id, r => r.phase === 'CANCELLED' ? r : eventRun({ ...r, cancelApplied: true }, 'CANCELLING', '취소·정지 요청 접수. 서버와 로봇의 종료 상태를 기다립니다.')) }
    return applied
  }
  async function cancelCurrent() {
    if (!run || !isActive(run) || cancelling.current) return
    cancelling.current = true; setCancelBusy(true); cancelIntent.current.add(run.id)
    patch(run.id, r => eventRun({ ...r, cancelRequested: true }, 'CANCELLING', '취소 요청 중. 접수 응답 대기 중이면 해당 응답 확인 후 취소합니다.'))
    try { await requestCancellation(run) }
    catch (e) { setError(`취소 결과를 확인하지 못했습니다: ${strError(e)}. 필요하면 AMR 비상정지를 사용하세요.`) }
    finally { cancelling.current = false; setCancelBusy(false) }
  }
  async function performConfirmation() {
    if (!confirm || !confirm.allowDuringSend && (sending.current || pending)) return
    const task = confirm.action; setConfirm(null); setError('')
    try { await task() } catch (e) { setError(strError(e)) }
  }
  const requestStart = () => setConfirm({ title: `${DEMOS.find(d => d.id === selected)?.title} 실행`, detail: `${p.mode === 'real' ? '실제 로봇' : '시뮬레이션'} · ${p.operation === 'ai' ? 'AI' : '알고리즘'} · 대상 ${selected === 'D01' ? '자동 배차 (AMR 선택은 서버에서 수행)' : selected === 'D02' ? 'AMR 3대' : robotId} · ${selected === 'D01' ? `총 ${total}개` : selected === 'D06' ? `충전소 ${charger?.station}` : selected === 'D02' ? TRAFFIC[traffic]?.title : nodeName(selected === 'D05' ? markerNode : node)}. 실행 중에도 화면 탭을 바꿀 수 있습니다.`, action: start })
  const report = () => downloadReport({ schema_version: 1, exported_at: new Date().toISOString(), scope: '브라우저 관측 기록. 서버 데모 엔진이나 물품 이동 성공을 증명하지 않음.', runs, communication: stats, robots: p.robots, omx: p.omx, orders: p.orders, scenario: p.scenario, docking: p.docks })

  return <div className="demo-page">
    <div className="demo-heading"><div><div className="demo-eyebrow">DEMO CONSOLE</div><h1>동작을 나눠서, 흐름은 한눈에</h1><p>현재 FMS API와 수신 데이터로 시험합니다. 탭을 이동해도 실행은 계속됩니다.</p></div><button onClick={report}>관측 기록 다운로드 ↓</button></div>
    <div className="demo-tabs" role="tablist" aria-label="데모 정보 탭">{['시험', '통신·속도', '실행 기록'].map(t => <button role="tab" aria-selected={tab === t} key={t} onClick={() => setTab(t)}>{t}</button>)}<span className="demo-tab-note">{p.mode === 'real' ? '실제 장비' : '시뮬레이션'} / {p.operation === 'ai' ? 'AI' : '알고리즘'}</span></div>
    {(error || p.ordersError || p.fleetError || p.scenarioError) && <div role="alert" className="demo-alert"><strong>확인이 필요한 상태</strong><p>{error || p.ordersError || p.fleetError || p.scenarioError}</p><button onClick={() => window.dispatchEvent(new Event('fms:request-restart'))}>FMS 재시작 확인창 열기</button><small>전체 FMS가 종료 후 재시작됩니다. Pi 자체 재부팅 기능은 제공되지 않습니다.</small></div>}
    {notice && <p role="status" className="demo-notice">{notice}</p>}
    {preparing && <div className="demo-runbar"><p>실행 조건을 확인 중입니다. 아직 동작 명령은 보내지 않았습니다.</p><button onClick={() => { abortPreparation.current = true; setNotice('준비 취소 요청. 현재 조건 조회가 끝나면 동작 명령 없이 종료합니다.') }}>실행 준비 취소</button></div>}
    {run && <section className={`demo-runbar ${run.phase === 'REVIEW' || run.phase === 'FAILED' ? 'warning' : ''}`} aria-live="polite"><div><Pill tone={run.phase === 'FAILED' || run.phase === 'REVIEW' ? 'orange' : active ? 'blue' : run.phase === 'COMPLETED' ? 'green' : ''}>{RUN_LABEL[run.phase]}</Pill> <strong>{run.demo} · {run.target}</strong><p>{run.message}</p><small>서버 ID {run.sourceId ?? '독립 작업 ID 미제공/확인 중'} · 시작 {clock(run.started)}</small></div><div className="demo-actions">
      {active && <button className="danger" disabled={cancelBusy} onClick={() => setConfirm({ title: ['D01', 'D02'].includes(run.demo) ? '현재 실행 취소 요청' : `${run.target} 이동 취소·정지`, detail: ['D01', 'D02'].includes(run.demo) ? '이 화면에서 접수한 서버 ID만 취소합니다. 취소 후 복귀는 계속될 수 있습니다. 이미 물품 처리가 완료되어 복귀 중인 AMR은 기존 비상정지 API로 정지하며 해제 조작이 필요합니다.' : '선택 AMR을 비상정지합니다. 정상 취소와 다르며 해당 로봇의 작업에도 영향을 줍니다.', danger: true, allowDuringSend: true, action: cancelCurrent })}>{cancelBusy ? '취소 전송 중…' : run.cancelRequested ? '취소 재확인' : ['D01', 'D02'].includes(run.demo) ? '이 실행 취소' : '이동 취소·정지'}</button>}
      {(run.phase === 'REVIEW' || run.phase === 'FAILED') && <button onClick={() => window.dispatchEvent(new Event('fms:request-restart'))}>FMS 재시작 확인</button>}
      {(run.phase === 'REVIEW' || run.phase === 'FAILED') && <button onClick={() => setConfirm({ title: '관측 기록 닫기', detail: '로봇 동작을 중지하거나 실패 주문을 삭제하는 기능이 아닙니다. 현재 장비와 서버 상태를 직접 확인했을 때만 기록을 닫으세요.', action: async () => { patch(run.id, r => eventRun(r, 'CLOSED', '사용자가 상태를 확인하고 브라우저 관측을 닫음. 물리 동작 중지·성공을 의미하지 않음.')); setError('') } })}>상태 확인 후 기록 닫기</button>}
    </div></section>}
    {tab === '시험' && <>
      {p.operation === 'ai' && <label className="demo-label demo-model-select">AI 경로 모델 (주문 배차는 기존 배차기 사용)
        <select value={p.routeModel ? `${p.routeModel.selector}:${p.routeModel.model}` : ''} disabled={active || pending}
          onChange={e => { const model = p.routeModels.find(m => `${m.selector}:${m.model}` === e.target.value); if (model) p.onModelChange(model) }}>
          {!p.routeModels.length && <option value="">설정된 모델 없음 · 통합 관제의 모델 서버 상태 확인</option>}
          {p.routeModels.map(m => <option key={`${m.selector}:${m.model}`} value={`${m.selector}:${m.model}`}>{m.selector} / {m.model}</option>)}
        </select><small>교통 시험 D02는 기존 서버 시나리오에 설정된 모델을 사용합니다.</small>
      </label>}
      <div className="demo-catalog">{DEMOS.map(d => <button key={d.id} className={selected === d.id ? 'selected' : ''} onClick={() => switchDemo(d.id)}><span>{d.id}<Pill>{d.tag}</Pill></span><strong>{d.title}</strong><small>{d.description}</small></button>)}</div>
      <div className="demo-columns"><section className="demo-card"><h2>{selected} · {DEMOS.find(d => d.id === selected)?.title}</h2>
        {['D03', 'D05', 'D06'].includes(selected) && <label className="demo-label">대상 로봇<select value={robotId} onChange={e => setRobotId(e.target.value)}>{['robot1', 'robot2', 'robot3'].map(id => <option value={id} key={id}>{id} · {STATUS[p.robots.find(r => r.backendId === id)?.status ?? 'OFFLINE'] ?? '상태 미확인'}</option>)}</select></label>}
        {selected === 'D01' && <><p>AMR·집기 순서·하차 작업대는 기존 배차기가 결정합니다.</p><div className="demo-quantities">{(['A', 'B', 'C', 'D'] as const).map(item => <label key={item}>{item}<input aria-label={`${item} 수량`} type="number" min={0} max={5} step={1} value={items[item]} onChange={e => setItems(prev => ({ ...prev, [item]: Number.isFinite(Number(e.target.value)) ? Math.max(0, Math.min(5, Math.trunc(Number(e.target.value)))) : 0 }))} /></label>)}</div><p>합계 <strong>{total} / 5개</strong> · 집기 N5/N6 → 하차 N3/N4 → 복귀</p></>}
        {selected === 'D02' && <><label className="demo-label">교통 상황<select value={traffic} onChange={e => setTraffic(e.target.value)}>{Object.entries(TRAFFIC).map(([key, value]) => <option key={key} value={key}>{value.title}</option>)}</select></label><div className="demo-note"><strong>시작 배치</strong><p>{TRAFFIC[traffic].starts}</p><small>로봇 순서대로 목표 {TRAFFIC[traffic].goals}</small><p>실제 장비는 현장에서 배치하세요. 시뮬레이션만 기존 서버가 초기 위치를 준비합니다.</p><small>기존 시나리오 전용 test.geojson과 동일한 그래프가 필요합니다. 서버가 시작 위치·연결·주문 충돌을 검증합니다.</small></div>{!enabledCases.length && <p className="demo-warning">현재 모드에 대응하는 서버 시나리오가 없습니다.</p>}</>}
        {selected === 'D03' && <label className="demo-label">목표 노드<select value={node} onChange={e => setNode(e.target.value)}><option value="" disabled>목표 선택</option>{p.nodes.map(id => <option key={id} value={id}>{nodeName(id)}{MARKERS[id] ? ` · 마커 ${MARKERS[id]} 자동 정렬 포함` : ''}</option>)}</select></label>}
        {selected === 'D04' && <><label className="demo-label">대상 OMX<select value={omxId} onChange={e => setOmxId(e.target.value)}>{['omx1', 'omx2', 'omx3', 'omx4'].map(id => <option key={id}>{id}</option>)}</select></label><p>{omxId === 'omx1' ? 'A/B 집기 (OMX1의 모델 A/B 교차 매핑은 OMX 설정에 유지)' : omxId === 'omx2' ? 'C/D 집기' : omxId === 'omx3' ? 'E 하차 (임시 F 모델 여부는 OMX 설정에서 관리)' : 'F 하차'}</p><div className="demo-note"><strong>관측·서비스 재시작 가능</strong><p>기존 서버에는 OMX 단독 작업 전송 API가 없습니다. AMR 없이 집기·하차를 실행하는 버튼은 제공할 수 없습니다.</p><button disabled>OMX 단독 작업 · API 미제공</button></div>{chosenOmx && <button disabled={p.mode !== 'real' || active || pending || ['WORKING', 'COMMAND_SENT'].includes(chosenOmx.state) || !chosenOmx.restart_available || !!p.omxError} onClick={() => setConfirm({ title: `${omxId} 서비스 재시작`, detail: 'Pi 전체 재부팅이 아닌 OMX 관리 서비스 재시작입니다. 서버에서 작업 중 여부와 SSH 설정을 검사합니다.', danger: true, action: async () => { if (sending.current) return; sending.current = true; setPending(true); try { const result = await restartOmx(omxId); if (!result.accepted) throw new Error(result.message); setError(''); setNotice(result.message); } finally { sending.current = false; setPending(false) } } })}>선택 OMX 서비스 재시작</button>}<small className="demo-muted">{p.omxError || (!chosenOmx?.restart_available ? '재시작 비활성: 작업 중·연결·SSH 설정 또는 현재 모드를 확인하세요.' : '')}</small></>}
        {selected === 'D05' && <><label className="demo-label">목표 마커<select value={markerNode} onChange={e => setMarkerNode(e.target.value)}>{Object.entries(MARKERS).map(([n, id]) => <option key={n} value={n} disabled={!p.nodes.includes(n)}>{nodeName(n)} · 마커 ID {id}</option>)}</select></label><p>{p.mode === 'real' ? '노드 이동이 끝나면 기존 AMR 정렬 기능이 실행됩니다.' : '시뮬레이션은 노드 이동만 확인합니다. 실제 마커 인식·정렬은 검증하지 않습니다.'} 주문과 OMX 작업을 만들지 않습니다.</p><div className="demo-note">목표 ID <strong>{MARKERS[markerNode]}</strong> · 검출된 ID/정렬 오차/정렬 속도 명령은 현재 API 미제공.<p><button disabled>현재 자리에서 정렬만 재실행 · API 미제공</button></p></div></>}
        {selected === 'D06' && <><div className="demo-note"><strong>{robotId} → 충전소 {charger?.station} / {nodeName(charger?.node)}</strong><p>충전소 진입·접점 안정·도킹 완료 상태를 확인합니다. 충전 전류·실제 배터리 충전량은 별도 계측값입니다.</p></div>{p.mode !== 'real' && <p className="demo-warning">충전 도킹은 실제 모드에서 사용할 수 있습니다.</p>}</>}
        {selected !== 'D04' && <><div className="demo-checks"><span className={ordersFresh ? 'good' : 'bad'}>{ordersFresh ? '●' : '○'} 주문 상태 수신</span><span className={p.scenario && !p.scenarioError ? 'good' : 'bad'}>● 서버 시나리오 확인</span><span>활성 주문 {p.orders.filter(o => o.mode === p.mode && activeOrder(o)).length} · 동작 중 AMR {busyRobots.length}</span></div><button className="primary demo-start" disabled={blocked || p.orders.some(o => o.mode === p.mode && activeOrder(o)) || selected === 'D01' && (total < 1 || total > 5) || selected === 'D02' && !enabledCases.length || ['D03', 'D05', 'D06'].includes(selected) && (!robotFresh || !robot?.connected || !robot.hasPose || !['IDLE', 'CHARGING'].includes(robot.status) || !!robot.route) || selected === 'D06' && p.mode !== 'real'} onClick={requestStart}>{pending ? '확인 중…' : active ? '진행 중인 실행 확인' : '조건 확인 후 실행'}</button><small className="demo-muted">접수 응답과 실제 완료를 분리해 확인합니다. 독립 AMR 이동은 서버 실행 ID가 없어 화면 새로고침 후 자동 성공 판정을 하지 않습니다.</small></>}
      </section>
      <section className="demo-card"><h2>{selected === 'D04' ? '선택 OMX 상태' : '현재 장비·진행 상태'}</h2>
        {selected === 'D04' ? chosenOmx ? <><div className="demo-kpis"><Metric label="연결" value={chosenOmx.connected && !p.omxError ? '온라인' : '미확인'} /><Metric label="상태" value={STATUS[chosenOmx.state] ?? chosenOmx.state} note={chosenOmx.detail} /><Metric label="진행" value={`${chosenOmx.current_count} / ${chosenOmx.total_count}`} note={chosenOmx.count_kind === 'motion_cycles' ? '동작 사이클 수 · 물품 이동 미검증' : 'OMX 보고값'} /></div><p>{chosenOmx.message || '최근 작업 메시지 없음'}</p><p>작업 ID {chosenOmx.job_id || '없음'}</p><div className="demo-pallets">{Object.entries(chosenOmx.stock?.palettes ?? {}).map(([key, stock]) => <div key={key}><strong>팔레트 {key}</strong><Pill tone={chosenOmx.connected && chosenOmx.stock.fresh && !p.omxError ? stock.state === 'PRESENT' ? 'green' : stock.state === 'EMPTY' ? 'blue' : 'orange' : 'orange'}>{!chosenOmx.connected || !chosenOmx.stock.fresh || p.omxError ? '오래된 결과' : stock.state === 'PRESENT' ? '물건 있음' : stock.state === 'EMPTY' ? '비었음' : '판정 불가'}</Pill><small>{stock.reason}</small></div>)}</div><small className="demo-muted">재고 개수는 계측하지 않습니다. OMX3·4는 팔레트 재고 관측 대상이 아닙니다.</small></> : <p>선택 OMX의 상태가 아직 수신되지 않았습니다.</p> : <><div className="demo-robot-list">{p.robots.map(r => <button key={r.id} onClick={() => setRobotId(r.backendId)} className={r.backendId === robotId ? 'selected' : ''}><strong>{r.backendId}</strong><Pill tone={r.connected && stats.robots[r.backendId] && stats.at - stats.robots[r.backendId].at < 6000 ? 'green' : 'orange'}>{STATUS[r.status] ?? r.status}</Pill><span>{nodeName(r.occupiedNode)} · {r.batteryVoltage == null ? '전압 미제공' : `${r.batteryVoltage.toFixed(1)} V`}</span><small>{r.pauseReason || (r.route?.node_ids?.length ? `경로 ${r.route.node_ids.map(nodeName).join(' → ')}` : '표시할 경로 없음')}</small></button>)}</div>
          <div className="demo-kpis"><Metric label="선택 로봇 추정 속도" value={robotFresh && receipt?.speed != null ? `${receipt.speed.toFixed(2)} m/s` : '미확인'} note="위치 변화 기반 · 실제 속도 명령 아님" /><Metric label="상태 수신 간격" value={robotFresh ? formatMs(receipt?.periodMs) : '미확인'} note="브라우저 REST 수신 간격" /></div>
          {selected === 'D06' && <div className="demo-pallets">{p.docks.filter(d => d.robot_id === robotId).map(d => <div key={d.station}><strong>충전소 {d.station}</strong><span>접점 {d.contact === 'CONTACT' ? '접촉됨' : d.contact === 'RELEASED' ? '떨어짐' : '미확인'} · 안정 {d.stable ? '예' : '아니오'} · 도킹 완료 {d.docking_complete ? '예' : '아니오'}</span><small>센서 데이터 {d.age_s == null ? '미수신' : `${d.age_s.toFixed(1)}초 전`} · 브로커 {d.broker_connected ? '연결' : '끊김'}</small></div>)}</div>}
          {(selected === 'D01' || selected === 'D02') && <div className="demo-source-list">{p.orders.filter(o => o.mode === p.mode && (activeOrder(o) || o.order_id === run?.sourceId)).map(o => <div key={o.order_id}><strong>{o.order_name}</strong><p>{o.robot_id ?? '자동 배차 대기'} · {PHASE[o.phase] ?? o.phase}</p><small>{o.error || o.dispatch_reason || o.recovery_status || o.status}</small></div>)}<small>집기 점유 {Object.entries(p.pickups).map(([k, v]) => `${nodeName(k)} ${v.status}`).join(' / ') || '미수신'}</small><small>하차 점유 {Object.entries(p.workstations).map(([k, v]) => `${nodeName(k)} ${v.status}`).join(' / ') || '미수신'}</small></div>}
        </>}
      </section></div>
      <details className="demo-card demo-map"><summary>현재 지도·경로·로봇 위치 보기</summary><div style={{ height: 440, position: 'relative', display: 'flex', marginTop: 14 }}>{p.mapView}</div></details>
    </>}
    {tab === '통신·속도' && <CommunicationPanel stats={stats} mode={p.mode} robots={p.robots} omx={p.omx} docks={p.docks} omxError={p.omxError} />}
    {tab === '실행 기록' && <section className="demo-card"><h2>이 브라우저의 최근 시험</h2><p>최대 20건 · 각 160개 상태 변화. 주문/교통 시험은 서버 ID로 다시 조회하며, 개별 이동은 새로고침 시 확인을 요청합니다.</p>{runs.length === 0 && <div className="demo-empty">아직 실행한 시험이 없습니다. 시험 탭에서 대상을 선택하세요.</div>}{runs.map(r => <details className="demo-record" key={r.id} open={r.id === run?.id}><summary><strong>{r.demo} · {r.target}</strong><Pill>{RUN_LABEL[r.phase]}</Pill><span>{clock(r.started)}</span></summary><p>{r.message}</p><small>서버 ID {r.sourceId ?? '미제공'} · {r.mode} / {r.operation}</small>{r.demo === 'D02' && r.sourceId && <p><a href={scenarioRecordUrl(r.sourceId)} target="_blank" rel="noreferrer">기존 서버 시나리오 기록 열기 ↗</a></p>}<ol className="demo-timeline">{r.events.slice(-30).map((e, i) => <li key={i}><time>{clock(e.at)}</time>{e.text}</li>)}</ol></details>)}</section>}
    <p className="demo-footnote">정상 취소와 비상정지는 다릅니다. AMR 비상정지는 OMX 물리 정지를 대신하지 않습니다. 서버에 없는 검출 ID·정렬 오차·정확한 물품 수는 표시하지 않습니다.</p>
    {confirm && createPortal(<div className="demo-modal-backdrop" role="dialog" aria-modal="true" aria-label={confirm.title} onKeyDown={e => {
      if (e.key === 'Escape') setConfirm(null)
      if (e.key === 'Tab') {
        const buttons = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'))
        const first = buttons[0], last = buttons[buttons.length - 1]
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
      }
    }}><section className="demo-modal"><h2>{confirm.title}</h2><p>{confirm.detail}</p><div className="demo-actions"><button autoFocus onClick={() => setConfirm(null)}>돌아가기</button><button className={confirm.danger ? 'danger' : 'primary'} disabled={pending && !confirm.allowDuringSend} onClick={() => void performConfirmation()}>확인 후 진행</button></div></section></div>, document.body)}
  </div>
}
