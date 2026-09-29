export const PICK_BRANCHES = {
  M1:{name:'事件催化',logic:'上下文命中重组、并购、控制权等关键词；还需核验原始公告。',formula:'30 + 20×事件词命中数 + 1.8×正涨幅 + 1.2×正突破幅度 − 8×负面词命中数'},
  M2:{name:'题材线索',logic:'上下文命中政策、产业等题材词，结合涨幅与量比评分。',formula:'30 + 16×题材词命中数 + 2×正涨幅 + 4×min(量比,4)'},
  M3:{name:'量价活跃',logic:'主要依据成交额相对5日基准、量比与换手率。',formula:'30 + 10×min(成交额倍数,5) + 7×min(量比,5) + min(换手率,20)'},
  M4:{name:'突破延续',logic:'近期有涨停特征且突破幅度为正，再结合量比。',formula:'27 + 11×20日涨停特征次数 + 2×正突破幅度 + 5×min(量比,4)'},
  M5:{name:'经营催化',logic:'上下文命中订单、中标、业绩等关键词，结合突破与均线状态。',formula:'29 + 18×经营词命中数 + 1.5×正突破幅度 + (均线多头时8)'},
  'M6-A':{name:'二阶段修复',logic:'10日有至少2次涨停特征，区间修复位置≥60%。',formula:'25 + 14×10日涨停特征次数 + 28×修复比例 + 0.8×min(换手率,25) + (高于VWAP时8)'},
  'M6-B':{name:'强波动修复',logic:'10日有至少2次涨停特征，跌幅≤−7%但区间修复位置≥65%。',formula:'18 + 15×10日涨停特征次数 + 修复项 + 0.7×min(换手率,25)；满足深跌修复时修复项35，否则25×修复比例'},
}
const valid = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
const number = (value,d=2) => valid(value) ? Number(value).toFixed(d) : '—'
const strings = value => (Array.isArray(value) ? value : typeof value === 'string' ? [value] : []).filter(item => typeof item === 'string' && item.trim()).map(item => item.trim())
const unique = value => [...new Set(value)]
const recordedReason = value => !/(?:模型.*(?:未通过发布门槛|不可用|未就绪|未训练)|仅显示规则观察分|未加载模型)/.test(value)
const missingReason = '旧记录未保存具体入选理由，不能据此补写；请查看原记录说明。'
const present = value => value !== null && value !== undefined && value !== ''
function displayValue(value) {
  if (!present(value) || (typeof value === 'number' && !Number.isFinite(value))) return '未取得'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return Number(value.toFixed(4)).toLocaleString('zh-CN', { maximumFractionDigits: 4 })
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
export function normalizeIndicatorEvidence(rows) {
  return (Array.isArray(rows) ? rows : []).filter(row => row && typeof row === 'object').map((row, index) => {
    const value = displayValue(row.value)
    return {
      key: row.key || `indicator-${index}`, label: row.label || row.key || '未命名指标',
      value: value === '未取得' ? value : `${value}${row.unit || ''}`,
      source: displayValue(row.source), asOf: displayValue(row.asOf), threshold: displayValue(row.threshold),
      status: value === '未取得' ? '未取得' : ({ pass: '满足', passed: '满足', fail: '不满足', failed: '不满足', warning: '需注意', missing: '未取得', observed: '已记录', info: '参考', reference: '参考', unknown: '未核验' }[row.status] || row.status || '未标注'),
      role: { selection: '入选依据', risk: '风险指标', context: '背景指标', reference: '参考指标', exclusion: '排除条件', gate: '筛选门槛' }[row.role] || row.role || '未标注用途',
    }
  })
}
function legacyIndicators(pick, features) {
  const quote = pick.quote || {}
  const fields = [
    ['price', '快照价格', '元'], ['change_pct', '当日涨跌幅', '%'], ['amount', '成交额', '元'],
    ['volume_ratio', '量比', '倍'], ['turnover_rate', '换手率', '%'],
    ['amount_multiple_5d', '成交额 / 5日基准', '倍'], ['breakout_20d_pct', '20日突破幅度', '%'],
    ['recent_limitups_10d', '10日涨停特征', '次'], ['recent_limitups_20d', '20日涨停特征', '次'],
    ['recovery_ratio', '区间修复位置', '%'], ['distance_to_limit_pct', '距涨停幅度', '%'], ['daily_data_points', '历史K线', '根'],
  ]
  return fields.map(([key, label, unit]) => {
    let value = features[key]
    const quoted = ['price', 'change_pct'].includes(key) && present(quote[key])
    if (quoted) value = quote[key]
    if (key === 'price') value ??= pick.referencePrice
    if (key === 'change_pct') value ??= pick.currentChange
    if (key === 'volume_ratio') value ??= pick.volumeRatio
    if (key === 'amount_multiple_5d') value ??= pick.amountMultiple
    if (key === 'recovery_ratio') value = valid(value ?? pick.R) ? Number(value ?? pick.R) * 100 : null
    return { key, label, value, unit, source: quoted ? quote.source : features.snapshot_source,
      asOf: quoted ? quote.provider_timestamp || quote.source_time : features.as_of || features.asOf,
      threshold: null, status: 'observed', role: 'reference' }
  })
}
export function explainPick(pick = {}) {
  pick = pick || {}
  const features=pick.featureSnapshot||pick.features||{}, scores=pick.scoreBreakdown||pick.scores||{}
  const branch=pick.strategyBranch||pick.modelBranch||pick.model||scores.selectedModel||features.model_branch||''
  const meta=PICK_BRANCHES[branch]
  const structured = Boolean(pick.scoreVersion || pick.recommendationLogicVersion || Array.isArray(pick.selectionReasons) || Array.isArray(pick.indicatorEvidence))
  const reasons = unique(strings(structured ? pick.selectionReasons : pick.tierReasons || pick.tier_reasons).filter(recordedReason))
  const reason = reasons.join('；') || (structured ? '本轮未保存充分的入选理由，请先核对证据。' : missingReason)
  const indicators = normalizeIndicatorEvidence(structured ? pick.indicatorEvidence : legacyIndicators(pick, features))
  const risks = unique([...strings(pick.riskReasons), ...strings(pick.risks), ...strings(pick.data_quality?.gaps)])
  const entry = pick.entryPlan || {}
  const v11 = String(pick.scoreVersion || '').startsWith('stockking-v1.1')
  const entryPlan = {
    trigger: strings(entry.trigger || pick.triggerConditions || pick.upgradeConditions || pick.triggers).join('；') || '未取得',
    invalidations: strings(entry.invalidations || pick.invalidationConditions || pick.invalidations).join('；') || '未取得',
    noChase: strings(entry.noChase || pick.noChase).join('；') || '未取得',
    holdingWindow: strings(entry.holdingWindow || pick.horizon || pick.expectedHoldingRange).join('；') || '未取得',
  }
  const nlsLabels={tailStrength:['尾盘强度',20],volumePriceStructure:['量价结构',20],sectorLeaderResonance:['题材词代理分',15],nextDaySpace:['涨停距离分',15],identityMemory:['历史辨识度',15],catalystExpectationGap:['催化词分',10],riskSafety:['风险项',5]}
  const nls=valid(pick.nls)
  return {
    branch, title:meta?`${branch} · ${meta.name}`:branch||'策略筛选', reason, reasons, logic:meta?.logic||reason,
    structured, legacyNotice: structured ? '' : '旧记录未保存结构化证据链；以下仅展示当时已保存的数值，来源、时间和阈值缺失处标为未取得。',
    savedExplanation: !structured && typeof pick.thesis === 'string' ? pick.thesis : '',
    logicVersion: pick.scoreVersion || pick.recommendationLogicVersion || '旧记录未保存', indicators, risks, entryPlan,
    evidenceChecks: Object.entries(pick.evidenceChecks || {}).map(([key, passed]) => ({key,
      label: {minute_structure:'分钟结构',sector_resonance:'行业同刻联动',history_20d:'20日同刻基准',relative_volume:'即时放量',fund_direction:'资金方向'}[key] || key,
      passed: passed === true})),
    baselineDays: pick.historicalCoverageDays ?? pick.baselineHistoryDays ?? pick.baselineEvidence?.historyDays ?? null,
    missingHistoryDates: strings(pick.baselineEvidence?.missingHistoryDates),
    baselineSource: pick.baselineEvidence?.baselineSource || '未取得',
    facts: indicators.map(({label, value}) => ({label, value})),
    scoreMeaning: pick.scoreMeaning || '研究排序分，仅用于候选比较；不是胜率、上涨概率或预期收益。',
    scoreLabel:nls?'尾盘研究分 NLS':'研究排序分', formula:v11?'FinalScore = clip(max(Early, MainRise) − 0.2 × DistributionRisk, 0, 100)。缺失因子按可用权重重新分配；0.2为未验证初值。':meta?.formula||'该快照未提供公式版本',
    modelScores:v11 ? [['Early',pick.earlyScore],['MainRise',pick.mainRiseScore],['DistributionRisk',pick.distributionRisk], ...Object.entries(pick.factorScores || {})].map(([key,value])=>({key,label:key,value:valid(value)?number(value):'未取得'})) : Object.entries(scores.models||{}).filter(([,value])=>valid(value)).map(([key,value])=>({key,label:PICK_BRANCHES[key]?.name||key,value:number(value),selected:key===branch})),
    nlsScores:nls?Object.entries(nlsLabels).map(([key,[label,max]])=>({key,label,value:valid(scores.nls?.[key])?`${number(scores.nls[key])} / ${max}`:'未提供'})):[],
    evidence:(Array.isArray(pick.evidence)?pick.evidence:[]).map(item=>({...item,url:safeEvidenceURL(item.url)})),
    source:pick.quote?.source||features.snapshot_source||'未取得', quality:pick.data_quality||{},
  }
}
export function safeEvidenceURL(value) { try { const url=new URL(value); return ['https:','http:'].includes(url.protocol)?url.href:'' } catch { return '' } }
