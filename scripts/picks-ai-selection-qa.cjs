const fs=require('fs'),path=require('path'),assert=require('node:assert/strict')
const {chromium}=require(path.join(require('os').homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const origin=process.env.QA_ORIGIN||'http://127.0.0.1:5174'
const source=fs.readFileSync(path.join(__dirname,'research-experience-ui-qa.cjs'),'utf8').replaceAll('\r\n','\n')
const fixture=source.slice(source.indexOf('      window.__qaCalls=[]'),source.indexOf('\n    })\n    const page='))
;(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true})
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1050}})
    await context.route('**/*',r=>r.request().url().startsWith(origin)?r.continue():r.abort())
    await context.addInitScript({content:fixture})
    await context.addInitScript(()=>{
      const original=window.go.main.App
      window.go.main.App=new Proxy({}, {get:(_,key)=>async(...args)=>{
        if(key==='GetStockKingAIProviders')return [{id:11,name:'甲平台',model:'deepseek-v4-pro',ready:true},{id:22,name:'乙平台',model:'deepseek-v4-flash',ready:true}]
        if(key==='GetStockKingPreference'&&args[0]==='picks.ai.configId')return localStorage.getItem('qa-picks-model')||''
        if(key==='SetStockKingPicksAIConfig'){localStorage.setItem('qa-picks-model',String(args[0]));return null}
        return original[key](...args)
      }})
    })
    const page=await context.newPage(), errors=[]
    page.on('pageerror',e=>errors.push(e.message))
    await page.goto(origin+'/#/king-picks',{waitUntil:'networkidle'})
    assert.equal(await page.getByLabel('当日机会AI模型').inputValue(),'')
    await page.getByRole('button',{name:'刷新全部',exact:true}).click()
    assert.equal(await page.evaluate(()=>window.__qaCalls.filter(c=>c.method==='GetKingPicks').length),0)
    await page.getByLabel('当日机会AI平台').selectOption({label:'乙平台'})
    await page.getByLabel('当日机会AI模型').selectOption('22')
    await page.waitForFunction(()=>localStorage.getItem('qa-picks-model')==='22')
    assert.equal(await page.evaluate(()=>window.__qaCalls.filter(c=>c.method==='GetKingPicks').length),0)
    await page.reload({waitUntil:'networkidle'})
    assert.equal(await page.getByLabel('当日机会AI平台').inputValue(),'乙平台')
    assert.equal(await page.getByLabel('当日机会AI模型').inputValue(),'22')
    await page.getByRole('button',{name:'刷新全部',exact:true}).click()
    await page.waitForFunction(()=>window.__qaCalls.filter(c=>c.method==='GetKingPicks').length===1)
    assert.deepEqual(errors,[])
    console.log('PASS: explicit model selection, persists, no call on entry/selection/reload, one manual scan')
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1})
