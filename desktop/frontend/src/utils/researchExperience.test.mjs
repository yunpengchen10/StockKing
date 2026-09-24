import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { BUILTIN_TEMPLATES, freshTemplateLibrary, loadTemplateLibrary, deleteTemplate, restoreMissingTemplates } from './researchTemplates.mjs'
import { INDICATORS, indicatorFor, combinationsFor } from './indicatorGuide.mjs'
import { explainPick, safeEvidenceURL } from './pickExplain.mjs'
import { formatYmdCompactShanghai, formatYmdHmsCompactShanghai } from './chartDate.mjs'

test('history pagination formats Shanghai dates across midnight and leap years',()=>{
  assert.equal(formatYmdCompactShanghai(Date.parse('2026-01-01T12:00:00+08:00')-86400000),'20251231')
  assert.equal(formatYmdCompactShanghai(Date.parse('2024-03-01T12:00:00+08:00')-86400000),'20240229')
  assert.equal(formatYmdHmsCompactShanghai(Date.parse('2026-09-08T00:00:00+08:00')-60000),'20260907235900')
  assert.equal(formatYmdHmsCompactShanghai(Date.parse('2026-09-08T00:00:00+08:00')),'20260908000000')
  assert.equal(formatYmdCompactShanghai(NaN),'')
})

test('eight ready-to-use prompts coexist with legacy edits',()=>{
  const legacy=[{id:'mine',name:'旧模板',content:'保留我的提示词'}]
  const result=loadTemplateLibrary('',JSON.stringify(legacy))
  assert.equal(BUILTIN_TEMPLATES.length,8)
  assert.equal(result.items.length,9)
  assert.equal(result.items.at(-1).content,legacy[0].content)
  assert(BUILTIN_TEMPLATES.every(item=>item.content.includes('不编造') && item.purpose))
})
test('edited and deleted builtins survive reload; restore only fills missing ones',()=>{
  let result=freshTemplateLibrary()
  result.items[0].content='用户修改的内置模板'
  result=deleteTemplate(result,'builtin-short')
  result=loadTemplateLibrary(JSON.stringify(result))
  assert(!result.items.some(item=>item.id==='builtin-short'))
  assert.equal(result.items[0].content,'用户修改的内置模板')
  result=restoreMissingTemplates(result)
  assert(result.items.some(item=>item.id==='builtin-short'))
  assert.equal(result.items[0].content,'用户修改的内置模板')
})
test('invalid saved libraries fail visibly instead of being treated as empty',()=>{
  assert.throws(()=>loadTemplateLibrary('{bad'))
  assert.throws(()=>loadTemplateLibrary(JSON.stringify({version:2,items:[{id:'x'}]})))
  assert.throws(()=>loadTemplateLibrary('',JSON.stringify({items:[]})))
})
test('every plotted toggle and every emitted signal has an actionable guide',()=>{
  const chart=readFileSync(new URL('../components/StockLightweightKlineChart.vue',import.meta.url),'utf8')
  const keys=chart.match(/const PERSISTED_INDICATOR_KEYS = \[([\s\S]*?)\]/)[1].match(/show\w+/g)
  assert.equal(keys.length,51)
  for(const key of keys) assert(indicatorFor(key),key)
  const signals=[...chart.matchAll(/signals\.push\(\{ name: '([^']+)'/g)].map(item=>item[1])
  for(const name of signals) assert(indicatorFor(name),name)
  for(const item of INDICATORS) {
    assert(item.read && item.formula,item.key)
    for(const combo of combinationsFor(item.key)) for(const key of combo.keys) assert(indicatorFor(key),key)
  }
  assert(indicatorFor('RSI').caution.includes('非 Wilder'))
  assert(indicatorFor('VWAP').caution.includes('滚动'))
  assert(indicatorFor('ZigZag').caution.includes('重绘'))
  assert(indicatorFor('Fractal').caution.includes('右侧2根'))
})
test('pick explanation uses recorded data, distinguishes NLS, and preserves missingness',()=>{
  const pick={modelBranch:'M3',score:77,nls:null,featureSnapshot:{amount_multiple_5d:1.5,volume_ratio:2,turnover_rate:0},scoreBreakdown:{models:{M3:77,M1:41}},evidence:[{url:'javascript:alert(1)',summary:'不可执行'}]}
  const result=explainPick(pick)
  assert(result.reason.includes('旧记录未保存具体入选理由'))
  assert.equal(result.indicators.find(row=>row.key==='amount_multiple_5d').value,'1.5倍')
  assert.equal(result.indicators.find(row=>row.key==='volume_ratio').value,'2倍')
  assert.equal(result.indicators.find(row=>row.key==='turnover_rate').value,'0%')
  assert.equal(result.indicators.find(row=>row.key==='daily_data_points').value,'未取得')
  assert.equal(result.nlsScores.length,0)
  assert.equal(result.evidence[0].url,'')
  assert.equal(result.modelScores[0].selected,true)
  assert.equal(explainPick({...pick,nls:0}).nlsScores.length,7)
  assert.equal(explainPick({}).facts[0].value,'未取得')
  assert(explainPick({tierReasons:['低波动']}).reason.includes('低波动'))
  assert.equal(safeEvidenceURL('https://example.com/a'),'https://example.com/a')
})
