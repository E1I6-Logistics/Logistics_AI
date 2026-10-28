import { useEffect, useRef, useState } from 'react'
import { getDockingSettings, setDockContactVerification, type DockingSettingsDto } from '../api/fmsApi'
import { C } from '../constants/theme'

export default function DockContactToggle({ disabled }: { disabled: boolean }) {
  const [settings, setSettings] = useState<DockingSettingsDto | null>(null)
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
          const next = await getDockingSettings()
          if (!closed && expected === generation.current) { setSettings(next); setError('') }
        }
      } catch (e) {
        if (!closed && expected === generation.current) {
          setError(e instanceof Error ? e.message : '도킹 접촉 설정 조회 실패')
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
      const next = await setDockContactVerification(!settings.contact_verification_enabled)
      if (mounted.current) setSettings(next)
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : '도킹 접촉 설정 변경 실패')
    } finally {
      writing.current = false
      if (mounted.current) setUpdating(false)
    }
  }

  const enabled = settings?.contact_verification_enabled ?? false
  const unavailable = disabled || updating || !settings || Boolean(error)

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      <button type="button" role="switch" aria-label="실제 로봇 도킹 접촉 검증" aria-checked={enabled}
        aria-busy={updating} disabled={unavailable} onClick={() => { void change() }}
        title={settings
          ? '실제 로봇 전체 · ON: 도킹 성공과 리미트 스위치 ON 확인. OFF: 도킹 성공만 확인. 배차 직전 ON 재검사는 하지 않습니다.'
          : '도킹 접촉 설정을 불러오는 중입니다.'}
        style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '5px 9px', borderRadius: 6,
          border: `1px solid ${enabled ? '#BFE0D0' : C.line}`, background: enabled ? '#EDF8F2' : C.subtle,
          color: C.text, fontSize: 10.5, cursor: unavailable ? 'not-allowed' : 'pointer', opacity: unavailable ? .65 : 1 }}>
        <span>도킹 접촉 검증</span>
        <span aria-hidden="true" style={{ position: 'relative', width: 27, height: 16, borderRadius: 9,
          background: enabled ? C.success : '#939EAC', flexShrink: 0 }}>
          <span style={{ position: 'absolute', top: 2, left: enabled ? 13 : 2, width: 12, height: 12,
            borderRadius: '50%', background: '#fff', transition: 'left .15s' }} />
        </span>
        <strong>{updating ? '변경 중…' : error ? '조회 오류' : !settings ? '조회 중…' : enabled ? 'ON' : 'OFF'}</strong>
      </button>
      {error && <span role="alert" title={error} style={{ color: C.danger, fontSize: 10 }}>도킹 접촉 설정을 확인하지 못했습니다.</span>}
    </div>
  )
}
