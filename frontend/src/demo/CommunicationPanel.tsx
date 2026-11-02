import type { RobotMode, OmxDeviceDto, DockingStationDto } from '../api/fmsApi'
import type { ManagedRobot } from '../hooks/useRobotFleet'
import { observationSnapshot } from './observations'

type Props = { stats: ReturnType<typeof observationSnapshot>; mode: RobotMode; robots: ManagedRobot[]; omx: OmxDeviceDto[]; docks: DockingStationDto[]; omxError: string | null }
const ms = (v: number | null | undefined) => v == null ? '—' : `${v.toFixed(0)} ms`
const age = (at: number | null | undefined, now: number) => !at || at > now + 5000 ? '미확인' : `${Math.max(0, (now - at) / 1000).toFixed(1)}초 전`
const time = (at: number) => new Date(at).toLocaleTimeString('ko-KR', { hour12: false })
const names: Record<string, string> = {
  '/api/connections': 'Zenoh 연결 목록', '/api/robots': 'AMR 상태', '/api/omx': 'OMX 상태·팔레트',
  '/api/orders': '주문', '/api/robots/docking-stations': 'MQTT 도킹 센서',
  '/api/scenarios/status': '교통 시험 상태', '/api/system/web': 'FMS 웹서버', '/api/mode': '운영 모드',
  '/api/command/route-models': '경로 모델 목록', '/api/route/nodes': '지도 노드', '/api/route/graph': '경로 지도',
}
function Badge({ text, ok }: { text: string; ok?: boolean | null }) {
  return <span className={`demo-pill ${ok === true ? 'green' : ok === false ? 'orange' : ''}`}>{text}</span>
}
export default function CommunicationPanel({ stats, mode, robots, omx, docks, omxError }: Props) {
  const real = mode === 'real'
  const zenoh = stats.connections
  const zenohFresh = real && zenoh.available && stats.at - zenoh.at < 6000
  const dockApi = stats.apis.find(a => a.last.path === '/api/robots/docking-stations' && a.last.method === 'GET')
  const mqttFresh = real && !!dockApi?.last.ok && stats.at - dockApi.last.at < 6000
  const broker = mqttFresh && docks.length ? (docks.every(d => d.broker_connected) ? true : docks.every(d => !d.broker_connected) ? false : null) : null
  const omxApi = stats.apis.find(a => a.last.path === '/api/omx' && a.last.method === 'GET')
  const omxFresh = real && !omxError && !!omxApi?.last.ok && stats.at - omxApi.last.at < 6000
  const apis = [...stats.apis].sort((a, b) => a.last.path.localeCompare(b.last.path) || a.key.localeCompare(b.key))
  const lastWs = stats.wsTimes[stats.wsTimes.length - 1]
  return <>
    <div className="demo-protocols">
      <section className="demo-card"><h2>대시보드 WebSocket</h2>
        <Badge text={stats.wsState} ok={stats.wsState === '연결됨'} />
        <p>브라우저 ↔ FMS 상태 화면</p>
        <div className="demo-comm-pair"><span>최근 10초 수신량</span><strong>{(stats.wsTimes.filter(at => stats.at - at < 10000).length / 10).toFixed(1)}건/초</strong></div>
        <div className="demo-comm-pair"><span>마지막 메시지</span><strong>{age(lastWs, stats.at)}</strong></div>
      </section>
      <section className="demo-card"><h2>Zenoh · AMR</h2>
        <Badge text={!real ? '실제 모드에서 확인' : zenohFresh ? '연결 목록 조회 정상' : '연결 목록 확인 불가'} ok={real ? zenohFresh : null} />
        <p>FMS ↔ Zenoh 라우터의 기존 조회 결과</p>
        {real && <><div className="demo-comm-pair"><span>최근 정상 조회</span><strong>{age(zenoh.lastSuccessAt, stats.at)}</strong></div>
          {robots.map(r => { const d = zenoh.devices.find(device => device.name === r.backendId); return <div className="demo-comm-pair" key={r.id}><span>{r.backendId} <small>{d?.ip || r.ip}</small></span><Badge text={!zenohFresh ? '미확인' : !d ? '목록에 없음' : d.blocked ? '차단' : d.connected ? '연결' : '끊김'} ok={zenohFresh && d ? d.connected && !d.blocked : null} /></div> })}
          {!zenohFresh && <small className="demo-warning">{zenoh.error || '최근 연결 조회를 기다리는 중'}</small>}</>}
      </section>
      <section className="demo-card"><h2>MQTT · OMX</h2>
        <Badge text={!real ? '실제 모드에서 확인' : broker == null ? '브로커 상태 미확인' : broker ? 'FMS 브로커 연결됨' : 'FMS 브로커 연결 끊김'} ok={broker} />
        <p>브로커: 도킹 API / 장치 수신: OMX API</p>
        {real && omx.map(d => <div className="demo-comm-pair" key={d.omx_id}><span>{d.omx_id}<small>수신 {age(d.last_seen * 1000, stats.at)}</small></span><Badge text={!omxFresh ? '미확인' : d.connected ? '연결' : '끊김'} ok={omxFresh ? d.connected : null} /></div>)}
        {real && !omx.length && <small>OMX 수신 정보 없음</small>}
      </section>
    </div>
    <section className="demo-card"><h2>API별 요청 현황</h2>
      <p>API마다 한 줄로 묶었습니다. 항목을 펼치면 요청 이력을 볼 수 있습니다. 응답 시간은 브라우저에서 응답 헤더를 받기까지이며, ROS·MQTT 전송 지연은 아닙니다.</p>
      <div className="demo-api-list">
        <div className="demo-api-summary demo-api-header" aria-hidden="true"><span>API</span><span>최근 결과</span><span>최근 / 평균 응답</span><span>평균 응답 간격</span><span>최근 확인</span></div>
        {apis.map(a => <details className="demo-api" key={a.key}>
          <summary className="demo-api-summary"><span className="demo-api-name"><strong>{names[a.last.path.split('?')[0]] ?? '기타 요청'}</strong><code>{a.last.method} {a.last.path}</code></span>
            <Badge text={a.last.ok ? '응답 정상' : '실패'} ok={a.last.ok} />
            <span>{ms(a.last.ms)} <small>/ {ms(a.averageMs)}</small></span>
            <span>{ms(a.periodMs)}</span><span>{age(a.last.at, stats.at)}</span>
          </summary>
          <div className="demo-api-detail"><p>최근 {a.count}건 · 실패 {a.failures}건 · 간격은 응답 관측값입니다. 단발 요청은 주기 통신이 아닙니다.</p>
            <div className="demo-table-wrap"><table><thead><tr><th>시각</th><th>결과</th><th>응답 시간</th></tr></thead><tbody>{[...a.samples].reverse().map((s, i) => <tr key={`${s.at}-${i}`}><td>{time(s.at)}</td><td>{s.ok ? '응답 정상' : '실패'}</td><td>{ms(s.ms)}</td></tr>)}</tbody></table></div>
          </div>
        </details>)}
        {!apis.length && <div className="demo-empty">기존 API 요청을 기다리는 중입니다.</div>}
      </div>
      <small className="demo-muted">최대 API 64종 · API별 최근 40건 보관. 별도 폴링·ROS·MQTT 구독을 추가하지 않습니다.</small>
    </section>
    <details className="demo-card demo-motion"><summary>AMR 수신·추정 속도 자세히 보기</summary>
      <p>기존 상태 수신으로 계산한 화면용 추정값입니다. 장치 원본 발행 주기나 주행 명령값과는 다릅니다.</p>
      <div className="demo-table-wrap"><table><thead><tr><th>AMR</th><th>상태 수신</th><th>수신 간격</th><th>추정 선속도</th><th>추정 각속도</th></tr></thead><tbody>{robots.map(r => {
        const o = stats.robots[r.backendId]; const fresh = o && stats.at - o.at < 6000
        return <tr key={r.id}><td>{r.backendId}</td><td>{age(o?.at, stats.at)}<small className="demo-muted">{o?.sourceAt ? `원본 ${o.sourceAt}` : '브라우저 수신 시각 사용'}</small></td><td>{fresh ? ms(o.periodMs) : '—'}</td><td>{fresh && o.speed != null ? `${o.speed.toFixed(2)} m/s` : '—'}</td><td>{fresh && o.angular != null ? `${o.angular.toFixed(2)} rad/s` : '—'}</td></tr>
      })}</tbody></table></div>
    </details>
  </>
}
