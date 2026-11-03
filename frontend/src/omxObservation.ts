import type { OmxDeviceDto } from './api/fmsApi'

// HTTP 조회 실패와 실제 장치 OFFLINE은 서로 다른 관측이다.
export function delayedOmxSnapshot(previous: OmxDeviceDto[]): OmxDeviceDto[] {
  return previous.map(row => ({ ...row, observation_stale: true }))
}

export function omxConnectionPresentation(device: OmxDeviceDto) {
  if (device.observation_stale) return { color: '#D89B26', label: '조회 지연 · 마지막 수신 상태' }
  return device.connected
    ? { color: '#2589F5', label: device.state }
    : { color: '#9AA4B0', label: '연결 끊김' }
}
