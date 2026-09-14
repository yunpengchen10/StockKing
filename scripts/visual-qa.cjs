// Local visual QA only. All bridge values are explicit empty-state fixtures;
// this file is never included in the production frontend.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || require('path').join(require('os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const fs = require('fs'), path = require('path')
const viewportWidth = Number(process.env.QA_WIDTH || 1440)
const origin = process.env.QA_ORIGIN || 'http://127.0.0.1:5173'
const out = path.join(__dirname, '../artifacts/v2.4.0/ui-qa' + (viewportWidth === 1440 ? '' : '-' + viewportWidth) + (origin.endsWith(':5174') ? '-production' : ''))
fs.mkdirSync(out, { recursive:true })
;(async () => {
  const browser = await chromium.launch({ headless:true, channel:'msedge' })
  const context = await browser.newContext({viewport:{width:viewportWidth,height:1000},deviceScaleFactor:1})
  await context.addInitScript(() => {
    const preferences = {}, calls = []
    const fixed = {
      GetConfig: {darkTheme:true,aiConfigs:[],AiConfigs:[]}, GetEngineStatus:{ready:false,state:'unavailable',message:'尚未连接研究服务'},
      IsTradingTime:false,IsHKTradingTime:false,IsUSTradingTime:false,
      GetInvestmentAccounts:{accounts:[]}, GetInvestmentSnapshot:{accounts:[],account_count:0,as_of:'',total_equity:null,total_cash:null,total_market_value:null,realized_pnl:null,unrealized_pnl:null},
      GetInvestmentTrades:{items:[]},ListInvestmentPlans:[],GetStockKingAIAdviceHistory:[],GetStockKingAIProviders:[],
      GetModelMetrics:{models:[],tierModels:{}},ListQuantTrainingTasks:{tasks:[]},ListQuantTasks:{tasks:[]},ListExecutableBacktests:{reports:[]},
      GetLatestKingPicks:{adaptive:{candidates:[]},tiers:{},nonKeChuang:[],kechuang:[]},GetKingPicksHistory:{runs:[]},GetStockKingBackgroundLearning:false,GetStockKingBackgroundStatus:{status:'never_run'},
      GetStockKingResearchEvidence:{evidence:{goTechnical:{available:false}},tierModelAnalysis:{tiers:{}},workspaceProvenance:{retrievedAt:new Date().toISOString()}},
      GetResearchNote:{},GetFollowList:[],GetGroupList:[],GetAllGroupStocks:[],GetAllStockList:[],GetAiConfigs:[],GetMarketEmotion:{},GetRzrqRank:{data:[]},
    }
    window.__qaCalls=calls
    window.go = {main:{App:new Proxy({}, {get:(_,key)=>async(...args)=>{
      calls.push(String(key))
      if (key === 'GetStockKingPreference') return preferences[args[0]] || ''
      if (key === 'SaveStockKingPreference') { preferences[args[0]]=args[1]; return null }
      if (key === 'GetStockList') return [{ts_code:'600000.SH',name:'浦发银行'}]
      if (key === 'SaveStockKingAIAdvice') return {ID:1,Content:args[2],StockCode:args[0],StockName:args[1]}
      return structuredClone(Object.hasOwn(fixed,key) ? fixed[key] : [])
    }})}}
    window.runtime = new Proxy({}, {get:(_,key)=> key==='EventsOnMultiple' ? (event,callback)=>{ if(event==='loadingMsg')setTimeout(()=>callback('done'),50);return()=>{} } : ()=>{}})
  })
  const errors=[], results=[]
  for (const [name,route] of [['home','/home'],['portfolio','/portfolio'],['ai','/ai-advice?code=600000.SH'],['quant','/quant-models'],['picks','/king-picks'],['watchlist','/stock'],['kline','/kline-analysis'],['multi','/multi-kline']]) {
    const page=await context.newPage()
    page.on('pageerror', e => errors.push({page:name,message:e.message}))
    await page.goto(origin+'/#'+route,{waitUntil:'networkidle'})
    await page.screenshot({path:path.join(out,name+'.png'),fullPage:true})
    results.push({page:name,title:await page.title(),text:(await page.locator('body').innerText()).slice(0,2000),horizontalOverflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),calls:await page.evaluate(()=>[...new Set(window.__qaCalls)])})
    await page.close()
  }
  fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({results,errors},null,2))
  console.log(JSON.stringify({pages:results.map(r=>({page:r.page,horizontalOverflow:r.horizontalOverflow})),errors}))
  await browser.close()
  if(errors.length || results.some(r=>r.horizontalOverflow))process.exitCode=1
})().catch(e=>{console.error(e);process.exitCode=1})
