import { useEffect, useRef, useState } from 'react'
import { getDispatchSettings, setDispatchVoltageCheck, type DispatchSettingsDto } from '../api/fmsApi'
import { C } from '../constants/theme'

export default function DispatchVoltageToggle({ disabled }: { disabled: boolean }) {
  const [settings, setSettings] = useState<DispatchSettingsDto | null>(null)
  const [updating, setUpdating] = useState(false)
  const [error, setError] = useState('')
  const writing = useRef(false)
  const generation = useRef(0)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    let closed = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      const expected = generation.current
      try {
        if (!writing.current) {
          const next = await getDispatchSettings()
          if (!closed && expected === generation.current) { setSettings(next); setError('') }
        }
      } catch (e) {
        if (!closed && expected === generation.current) {
          setError(e instanceof Error ? e.message : '배차 전압 설정 조회 실패')
        }
      } finally {
        if (!closed) timer = setTimeout(() => { void poll() }, 2000)
      }
    }
    void poll()
    return () => { closed = true; mounted.current = false; clearTimeout(timer) }
  }, [])

  const change = async () => {
    if (!settings || disabled || writing.current) return
    writing.current = true
    generation.current += 1
    setUpdating(true)
    setError('')
    try {
      const next = await setDispatchVoltageCheck(!settings.battery_voltage_check_enabled)
      if (mounted.current) setSettings(next)
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : '배차 전압 설정 변경 실패')
    } finally {
      writing.current = false
      if (mounted.current) setUpdating(false)
    }
  }

  const enabled = settings?.battery_voltage_check_enabled ?? false
  const unavailable = disabled || updating || !settings || Boolean(error)
  const threshold = settings ? `${settings.minimum_dispatch_voltage.toFixed(1)}V` : ''

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      <button type="button" role="switch" aria-label="실제 로봇 배차 전압 검사" aria-checked={enabled}
        aria-busy={updating} disabled={unavailable} onClick={() => { void change() }}
        title={settings
          ? `실제 로봇 전체 · 알고리즘/AI 공통 · 최근 ${settings.voltage_window_s}초 최저 전압 ${threshold} 기준. OFF는 전압 조건만 생략합니다.`
          : '배차 전압 설정을 불러오는 중입니다.'}
        style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '5px 9px', borderRadius: 6,
          border: `1px solid ${enabled ? '#BFE0D0' : C.line}`, background: enabled ? '#EDF8F2' : C.subtle,
          color: C.text, fontSize: 10.5, cursor: unavailable ? 'not-allowed' : 'pointer', opacity: unavailable ? .65 : 1 }}>
        <span>배차 전압 검사</span>
        <span aria-hidden="true" style={{ position: 'relative', width: 27, height: 16, borderRadius: 9,
          background: enabled ? C.success : '#939EAC', flexShrink: 0 }}>
          <span style={{ position: 'absolute', top: 2, left: enabled ? 13 : 2, width: 12, height: 12,
            borderRadius: '50%', background: '#fff', transition: 'left .15s' }} />
        </span>
        <strong>{updating ? '변경 중…' : error ? '조회 오류' : !settings ? '조회 중…' : enabled ? `ON · ${threshold}` : 'OFF'}</strong>
      </button>
      {error && <span role="alert" title={error} style={{ color: C.danger, fontSize: 10 }}>배차 전압 설정을 확인하지 못했습니다.</span>}
    </div>
  )
}
