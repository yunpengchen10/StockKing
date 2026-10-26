const finite = value => value !== null && value !== undefined && value !== '' && typeof value !== 'boolean' && Number.isFinite(Number(value))
const price = value => finite(value) && Number(value) > 0 ? Number(value).toFixed(2) : '未取得'
const time = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未记录'

export function entryExecutionSummary(plan) {
  if (!plan) return null
  const states = { ready: '价格在允许区间', above_entry_band: '超过追价上限', below_entry_band: '等待价格回到区间',
    entry_plan_invalidated: '原计划已失效', entry_plan_expired: '原计划已到期',
    trading_calendar_unavailable: '交易日历待核验', outside_entry_session: '不在入场时段',
    entry_quote_unverified: '报价待核验', entry_evidence_not_qualified: '入场证据未通过',
    entry_atr_or_support_missing: '风险距离或支撑缺失', no_complete_entry_minute: '有效期内无完整入场分钟',
    entry_state_unavailable: '冻结计划暂不可用' }
  const label = states[plan.state] || '入场计划待核验'
  const model = plan.modelEligible === true ? 'T+1收益模型通过' : 'T+1收益模型尚未通过'
  return { label: `保存时：${label} · ${model}`,
    detail: plan.triggerAt ? `首次触发 ¥${price(plan.triggerPrice)}（${time(plan.triggerAt)}）；允许区间 ¥${price(plan.minimumPrice)}–${price(plan.maximumPrice)}；失效参考 ¥${price(plan.invalidationPrice)}；有效至 ${time(plan.expiresAt)}；退出目标 ${plan.targetSession || '日历待核验'} 收盘。` : '尚无可核验的数值入场计划。',
    meaning: '重复扫描不重置首次触发价。以上为研究计划，非成交回执；失效价不保证能够卖出。' }
}

export function dailyObservationCategory(value) {
  return ({ observation_watchlist: '日线观察', attack_watchlist: '进攻观察', unselected_control: '未入选对照',
    selected: '历史入选', profile_selected: '分档观察' })[value] || '其他观察'
}

export function dailyObservationRows(review, includeControls = false) {
  return (review?.items || []).filter(row => includeControls || row.first_category !== 'unselected_control')
    .flatMap(row => (row.outcomes?.length ? row.outcomes : [{ status: 'not_reviewed', horizon: null }])
      .map(outcome => ({ ...outcome, code: row.code, name: row.name, tradingDay: row.trading_day,
        observationId: row.observation_id, category: dailyObservationCategory(row.first_category),
        taskVersion: row.task_version, referencePrice: row.reference_price, referenceVerified: row.reference_verified,
        referenceGaps: row.reference_gaps || [] })))
}

export function dailyObservationStatus(value) {
  return ({ observed: '价格观察完成', not_matured: '保存时尚未到期', unverified_reference: '首次参考价不可核验',
    missing_calendar: '交易日历待补', calendar_unavailable: '交易日历待补', missing_close: '收盘证据待补',
    pending_data: '行情证据待补', unverified_close: '收盘未核验', not_reviewed: '待执行复盘' })[value] || '证据待补'
}

export function settlementHealthSummary(health) {
  if (!health) return '尚无结算状态'
  return `应结算 ${health.dueCount ?? '—'} · 逾期待处理 ${health.overdueCount ?? '—'} · 行情待补 ${health.missingDataCount ?? '—'} · 最近成功批次 ${time(health.lastSuccessAt)}`
}
