// UI acceptance against explicit in-memory Wails fixtures. Never loads user data
// or calls the real investment service. Do not include this file in production.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || require('path').join(require('os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path')
const origin = process.env.QA_ORIGIN || 'http://127.0.0.1:5173'
const out = path.join(__dirname, '../artifacts/v2.4.0/portfolio-ui-qa')
fs.mkdirSync(out, { recursive: true })
const results = [], pageErrors = [], blockedRequests = []

async function installFixture(context, oddLot = false) {
  await context.route('**/*', route => {
    const url = route.request().url()
    if (url.startsWith(origin + '/') || url.startsWith('data:') || url.startsWith('blob:')) return route.continue()
    blockedRequests.push(url); return route.abort()
  })
  await context.addInitScript(({ oddLot }) => {
    const today = new Intl.DateTimeFormat('en-CA', { timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit' }).format(new Date())
    const profile = { riskPct:.5, maxTotalRiskPct:2, maxPositionPct:20, short:20, swing:40, long:40, annualCost:0, minutesPerDay:15 }
    const state = {
      accounts:oddLot ? [{id:71,name:'QA 零股夹具',market:'cn',base_currency:'CNY',is_active:true}] : [],
      cash:oddLot ? {71:10000} : {}, plans:[], trades:[], calls:[], preferences:oddLot ? {'investment.profile.v1':JSON.stringify(profile)} : {},
      seedPositions:oddLot ? [{account_id:71,symbol:'000001.SZ',quantity:50,avg_cost:20}] : [],
    }
    const quote = symbol => symbol === '000001.SZ' ? 20 : 10
    function snapshot() {
      const accounts = state.accounts.map(a => {
        const positions = new Map(state.seedPositions.filter(p=>p.account_id===a.id).map(p=>[p.symbol,{...p}]))
        for (const t of state.trades.filter(t=>t.account_id===a.id)) {
          const p = positions.get(t.symbol) || {symbol:t.symbol,quantity:0,avg_cost:t.price}
          if (t.side==='buy') { p.avg_cost=(p.avg_cost*p.quantity+t.price*t.quantity)/(p.quantity+t.quantity); p.quantity+=t.quantity }
          else p.quantity-=t.quantity
          positions.set(t.symbol,p)
        }
        const rows = [...positions.values()].filter(p=>p.quantity>0).map(p=>({...p,market:'cn',currency:'CNY',last_price:quote(p.symbol),market_value_base:p.quantity*quote(p.symbol),unrealized_pnl_base:(quote(p.symbol)-p.avg_cost)*p.quantity,price_date:today,price_source:'EXPLICIT_UI_TEST_FIXTURE',price_available:true,price_stale:false,valuation_currency:'CNY'}))
        const mv = rows.reduce((s,p)=>s+p.market_value_base,0)
        return {account_id:a.id,account_name:a.name,market:'cn',base_currency:'CNY',total_cash:state.cash[a.id]||0,total_market_value:mv,total_equity:(state.cash[a.id]||0)+mv,realized_pnl:0,unrealized_pnl:rows.reduce((s,p)=>s+p.unrealized_pnl_base,0),fee_total:0,tax_total:0,fx_stale:false,data_quality:'ok',positions:rows}
      })
      const sum = key => accounts.reduce((s,a)=>s+a[key],0)
      return {accounts,account_count:accounts.length,as_of:today,currency:'CNY',total_cash:sum('total_cash'),total_market_value:sum('total_market_value'),total_equity:sum('total_equity'),realized_pnl:0,unrealized_pnl:sum('unrealized_pnl'),fee_total:0,tax_total:0}
    }
    const methods = {
      GetConfig:()=>({darkTheme:false}), GetEngineStatus:()=>({ready:true,state:'ready'}), IsTradingTime:()=>false, IsHKTradingTime:()=>false, IsUSTradingTime:()=>false,
      GetInvestmentAccounts:()=>({accounts:state.accounts}), GetInvestmentSnapshot:snapshot,
      GetInvestmentTrades:accountId=>({items:state.trades.filter(t=>t.account_id===accountId)}), ListInvestmentPlans:()=>state.plans,
      GetStockKingPreference:key=>state.preferences[key]||'', SaveStockKingPreference:(key,value)=>{state.preferences[key]=value},
      CreateInvestmentAccount:name=>{const a={id:state.accounts.length+1,name,market:'cn',base_currency:'CNY',is_active:true};state.accounts.push(a);state.cash[a.id]=0;return a},
      RecordInvestmentCash:entry=>{state.cash[entry.account_id]+=(entry.direction==='in'?1:-1)*entry.amount;return{id:state.calls.length}},
      SaveInvestmentPlan:plan=>{const at=state.plans.findIndex(p=>p.id===plan.id);if(at<0)state.plans.push(plan);else state.plans[at]=plan;return plan},
      RecordInvestmentTrade:trade=>{
        if(state.trades.some(t=>t.account_id===trade.account_id&&t.trade_uid===trade.trade_uid))throw new Error('duplicate fixture trade UID')
        if(trade.side==='sell') {const p=snapshot().accounts.find(a=>a.account_id===trade.account_id)?.positions.find(p=>p.symbol===trade.symbol);if(!p||p.quantity<trade.quantity)throw new Error('fixture oversell')}
        state.cash[trade.account_id]+=(trade.side==='buy'?-1:1)*trade.quantity*trade.price-trade.fee-trade.tax
        const t={...trade,id:state.trades.length+1};state.trades.push(t);return{id:t.id}
      },
    }
    window.__portfolioQa=state
    window.go={main:{App:new Proxy({}, {get:(_,key)=>async(...args)=>{state.calls.push({method:String(key),args:structuredClone(args)});if(!methods[key])throw new Error(`Unexpected fixture bridge call: ${String(key)}`);return structuredClone(methods[key](...structuredClone(args)))}})}}
    window.runtime=new Proxy({}, {get:(_,key)=>key==='EventsOnMultiple'?(event,callback)=>{if(event==='loadingMsg')setTimeout(()=>callback('done'),10);return()=>{}}:()=>{}})
  }, {oddLot})
}

async function run(name, fn) {
  try { await fn(); results.push({name,status:'passed'}); console.log('PASS '+name) }
  catch(error) {results.push({name,status:'failed',error:error.message});throw error}
}
const modal = page => page.locator('.n-modal:visible')
async function submit(page) {await modal(page).getByRole('button',{name:'保存',exact:true}).click()}
async function waitClosed(page) {await page.locator('.n-modal').waitFor({state:'detached'})}
async function bridge(page) {return await page.evaluate(()=>structuredClone(window.__portfolioQa))}
async function newPlan(page,quantity='100') {
  await page.getByRole('button',{name:'新建计划',exact:true}).first().click()
  const dialog=modal(page)
  await dialog.getByLabel('股票代码',{exact:true}).fill('600000.SH')
  await dialog.getByLabel('股票名称',{exact:true}).fill('QA 浦发夹具')
  await dialog.getByLabel('参考买价',{exact:true}).fill('10')
  await dialog.getByLabel('失效价',{exact:true}).fill('9')
  await dialog.getByLabel('计划数量',{exact:true}).fill(quantity)
  await dialog.getByLabel('入场依据',{exact:true}).fill('明确的 UI 测试研究依据')
  await dialog.getByLabel('失效条件',{exact:true}).fill('测试价格低于9时复核退出')
}

;(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'})
  let page
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}})
    await installFixture(context)
    page=await context.newPage();page.on('pageerror',error=>pageErrors.push(error.message))
    await page.goto(origin+'/#/portfolio',{waitUntil:'networkidle'})
    await run('first launch defaults to dark despite legacy light config',async()=>assert.equal(await page.locator('html').getAttribute('theme-mode'),'dark'))
    await run('create account through the UI',async()=>{
      await page.getByRole('button',{name:'添加账户',exact:true}).click()
      await modal(page).getByLabel('账户名称',{exact:true}).fill('QA A股账户（内存夹具）')
      await submit(page);await waitClosed(page)
      assert.equal((await bridge(page)).accounts[0].name,'QA A股账户（内存夹具）')
    })
    await run('record cash deposit through the UI',async()=>{
      await page.getByRole('button',{name:'出入金',exact:true}).click()
      await modal(page).getByLabel('实际金额（元）',{exact:true}).fill('10000')
      await submit(page);await waitClosed(page)
      assert.equal((await bridge(page)).cash[1],10000)
    })
    await run('save a valid risk budget',async()=>{
      await page.getByRole('button',{name:'风险预算',exact:true}).click()
      await submit(page);await waitClosed(page)
      assert.equal(JSON.parse((await bridge(page)).preferences['investment.profile.v1']).riskPct,.5)
    })
    await run('insufficient minimum lot cannot create a plan',async()=>{
      await newPlan(page)
      assert.equal(await modal(page).locator('.sizing-preview b').innerText(),'最多 0 股')
      await submit(page)
      await page.getByText('可用预算不足一个交易单位，保持观察',{exact:true}).first().waitFor()
      assert.equal((await bridge(page)).plans.length,0)
      await modal(page).getByRole('button',{name:'取消',exact:true}).click();await waitClosed(page)
    })
    await run('over-limit plan is rejected before bridge persistence',async()=>{
      await page.getByRole('button',{name:'出入金',exact:true}).click()
      await modal(page).getByLabel('实际金额（元）',{exact:true}).fill('90000')
      await submit(page);await waitClosed(page)
      await newPlan(page,'500')
      assert.equal(await modal(page).locator('.sizing-preview b').innerText(),'最多 400 股')
      await submit(page)
      assert.equal((await bridge(page)).plans.length,0)
      assert.equal(await modal(page).count(),1)
    })
    await run('valid plan is persisted as watching without a trade',async()=>{
      await modal(page).getByLabel('计划数量',{exact:true}).fill('400')
      await submit(page);await waitClosed(page)
      const state=await bridge(page)
      assert.equal(state.plans.length,1);assert.equal(state.plans[0].status,'watching');assert.equal(state.trades.length,0)
    })
    await run('linked actual purchase locks symbol and keeps the plan ID',async()=>{
      await page.locator('.plan-card').getByRole('button',{name:'记录成交',exact:true}).click()
      assert.equal(await modal(page).getByLabel('股票代码',{exact:true}).getAttribute('readonly'),'')
      await modal(page).getByLabel('实际成交价',{exact:true}).fill('10')
      await modal(page).getByLabel(/备注/).fill('QA actual buy')
      await modal(page).getByRole('button',{name:/记录成交/}).click();await waitClosed(page)
      const state=await bridge(page)
      assert.equal(state.plans[0].status,'holding');assert.equal(state.trades[0].quantity,400)
      assert.equal(state.trades[0].note,`SKPLAN:${state.plans[0].id} QA actual buy`)
      assert.equal(state.cash[1],96000)
    })
    await run('partial sale keeps the plan holding and recalculates actual remaining risk',async()=>{
      await page.locator('.plan-card').getByRole('button',{name:'记录成交',exact:true}).click()
      assert.equal(await modal(page).getByLabel('买卖方向').inputValue(),'sell')
      await modal(page).getByLabel('实际数量',{exact:true}).fill('150')
      await modal(page).getByLabel('实际成交价',{exact:true}).fill('10')
      await modal(page).getByRole('button',{name:/记录成交/}).click();await waitClosed(page)
      const state=await bridge(page)
      assert.equal(state.plans[0].status,'holding');assert.equal(state.trades[1].quantity,150)
      assert.match(await page.locator('.plan-numbers').innerText(),/持仓数量\s*250/)
      await page.locator('.budget-details > summary').click()
      assert.match(await page.locator('.budget-scope').innerText(),/风险占用 ¥270\.00/)
    })
    await run('an actual holding cannot release risk by ending its plan',async()=>{
      const previous=(await bridge(page)).calls.filter(c=>c.method==='SaveInvestmentPlan').length
      await page.locator('.plan-card').getByRole('button',{name:'结束计划',exact:true}).click()
      await page.getByText(/不能通过结束计划释放风险/).waitFor()
      const state=await bridge(page)
      assert.equal(state.plans[0].status,'holding');assert.equal(state.calls.filter(c=>c.method==='SaveInvestmentPlan').length,previous)
    })
    await page.screenshot({path:path.join(out,'portfolio-partial-sale.png'),fullPage:true})
    await run('50-share existing position can be attributed without inventing a trade',async()=>{
      const oddContext=await browser.newContext({viewport:{width:1440,height:1000}})
      await installFixture(oddContext,true)
      const oddPage=await oddContext.newPage();oddPage.on('pageerror',error=>pageErrors.push(error.message))
      await oddPage.goto(origin+'/#/portfolio',{waitUntil:'networkidle'})
      await oddPage.getByText(/尚未归属计划/).waitFor()
      await oddPage.getByRole('button',{name:'持仓',exact:true}).click()
      await oddPage.getByRole('button',{name:'归属计划',exact:true}).click()
      assert.equal(await modal(oddPage).getByLabel('计划数量',{exact:true}).inputValue(),'50')
      assert.equal(await modal(oddPage).getByLabel('计划数量',{exact:true}).getAttribute('readonly'),'')
      await modal(oddPage).getByLabel('失效价',{exact:true}).fill('19')
      await modal(oddPage).getByLabel('入场依据',{exact:true}).fill('QA 已有50股，仅归属')
      await modal(oddPage).getByLabel('失效条件',{exact:true}).fill('低于19复核')
      await submit(oddPage);await waitClosed(oddPage)
      const state=await bridge(oddPage)
      assert.equal(state.plans.length,1);assert.equal(state.plans[0].quantity,50);assert.equal(state.plans[0].status,'holding');assert.equal(state.trades.length,0)
      assert.equal(state.calls.filter(c=>c.method==='RecordInvestmentTrade').length,0)
      await oddPage.locator('.budget-details > summary').click()
      assert.match(await oddPage.locator('.budget-scope').innerText(),/风险占用 ¥64\.00/)
      await oddPage.screenshot({path:path.join(out,'portfolio-odd-lot-attributed.png'),fullPage:true})
      await oddContext.close()
    })
    assert.deepEqual(pageErrors,[])
    assert.deepEqual(blockedRequests,[])
  } catch(error) {
    if(page) {await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});fs.writeFileSync(path.join(out,'failure-page.txt'),await page.locator('body').innerText().catch(()=>''))}
    process.exitCode=1;console.error(error)
  } finally {
    fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({fixtureOnly:true,productionServicesCalled:false,origin,results,pageErrors,blockedRequests},null,2))
    await browser.close()
    console.log(JSON.stringify({passed:results.filter(r=>r.status==='passed').length,failed:results.filter(r=>r.status==='failed').length,pageErrors,blockedRequests}))
  }
})()
