const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/omxObservation.ts'), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
const mod = { exports: {} }; new Function('module', 'exports', js)(mod, mod.exports)
const { delayedOmxSnapshot, omxConnectionPresentation } = mod.exports

test('one HTTP timeout never fabricates four physical disconnects or loses job progress', () => {
  const rows = [1,2,3,4].map(n => ({ omx_id: `omx${n}`, connected: true, state: 'WORKING', current_count: 1, total_count: 3, job_id: 'ongoing' }))
  const delayed = delayedOmxSnapshot(rows)
  assert.equal(delayed.filter(r => r.connected).length, 4)
  assert.ok(delayed.every(r => r.current_count === 1 && r.job_id === 'ongoing'))
  assert.ok(rows.every(r => !r.observation_stale))
  assert.ok(delayed.every(r => omxConnectionPresentation(r).label.includes('조회 지연')))
})

test('fresh server OFFLINE still displays actual disconnect; fresh success clears delay', () => {
  assert.equal(omxConnectionPresentation({ connected: false }).label, '연결 끊김')
  assert.equal(omxConnectionPresentation({ connected: true, state: 'WORKING' }).label, 'WORKING')
})
