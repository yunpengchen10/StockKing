export const DISPLAY_SNAPSHOT_KEY = 'stock-king:picks:display:v1'
export const LEGACY_SNAPSHOT_KEY = 'stock-king:picks:snapshot:v2.3'
export const REFRESH_TASK_KEY = 'stock-king:picks:refresh-task:v1'

const failedStatuses = new Set(['unavailable', 'failed', 'expired', 'cancelled', 'retired_slot', 'missed_slot', 'not_due', 'already_claimed', 'not_found'])
export function isSavedPicks(payload) {
  const adaptive = payload?.adaptive
  const status = String(adaptive?.status || '')
  return Array.isArray(adaptive?.candidates) && !failedStatuses.has(status) && !status.startsWith('skipped')
}

function readJSON(storage, key) {
  try { return JSON.parse(storage?.getItem(key) || 'null') } catch (_) { return null }
}
function writeJSON(storage, key, value) {
  try { storage?.setItem(key, JSON.stringify(value)); return true } catch (_) { return false }
}
function remove(storage, key) {
  try { storage?.removeItem(key) } catch (_) { /* Storage may be disabled. */ }
}
function withoutTask(payload) {
  const { refreshTask, displaySyncError, displayRecovery, ...snapshot } = payload
  return snapshot
}
export function readDisplayedPicks(storage) {
  const saved = readJSON(storage, DISPLAY_SNAPSHOT_KEY)
  if (saved?.version === 1 && isSavedPicks(saved.payload)) return saved.payload
  const legacy = readJSON(storage, LEGACY_SNAPSHOT_KEY)
  return isSavedPicks(legacy) ? legacy : null
}
export function saveDisplayedPicks(storage, payload) {
  if (!isSavedPicks(payload)) return
  if (!writeJSON(storage, DISPLAY_SNAPSHOT_KEY, { version: 1, payload: withoutTask(payload) })) {
    // Large evidence snapshots can exceed browser storage quotas. The server
    // keeps the full result; never leave an older cache masquerading as it.
    remove(storage, DISPLAY_SNAPSHOT_KEY)
    remove(storage, LEGACY_SNAPSHOT_KEY)
  }
}
function snapshotTime(payload) {
  return Date.parse(payload?.generatedAt || payload?.adaptive?.generatedAt || payload?.adaptive?.generated_at || '') || 0
}
export function selectDisplayedPicks(local, remote) {
  if (!isSavedPicks(remote)) return local
  const saved = withoutTask(remote)
  if (!isSavedPicks(local)) return saved
  // A completed empty scan is meaningful too. Compare generation times, not
  // whether a snapshot was manual or happened to contain candidates.
  return snapshotTime(remote) >= snapshotTime(local) ? saved : local
}

export function createPicksRefreshController({
  startTask, getTask, getDisplay, getSnapshot, storage, onState = () => {}, onResult = () => {},
  now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout,
  timeoutMs = 30 * 60_000, requestTimeoutMs = 20_000, displayRequestTimeoutMs = 50_000, pollMs = 1000,
}) {
  let disposed = false, generation = 0, pollTimer = null, clockTimer = null, cancelRequest = null
  let startedAt = 0, waitStartedAt = 0, initialRecoveryProbe = false, retryCount = 0, restartMissingWith = null
  let activeStrategy = 'all'
  let activeVersion = ''
  let activeFingerprint = ''
  let state = { loading: false, taskId: '', progress: 0, message: '', elapsedSeconds: 0, error: '', notice: '' }
  const emit = patch => { state = { ...state, ...patch }; if (!disposed) onState({ ...state }) }
  const current = token => !disposed && state.loading && generation === token
  function clearWork() {
    if (pollTimer !== null) clearTimer(pollTimer)
    if (clockTimer !== null) clearTimer(clockTimer)
    pollTimer = clockTimer = null
    cancelRequest?.()
    cancelRequest = null
  }
  function finish(error = '', clearStored = false) {
    generation++
    clearWork()
    if (clearStored) remove(storage, REFRESH_TASK_KEY)
    emit({ loading: false, error })
  }
  const timeoutMessage = `本次等待超过 ${Math.ceil(timeoutMs / 60_000)} 分钟，已停止等待；后台任务不会因此取消。可点击“读取已保存结果”，或点击“刷新扫描”恢复查询同一任务。`
  function tick(token) {
    if (!current(token)) return
    emit({ elapsedSeconds: Math.max(0, Math.floor((now() - startedAt) / 1000)) })
    if (!initialRecoveryProbe && now() - waitStartedAt >= timeoutMs) { finish(timeoutMessage); return }
    clockTimer = setTimer(() => tick(token), 1000)
  }
  function request(operation, deadlineMs = requestTimeoutMs) {
    return new Promise((resolve, reject) => {
      let settled = false
      const settle = (callback, value) => {
        if (settled) return
        settled = true
        clearTimer(timer)
        cancelRequest = null
        callback(value)
      }
      const timer = setTimer(() => settle(reject, new Error('扫描状态连接超时')), deadlineMs)
      cancelRequest = () => settle(reject, new Error('页面已停止等待'))
      Promise.resolve().then(operation).then(value => settle(resolve, value), error => settle(reject, error))
    })
  }
  async function handleTask(task, token) {
    if (task?.strategyId && task.strategyId !== activeStrategy) throw new Error('扫描任务的策略与本次请求不一致，请重新读取已保存结果。')
    if (activeVersion && task?.strategyVersion && task.strategyVersion !== activeVersion) throw new Error('扫描任务的策略版本已变化，请重新刷新。')
    if (activeFingerprint && task?.strategyFingerprint && task.strategyFingerprint !== activeFingerprint) throw new Error('扫描任务的规则配置已变化，请重新刷新。')
    const status = String(task?.status || '')
    emit({ progress: Math.min(100, Math.max(0, Number(task?.progress) || 0)), message: task?.message || '正在扫描', strategyId: activeStrategy, strategyName: task?.strategyName || '', strategyVersion: activeVersion })
    if (['completed', 'succeeded', 'done'].includes(status)) {
      const superseded = task.publicationStatus === 'superseded'
      let saved = task.result
      const legacyResult = isSavedPicks(saved)
      if (!legacyResult && task.resultPersisted && typeof getDisplay === 'function') {
        emit({ message: '扫描已完成并保存，正在读取结果' })
        saved = await request(getDisplay, displayRequestTimeoutMs)
        if (!current(token)) return true
        const expectedId = superseded ? task.displayRunId : task.publishedRunId || task.scanRunId || task.displayRunId
        const receivedId = saved?.displayRunId || saved?.adaptive?.run_id || saved?.adaptive?.runId
        const expectedTime = Date.parse(task.displayGeneratedAt || '') || 0
        if (expectedId && receivedId !== expectedId && typeof getSnapshot === 'function') {
          const frozen = await request(() => getSnapshot(expectedId), displayRequestTimeoutMs)
          if (!current(token)) return true
          saved = frozen?.adaptive ? frozen : { displayRunId: frozen?.run_id, generatedAt: frozen?.result?.generatedAt || frozen?.as_of, adaptive: frozen?.result }
          if ((saved?.displayRunId || saved?.adaptive?.run_id) !== expectedId) throw new Error('未取得本次扫描对应的已保存结果。')
        }
        // A later scheduled scan may already have replaced this manual scan.
        // Otherwise, never label an older saved view as this task's result.
        if (expectedId && (saved?.displayRunId || saved?.adaptive?.run_id || saved?.adaptive?.runId) !== expectedId && !(expectedTime && snapshotTime(saved) > expectedTime)) {
          throw new Error('扫描结果已保存，但展示版本尚未同步。请点击“读取已保存结果”重试。')
        }
      }
      if (!isSavedPicks(saved)) throw new Error('扫描返回的推荐快照不完整，上次推荐仍保留，请点击“读取已保存结果”重试。')
      const savedStrategy = saved?.adaptive?.publishedPicks?.strategyId || saved?.strategyId || saved?.adaptive?.strategyId
      if (!superseded && savedStrategy && savedStrategy !== activeStrategy) throw new Error('保存结果的策略与本次扫描不一致，已保留原结果。')
      const savedVersion = saved?.adaptive?.publishedPicks?.strategyVersion
      if (!superseded && savedVersion && activeVersion && savedVersion !== activeVersion) throw new Error('保存结果的策略版本与本次扫描不一致，已保留原结果。')
      if (!current(token)) return true
      if (superseded) emit({notice:'本次扫描完成时已有更新一轮推荐，本次结果未发布；当前显示较新的已发布推荐。'})
      onResult({ ...saved, displaySnapshotVersion: 1, ...(legacyResult ? { displaySource: 'manual' } : {}) })
      finish('', true)
      return true
    }
    if (failedStatuses.has(status) || status.startsWith('skipped')) {
      finish(task?.error || task?.message || '本次扫描未完成，上次推荐仍保留，请重试。', true)
      return true
    }
    return false
  }
  async function poll(token) {
    if (!current(token)) return
    try {
      const task = await request(() => getTask(state.taskId))
      if (!current(token)) return
      if (await handleTask(task, token)) return
      initialRecoveryProbe = false
      retryCount = 0
      if (now() - waitStartedAt >= timeoutMs) { finish(timeoutMessage); return }
      pollTimer = setTimer(() => poll(token), pollMs)
    } catch (error) {
      if (current(token)) {
        const detail = error?.message || String(error)
        const missing = /\b404\b|not[ _-]?found|不存在|已过期/i.test(detail)
        initialRecoveryProbe = false
        // An explicit refresh must still work after the engine was restarted.
        // A confirmed missing task is safe to replace; a network failure is not.
        if (missing && restartMissingWith !== null) {
          const { maxPerBoard, strategyId, strategyVersion, strategyFingerprint } = restartMissingWith
          restartMissingWith = null
          finish('', true)
          await start(maxPerBoard, strategyId, strategyVersion, strategyFingerprint)
          return
        }
        const transient = /timeout|timed out|deadline exceeded|超时|network|failed to fetch|connection (?:reset|refused)|ECONN|\b50[234]\b/i.test(detail)
        if (!missing && transient && now() - waitStartedAt < timeoutMs) {
          emit({ message: '状态连接暂时中断，后台任务继续运行，正在恢复查询；也可点击“读取已保存结果”。', error: '' })
          pollTimer = setTimer(() => poll(token), Math.min(5000, pollMs * 2 ** Math.min(++retryCount, 3)))
          return
        }
        finish(missing ? '上次扫描任务已结束或软件重启后已失效，已保存推荐仍保留。请点击“刷新扫描”重新开始。'
          : now() - waitStartedAt >= timeoutMs ? timeoutMessage : `${detail} 已保存推荐仍保留，可点击“读取已保存结果”。`, missing)
      }
    }
  }
  function begin(taskId = '', since = now(), recovering = false, freshWait = false) {
    startedAt = since
    waitStartedAt = freshWait ? now() : since
    initialRecoveryProbe = recovering
    retryCount = 0
    restartMissingWith = null
    const token = ++generation
    emit({ loading: true, taskId, strategyId: activeStrategy, strategyName: '', strategyVersion: activeVersion, error: '', notice: '', progress: 0, message: recovering ? '正在恢复上次扫描进度' : '正在提交本地扫描', elapsedSeconds: Math.max(0, Math.floor((now() - since) / 1000)) })
    tick(token)
    return token
  }
  async function start(maxPerBoard, strategyId = 'all', strategyVersion = '', strategyFingerprint = '') {
    if (disposed || state.loading) return false
    activeStrategy = strategyId || 'all'
    activeVersion = strategyVersion || ''
    activeFingerprint = strategyFingerprint || ''
    // A timed-out status read does not cancel its server task. Reuse its id
    // before considering another POST, including after reopening this page.
    const cached = readJSON(storage, REFRESH_TASK_KEY)
    if ((cached?.strategyId || 'all') === activeStrategy && (!activeVersion || cached?.strategyVersion === activeVersion)
      && (!activeFingerprint || cached?.strategyFingerprint === activeFingerprint)
      && resume(null, { force: true, restartIfMissing: { maxPerBoard, strategyId: activeStrategy, strategyVersion: activeVersion, strategyFingerprint: activeFingerprint } })) return true
    const token = begin()
    try {
      const task = await request(() => startTask(maxPerBoard, activeStrategy))
      if (!current(token)) return false
      if (!task?.task_id) throw new Error(task?.error || task?.message || '无法启动扫描任务，请重试。')
      if (task.strategyId && task.strategyId !== activeStrategy) throw new Error('扫描任务的策略与本次请求不一致，请重新读取已保存结果。')
      if (activeVersion && task.strategyVersion && task.strategyVersion !== activeVersion) throw new Error('扫描任务的策略版本已变化，请重新刷新。')
      if (!activeVersion) activeVersion = task.strategyVersion || ''
      activeFingerprint = task.strategyFingerprint || activeFingerprint
      emit({ taskId: task.task_id })
      writeJSON(storage, REFRESH_TASK_KEY, { task_id: task.task_id, startedAt, strategyId: activeStrategy, strategyVersion: activeVersion, strategyFingerprint: activeFingerprint })
      if (!await handleTask(task, token)) void poll(token)
      return true
    } catch (error) {
      if (current(token)) finish(error?.message || String(error))
      return false
    }
  }
  function resume(serverTask = null, { force = false, restartIfMissing = null } = {}) {
    if (disposed || state.loading) return false
    const cached = readJSON(storage, REFRESH_TASK_KEY)
    const task = serverTask?.task_id ? serverTask : cached
    if (!task?.task_id) return false
    activeStrategy = task.strategyId || 'all'
    activeVersion = task.strategyVersion || ''
    activeFingerprint = task.strategyFingerprint || ''
    // Periodic display reads must not restart an exhausted/error wait forever.
    // An explicit refresh click may make one new recovery probe.
    if (!force && state.taskId === task.task_id && state.error) return false
    const rawStartedAt = task.startedAt ?? task.started_at ?? task.created_at
    const parsed = typeof rawStartedAt === 'number' ? rawStartedAt : Date.parse(rawStartedAt || '')
    const cachedStart = cached?.task_id === task.task_id ? cached.startedAt : null
    const since = Number.isFinite(parsed) ? parsed : Number.isFinite(cachedStart) ? cachedStart : now()
    const token = begin(task.task_id, Math.min(now(), since), true, force)
    restartMissingWith = restartIfMissing
    writeJSON(storage, REFRESH_TASK_KEY, { task_id: task.task_id, startedAt, strategyId: activeStrategy, strategyVersion: activeVersion, strategyFingerprint: activeFingerprint })
    void poll(token)
    return true
  }
  function dispose() { disposed = true; generation++; clearWork() }
  return { start, resume, dispose, getState: () => ({ ...state }) }
}
