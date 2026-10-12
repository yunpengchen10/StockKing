import test from 'node:test'
import assert from 'node:assert/strict'
import { withRecordReadTimeout } from './recordRead.mjs'

test('a record request that never settles is released for retry', async () => {
  await assert.rejects(withRecordReadTimeout(new Promise(() => {}), 10), /记录读取超时/)
  assert.deepEqual(await withRecordReadTimeout(Promise.resolve({ items: [] }), 100), { items: [] })
})

test('record transport errors are preserved', async () => {
  const failure = new Error('engine unavailable')
  await assert.rejects(withRecordReadTimeout(Promise.reject(failure), 100), error => error === failure)
})
