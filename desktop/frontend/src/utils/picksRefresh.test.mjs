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

test('latest completed scan replaces old candidates even when empty', () => {
  const storage = memoryStorage()
  const old = snapshot()
  storage.setItem(LEGACY_SNAPSHOT_KEY, JSON.stringify(old))
  assert.deepEqual(readDisplayedPicks(storage), old)
  const emptyMigration = { ...snapshot([], '2026-09-30T09:20:00+08:00'), displaySnapshotVersion: 1, displaySource: 'migration' }
  assert.deepEqual(selectDisplayedPicks(old, emptyMigration), emptyMigration)
  const emptyManual = { ...emptyMigration, displaySource: 'manual' }
  const selected = selectDisplayedPicks(old, emptyManual)
  saveDisplayedPicks(storage, selected)
  assert.deepEqual(readDisplayedPicks(storage), emptyManual)
  assert.deepEqual(selectDisplayedPicks(emptyManual, snapshot()), emptyManual)
  assert.ok(storage.getItem(DISPLAY_SNAPSHOT_KEY))
})

test('manual caches cannot pin the display over newer scheduled scans or roll it back', () => {
  const oldManual = { ...snapshot(), displaySnapshotVersion: 1, displaySource: 'manual' }
  const latest = { ...snapshot([{ code: '600002' }], '2026-10-13T10:30:00+08:00'), displaySnapshotVersion: 1, displaySource: 'scheduled' }
  assert.deepEqual(selectDisplayedPicks(oldManual, latest), latest)
  assert.deepEqual(selectDisplayedPicks(latest, oldManual), latest)
  const nextDayOnly = { ...snapshot([], '2026-10-13T14:55:00+08:00'), displaySource: 'scheduled' }
  nextDayOnly.adaptive.nextDayWatchlist = [{ code: '600003' }]
  assert.deepEqual(selectDisplayedPicks(latest, nextDayOnly), nextDayOnly)
})

test('persisted display excludes active task metadata and ignores malformed snapshots', () => {
  const storage = memoryStorage()
  saveDisplayedPicks(storage, { ...snapshot(), refreshTask: { task_id: 'active' }, displaySyncError: 'temporary history failure', displayRecovery: { status: 'recovering_history' } })
  assert.equal(readDisplayedPicks(storage).refreshTask, undefined)
  assert.equal(readDisplayedPicks(storage).displaySyncError, undefined)
  assert.equal(readDisplayedPicks(storage).displayRecovery, undefined)
  assert.deepEqual(selectDisplayedPicks(snapshot(), {}), snapshot())
  saveDisplayedPicks(storage, { adaptive: { status: 'failed', candidates: [] } })
  assert.deepEqual(readDisplayedPicks(storage), snapshot())
})

test('a full browser cache cannot keep resurrecting a superseded recommendation', () => {
  const storage = memoryStorage()
  saveDisplayedPicks(storage, snapshot())
  storage.setItem(LEGACY_SNAPSHOT_KEY, JSON.stringify(snapshot()))
  storage.setItem = () => { throw new Error('QuotaExceededError') }
  saveDisplayedPicks(storage, snapshot([], '2026-10-13T14:55:00+08:00'))
  assert.equal(readDisplayedPicks(storage), null)
  assert.equal(storage.getItem(DISPLAY_SNAPSHOT_KEY), null)
  assert.equal(storage.getItem(LEGACY_SNAPSHOT_KEY), null)
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

test('completed receipt fetches the persisted display once and preserves its source', async () => {
  const storage = memoryStorage(), clock = fakeClock(), results = []
  const fresh = { ...snapshot([], '2026-11-03T10:30:00+08:00'), displayRunId: 'saved-new', displaySource: 'history' }
  let reads = 0
  const controller = createPicksRefreshController({ ...clock, storage,
    startTask: async () => ({ task_id: 'receipt', status: 'pending' }),
    getTask: async () => ({ status: 'completed', progress: 100, result: null, resultPersisted: true,
      displayRunId: fresh.displayRunId, displayGeneratedAt: fresh.generatedAt }),
    getDisplay: async () => { reads++; return fresh }, onResult: value => results.push(value),
  })
  await controller.start(5); await flush()
  assert.equal(reads, 1)
  assert.deepEqual(results[0].adaptive.candidates, [])
  assert.equal(results[0].displaySource, 'history')
  assert.equal(controller.getState().loading, false)
  assert.equal(storage.getItem(REFRESH_TASK_KEY), null)
  assert.equal(clock.pending, 0)
})

test('receipt never labels an older display as complete, but accepts a later scheduled scan', async () => {
  for (const newer of [false, true]) {
    const storage = memoryStorage(), clock = fakeClock(), results = []
    const shown = { ...snapshot([], newer ? '2026-11-03T14:55:00+08:00' : '2026-11-02T14:55:00+08:00'), displayRunId: 'another-run' }
    const controller = createPicksRefreshController({ ...clock, storage,
      startTask: async () => ({ task_id: 'receipt', status: 'pending' }),
      getTask: async () => ({ status: 'completed', resultPersisted: true, displayRunId: 'expected', displayGeneratedAt: '2026-11-03T10:30:00+08:00' }),
      getDisplay: async () => shown, onResult: value => results.push(value),
    })
    await controller.start(5); await flush()
    assert.equal(results.length, newer ? 1 : 0)
    assert.equal(controller.getState().loading, false)
    if (!newer) {
      assert.match(controller.getState().error, /展示版本尚未同步/)
      assert.equal(JSON.parse(storage.getItem(REFRESH_TASK_KEY)).task_id, 'receipt')
    }
    controller.dispose()
  }
})

test('Go deadline failure resumes the same task without submitting a second scan', async () => {
  const storage = memoryStorage(), clock = fakeClock(), results = []
  let starts = 0, polls = 0
  const controller = createPicksRefreshController({ ...clock, storage,
    startTask: async () => ({ task_id: `scan-${++starts}`, status: 'pending' }),
    getTask: async id => {
      assert.equal(id, 'scan-1')
      if (++polls === 1) throw new Error('Get localhost:57653/api/v1/stock-king/picks/refresh/tasks/scan-1: context deadline exceeded')
      return { status: 'completed', result: snapshot() }
    }, onResult: value => results.push(value),
  })
  await controller.start(5); await flush()
  assert.equal(controller.getState().loading, true)
  assert.match(controller.getState().message, /后台任务继续运行/)
  assert.equal(await controller.start(5), false)
  await clock.advance(2000)
  assert.equal(starts, 1)
  assert.equal(results.length, 1)
  assert.equal(controller.getState().error, '')
  assert.equal(clock.pending, 0)
})

test('hung status request is retried, ignores its late reply, and keeps one server task', async () => {
  const storage = memoryStorage(), clock = fakeClock(), results = []
  let late, polls = 0
  const controller = createPicksRefreshController({ ...clock, storage, requestTimeoutMs: 100, pollMs: 100,
    startTask: async () => ({ task_id: 'same', status: 'pending' }),
    getTask: () => ++polls === 1 ? new Promise(resolve => { late = resolve }) : Promise.resolve({ status: 'completed', result: snapshot() }),
    onResult: value => results.push(value),
  })
  await controller.start(5); await clock.advance(100)
  assert.equal(controller.getState().loading, true)
  await clock.advance(200)
  assert.equal(results.length, 1)
  late({ status: 'completed', result: snapshot([{ code: 'wrong-late-result' }]) }); await flush()
  assert.equal(results.length, 1)
  assert.equal(clock.pending, 0)
})

test('repeated connection failures stop at the total limit and only explicit refresh probes again', async () => {
  const storage = memoryStorage(), clock = fakeClock(), results = []
  let starts = 0, complete = false
  const controller = createPicksRefreshController({ ...clock, storage, timeoutMs: 3000, pollMs: 100,
    startTask: async () => { starts++; return { task_id: 'bounded', status: 'pending' } },
    getTask: async () => { if (!complete) throw new Error('context deadline exceeded'); return { status: 'completed', result: snapshot() } },
    onResult: value => results.push(value),
  })
  await controller.start(5); await clock.advance(3000)
  assert.equal(controller.getState().loading, false)
  assert.match(controller.getState().error, /已停止等待.*后台任务不会因此取消/)
  assert.equal(JSON.parse(storage.getItem(REFRESH_TASK_KEY)).task_id, 'bounded')
  assert.equal(controller.resume({ task_id: 'bounded', status: 'processing' }), false)
  assert.equal(clock.pending, 0)
  complete = true
  await controller.start(5); await flush()
  assert.equal(starts, 1)
  assert.equal(results.length, 1)
  assert.equal(clock.pending, 0)
})

test('completed display read timeout retries the receipt while preserving the previous view', async () => {
  const storage = memoryStorage(), clock = fakeClock(), results = []
  let reads = 0, starts = 0
  const fresh = { ...snapshot(), displayRunId: 'persisted' }
  const controller = createPicksRefreshController({ ...clock, storage,
    startTask: async () => { starts++; return { task_id: 'persisted-job', status: 'pending' } },
    getTask: async () => ({ status: 'completed', resultPersisted: true, displayRunId: 'persisted' }),
    getDisplay: async () => { if (++reads === 1) throw new Error('context deadline exceeded'); return fresh },
    onResult: value => results.push(value),
  })
  await controller.start(5); await flush()
  assert.equal(results.length, 0)
  assert.equal(controller.getState().loading, true)
  await clock.advance(2000)
  assert.equal(starts, 1)
  assert.equal(reads, 2)
  assert.equal(results.length, 1)
  assert.equal(storage.getItem(REFRESH_TASK_KEY), null)
})

test('an expired task recovery allows its completed receipt display read to finish', async () => {
  const storage = memoryStorage(), clock = fakeClock(), results = []
  storage.setItem(REFRESH_TASK_KEY, JSON.stringify({ task_id: 'old-completed', startedAt: 0 }))
  let completeRead
  const controller = createPicksRefreshController({ ...clock, storage, timeoutMs: 1000,
    getTask: async () => ({ status: 'completed', resultPersisted: true, displayRunId: 'saved' }),
    getDisplay: () => new Promise(resolve => { completeRead = resolve }), onResult: value => results.push(value),
  })
  assert.equal(controller.resume(), true)
  await clock.advance(2000)
  assert.equal(controller.getState().loading, true)
  completeRead({ ...snapshot(), displayRunId: 'saved' }); await flush()
  assert.equal(results.length, 1)
  assert.equal(controller.getState().loading, false)
  assert.equal(clock.pending, 0)
})
