export const DEMOS = [
  { id: 'D01', title: '주문 전체 흐름', description: '주문 → 배차 → 집기 → 하차 → 복귀', tag: '통합 시험' },
  { id: 'D02', title: '교통 제어', description: '기존 3대 시나리오 · 경합과 대기 관측', tag: '3대 동시' },
  { id: 'D03', title: 'AMR 단독 이동', description: '로봇·목표 노드를 선택해 이동', tag: '독립 시험' },
  { id: 'D04', title: 'OMX 상태·팔레트', description: '연결·진행 수량·A/B/C/D 재고 유무', tag: '실시간 관측' },
  { id: 'D05', title: '마커 이동·정렬', description: '마커 노드 이동 후 기존 자동 정렬', tag: 'ArUco' },
  { id: 'D06', title: '충전 도킹', description: '로봇별 충전소 이동·접점 관측', tag: '실제 모드' },
] as const
export type DemoId = typeof DEMOS[number]['id']
export const MARKERS: Record<string, number> = { '5': 24, '6': 25, '4': 26, '3': 27 }
export const CHARGERS: Record<string, { node: string; station: number }> = {
  robot1: { node: '0', station: 1 }, robot2: { node: '1', station: 2 }, robot3: { node: '2', station: 3 },
}
// tests/traffic_scenarios.json의 기존 시작 위치. 이동 준비 명령은 보내지 않는다.
export const TRAFFIC: Record<string, { title: string; starts: string; goals: string }> = {
  corridor_head_on: { title: '좁은 통로 마주침', starts: 'robot1 N9 · robot2 N7 · robot3 N0', goals: 'N7 · N9 · 대기' },
  junction_contention: { title: '교차로 경합', starts: 'robot1 N9 · robot2 N8 · robot3 N5', goals: 'N11 · N5 · N9' },
  bottleneck: { title: '병목 구간', starts: 'robot1 N0 · robot2 N1 · robot3 N2', goals: 'N4 · N3 · N6' },
  deadlock_cycle: { title: '순환 대기', starts: 'robot1 N9 · robot2 N7 · robot3 N8', goals: 'N7 · N8 · N9' },
  priority_yield: { title: '우선순위 양보', starts: 'robot1 N1 · robot2 N12 · robot3 N8', goals: 'N4 · N1 · N6' },
}
export const RUN_LABEL: Record<RunPhase, string> = {
  SENDING: '전송·접수 확인 중', RUNNING: '진행 중', CANCELLING: '중지·복귀 확인 중', COMPLETED: '완료 상태 확인',
  FAILED: '실패', CANCELLED: '취소 상태 확인', CLOSED: '관측 기록 닫힘', REVIEW: '상태 확인 필요',
}
export type RunPhase = 'SENDING' | 'RUNNING' | 'CANCELLING' | 'COMPLETED' | 'FAILED' | 'CANCELLED' | 'CLOSED' | 'REVIEW'
export type DemoRun = {
  id: string; demo: DemoId; mode: 'real' | 'simulation'; operation: 'ai' | 'algorithm'; phase: RunPhase;
  started: number; updated: number; target: string; node?: string; sourceId?: string; sourceName?: string;
  instance?: string; message: string; cancelRequested?: boolean; cancelApplied?: boolean; seenBusy?: boolean; lastReceipt?: number; arrivedSamples?: number;
  events: { at: number; text: string }[];
}
export const isActive = (run: DemoRun | null) => !!run && ['SENDING', 'RUNNING', 'CANCELLING', 'REVIEW'].includes(run.phase)
export function eventRun(run: DemoRun, phase: RunPhase, message: string): DemoRun {
  if (run.phase === phase && run.message === message) return run
  return { ...run, phase, message, updated: Date.now(), events: [...run.events, { at: Date.now(), text: message }].slice(-160) }
}
export function activeOrder(order: { status: string; phase?: string; recovery_status?: string }) {
  return ['PENDING', 'IN_PROGRESS'].includes(order.status) || order.status === 'COMPLETED' && order.phase !== 'COMPLETED' || ['PENDING', 'RETURNING', 'BLOCKED'].includes(order.recovery_status ?? '')
}
const STORAGE = 'fms-demo-observation-v1'
export function loadRuns(): DemoRun[] {
  try {
    const rows: unknown = JSON.parse(sessionStorage.getItem(STORAGE) ?? '[]')
    if (!Array.isArray(rows)) return []
    return rows.filter((r): r is DemoRun => !!r && typeof r === 'object' && typeof r.id === 'string' &&
      DEMOS.some(d => d.id === r.demo) && Object.prototype.hasOwnProperty.call(RUN_LABEL, r.phase) && Number.isFinite(r.started) && Number.isFinite(r.updated) && ['ai', 'algorithm'].includes(r.operation) &&
      ['real', 'simulation'].includes(r.mode) && typeof r.target === 'string' && Array.isArray(r.events) && r.events.length <= 160 && r.events.every((e: {at: unknown; text: unknown}) => !!e && Number.isFinite(e.at) && typeof e.text === 'string'))
      .slice(0, 20).map(r => isActive(r) && !['D01', 'D02'].includes(r.demo)
        ? eventRun(r, 'REVIEW', '화면이 다시 열렸습니다. 개별 이동에는 서버 작업 ID가 없어 자동 완료 판정을 보류합니다. 로봇 상태를 확인하세요.') : r)
  } catch { return [] }
}
export function saveRuns(rows: DemoRun[]) {
  try { sessionStorage.setItem(STORAGE, JSON.stringify(rows.slice(0, 20))) } catch { /* 저장 실패가 로봇 동작을 바꾸지는 않는다. */ }
}
export function downloadReport(value: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
  const a = document.createElement('a'); a.href = url; a.download = `fms-demo-${new Date().toISOString().replace(/[:.]/g, '-')}.json`
  a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
