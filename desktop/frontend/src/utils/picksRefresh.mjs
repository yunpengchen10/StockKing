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
  try { storage?.setItem(key, JSON.stringify(value)) } catch (_) { /* The server also persists the display. */ }
}
function remove(storage, key) {
  try { storage?.removeItem(key) } catch (_) { /* Storage may be disabled. */ }
}
function withoutTask(payload) {
  const { refreshTask, ...snapshot } = payload
  return snapshot
}
export function readDisplayedPicks(storage) {
  const saved = readJSON(storage, DISPLAY_SNAPSHOT_KEY)
  if (saved?.version === 1 && isSavedPicks(saved.payload)) return saved.payload
  const legacy = readJSON(storage, LEGACY_SNAPSHOT_KEY)
  return isSavedPicks(legacy) ? legacy : null
}
export function saveDisplayedPicks(storage, payload) {
  if (isSavedPicks(payload)) writeJSON(storage, DISPLAY_SNAPSHOT_KEY, { version: 1, payload: withoutTask(payload) })
}
function snapshotTime(payload) {
  return Date.parse(payload?.generatedAt || payload?.adaptive?.generatedAt || payload?.adaptive?.generated_at || '') || 0
}
export function selectDisplayedPicks(local, remote) {
  if (!isSavedPicks(remote)) return local
  const saved = withoutTask(remote)
  if (!isSavedPicks(local)) return saved
  // An explicitly refreshed empty result is meaningful. A legacy empty scan is not.
  if (remote.displaySnapshotVersion && remote.displaySource === 'manual') return saved
  if (local.displaySnapshotVersion && local.displaySource === 'manual') return local
  const localCount = local.adaptive.candidates.length
  const remoteCount = remote.adaptive.candidates.length
  if (localCount && !remoteCount) return local
  if (remoteCount && !localCount) return saved
  return snapshotTime(remote) >= snapshotTime(local) ? saved : local
}

export function createPicksRefreshController({
  startTask, getTask, storage, onState = () => {}, onResult = () => {},
  now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout,
  timeoutMs = 30 * 60_000, requestTimeoutMs = 20_000, pollMs = 1000,
}) {
  let disposed = false, generation = 0, pollTimer = null, clockTimer = null, cancelRequest = null
  let startedAt = 0, initialRecoveryProbe = false
  let state = { loading: false, taskId: '', progress: 0, message: '', elapsedSeconds: 0, error: '' }
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
  const timeoutMessage = `本次扫描超过 ${Math.ceil(timeoutMs / 60_000)} 分钟，已停止等待。上次推荐仍保留，可点击“刷新扫描”恢复查看。`
  function tick(token) {
    if (!current(token)) return
    emit({ elapsedSeconds: Math.max(0, Math.floor((now() - startedAt) / 1000)) })
    if (!initialRecoveryProbe && now() - startedAt >= timeoutMs) { finish(timeoutMessage); return }
    clockTimer = setTimer(() => tick(token), 1000)
  }
  function request(operation) {
    return new Promise((resolve, reject) => {
      let settled = false
      const settle = (callback, value) => {
        if (settled) return
        settled = true
        clearTimer(timer)
        cancelRequest = null
        callback(value)
      }
      const timer = setTimer(() => settle(reject, new Error('扫描状态连接超时，上次推荐仍保留，可点击“刷新扫描”重试。')), requestTimeoutMs)
      cancelRequest = () => settle(reject, new Error('页面已停止等待'))
      Promise.resolve().then(operation).then(value => settle(resolve, value), error => settle(reject, error))
    })
  }
  function handleTask(task) {
    const status = String(task?.status || '')
    emit({ progress: Math.min(100, Math.max(0, Number(task?.progress) || 0)), message: task?.message || '正在扫描' })
    if (['completed', 'succeeded', 'done'].includes(status)) {
      if (!isSavedPicks(task.result)) throw new Error('扫描返回的推荐快照不完整，上次推荐仍保留，请重试。')
      onResult({ ...task.result, displaySnapshotVersion: 1, displaySource: 'manual' })
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
      initialRecoveryProbe = false
      if (handleTask(task)) return
      if (now() - startedAt >= timeoutMs) { finish(timeoutMessage); return }
      pollTimer = setTimer(() => poll(token), pollMs)
    } catch (error) {
      if (current(token)) {
        const detail = error?.message || String(error)
        const missing = /\b404\b|not[ _-]?found|不存在|已过期/i.test(detail)
        finish(missing ? '上次扫描任务已结束或软件重启后已失效，已保存推荐仍保留。请点击“刷新扫描”重新开始。' : detail, missing)
      }
    }
  }
  function begin(taskId = '', since = now(), recovering = false) {
    startedAt = since
    initialRecoveryProbe = recovering
    const token = ++generation
    emit({ loading: true, taskId, error: '', progress: 0, message: recovering ? '正在恢复上次扫描进度' : '正在提交本地扫描', elapsedSeconds: Math.max(0, Math.floor((now() - since) / 1000)) })
    tick(token)
    return token
  }
  async function start(maxPerBoard) {
    if (disposed || state.loading) return false
    const token = begin()
    try {
      const task = await request(() => startTask(maxPerBoard))
      if (!current(token)) return false
      if (!task?.task_id) throw new Error(task?.error || task?.message || '无法启动扫描任务，请重试。')
      emit({ taskId: task.task_id })
      writeJSON(storage, REFRESH_TASK_KEY, { task_id: task.task_id, startedAt })
      if (!handleTask(task)) void poll(token)
      return true
    } catch (error) {
      if (current(token)) finish(error?.message || String(error))
      return false
    }
  }
  function resume(serverTask = null) {
    if (disposed || state.loading) return false
    const cached = readJSON(storage, REFRESH_TASK_KEY)
    const task = serverTask?.task_id ? serverTask : cached
    if (!task?.task_id) return false
    const rawStartedAt = task.startedAt ?? task.started_at ?? task.created_at
    const parsed = typeof rawStartedAt === 'number' ? rawStartedAt : Date.parse(rawStartedAt || '')
    const cachedStart = cached?.task_id === task.task_id ? cached.startedAt : null
    const since = Number.isFinite(parsed) ? parsed : Number.isFinite(cachedStart) ? cachedStart : now()
    const token = begin(task.task_id, Math.min(now(), since), true)
    writeJSON(storage, REFRESH_TASK_KEY, { task_id: task.task_id, startedAt })
    void poll(token)
    return true
  }
  function dispose() { disposed = true; generation++; clearWork() }
  return { start, resume, dispose, getState: () => ({ ...state }) }
}
