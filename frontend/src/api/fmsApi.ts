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
export type DockingStationDto = {
  station: number
  contact: 'CONTACT' | 'RELEASED' | 'UNKNOWN'
  received_at_unix: number | null
  age_s: number | null
  stable: boolean
  broker_connected: boolean
  freshness_policy: 'event' | 'heartbeat'
  robot_id: string | null
  docking_complete: boolean
}

export function getDockingStations() {
  return request<{ mode: RobotMode; stations: DockingStationDto[] }>('/api/robots/docking-stations', {
    signal: AbortSignal.timeout(3000),
  })
}
export type OperationMode = 'algorithm' | 'ai'
export type OrderTrafficPolicy = 'KEV' | 'MIN_EXPECTED_TIME' | 'KEV_STRATEGY_ORDER'

export type RobotModeDto = {
  mode: RobotMode
  operation_mode: OperationMode
  feature_engines: Record<string, 'algorithm' | 'ai'>
  real_available: boolean
  simulation_active: boolean
  simulation_preview?: boolean
  order_traffic?: {
    enabled: boolean
    route_source: string
    selection_policy: OrderTrafficPolicy | null
    available_policies?: OrderTrafficPolicy[]
    scope: string
  }
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

export function setOrderTrafficPolicy(selection_policy: OrderTrafficPolicy) {
  return request<RobotModeDto>('/api/mode/order-traffic', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ selection_policy }),
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
export type ScenarioRouteSource = 'EXISTING_SHORTEST_PATH' | 'KEV'
export type ScenarioItem = {
  name: string; description: string
  group?: string
  test_goal?: string
  route_source?: ScenarioRouteSource
  route_source_options?: ScenarioRouteSource[]
  traffic_selector?: 'kev' | 'fixed' | 'search' | 'guided'
}
export type ScenarioStatus = {
  state: 'idle' | 'running' | 'stopping' | 'completed' | 'failed'
  name: string | null
  run_id: string | null
  mode?: RobotMode
  description?: string
  elapsed?: number
  message: string
  kind?: 'order_stream'
  route_source?: ScenarioRouteSource | 'SUPPLIED_PATHS'
  traffic_selector?: 'kev' | 'fixed' | 'search' | 'guided'
  coordination?: {
    decision: { joint_action_id: string }
    recovery_search?: {
      status: string
      checks: number
      found_steps: number | null
      elapsed_seconds: number
      limits: { max_checks: number; max_steps: number; max_seconds: number }
      model_calls?: number
      motion_estimates_enabled?: boolean
      branch_policy?: string
      code_after_model_budget_decisions?: number
      deferred_branches?: number
      resumed_branches?: number
      guidance_calls?: {
        call: number; status: string
        check_result?: string
        proposed_control?: string
        proposed_move?: {
          robot_id: string; action: string; path: string[]; purpose?: string
          estimated_motion?: {
            distance_m: number; travel_seconds: number
            mean_goal_completion_lower_bound_seconds: number | null
          }
        }
      }[]
    } | null
    candidate_descriptions?: Record<string, string>
    candidate_estimates?: Record<string, {
      mean_goal_completion_seconds: number | null
      total_distance_m: number
      all_steps_completion_seconds: number | null
    }>
    actual_metrics?: {
      outcome: string
      mean_goal_completion_seconds: number | null
      mean_request_to_goal_seconds: number | null
      setup_seconds: number | null
      selection_seconds: number | null
      max_turn_wait_seconds: number | null
      unfinished_robot_ids: string[]
    }
  }
  order_stream?: {
    generated: number; pending: number; in_progress: number; completed: number
    failed: number; cancelled: number; returning: number
    interval_seconds: number; max_pending: number
  }
  robots: { robot_id: string; status: string; current_node: string | null; goal: string | null }[]
}

export function getScenarios() {
  return request<ScenarioItem[]>('/api/scenarios')
}

export function getScenarioStatus() {
  return request<ScenarioStatus>('/api/scenarios/status')
}

export function runScenario(name: string, route_source?: ScenarioRouteSource) {
  return request<ScenarioStatus>('/api/scenarios/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, route_source }),
  })
}

export function stopOrderSupply() {
  return request<ScenarioStatus>('/api/scenarios/stop', { method: 'POST' })
}
