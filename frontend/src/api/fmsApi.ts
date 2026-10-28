const DEFAULT_API_BASE = `${window.location.protocol}//${window.location.hostname}:8000`
const API_BASE = (import.meta.env.VITE_FMS_API_BASE ?? DEFAULT_API_BASE).replace(/\/$/, '')

export const MAP_IMAGE_URL = `${API_BASE}/api/map/image`

function wsBaseFromHttp(base: string) {
  if (base.startsWith('https://')) return `wss://${base.slice('https://'.length)}`
  if (base.startsWith('http://')) return `ws://${base.slice('http://'.length)}`
  return base
}

export const WS_BASE = (import.meta.env.VITE_FMS_WS_BASE ?? wsBaseFromHttp(API_BASE)).replace(/\/$/, '')

export type RobotMode = 'real' | 'simulation'
export type OperationMode = 'algorithm' | 'ai'

export type RobotModeDto = {
  mode: RobotMode
  operation_mode: OperationMode
  feature_engines: Record<string, 'algorithm' | 'ai'>
  real_available: boolean
  simulation_active: boolean
}

export type RouteNodeDto = {
  id: string | number
  x: number
  y: number
  frame?: string
  pixel_x?: number
  pixel_y?: number
  properties?: Record<string, unknown>
}

export type GeoJsonFeature = {
  type: 'Feature'
  geometry?: {
    type?: string
    coordinates?: unknown
  } | null
  properties?: Record<string, unknown> | null
}

export type GeoJsonFeatureCollection = {
  type: 'FeatureCollection'
  name?: string
  features: GeoJsonFeature[]
}

export type MapInfoDto = {
  resolution: number
  width: number
  height: number
  image_url: string
  image_name: string
}

export function getMapInfo() {
  return request<MapInfoDto>('/api/map/info')
}

export function getRobotMode() {
  return request<RobotModeDto>('/api/mode')
}

export type DispatchSettingsDto = {
  mode: 'real'
  battery_voltage_check_enabled: boolean
  minimum_dispatch_voltage: number
  voltage_window_s: number
}

export type DockingSettingsDto = {
  mode: 'real'
  contact_verification_enabled: boolean
}

export function getDockingSettings() {
  return request<DockingSettingsDto>('/api/docking/settings')
}

export function setDockContactVerification(enabled: boolean) {
  return request<DockingSettingsDto>('/api/docking/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contact_verification_enabled: enabled }),
  })
}

export function getDispatchSettings() {
  return request<DispatchSettingsDto>('/api/dispatch/settings')
}

export function setDispatchVoltageCheck(enabled: boolean) {
  return request<DispatchSettingsDto>('/api/dispatch/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ battery_voltage_check_enabled: enabled }),
  })
}

export async function setRobotMode(mode: RobotMode, operation_mode: OperationMode) {
  const result = await request<RobotModeDto>('/api/mode', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, operation_mode }),
  })
  if (!result.operation_mode) {
    throw new Error('실행 중인 백엔드가 이전 버전입니다. 백엔드 재시작 후 운영 모드를 전환하세요.')
  }
  return result
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    const text = await response.text()
    let message = text
    try {
      const body = JSON.parse(text) as { detail?: unknown }
      if (typeof body.detail === 'string') message = body.detail
    } catch { /* JSON이 아닌 오류는 원문 표시 */ }
    throw new Error(`${response.status} ${response.statusText}${message ? `: ${message}` : ''}`)
  }
  return response.json() as Promise<T>
}

export function getRouteNodes() {
  return request<RouteNodeDto[]>('/api/route/nodes?include_pixel=true')
}

export function getRouteGraph() {
  return request<GeoJsonFeatureCollection>('/api/route/graph?raw=true')
}

export type RouteModelDto = { selector: 'ollama' | 'laya' | 'kev'; model: string }
export type RouteModelGroupsDto = { ollama: RouteModelDto[]; decision_model: RouteModelDto[] }

export type LlmRoutePreviewDto = {
  mode: RobotMode
  robot_id: string
  start_node: string
  target_node: string
  dispatched: boolean
  path: string[]
  model: string | null
  total_weight: number
  calls: number
  baseline_path: string[]
  baseline_weight: number
  same_path: boolean
  same_distance: boolean
  distance_difference: number
}

export function getRouteModels() {
  return request<RouteModelGroupsDto>('/api/command/route-models')
}

export function previewLlmRoute(robotId: string, nodeId: string | number, routeModel: RouteModelDto) {
  return request<LlmRoutePreviewDto>('/api/command/llm-route-preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      robot_id: robotId, node_id: nodeId,
      selector: routeModel.selector, model: routeModel.model,
    }),
  })
}

export function sendGoalNode(
  robotId: string,
  nodeId: string | number,
  drivingMode: 'standard' | 'llm' = 'standard',
  routeModel?: RouteModelDto,
) {
  return request<Record<string, unknown>>('/api/command/goal-node', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      robot_id: robotId, node_id: nodeId, driving_mode: drivingMode,
      selector: routeModel?.selector, model: routeModel?.model,
    }),
  })
}

export function sendCharging(robotId: string) {
  return request<Record<string, unknown>>('/api/command/charging', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ robot_id: robotId }),
  })
}

export function sendGoalCoordinate(robotId: string, targetX: number, targetY: number) {
  return request<Record<string, unknown>>('/api/command/goal', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ robot_id: robotId, target_x: targetX, target_y: targetY }),
  })
}

export function returnNearestNode(robotId: string) {
  return request<Record<string, unknown>>(
    '/api/command/return-nearest-node',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ robot_id: robotId }),
    },
  )
}

export function stopRobot(robotId: string) {
  return request<Record<string, unknown>>('/api/command/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ robot_id: robotId }),
  })
}

export function emergencyReleaseRobot(robotId: string) {
  return request<Record<string, unknown>>('/api/command/emergency-release', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ robot_id: robotId }),
  })
}

export function stopAllRobots() {
  return request<Record<string, unknown>>('/api/command/stop-all', {
    method: 'POST',
  })
}

export function emergencyReleaseAll() {
  return request<Record<string, unknown>>('/api/command/emergency-release-all', {
    method: 'POST',
  })
}

export function cmdVelWsUrl() {
  return `${WS_BASE}/ws/cmd_vel`
}

export type ConnectionDeviceDto = {
  ip: string
  name: string
  known: boolean
  connected: boolean
  blocked: boolean
  state: 'CONNECTED' | 'OFFLINE' | 'BLOCKED' | string
}

export type RobotRoute = {
  node_ids: string[]
  edge_ids: string[]
  phase: 'ready' | 'moving'
  segment_index: number
}

export type RobotStateDto = {
  robot_id: string
  ui_id?: string
  status: string
  occupied_node?: string | null
  occupied_edge?: string | null
  pause_reason?: string | null
  pause_reason_code?: string | null
  battery: number | null
  battery_voltage?: number | null
  dispatch_battery_block_reason?: 'battery_unknown' | 'low_battery' | null
  x: number
  y: number
  yaw: number
  updated_at?: string | null
  mode: 'real' | 'simulation'
  route?: RobotRoute | null
  pixel_x?: number | null
  pixel_y?: number | null
  map_pose_received: boolean
  connection_state: string
}

export type OmxDeviceDto = {
  omx_id: string
  connected: boolean
  x: number
  y: number
  pixel_x: number
  pixel_y: number
}

export function getConnections() {
  return request<{ devices: ConnectionDeviceDto[]; status?: 'unavailable' }>('/api/connections')
}

export function getRobots() {
  return request<RobotStateDto[]>('/api/robots')
}

export function dashboardWsUrl() {
  return `${WS_BASE}/ws/dashboard`
}

export function getOmxDevices() {
  return request<OmxDeviceDto[]>('/api/omx')
}
export type ScenarioItem = { name: string; description: string }
export type ScenarioStatus = {
  state: 'idle' | 'running' | 'stopping' | 'completed' | 'failed'
  name: string | null
  run_id: string | null
  mode?: RobotMode
  description?: string
  elapsed?: number
  message: string
  record_url?: string
  metrics?: OrderStressRecord
  robots: { robot_id: string; status: string; current_node: string | null; goal: string | null }[]
}

export function getScenarios() {
  return request<ScenarioItem[]>('/api/scenarios')
}

export function getScenarioStatus() {
  return request<ScenarioStatus>('/api/scenarios/status')
}

export function runScenario(name: string) {
  return request<ScenarioStatus>('/api/scenarios/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}


export type OrderStressRecord = {
  run_id: string
  state: string
  elapsed: number
  seed: number
  created: number
  completed: number
  failed: number
  open_orders: number
  failures?: { order_id: string; robot_id: string | null; phase: string | null; reason: string | null; node: string | null; edge: string | null; destination: string | null }[]
}

export function stopScenario() {
  return request<ScenarioStatus>('/api/scenarios/stop', { method: 'POST' })
}

export function scenarioRecordUrl(runId: string) {
  return `${API_BASE}/api/scenarios/records/${encodeURIComponent(runId)}`
}


export async function downloadScenarioRecord(runId: string) {
  const response = await fetch(scenarioRecordUrl(runId))
  if (!response.ok) throw new Error('시험 기록 다운로드에 실패했습니다. 다시 시도하세요.')
  const blob = await response.blob()
  const record: { file_name?: string; started_at: string } = JSON.parse(await blob.text())
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = record.file_name ?? `자동배차테스트[${new Date(record.started_at)
    .toLocaleString('sv-SE', { timeZone: 'Asia/Seoul', hour12: false }).replace(/:/g, '-')}].json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function resetScenario(runId: string) {
  return request<ScenarioStatus>('/api/scenarios/reset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: runId }),
  })
}


export type DockingStationDto = {
  station: number
  contact: 'CONTACT' | 'RELEASED' | 'UNKNOWN'
  received_at_unix: number | null
  age_s: number | null
  stable: boolean
  broker_connected: boolean
  robot_id: string | null
  docking_complete: boolean
}

export function getDockingStations() {
  return request<DockingStationDto[]>('/api/robots/docking-stations', { signal: AbortSignal.timeout(3000) })
}
