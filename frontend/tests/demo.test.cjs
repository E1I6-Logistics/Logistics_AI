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
