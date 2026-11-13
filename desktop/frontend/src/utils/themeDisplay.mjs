// Display frozen evidence only. These helpers never select a theme or create a
// result from today's prices while viewing a historical recommendation.
const list = value => Array.isArray(value) ? value.filter(row => row && typeof row === 'object') : []
const numeric = value => typeof value !== 'boolean' && value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null
const text = value => typeof value === 'string' ? value : ''
const bounded = (value, low, high) => { const n = numeric(value); return n !== null && n >= low && n <= high ? n : null }
const messages = value => (Array.isArray(value) ? value : [value]).map(item => text(item) || text(item?.message) || text(item?.reason)).filter(Boolean)

export function themePercent(value, digits = 2, signed = false) {
  const n = numeric(value)
  return n === null ? '未取得' : `${signed && n > 0 ? '+' : ''}${n.toFixed(digits)}%`
}

export function themeIdentity(value) {
  if (!value || typeof value !== 'object' || !text(value.id) || !text(value.name)) return null
  return {
    id: value.id, name: value.name, rank: bounded(value.rank, 1, 100000),
    leaderRank: bounded(value.leaderRank, 1, 100000), heatScore: bounded(value.heatScore, 0, 1),
    stage: ['启动', '持续', '分歧', '退潮', '观察'].includes(value.stage) ? value.stage : '阶段待确认',
    selected: value.selected === true, relativeStrengthPct: numeric(value.relativeStrengthPct),
  }
}

export function themeBoardDisplay(board, now, historical = false) {
  const supplied = board && typeof board === 'object'
  const sourceTime = text(board?.sourceTime) || null
  const source = text(board?.source) || null
  const timestamp = Date.parse(sourceTime || '')
  const age = Number(now) - timestamp
  const fresh = !historical && board?.status === 'observed' && Boolean(source)
    && Number.isFinite(age) && age >= 0 && age <= 300000
  const seen = new Set()
  const themes = list(board?.themes).flatMap(row => {
    const identity = themeIdentity(row)
    if (!identity || seen.has(identity.id)) return []
    seen.add(identity.id)
    const coverage = row.coverage || {}
    return [{ ...identity, sourceTime: text(row.sourceTime) || null,
      changePct: numeric(row.changePct), breadthPct: bounded(row.breadthPct, 0, 100),
      amountSharePct: bounded(row.amountSharePct, 0, 100),
      feedbackPenalty: bounded(row.feedbackPenalty, 0, .1),
      explanation: messages(row.explanation).join('；'),
      coverage: { complete: coverage.complete === true, expectedCount: bounded(coverage.expectedCount, 0, 100000),
        validCount: bounded(coverage.validCount, 0, 100000), pct: bounded(coverage.pct, 0, 100) },
    }]
  })
  const selected = themes.filter(row => row.selected)
  return {
    supplied: Boolean(supplied), fresh, source, sourceTime, themes,
    selected, cards: selected.length ? selected : themes.slice(0, 3), rankedPreview: themes.slice(0, 20), gaps: messages(board?.gaps),
    title: historical ? '历史主线 · 当时判断' : '今日主线',
    stateLabel: historical ? '历史快照 · 未做当前核验' : !supplied ? '本轮未记录题材榜'
      : board.status !== 'observed' ? '题材证据待补齐' : fresh ? '题材源时间在5分钟内' : '已保存题材快照 · 需刷新核验',
    emptyMessage: !supplied ? '该轮尚未记录热门题材及成分证据，无法判断当时市场主线。'
      : themes.length ? '本轮没有完成主线入选确认，请查看成分覆盖与数据缺口。' : '本轮未取得可核验题材榜，未用行业分类代替热点题材。',
    scope: '相对强度为较概念涨幅中位数的差；成交占比以全部概念成交额汇总为分母，成分可能重叠。',
  }
}

export function themeFeedbackDisplay(feedback, now, historical = false) {
  const supplied = feedback && typeof feedback === 'object'
  const asOf = text(feedback?.asOf) || null
  const timestamp = Date.parse(asOf || '')
  const future = Number.isFinite(timestamp) && timestamp > Number(now)
  const unavailable = !supplied || future || ['unavailable', 'failed', 'empty', 'pending', 'pending_data', 'no_history', 'no_scans', 'expired'].includes(feedback?.status)
  const sourceDay = text(feedback?.sourceDay) || null
  const adjustments = Object.entries(feedback?.adjustments || {}).flatMap(([id, value]) => {
    const penalty = bounded(value, 0, .1)
    return penalty !== null && penalty > 0 ? [{ id, penalty }] : []
  })
  const themeRows = future ? [] : list(feedback?.themes).map(row => ({ ...row,
    continuationLabel: { weakened: '后续走弱', held: '保持强度', mixed: '出现分歧', unknown: '待核验' }[row.continuation] || '待核验',
    attributions: list(row.attributions).map(item => ({ code: text(item.code), label: text(item.label), evidence: text(item.evidence) })).filter(item => item.label),
  }))
  const relevant = themeRows.filter(row => row.selected === true || numeric(row.observedCount) > 0 || row.attributions.length)
  return {
    supplied: Boolean(supplied), available: !unavailable, asOf, sourceDay,
    title: historical ? '每日反馈 · 当时已知结果' : '每日反馈',
    statusLabel: future ? '反馈时间晚于当前时间，未采用' : !supplied ? '尚无题材反馈'
      : { observed: '已核验价格观察', reviewed: '已核验价格观察', partial: '部分结果待核验', pending: '等待收盘证据',
        pending_data: '等待收盘证据', no_history: '尚无已结算记录', no_scans: '当日尚无冻结扫描', expired: '反馈已过期',
        unavailable: '反馈证据不足', failed: '本次反馈未完成', empty: '尚无已结算记录' }[feedback.status] || '已保存反馈',
    summary: future ? '' : messages(feedback?.summary).join('；'),
    themes: relevant, unobservedThemeCount: themeRows.length - relevant.length,
    missedLeaders: future ? [] : list(feedback?.missedLeaders),
    adjustments: unavailable ? [] : adjustments, gaps: messages(feedback?.gaps),
    meaning: text(feedback?.meaning) || '结果为冻结参考价至核验报价的价格变化，不是成交收益；反馈用于观察优先级，未自动训练或修改模型权重。',
  }
}
