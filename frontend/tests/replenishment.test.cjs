const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
test('HTTP LAN에서도 crypto.randomUUID 없이 유효하고 서로 다른 요청 ID를 만든다', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/components/palletReplenishmentId.ts'), 'utf8')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const mod = { exports: {} }
  new Function('module', 'exports', 'crypto', js)(mod, mod.exports, { getRandomValues: require('node:crypto').webcrypto.getRandomValues.bind(require('node:crypto').webcrypto) })
  const ids = Array.from({length:20}, () => mod.exports.replenishmentRequestId())
  assert.equal(new Set(ids).size,20)
  for (const id of ids) assert.match(id,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
})
