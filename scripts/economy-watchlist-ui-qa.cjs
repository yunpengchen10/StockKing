const fs=require('fs'),path=require('path'),assert=require('node:assert/strict')
const {chromium}=require(path.join(require('os').homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const origin='http://127.0.0.1:5175',out=path.join(__dirname,'../artifacts/v2.5.0/ui-qa')
fs.mkdirSync(out,{recursive:true})
const source=fs.readFileSync(path.join(__dirname,'research-experience-ui-qa.cjs'),'utf8').replaceAll('\r\n','\n')
const fixture=source.slice(source.indexOf('      window.__qaCalls=[]'),source.indexOf('\n    })\n    const page='))
;(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true})
 try{
 const context=await browser.newContext({viewport:{width:1440,height:1000}})
 await context.route('**/*',r=>r.request().url().startsWith(origin)?r.continue():r.abort())
 await context.addInitScript({content:fixture})
 await context.addInitScript(()=>{
  const original=window.go.main.App
  let groups=[{ID:1,name:'观察',sort:1}],members=[{stockCode:'sh600001',groupId:1}]
  const rows=[{StockCode:'sh600001',Name:'新增样例',Price:11,SelectionPrice:10,SelectionStatus:'recorded',SelectionQuoteTime:'2026-09-10 09:30:00',LatestQuoteTime:'2026-09-10 10:30:00',LatestQuoteStatus:'available',Time:'2026-09-10T09:30:00+08:00'}, {StockCode:'sz000001',Name:'旧自选样例',Price:12,SelectionPrice:null,LatestQuoteTime:'2026-09-10 10:30:00',Time:'2026-08-01T09:30:00+08:00'}]
  window.go.main.App=new Proxy({}, {get:(_,key)=>async(...args)=>{
   window.__economyCalls ||= [];window.__economyCalls.push({key,args})
   if(key==='GetStockKingAIBudget')return {usedRequests:0,settings:{daily_limit:10,input_tokens:8000,output_tokens:2000},records:[]}
   if(key==='SaveStockKingAIBudget')return args[0]
   if(key==='GetGroupList')return structuredClone(groups)
   if(key==='GetAllGroupStocks')return structuredClone(members)
   if(key==='GetFollowList'||key==='RefreshStockKingWatchlist')return args[0]?rows.filter(r=>members.some(m=>m.groupId===args[0]&&m.stockCode===r.StockCode)):rows
   if(key==='AddGroup'){groups.push({ID:2,name:args[0].name});return '添加成功'}
   if(key==='UpdateGroup'){groups.find(g=>g.ID===args[0]).name=args[1];return '修改成功'}
   if(key==='RemoveGroup'){groups=groups.filter(g=>g.ID!==args[0]);members=members.filter(m=>m.groupId!==args[0]);return '移除成功'}
   if(key==='GetStockKingAIProviders')return [{id:11,name:'测试平台',model:'deepseek-v4-pro',ready:true}]
   if(key==='SetStockKingPicksAIConfig')return null
   if(key==='GetStockKingPreference'&&args[0]==='picks.ai.configId')return ''
   if(key==='ReviewStockKingPicks')return {markdown:'### 600000\n风险证据仅作观察',analyzedAt:'2026-09-10T10:30:00+08:00',cacheHit:false,usage:{total_tokens:120}}
   if(key==='GetKingPicks') {
    const result=await original[key](...args)
    result.adaptive.modelVersion='king-local-5d-v1'
    result.adaptive.model_version='king-local-5d-v1'
    result.adaptive.llmUsed=false
    for(const row of result.adaptive.candidates || []) {row.modelStatus='rule_fallback';row.modelBranch='本地规则观察';row.stateLabel='条件观察';row.thesis='五日模型未通过独立验证门槛'}
    return result
   }
   return original[key](...args)
  }})
 })
 const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message))
 await page.goto(origin+'/#/portfolio',{waitUntil:'networkidle'})
 await page.getByRole('heading',{name:'自选',exact:true}).waitFor()
 await page.getByText('+10.00%',{exact:true}).waitFor()
 await page.getByText('升级前未记录',{exact:true}).waitFor()
 assert.equal(await page.locator('a[href="#/portfolio"]').count(),0)
 await page.getByRole('button',{name:'管理分组',exact:true}).click()
 await page.getByRole('button',{name:'重命名',exact:true}).click()
 await page.locator('.n-dialog input').fill('重点观察')
 await page.locator('.n-dialog').getByRole('button',{name:'保存',exact:true}).click()
 await page.getByText('重点观察',{exact:true}).last().waitFor()
 await page.getByRole('button',{name:'删除',exact:true}).click()
 await page.locator('.n-dialog').getByRole('button',{name:'删除分组',exact:true}).click()
 await page.locator('.n-dialog').waitFor({state:'hidden'})
 await page.keyboard.press('Escape')
 await page.locator('.n-modal').waitFor({state:'hidden'})
 await page.getByText('新增样例',{exact:true}).waitFor()
 await page.screenshot({path:path.join(out,'watchlist-desktop.png'),fullPage:true})
 await page.setViewportSize({width:820,height:950});await page.screenshot({path:path.join(out,'watchlist-narrow.png'),fullPage:true})
 await page.setViewportSize({width:1440,height:1000})
 await page.goto(origin+'/#/king-picks',{waitUntil:'networkidle'})
 await page.getByRole('button',{name:'刷新全部',exact:true}).click()
 await page.waitForFunction(()=>window.__economyCalls.some(c=>c.key==='GetKingPicks'))
 assert.equal(await page.evaluate(()=>window.__economyCalls.filter(c=>c.key==='ReviewStockKingPicks').length),0)
 await page.getByLabel('当日机会AI平台').selectOption({label:'测试平台'})
 await page.getByLabel('当日机会AI模型').selectOption('11')
 await page.getByRole('button',{name:'AI 复审',exact:true}).click()
 await page.getByText('风险证据仅作观察',{exact:true}).waitFor()
 assert.equal(await page.evaluate(()=>window.__economyCalls.filter(c=>c.key==='ReviewStockKingPicks').length),1)
 await page.screenshot({path:path.join(out,'picks-review.png'),fullPage:true})
 assert.deepEqual(errors,[])
 fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({passed:true,checks:['retired route redirects','selection return +10%','legacy baseline missing','rename group','delete group keeps watchlist','local scan without AI selection','one explicit AI review','responsive screenshots'],errors},null,2))
 console.log('PASS economy/watchlist UI checks')
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1})
