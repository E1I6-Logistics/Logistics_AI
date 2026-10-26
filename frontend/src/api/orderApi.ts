import type { RouteModelDto } from './fmsApi'

const DEFAULT_API_BASE = `${window.location.protocol}//${window.location.hostname}:8000`
const API_BASE = (import.meta.env.VITE_FMS_API_BASE ?? DEFAULT_API_BASE).replace(/\/$/, '')

export type OrderItems = {
  A: number
  B: number
  C: number
  D: number
}

export type CreateOrderPayload = {
  order_name?: string
  items: OrderItems
  total_quantity: number
  driving_mode?: 'standard' | 'llm'
  operation_mode?: 'algorithm' | 'ai'
  selector?: RouteModelDto['selector']
  model?: string
}

export type CreateOrderResponse = {
  order_id: string
  order_name: string
  robot_id: string | null
  items: OrderItems
  total_quantity: number
  pickup_nodes: string[]
  workstation_node: '3' | '4' | null
  status: string
}

export type OrderDto = CreateOrderResponse & {
  operation_mode: 'algorithm' | 'ai'
  mode: 'simulation' | 'real'
  phase: string
  pickup_index: number
  error?: string
  failed_phase?: string
  recovery_status?: string
  recovery_error?: string
  retry_count?: number
  dispatch_reason?: string
}

export type PickupDto = {
  status: 'FREE' | 'OCCUPIED' | 'UNKNOWN'
  robot_ids: string[]
  order_name: string | null
  phase: string | null
  waiting_orders: string[]
  reserved_robot_ids: string[]
}

export type WorkstationDto = { status: 'FREE' | 'OCCUPIED' | 'ASSIGNED' | 'UNKNOWN'; order_id: string | null }

export async function getOrders() {
  const response = await fetch(`${API_BASE}/api/orders`)
  if (!response.ok) throw new Error('주문 상태를 읽지 못했습니다.')
  return response.json() as Promise<{
    orders: OrderDto[]
    workstations: Record<string, WorkstationDto>
    pickups: Record<string, PickupDto>
  }>
}

export async function retryOrder(orderId: string) {
  const response = await fetch(`${API_BASE}/api/orders/${encodeURIComponent(orderId)}/retry`, {
    method: 'POST',
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail ?? '주문을 재시도하지 못했습니다.')
  return data as OrderDto
}

export async function cancelOrder(orderId: string) {
  const response = await fetch(`${API_BASE}/api/orders/${encodeURIComponent(orderId)}`, { method: 'DELETE' })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail ?? '주문을 취소하지 못했습니다.')
  return data as OrderDto
}

export async function createOrder(payload: CreateOrderPayload) {
  const response = await fetch(`${API_BASE}/api/orders`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(
      `${response.status} ${response.statusText}${text ? `: ${text}` : ''}`,
    )
  }

  return response.json() as Promise<CreateOrderResponse>
}
