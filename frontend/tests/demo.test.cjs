const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
function load(file) {
  const source = fs.readFileSync(path.join(__dirname, '../src/demo', file), 'utf8')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText
  const module = { exports: {} }; new Function('module', 'exports', js)(module, module.exports); return module.exports
}
let storage = new Map()
global.sessionStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) }
const m = load('model.ts'), o = load('observations.ts')
const run = (demo = 'D03') => ({ id: 'test', demo, mode: 'real', operation: 'ai', phase: 'RUNNING', started: 1, updated: 1, target: 'robot1', message: '진행', events: [] })
test('마커·충전·교통 시작 위치를 실제 기존 정의와 대조한다', () => {
  assert.deepEqual(m.MARKERS, { 3: 27, 4: 26, 5: 24, 6: 25 })
  const gateway = fs.readFileSync(path.join(__dirname, '../../backend/app/ros2/ros_gateway.py'), 'utf8')
  for (const [node, marker] of Object.entries(m.MARKERS)) assert.match(gateway, new RegExp(`"${node}": ${marker}`))
  const cases = JSON.parse(fs.readFileSync(path.join(__dirname, '../../tests/traffic_scenarios.json'))).scenarios
  for (const [name, preset] of Object.entries(m.TRAFFIC)) {
    const row = cases.find(c => c.name === name)
    assert.equal(preset.starts, row.robots.map(r => `${r.id} N${r.start}`).join(' · '))
    assert.ok(cases.some(c => c.name === name + '_ai'))
  }
})
test('주문 실패·취소 후 복귀 중인 건도 새 독립 동작과 겹치지 않는다', () => {
  assert.equal(m.activeOrder({ status: 'CANCELLED', recovery_status: 'RETURNING' }), true)
  assert.equal(m.activeOrder({ status: 'FAILED', recovery_status: 'BLOCKED' }), true)
  assert.equal(m.activeOrder({ status: 'CANCELLED', recovery_status: 'RETURNED' }), false)
  assert.equal(m.activeOrder({ status: 'PENDING' }), true)
  assert.equal(m.activeOrder({ status: 'COMPLETED', phase: 'RETURNING' }), true)
  assert.equal(m.activeOrder({ status: 'COMPLETED', phase: 'COMPLETED' }), false)
})
test('새로고침 후 개별 이동은 완료로 추정하지 않고 서버 ID 주문은 재조회 가능 상태를 보존한다', () => {
  storage.clear(); m.saveRuns([run(), { ...run('D01'), sourceId: 'order-1' }])
  const restored = m.loadRuns()
  assert.equal(restored[0].phase, 'REVIEW')
  assert.equal(restored[1].phase, 'RUNNING')
  assert.equal(restored[1].sourceId, 'order-1')
  assert.equal(m.isActive(restored[0]), true)
})
test('손상된 세션 기록이 실행을 만들거나 화면을 깨뜨리지 않는다', () => {
  storage.set('fms-demo-observation-v1', '[{"demo":"D99"}]'); assert.deepEqual(m.loadRuns(), [])
  storage.set('fms-demo-observation-v1', '{'); assert.deepEqual(m.loadRuns(), [])
  storage.set('fms-demo-observation-v1', JSON.stringify([{ ...run(), events: [null] }])); assert.deepEqual(m.loadRuns(), [])
})
test('위치 기반 속도는 적절한 간격에서만 계산하고 위치 순간 이동은 제외한다', () => {
  const a = { at: 1000, x: 0, y: 0, yaw: Math.PI - .1, mode: 'real' }
  const b = { at: 3000, x: .4, y: 0, yaw: -Math.PI + .1, mode: 'real' }
  assert.equal(o.estimateMotion(a, b).speed, .2)
  assert.ok(Math.abs(o.estimateMotion(a, b).angular - .1) < 1e-8)
  for (const bad of [{ ...b, at: 1001 }, { ...b, at: 10000 }, { ...b, mode: 'simulation' }, { ...b, x: 100 }, { ...b, x: NaN }]) assert.equal(o.estimateMotion(a, bad).speed, null)
})
test('원본 시각 중복·역전은 수신 시각으로 속도를 위조하지 않는다', () => {
  const a = { at: 1000, x: 0, y: 0, yaw: 0, mode: 'simulation', sourceAt: '2026-11-02T00:00:00Z' }
  assert.equal(o.estimateMotion(a, { ...a, at: 3000, x: 1 }).speed, null)
  assert.equal(o.estimateMotion(a, { ...a, at: 3000, sourceAt: '2026-11-01T00:00:00Z' }).speed, null)
})
test('동일 상태 이벤트 중복은 쌓지 않으며 기록·내역 크기를 제한한다', () => {
  let r = run(); assert.equal(m.eventRun(r, 'RUNNING', '진행'), r)
  for (let i = 0; i < 300; i++) r = m.eventRun(r, 'RUNNING', String(i))
  assert.equal(r.events.length, 160)
  storage.clear(); m.saveRuns(Array.from({ length: 30 }, () => run('D01'))); assert.equal(m.loadRuns().length, 20)
})
test('통신 관측 버퍼는 120건으로 제한하며 실패도 표시하고 원본 시각 유무를 보존한다', () => {
  for (let i = 0; i < 180; i++) { o.recordHttp('/api/robots', performance.now(), i % 2 === 0); o.recordWs('연결됨', true) }
  o.recordRobot('robot1', { x: 0, y: 0, yaw: 0, mode: 'real' }, true)
  const s = o.observationSnapshot()
  assert.equal(s.http.length, 120); assert.equal(s.wsTimes.length, 120)
  assert.equal(s.http[119].ok, false); assert.equal(s.robots.robot1.sourceAt, undefined)
  assert.equal(s.robots.robot1.speed, null)
})
test('API별 집계는 메서드를 구분하고 응답 간격·평균·실패를 보존한다', () => {
  const samples = [
    { at: 3000, path: '/api/orders', ms: 9, ok: false },
    { at: 1000, path: '/api/orders', ms: 3, ok: true },
  ]
  const summary = o.summarizeHttp(samples)
  assert.equal(summary.averageMs, 6); assert.equal(summary.periodMs, 2000)
  assert.equal(summary.failures, 1); assert.equal(summary.last.at, 3000)
  assert.equal(o.summarizeHttp(samples.slice(0, 1)).periodMs, null)
  assert.equal(o.summarizeHttp([]).averageMs, null)
  o.recordHttp('/api/orders', performance.now(), true, 'GET')
  o.recordHttp('/api/orders', performance.now(), false, 'POST')
  const s = o.observationSnapshot()
  assert.ok(s.apis.some(a => a.key === 'GET /api/orders'))
  assert.equal(s.apis.find(a => a.key === 'POST /api/orders').failures, 1)
})
test('한 API의 요청 폭증이 다른 API 이력을 밀어내지 않고 전체 크기도 제한한다', () => {
  o.recordHttp('/api/rare', performance.now(), true)
  for (let i = 0; i < 150; i++) o.recordHttp('/api/frequent', performance.now(), true)
  let s = o.observationSnapshot()
  assert.equal(s.apis.find(a => a.key === 'GET /api/rare').count, 1)
  assert.equal(s.apis.find(a => a.key === 'GET /api/frequent').count, 40)
  for (let i = 0; i < 90; i++) o.recordHttp('/api/bounded/' + i, performance.now(), true)
  assert.equal(o.observationSnapshot().apis.length, 64)
})
test('Zenoh HTTP 200의 조회 불가도 연결 정상으로 표시하지 않고 마지막 성공을 보존한다', () => {
  o.recordConnections([{ name: 'robot1', ip: '192.168.0.1', connected: true, blocked: false, state: 'CONNECTED' }], true)
  const previous = o.observationSnapshot().connections
  o.recordConnections(undefined, false, '조회 불가')
  const next = o.observationSnapshot().connections
  assert.equal(next.available, false); assert.equal(next.lastSuccessAt, previous.lastSuccessAt)
  assert.equal(next.devices[0].name, 'robot1'); assert.equal(next.error, '조회 불가')
  next.devices[0].connected = false
  assert.equal(o.observationSnapshot().connections.devices[0].connected, true)
})


test('위치 미수신·null 좌표로 가짜 속도나 0,0 관측을 만들지 않는다', () => {
  o.recordRobot('missing', {x:null,y:null,yaw:null,mode:'real'}, false)
  assert.equal(o.observationSnapshot().robots.missing, undefined)
  o.recordRobot('valid', {x:1,y:2,yaw:0,mode:'real'}, true)
  assert.ok(o.observationSnapshot().robots.valid)
  o.recordRobot('valid', {x:null,y:2,yaw:0,mode:'real'}, true)
  assert.equal(o.observationSnapshot().robots.valid, undefined)
})
