import test from 'node:test'
import assert from 'node:assert/strict'
import { createPicksRefreshController, DISPLAY_SNAPSHOT_KEY, LEGACY_SNAPSHOT_KEY, REFRESH_TASK_KEY, readDisplayedPicks, saveDisplayedPicks, selectDisplayedPicks } from './picksRefresh.mjs'

function memoryStorage() {
  const values = new Map()
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
}
const snapshot = (candidates = [{ code: '600001' }], generatedAt = '2026-09-18T10:30:00+08:00') => ({ schemaVersion: 4, generatedAt, adaptive: { candidates, status: 'completed_observations', generatedAt } })
const flush = async () => { for (let index = 0; index < 12; index++) await Promise.resolve() }
function fakeClock() {
  let clock = 10_000, nextId = 0
  const timers = new Map()
  return {
    now: () => clock,
    setTimer(fn, delay) { const id = ++nextId; timers.set(id, { fn, at: clock + delay }); return id },
    clearTimer(id) { timers.delete(id) },
    get pending() { return timers.size },
    async advance(ms) {
      const end = clock + ms
      await flush()
      for (;;) {
        const next = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
        if (!next) break
        const [id, timer] = next
        clock = timer.at; timers.delete(id); timer.fn(); await flush()
      }
      clock = end; await flush()
    },
  }
}

test('legacy candidates survive a newer empty migration; explicit manual empty results replace them', () => {
  const storage = memoryStorage()
  const old = snapshot()
  storage.setItem(LEGACY_SNAPSHOT_KEY, JSON.stringify(old))
  assert.deepEqual(readDisplayedPicks(storage), old)
  const emptyMigration = { ...snapshot([], '2026-09-30T09:20:00+08:00'), displaySnapshotVersion: 1, displaySource: 'migration' }
  assert.deepEqual(selectDisplayedPicks(old, emptyMigration), old)
  const emptyManual = { ...emptyMigration, displaySource: 'manual' }
  const selected = selectDisplayedPicks(old, emptyManual)
  saveDisplayedPicks(storage, selected)
  assert.deepEqual(readDisplayedPicks(storage), emptyManual)
  assert.deepEqual(selectDisplayedPicks(emptyManual, snapshot()), emptyManual)
  assert.ok(storage.getItem(DISPLAY_SNAPSHOT_KEY))
})

test('persisted display excludes active task metadata and ignores malformed snapshots', () => {
  const storage = memoryStorage()
  saveDisplayedPicks(storage, { ...snapshot(), refreshTask: { task_id: 'active' } })
  assert.equal(readDisplayedPicks(storage).refreshTask, undefined)
  assert.deepEqual(selectDisplayedPicks(snapshot(), {}), snapshot())
  saveDisplayedPicks(storage, { adaptive: { status: 'failed', candidates: [] } })
  assert.deepEqual(readDisplayedPicks(storage), snapshot())
})

test('refresh reports actual stages and replaces display only after a complete successful task', async () => {
  const storage = memoryStorage(), clock = fakeClock(), states = [], results = []
  let polls = 0
  const fresh = snapshot([], '2026-09-30T10:00:00+08:00')
  const controller = createPicksRefreshController({ ...clock, storage,
    startTask: async () => ({ task_id: 'new', status: 'pending', progress: 5, message: '等待扫描' }),
    getTask: async () => ++polls === 1 ? { status: 'processing', progress: 35, message: '核验分钟行情' } : { status: 'completed', progress: 100, result: fresh },
    onState: state => states.push(state), onResult: value => results.push(value),
  })
  await controller.start(5); await flush()
  assert.equal(controller.getState().loading, true)
  assert.equal(controller.getState().message, '核验分钟行情')
  assert.equal(results.length, 0)
  assert.equal(JSON.parse(storage.getItem(REFRESH_TASK_KEY)).task_id, 'new')
  await clock.advance(1000)
  assert.equal(results.length, 1)
  assert.deepEqual(results[0].adaptive.candidates, [])
  assert.equal(results[0].displaySource, 'manual')
  assert.equal(controller.getState().loading, false)
  assert.ok(states.some(state => state.elapsedSeconds === 1))
  assert.equal(storage.getItem(REFRESH_TASK_KEY), null)
  assert.equal(clock.pending, 0)
})

test('failed scan leaves saved recommendations unchanged and permits a new refresh', async () => {
  const storage = memoryStorage(), clock = fakeClock()
  saveDisplayedPicks(storage, snapshot())
  let starts = 0, replacements = 0
  const controller = createPicksRefreshController({ ...clock, storage,
    startTask: async () => ({ task_id: `job-${++starts}`, status: 'pending' }),
    getTask: async () => ({ status: 'failed', error: '行情数据不完整' }),
    onResult: () => replacements++,
  })
  await controller.start(5); await flush()
  assert.equal(controller.getState().error, '行情数据不完整')
  assert.equal(controller.getState().loading, false)
  assert.equal(replacements, 0)
  assert.deepEqual(readDisplayedPicks(storage), snapshot())
  await controller.start(5); await flush()
  assert.equal(starts, 2)
  assert.equal(clock.pending, 0)
})

test('leaving a page clears timers and ignores late responses; next page restores the same task', async () => {
  const storage = memoryStorage(), clock = fakeClock()
  let lateReply, staleResults = 0
  const first = createPicksRefreshController({ ...clock, storage,
    startTask: async () => ({ task_id: 'resume-me', status: 'pending' }),
    getTask: () => new Promise(resolve => { lateReply = resolve }),
    onResult: () => staleResults++,
  })
  await first.start(5); await flush()
  first.dispose()
  assert.equal(clock.pending, 0)
  lateReply({ status: 'completed', result: snapshot() }); await flush()
  assert.equal(staleResults, 0)
  const resumed = []
  const second = createPicksRefreshController({ ...clock, storage,
    startTask: async () => assert.fail('must reuse the existing task'),
    getTask: async taskId => { assert.equal(taskId, 'resume-me'); return { status: 'completed', result: snapshot() } },
    onResult: value => resumed.push(value),
  })
  assert.equal(second.resume(), true); await flush()
  assert.equal(resumed.length, 1)
  assert.equal(clock.pending, 0)
})

test('a hung task has a bounded wait and keeps its id for later recovery', async () => {
  const storage = memoryStorage(), clock = fakeClock()
  const controller = createPicksRefreshController({ ...clock, storage, timeoutMs: 3000, requestTimeoutMs: 10_000,
    startTask: async () => ({ task_id: 'slow', status: 'pending' }),
    getTask: () => new Promise(() => {}),
    onResult: () => assert.fail('must not replace saved picks'),
  })
  await controller.start(5); await clock.advance(3000)
  assert.equal(controller.getState().loading, false)
  assert.match(controller.getState().error, /停止等待/)
  assert.equal(JSON.parse(storage.getItem(REFRESH_TASK_KEY)).task_id, 'slow')
  assert.equal(clock.pending, 0)
})

test('expanded scans can exceed six minutes while retaining a thirty-minute wait limit', async () => {
  const storage = memoryStorage(), clock = fakeClock()
  const controller = createPicksRefreshController({ ...clock, storage,
    startTask: async () => ({ task_id: 'expanded', status: 'pending' }),
    getTask: async () => ({ status: 'running', progress: 60, message: '正在核验 306 只股票' }),
  })
  await controller.start(5)
  await clock.advance(7 * 60_000)
  assert.equal(controller.getState().loading, true)
  assert.equal(controller.getState().taskId, 'expanded')
  await clock.advance(23 * 60_000)
  assert.equal(controller.getState().loading, false)
  assert.match(controller.getState().error, /30 分钟/)
  assert.equal(JSON.parse(storage.getItem(REFRESH_TASK_KEY)).task_id, 'expanded')
  assert.equal(clock.pending, 0)
})

test('expired wait can recover a completed result, while nonexistent task ids are removed', async () => {
  const storage = memoryStorage(), clock = fakeClock(), results = []
  storage.setItem(REFRESH_TASK_KEY, JSON.stringify({ task_id: 'finished', startedAt: 0 }))
  const recovered = createPicksRefreshController({ ...clock, storage, timeoutMs: 1000,
    getTask: async () => ({ status: 'completed', result: snapshot() }), onResult: value => results.push(value),
  })
  recovered.resume(); await flush()
  assert.equal(results.length, 1)
  storage.setItem(REFRESH_TASK_KEY, JSON.stringify({ task_id: 'missing', startedAt: 0 }))
  const missing = createPicksRefreshController({ ...clock, storage,
    getTask: async () => { throw new Error('HTTP 404: task not found') },
  })
  missing.resume(); await flush()
  assert.match(missing.getState().error, /已失效/)
  assert.equal(storage.getItem(REFRESH_TASK_KEY), null)
  assert.equal(missing.resume(), false)
  assert.equal(clock.pending, 0)
})
