// Local UI test fixture only: Wails methods are replaced before application load.
// No brokerage, AI endpoint, user configuration or production database is used.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || require('path').join(require('os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const out = path.join(__dirname, '../artifacts/v2.4.2/ai-workspace-qa')
fs.mkdirSync(out, { recursive: true })
;(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1 })
  await context.addInitScript(() => {
    const preferences = JSON.parse(localStorage.getItem('qa-ai-preferences') || '{}')
    const history = JSON.parse(localStorage.getItem('qa-ai-history') || '[]')
    window.__qaCalls = []; window.__qaRunMode = 'success'; window.__qaHistory = history
    const evidence = { symbol:'600000.SH', summary:'UI fixture: evidence only', evidence: { goTechnical: { available:true, source:'QA fixture / no live feed', date:'2026-09-04', close:10.12 }, dailyContext:{source:'QA fixture',publishedAt:'2026-09-04T07:00:00Z'} }, tierModelAnalysis:{tiers:{regular:{label:'BalancedRank',modelStatus:'rule_fallback',degradedReasons:['QA fixture: validation failed'],result:{rawRankSignal5d:99.99}}}}, workspaceProvenance:{symbolCode:'600000.SH',source:'QA fixture',retrievedAt:'2026-09-07T02:00:00Z'} }
    const fixed = { GetConfig:{darkTheme:true,aiConfigs:[],AiConfigs:[]}, GetEngineStatus:{ready:false,state:'unavailable',message:'QA fixture'}, IsTradingTime:false,IsHKTradingTime:false,IsUSTradingTime:false, GetStockKingAIProviders:[{id:11,name:'Alpha 测试平台',model:'alpha-model',ready:true},{id:22,name:'Beta 测试平台',model:'beta-model',ready:true}], GetStockKingResearchEvidence:evidence, GetResearchNote:{coreLogic:'QA fixture: original note'}, GetStockList:[{ts_code:'600000.SH',name:'浦发银行'}], GetFollowList:[],GetGroupList:[],GetAllGroupStocks:[],GetAllStockList:[],GetAiConfigs:[],GetMarketEmotion:{},GetRzrqRank:{data:[]} }
    window.go = {main:{App:new Proxy({}, {get:(_,key)=>async(...args)=>{
      window.__qaCalls.push({method:String(key),args})
      if(key==='GetStockKingPreference') return preferences[args[0]] || ''
      if(key==='SaveStockKingPreference'){ preferences[args[0]]=args[1];localStorage.setItem('qa-ai-preferences',JSON.stringify(preferences));return null }
      if(key==='GetStockKingAIAdviceHistory') return structuredClone(history)
      if(key==='GetAIResponseResultList') return {list:structuredClone(history),total:history.length,totalPages:1}
      if(key==='SaveStockKingAIAdvice') { const item={ID:history.length+1,content:args[2],stockCode:args[0],stockName:args[1],modelName:args[3],question:'Stock King 结构化研究建议',CreatedAt:'2026-09-07T10:00:00+08:00'};history.unshift(item);localStorage.setItem('qa-ai-history',JSON.stringify(history));return structuredClone(item) }
      if(key==='RunStockKingAIResearch') {
        if(window.__qaRunMode==='pending') return new Promise((resolve,reject)=>{window.__qaReject=reject})
        return {...structuredClone(evidence),llmExplanation:JSON.stringify({markdown:'# API fixture report\n\nEvidence based explanation.',quantComparison:[{model:'regular',conclusion:'agree',reason:'This AI claim does not pass failed quant validation.'}]}),workspaceAnalyzedAt:'2026-09-07T02:05:00Z',llmExplanationModel:'beta-model',llmExplanationStatus:'completed'}
      }
      if(key==='CancelStockKingAIResearch'){if(window.__qaReject){window.__qaReject(new Error('QA fixture cancelled'));window.__qaReject=null};return true}
      return structuredClone(Object.hasOwn(fixed,key)?fixed[key]:[])
    }})}}
    window.runtime=new Proxy({}, {get:(_,key)=>key==='EventsOnMultiple'?(event,callback)=>{if(event==='loadingMsg')setTimeout(()=>callback('done'),50);return()=>{}}:()=>{}})
  })
  const page = await context.newPage(), errors = [], checks = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('http://127.0.0.1:5173/#/ai-advice?code=600000.SH&name=浦发银行',{waitUntil:'networkidle'})
  await page.getByRole('heading',{name:'AI 研究',exact:true}).waitFor()
  assert.equal(await page.evaluate(()=>window.__qaCalls.filter(c=>c.method==='RunStockKingAIResearch').length),0)
  assert.equal(await page.getByRole('button',{name:'运行研究',exact:true}).isDisabled(),true)
  checks.push('Opening page never auto-runs API; explicit provider selection required')

  await page.locator('nav.source-tabs button').filter({hasText:'研究包'}).click()
  await page.getByRole('button',{name:'生成研究包'}).click()
  await page.getByLabel('研究包原文').waitFor()
  const researchPackage=await page.getByLabel('研究包原文').inputValue()
  assert.ok(researchPackage.includes('600000.SH')&&researchPackage.includes('2026-09-04')&&researchPackage.includes('2026-09-07T02:00:00Z'))
  checks.push('Export carries selected stock, original source dates and separate retrieval time')

  await page.locator('input[type=file]').first().setInputFiles({name:'qa-prompt.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({name:'QA 模板',prompt:'仅引用已提供证据；不要提升失败模型。'}))})
  await page.getByRole('button',{name:'保存模板',exact:true}).click()
  await page.waitForFunction(()=>JSON.parse(localStorage.getItem('qa-ai-preferences')||'{}')['aiAdvice.promptTemplates.v2'])
  const template=await page.evaluate(()=>JSON.parse(JSON.parse(localStorage.getItem('qa-ai-preferences'))['aiAdvice.promptTemplates.v2']).items.find(item=>item.name==='QA 模板'))
  assert.equal(template.content,'仅引用已提供证据；不要提升失败模型。')
  checks.push('Imported JSON prompt template persists locally')

  await page.locator('nav.source-tabs button').filter({hasText:'导入报告'}).click()
  await page.getByLabel('外部报告原文').fill('{"stockCode":"000001.SZ","markdown":"错误对象"}')
  await page.getByRole('alert').filter({hasText:'股票不匹配'}).waitFor()
  assert.ok((await page.getByRole('alert').filter({hasText:'股票不匹配'}).getAttribute('title')).includes('000001.SZ'))
  assert.equal(await page.getByRole('button',{name:'保存报告',exact:true}).isDisabled(),true)
  await page.getByLabel('外部报告原文').fill('{"stockCode":"600000.SH","analyzedAt":"2026-02-30","markdown":"日期错误"}')
  await page.getByRole('alert').filter({hasText:'日期无效'}).waitFor()
  checks.push('Mismatching stock and impossible date are rejected in actual UI')

  const original='  # Imported fixture report\n\n股票代码：600000.SH\n\n<script>window.__qaXSS=1</script>\n\n[Unsafe](javascript:alert(1))\n\n**真实原文保留**\n '
  await page.getByLabel('外部报告原文').fill(original)
  await page.locator('.status-tags').getByText('时点未知',{exact:true}).waitFor()
  await page.locator('.status-tags').getByText('有效期未知',{exact:true}).waitFor()
  await page.getByRole('button',{name:'保存报告',exact:true}).click()
  await page.waitForFunction(()=>window.__qaHistory.length===1)
  const saved=await page.evaluate(()=>JSON.parse(window.__qaHistory[0].content))
  assert.equal(saved.raw,original);assert.equal(saved.source,'import');assert.equal(saved.analyzedAt,null)
  assert.equal(await page.evaluate(()=>window.__qaXSS),undefined)
  assert.equal(await page.locator('.safe-markdown a[href^="javascript:"]').count(),0)
  checks.push('Missing dates remain unknown; HTML cannot run; saved original is exact')
  await page.getByRole('button',{name:'保存报告',exact:true}).click()
  await page.waitForTimeout(200)
  assert.equal(await page.evaluate(()=>window.__qaHistory.length),1)
  checks.push('Repeated import save does not duplicate same report')
  await page.reload({waitUntil:'networkidle'})
  await page.getByRole('button',{name:'查看原文',exact:true}).click()
  assert.equal(await page.getByLabel('保存的原始报告').inputValue(),original)
  checks.push('Report, source, raw content and template survive page reload')
  await page.screenshot({path:path.join(out,'import-history.png'),fullPage:true})

  await page.locator('nav.source-tabs button').filter({hasText:'API 研究'}).click()
  await page.locator('.config-grid select').nth(0).selectOption({label:'Beta 测试平台'})
  await page.locator('.config-grid select').nth(1).selectOption('22')
  await page.getByRole('button',{name:'运行研究',exact:true}).click()
  await page.waitForFunction(()=>window.__qaHistory.length===2)
  const calls=await page.evaluate(()=>window.__qaCalls.filter(c=>c.method==='RunStockKingAIResearch'))
  assert.equal(calls.length,1);assert.equal(calls[0].args[0].configId,22)
  await page.getByText('API fixture report',{exact:true}).waitFor()
  assert.ok((await page.locator('.quant-card').filter({hasText:'BalancedRank'}).innerText()).includes('未通过 / 不可用'))
  assert.ok(!(await page.locator('.quant-card').filter({hasText:'BalancedRank'}).innerText()).includes('99.99'))
  checks.push('API uses selected second configuration; failed model remains failed despite AI agreement')
  await page.screenshot({path:path.join(out,'api-comparison.png'),fullPage:true})
  await page.evaluate(()=>window.__qaRunMode='pending')
  await page.getByRole('button',{name:'运行研究',exact:true}).click()
  await page.getByRole('button',{name:'取消研究',exact:true}).waitFor()
  assert.equal(await page.getByRole('button',{name:'研究进行中…',exact:true}).isDisabled(),true)
  await page.getByRole('button',{name:'取消研究',exact:true}).click()
  await page.getByRole('alert').filter({hasText:'已取消'}).waitFor()
  assert.equal(await page.evaluate(()=>window.__qaHistory.length),2)
  checks.push('Cancel invokes bridge once and does not save a late or partial result')

  await page.goto('http://127.0.0.1:5173/#/research?name=AI分析报告',{waitUntil:'networkidle'})
  await page.getByRole('button',{name:'查看',exact:true}).first().click()
  await page.locator('.safe-report-preview').getByRole('heading',{name:'API fixture report'}).waitFor()
  assert.ok(!(await page.locator('.safe-report-preview').innerText()).includes('stockking.ai-research.v1'))
  checks.push('Legacy research center unwraps new saved envelope and safely renders report')
  await page.screenshot({path:path.join(out,'research-center.png'),fullPage:true})
  fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({checks,errors},null,2))
  await browser.close()
  assert.equal(errors.length,0,errors.join('\n'))
  console.log(JSON.stringify({passed:checks.length,checks,errors},null,2))
})().catch(error=>{console.error(error);process.exitCode=1})
