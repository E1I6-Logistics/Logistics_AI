const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const source = fs.readFileSync(path.join(__dirname, '../src/components/WafPanel.tsx'), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 } }).outputText
const mod = { exports: {} }
const localRequire = name => name.includes('fmsApi') ? {} : name.includes('theme') ? { C: { muted: '#666', surface: '#fff' } } : require(name)
new Function('module', 'exports', 'require', js)(mod, mod.exports, localRequire)
const render = data => renderToStaticMarkup(React.createElement(mod.exports.default, { data, error: null, onClose() {} }))
const row = () => ({ available: true, busy: false, action_name: '/robot4/pallet_replace', can_cancel: false, job: null,
  robot: { connected: true, connection_state: 'CONNECTED', activity: 'READY', battery: 63, battery_voltage: 12.4, battery_fresh: true,
    last_seen: 100, battery_received_at: 100, pose: null, feedback: { stage: 'ALIGN_2', received_at: 100 }, ip: '192.168.0.44' } })
test('WAF 전용 패널은 실제 배터리·전압·raw stage를 표시하고 경로/조이스틱 제어를 만들지 않는다', () => {
  const html = render(row())
  for (const text of ['R-04', 'WAF', '63%', '12.40V', 'ALIGN_2', 'Zenoh 연결됨']) assert.ok(html.includes(text), text)
  for (const text of ['특정 노드 이동', '가까운 노드로 복귀', '충전소 이동', '조이스틱']) assert.ok(!html.includes(text), text)
})
test('미수신 배터리·위치는 0%나 원점으로 만들지 않는다', () => {
  const data = row();data.robot.battery = null;data.robot.battery_voltage = null;data.robot.battery_fresh = false
  const html = render(data)
  assert.ok(html.includes('잔량 미수신') && html.includes('최신 위치 미수신'))
  assert.ok(!html.includes('0%') && !html.includes('x=0.00'))
})
test('새 요청 접수 중에는 이전 요청의 마지막 stage를 현재 단계로 표시하지 않는다', () => {
  const data = row();data.busy = true;data.robot.activity = 'SENDING'
  data.job = { request_id: 'x', state: 'SENDING', item: 'A', destination: 1, started_at: 200, updated_at: 200, stage: '', message: '접수 중' }
  const html = render(data)
  assert.ok(!html.includes('ALIGN_2'))
  assert.ok(html.includes('아직 수신된 단계 없음'))
})
test('재시작 전 기록에는 종료 확인·기록 정리 버튼을 표시한다', () => {
  const data = row();data.busy = true;data.can_clear = true
  data.job = { request_id: 'old', state: 'UNKNOWN', item: 'A', message: '이전 작업 확인' }
  const html = render(data)
  assert.ok(html.includes('이전 작업 종료 확인·기록 정리'))
  assert.ok(!html.includes('보충 액션 취소'))
})
test('취소 접수 대기와 응답 실패 후 취소 재시도를 구분한다', () => {
  const data = row();data.busy = true
  data.job = { request_id: 'x', state: 'CANCELLING', item: 'A', cancel_requested: true, message: '대기' }
  assert.ok(render(data).includes('취소 요청 접수 · WAF 실제 종료 확인 중'))
  data.can_cancel = true;data.job.cancel_requested = false;data.job.state = 'UNKNOWN'
  const html = render(data)
  assert.ok(html.includes('보충 액션 취소'))
  assert.ok(!html.includes('WAF 실제 종료 확인 중'))
})
