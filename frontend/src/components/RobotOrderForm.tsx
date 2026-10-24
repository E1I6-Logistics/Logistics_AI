import { useEffect, useId, useMemo, useState } from 'react'

import { createOrder, type OrderItems } from '../api/orderApi'
import type { RouteModelDto, RouteModelGroupsDto } from '../api/fmsApi'
import { C, MONO } from '../constants/theme'

export type DispatchUnavailable = {
  robots: { id: string; reason: string | null }[]
}

type Props = {
  operationMode: 'algorithm' | 'ai'
  disabled?: boolean
  routeModels: RouteModelGroupsDto
  routeModelError: string | null
  llmOrderReady: boolean
  dispatchUnavailable?: DispatchUnavailable | null
}

// 백엔드 dispatch_block_reason 표시 문구
const DISPATCH_BLOCK_LABELS: Record<string, string> = {
  assigned_order: '주문 수행 중',
  robot_order: '주문 수행 중',
  recovering: '주문 취소 후 복귀 중',
  recovery_hold: '복귀 실패로 대기',
  returning_stopped: '복귀 중 정지',
  navigating: '이동 중',
  not_idle: '대기 상태 아님',
  on_edge: '통로 위(노드 미확정)',
  stale_pose: '위치 정보 지연',
  disconnected: '연결 끊김',
  low_battery: '배터리 전압 부족',
  battery_unknown: '배터리 전압 미확인',
  scenario_active: '시나리오 실행 중',
  status_busy: '상태 확인 중',
  unavailable: '배차 불가',
}

// 로봇이 주문을 끝내고 바로 다음 주문을 받는 순간처럼 잠깐 불가능한 상태는 알리지 않는다.
const DISPATCH_NOTICE_DELAY_MS = 1500

const INITIAL_ITEMS: OrderItems = {
  A: 0,
  B: 0,
  C: 0,
  D: 0,
}

// 기존 코드. 배차 불가 안내(dispatchUnavailable)를 받도록 바꾼다.
// export default function RobotOrderForm({ operationMode, disabled = false, routeModels, routeModelError, llmOrderReady }: Props) {
export default function RobotOrderForm({
  operationMode, disabled = false, routeModels, routeModelError, llmOrderReady, dispatchUnavailable = null,
}: Props) {
  const [items, setItems] = useState<OrderItems>(INITIAL_ITEMS)
  const [orderName, setOrderName] = useState('')
  const [expanded, setExpanded] = useState(true)
  const drivingMode = operationMode === 'ai' ? 'llm' : 'standard'
  const [selectedModelKey, setSelectedModelKey] = useState('')
  const fieldsId = useId()
  const [submitting, setSubmitting] = useState(false)
  const [message, setMessage] = useState<{
    tone: 'success' | 'danger'
    text: string
  } | null>(null)

  const totalQuantity = useMemo(
    () => Object.values(items).reduce((sum, quantity) => sum + quantity, 0),
    [items],
  )
  const validQuantity = totalQuantity >= 1 && totalQuantity <= 5
  const formDisabled = disabled || submitting
  const availableModels = useMemo(
    () => [...routeModels.ollama, ...routeModels.decision_model],
    [routeModels],
  )
  const selectedModel: RouteModelDto | undefined = availableModels.find(
    item => item.selector + ':' + item.model === selectedModelKey,
  )
  const canSubmit = validQuantity && (
    drivingMode === 'standard' || (llmOrderReady && Boolean(selectedModel))
  )

  useEffect(() => {
    setItems(INITIAL_ITEMS)
    setSelectedModelKey('')
    setMessage(null)
  }, [])

  const [dispatchNotice, setDispatchNotice] = useState<DispatchUnavailable | null>(null)
  useEffect(() => {
    if (!dispatchUnavailable) {
      setDispatchNotice(null)
      return
    }
    // 이미 표시 중이면 사유만 바로 갱신한다.
    if (dispatchNotice) {
      setDispatchNotice(dispatchUnavailable)
      return
    }
    const timer = window.setTimeout(() => setDispatchNotice(dispatchUnavailable), DISPATCH_NOTICE_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [dispatchUnavailable]) // eslint-disable-line react-hooks/exhaustive-deps

  const updateQuantity = (item: keyof OrderItems, rawValue: string) => {
    const parsed = Number(rawValue)
    const quantity = Number.isFinite(parsed)
      ? Math.max(0, Math.min(5, Math.trunc(parsed)))
      : 0

    setItems(current => ({ ...current, [item]: quantity }))
    setMessage(null)
  }

  const submit = async () => {
    if (!canSubmit || formDisabled) return

    try {
      setSubmitting(true)
      setMessage(null)

      const order = await createOrder({
        order_name: orderName || undefined,
        items,
        total_quantity: totalQuantity,
        operation_mode: operationMode,
        driving_mode: drivingMode,
        selector: drivingMode === 'llm' ? selectedModel?.selector : undefined,
        model: drivingMode === 'llm' ? selectedModel?.model : undefined,
      })

      setMessage({
        tone: 'success',
        text: `주문 ${order.order_name}이(가) 등록되었습니다.`,
      })
      setItems(INITIAL_ITEMS)
      setOrderName('')
    } catch (error) {
      setMessage({
        tone: 'danger',
        text: error instanceof Error ? error.message : '주문을 등록하지 못했습니다.',
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section
      aria-label="자동 배차 주문 입력"
      style={{
        padding: 11,
        border: `1px solid ${C.line}`,
        borderRadius: 10,
        background: 'rgba(255,255,255,.97)',
        boxShadow: '0 6px 20px rgba(31,45,61,.14)',
      }}
    >
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={fieldsId}
        onClick={() => setExpanded(current => !current)}
        style={{
          display: 'flex',
          width: '100%',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          padding: 0,
          border: 0,
          background: 'transparent',
          color: C.text,
          fontSize: 11,
          fontWeight: 800,
          textAlign: 'left',
          cursor: 'pointer',
        }}
      >
        <span>주문 입력 · 자동 배차</span>
        <span style={{ color: C.muted, fontSize: 9, fontWeight: 600 }}>
          {expanded ? '접기 ▲' : '펼치기 ▼'}
        </span>
      </button>

      <div
        id={fieldsId}
        hidden={!expanded}
        style={{
          marginTop: 9,
        }}
      >
        <label style={{ display: 'block', marginBottom: 8, color: C.muted, fontSize: 9 }}>
          주문명
          <input value={orderName} onChange={event => setOrderName(event.target.value)}
            disabled={formDisabled} placeholder="ORDER-001"
            style={{ display: 'block', width: '100%', height: 29, boxSizing: 'border-box',
              marginTop: 4, padding: '0 8px', border: `1px solid ${C.line}`, borderRadius: 6 }} />
        </label>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
            gap: 6,
          }}
        >
          {(Object.keys(items) as (keyof OrderItems)[]).map(item => (
            <label
              key={item}
              style={{
                display: 'block',
                color: C.text,
                fontSize: 9,
                fontWeight: 800,
              }}
            >
              <span style={{ display: 'block', marginBottom: 4 }}>
                {item} · N{item === 'A' || item === 'B' ? '5' : '6'}
              </span>
              <input
                type="number"
                min={0}
                max={5}
                step={1}
                value={items[item]}
                disabled={formDisabled}
                onChange={event => updateQuantity(item, event.target.value)}
                aria-label={`${item} 수량`}
                style={{
                  width: '100%',
                  minWidth: 0,
                  height: 29,
                  boxSizing: 'border-box',
                  border: `1px solid ${C.line}`,
                  borderRadius: 6,
                  padding: '0 8px',
                  background: formDisabled ? '#EEF1F5' : '#fff',
                  color: C.text,
                  font: `600 10px ${MONO}`,
                }}
              />
            </label>
          ))}
        </div>

        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 10,
            marginTop: 9,
          }}
        >
          <span style={{ color: C.muted, fontSize: 9 }}>작업대 자동 선택</span>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              color: totalQuantity > 5 ? C.danger : validQuantity ? C.success : C.muted,
              fontSize: 9,
              fontWeight: 800,
            }}
          >
            <span>적재량</span>
            <span>{totalQuantity} / 5</span>
          </div>
        </div>

        <div style={{ marginTop: 10, color: C.muted, fontSize: 11 }}>
          운영 모드: {operationMode === 'ai' ? 'AI' : '알고리즘'} · 상단 설정 적용
        </div>
        {drivingMode === 'llm' && (
          <div style={{ marginTop: 8 }}>
            <label htmlFor={fieldsId + '-model'} style={{ display: 'block', marginBottom: 4, color: C.muted, fontSize: 9 }}>
              주문 경로 모델
            </label>
            <select
              id={fieldsId + '-model'}
              value={selectedModelKey}
              disabled={formDisabled}
              onChange={event => { setSelectedModelKey(event.target.value); setMessage(null) }}
              style={{
                width: '100%',
                height: 29,
                border: '1px solid ' + C.line,
                borderRadius: 6,
                background: '#fff',
                color: C.text,
                fontSize: 9,
              }}
            >
              <option value="">모델 선택</option>
              <optgroup label="Ollama">
                {routeModels.ollama.map(item => (
                  <option key={'ollama:' + item.model} value={'ollama:' + item.model}>{item.model}</option>
                ))}
              </optgroup>
              <optgroup label="Decision Model">
                {routeModels.decision_model.map(item => (
                  <option key={item.selector + ':' + item.model} value={item.selector + ':' + item.model}>
                    {item.selector.toUpperCase()} · {item.model}
                  </option>
                ))}
              </optgroup>
            </select>
            {!llmOrderReady && (
              <div style={{ color: C.muted, fontSize: 8.5, marginTop: 4 }}>
                주문 LLM 경로의 서버 연결이 완료되면 등록할 수 있습니다.
              </div>
            )}
            {routeModelError && <div role="alert" style={{ color: C.danger, fontSize: 8.5, marginTop: 4 }}>{routeModelError}</div>}
          </div>
        )}

        {totalQuantity > 5 && (
          <div style={{ marginTop: 5, color: C.danger, fontSize: 8.5 }}>
            최대 적재량을 초과했습니다.
          </div>
        )}

        {dispatchNotice && (
          <div
            role="status"
            style={{
              marginTop: 7,
              padding: '6px 7px',
              borderRadius: 6,
              background: '#FFF6E8',
              color: C.warning,
              fontSize: 8.5,
              lineHeight: 1.4,
            }}
          >
            <div style={{ fontWeight: 800 }}>현재 배차 가능한 로봇이 없습니다.</div>
            <div>주문은 등록되며, 로봇이 가능해지면 순서대로 배차됩니다.</div>
            {dispatchNotice.robots.length === 0 ? (
              <div style={{ marginTop: 3 }}>연결된 로봇이 없습니다.</div>
            ) : (
              <div style={{ marginTop: 3, fontFamily: MONO }}>
                {dispatchNotice.robots
                  .map(robot => `${robot.id} ${DISPATCH_BLOCK_LABELS[robot.reason ?? ''] ?? robot.reason ?? '사유 미확인'}`)
                  .join(' · ')}
              </div>
            )}
          </div>
        )}

        {message && (
          <div
            role="status"
            style={{
              marginTop: 7,
              padding: '6px 7px',
              borderRadius: 6,
              background: message.tone === 'success' ? '#E9F8F3' : '#FFF0F2',
              color: message.tone === 'success' ? C.success : C.danger,
              fontSize: 8.5,
              lineHeight: 1.4,
            }}
          >
            {message.text}
          </div>
        )}

        <button
          type="button"
          onClick={submit}
          disabled={formDisabled || !canSubmit}
          style={{
            width: '100%',
            height: 34,
            marginTop: 8,
            border: 0,
            borderRadius: 7,
            background: !formDisabled && canSubmit ? C.primary : '#DDE3EC',
            color: !formDisabled && canSubmit ? '#fff' : '#9AA4B0',
            fontSize: 9.5,
            fontWeight: 800,
            cursor: !formDisabled && canSubmit ? 'pointer' : 'not-allowed',
          }}
        >
          {submitting ? '주문 등록 중…' : '주문 등록'}
        </button>
      </div>
    </section>
  )
}
