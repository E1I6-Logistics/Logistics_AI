/** 기존 요청/수신을 관측한다. 새 폴링이나 로봇 명령은 만들지 않는다. */
export type HttpSample = { at: number; path: string; ms: number; ok: boolean }
export type PoseSample = { at: number; x: number; y: number; yaw: number; mode: string; sourceAt?: string | null }
export type RobotObservation = { at: number; periodMs: number | null; speed: number | null; angular: number | null; sourceAt?: string | null }
const http: HttpSample[] = []
const wsTimes: number[] = []
const poses = new Map<string, PoseSample>()
const robots: Record<string, RobotObservation> = {}
let wsState = '연결 중'
let serverInstance = ''
export function recordHttp(path: string, started: number, ok: boolean) {
  http.push({ at: Date.now(), path, ms: Math.max(0, performance.now() - started), ok })
  if (http.length > 120) http.splice(0, http.length - 120)
}
export function recordWs(state: string, message = false) {
  wsState = state
  if (message) { wsTimes.push(Date.now()); if (wsTimes.length > 120) wsTimes.shift() }
}
export function setServerInstance(value: string) { serverInstance = value }
export function estimateMotion(previous: PoseSample | undefined, next: PoseSample) {
  if (!previous || previous.mode !== next.mode) return { speed: null, angular: null }
  // 서로 다른 수신원(REST/WS)을 섞어서 속도를 계산하지 않는다.
  const sourceClock = !!previous.sourceAt && !!next.sourceAt
  const dt = (sourceClock ? Date.parse(next.sourceAt!) - Date.parse(previous.sourceAt!) : next.at - previous.at) / 1000
  if (!(dt >= .05 && dt <= 5)) return { speed: null, angular: null }
  const speed = Math.hypot(next.x - previous.x, next.y - previous.y) / dt
  const angle = Math.atan2(Math.sin(next.yaw - previous.yaw), Math.cos(next.yaw - previous.yaw)) / dt
  if (!Number.isFinite(speed) || !Number.isFinite(angle) || speed > 2) return { speed: null, angular: null }
  return { speed, angular: angle }
}
export function recordRobot(id: string, sample: Omit<PoseSample, 'at'>, hasPose: boolean) {
  const next = { ...sample, at: Date.now() }
  const prev = poses.get(id)
  const period = prev ? next.at - prev.at : null
  robots[id] = { at: next.at, periodMs: period, sourceAt: sample.sourceAt,
    ...(hasPose ? estimateMotion(prev, next) : { speed: null, angular: null }) }
  if (hasPose) poses.set(id, next); else poses.delete(id)
}
export function observationSnapshot() {
  return { at: Date.now(), http: [...http], wsTimes: [...wsTimes], wsState, serverInstance, robots: { ...robots } }
}
