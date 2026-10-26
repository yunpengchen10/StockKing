const finite = value => value !== null && value !== undefined && value !== '' && typeof value !== 'boolean' && Number.isFinite(Number(value))
const percent = value => finite(value) ? `${(Number(value) * 100).toFixed(2)}%` : '未取得'

export function learningTaskLabel(taskId, queue) {
  if (taskId === 'morning_signal_to_same_session_close') return `上午方向观察 → 当日收盘${queue ? ` · ${queue}` : ''}`
  if (taskId === 'entry_today_exit_next_session') return '当日入场 → 下一交易日退出'
  if (taskId === 'next_session_price_event') return `次日行情事件 · ${queue === 'continuation' ? '延续' : queue === 'first_board' ? '潜伏' : '独立观察'}`
  if (taskId === 'entry_next_session_exit_following_session') return '次日入场 → 再下一交易日退出'
  return '历史任务口径'
}

export function learningTimeline(state = {}) {
  return [
    { key: 'check', label: '上次训练检查', at: state.lastLearningCheckAt || state.lastTrainingAt || null },
    // A legacy training-check timestamp must never imply a fitted model exists.
    { key: 'fit', label: '上次完成拟合', at: state.lastFitCompletedAt || null },
    { key: 'validation', label: '上次验证', at: state.lastValidationAt || null },
    { key: 'activation', label: '模型启用', at: state.activatedAt || null },
  ]
}

export function predictionSummary(prediction) {
  if (!prediction) return null
  if (prediction.status === 'rules_unvalidated') return { label: '规则研究，尚无已启用模型', detail: '暂不提供预测收益或大涨概率；复盘结果须经过训练与验证。' }
  if (prediction.status === 'shadow') return { label: '模型仍在前向验证', detail: '当前预测尚未用于正式排序。' }
  if (!['active', 'model_active', 'validated_model'].includes(prediction.status)) return { label: '预测不可用', detail: '数据或模型未通过本次检查，不使用预测值恢复准入。' }
  return {
    label: prediction.entryEligible === true ? '模型通过优势门槛' : '模型未发现足够优势',
    detail: `T+1预期净收益 ${percent(prediction.expectedNetReturn)} · 扣费盈利概率 ${percent(prediction.winProbability)} · 净收益达到5%概率 ${percent(prediction.bigMoveProbability)}${finite(prediction.bigMoveProbability) ? '' : '（辅助事件样本不足或尚未通过独立校准）'} · 收益下侧10%分位 ${percent(prediction.lowerReturnQuantile)}。均为可执行模拟口径，无法成交单独记录；概率不是保证，分位数不是预期收益置信下界。`,
  }
}

export function portfolioSummary(policy) {
  if (!policy) return ''
  return `本轮研究配置 ${percent(policy.plannedExposure)} · 保留现金 ${percent(policy.cashWeight)} · 总配置上限 ${percent(policy.totalExposureCap)}。不代表账户已下单。`
}

export function nextDayPredictionSummary(signal) {
  if (!signal?.learningStatus) return null
  if (signal.learningStatus === 'rules_unvalidated') return '规则观察，尚无已启用的次日事件模型。'
  if (signal.learningStatus !== 'active') return '次日模型或数据未通过检查，本轮不使用预测值。'
  return `次日收盘10%价格模式概率 ${percent(signal.probability)} · 预期价格变化 ${percent(signal.expectedPriceReturn)} · 价格收益下侧10%分位 ${percent(signal.returnQ10)}。价格模式不等于交易所核实涨停，也不代表可成交收益。`
}
