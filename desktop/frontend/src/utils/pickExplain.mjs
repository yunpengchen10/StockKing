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
export function explainPick(pick = {}) {
  if (pick.thesis) return { branch:'AI High',title:'T 至 T+5 · 剩余空间',reason:pick.thesis,logic:pick.thesis,
    facts:[{label:'状态',value:pick.stateLabel || '条件观察'},{label:'行情源时间',value:pick.quote?.provider_timestamp || '未知'}, {label:'买入区间',value:pick.buyRange || '待确认'},{label:'不追条件',value:pick.noChase || '未提供'}],
    modelScores:[],nlsScores:[],evidence:(pick.evidence||[]).map(x=>({...x,url:safeEvidenceURL(x.url)})),source:pick.quote?.source || '未知',quality:pick.data_quality || {} }
  const features=pick.featureSnapshot||pick.features||{}, scores=pick.scoreBreakdown||pick.scores||{}
  const branch=pick.modelBranch||pick.model||scores.selectedModel||features.model_branch||''
  const meta=PICK_BRANCHES[branch]
  const get=(key,fallback)=>features[key] ?? fallback
  const facts=[
    ['成交额 / 5日基准',get('amount_multiple_5d',pick.amountMultiple),'倍'],['量比',get('volume_ratio',pick.volumeRatio),'倍'],
    ['换手率',get('turnover_rate'),'%'],['20日突破幅度',get('breakout_20d_pct'),'%'],
    ['10日涨停特征',get('recent_limitups_10d'),'次'],['20日涨停特征',get('recent_limitups_20d'),'次'],
    ['区间修复位置',valid(get('recovery_ratio',pick.R))?Number(get('recovery_ratio',pick.R))*100:null,'%'],
    ['距涨停幅度',get('distance_to_limit_pct'),'%'],['历史K线',get('daily_data_points'),'根'],
  ].map(([label,value,unit])=>({label,value:valid(value)?`${number(value,unit==='次'||unit==='根'?0:2)}${unit}`:'未提供'}))
  let reason=meta ? `${meta.name} · ${meta.logic}` : (pick.tierReasons||pick.tier_reasons||[]).filter(item=>typeof item==='string').slice(0,2).join('；') || '旧快照缺少入选依据，请重扫获取明细'
  if(branch==='M3') reason=`量价活跃 · 成交额 ${facts[0].value} · 量比 ${facts[1].value}`
  if(branch.startsWith('M6')) reason=`${meta?.name||branch} · 10日涨停特征 ${facts[4].value} · 修复 ${facts[6].value}`
  const nlsLabels={tailStrength:['尾盘强度',20],volumePriceStructure:['量价结构',20],sectorLeaderResonance:['题材词代理分',15],nextDaySpace:['涨停距离分',15],identityMemory:['历史辨识度',15],catalystExpectationGap:['催化词分',10],riskSafety:['风险项',5]}
  const nls=valid(pick.nls)
  return {
    branch, title:meta?`${branch} · ${meta.name}`:branch||'策略筛选', reason, logic:meta?.logic||reason,
    scoreLabel:nls?'尾盘研究分 NLS':'分支研究分', formula:meta?.formula||'该快照未提供公式版本', facts,
    modelScores:Object.entries(scores.models||{}).filter(([,value])=>valid(value)).map(([key,value])=>({key,label:PICK_BRANCHES[key]?.name||key,value:number(value),selected:key===branch})),
    nlsScores:nls?Object.entries(nlsLabels).map(([key,[label,max]])=>({key,label,value:valid(scores.nls?.[key])?`${number(scores.nls[key])} / ${max}`:'未提供'})):[],
    evidence:(Array.isArray(pick.evidence)?pick.evidence:[]).map(item=>({...item,url:safeEvidenceURL(item.url)})),
    source:features.snapshot_source||'未提供', quality:pick.data_quality||{},
  }
}
export function safeEvidenceURL(value) { try { const url=new URL(value); return ['https:','http:'].includes(url.protocol)?url.href:'' } catch { return '' } }
