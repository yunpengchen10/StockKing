// Production UI with isolated fixtures. No market/AI calls or user settings.
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict')
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(require('os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const origin = process.env.QA_ORIGIN || 'http://127.0.0.1:5174'
const out = path.join(__dirname, '../artifacts/v2.4.3/picks-refresh-qa')
const source = fs.readFileSync(path.join(__dirname, 'research-experience-ui-qa.cjs'), 'utf8').replaceAll('\r\n', '\n')
const fixture = source.slice(source.indexOf('      window.__qaCalls=[]'), source.indexOf('\n    })\n    const page='))
fs.mkdirSync(out, {recursive:true})
;(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true})
  const checks = [], errors = []
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1050}})
    await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort())
    await context.addInitScript({content:fixture})
    await context.addInitScript(() => {
      const original = window.go.main.App
      window.go.main.App = new Proxy({}, {get:(_, key) => async (...args) => {
        if (key === 'GetStockKingPreference' && args[0] === 'picks.ai.configId') return '11'
        if (key === 'GetStockKingAIProviders') return [{id:11,name:'隔离测试平台',model:'deepseek-v4-pro',ready:true}]
        if (key === 'GetLatestKingPicks') {
          localStorage.setItem('qa-latest-calls', String(1 + Number(localStorage.getItem('qa-latest-calls'))))
          if (localStorage.getItem('qa-empty')) return {}
        }
        if (key === 'GetKingPicks') {
          const count = 1 + Number(localStorage.getItem('qa-scan-calls'))
          localStorage.setItem('qa-scan-calls', String(count))
          if (args[1] !== true) throw Error('manual refresh must force all groups')
          await new Promise(resolve => setTimeout(resolve, 200))
          if (localStorage.getItem('qa-fail')) throw Error('隔离测试：行情暂不可用')
          const result = await original[key](...args)
          result.adaptive.candidates[0].name = '手动扫描' + count
          return result
        }
        return original[key](...args)
      }})
    })
    const page = await context.newPage()
    page.on('pageerror', error => errors.push(error.message))
    const count = key => page.evaluate(k => Number(localStorage.getItem(k)), key)
    const scanCount = () => count('qa-scan-calls')
    const button = page.getByRole('button', {name:'刷新全部',exact:true})
    await page.goto(origin + '/#/king-picks', {waitUntil:'networkidle'})
    await page.getByText('测试银行', {exact:true}).waitFor()
    assert.equal(await scanCount(), 0)
    assert.equal(await page.getByRole('button', {name:'重扫',exact:true}).count(), 0)
    checks.push('First visit restores an existing snapshot without scanning')
    const latestCalls = await count('qa-latest-calls')
    await page.evaluate(() => { location.hash = '#/ai-advice' })
    await page.getByRole('heading', {name:'AI 研究',exact:true}).waitFor()
    await page.evaluate(() => { location.hash = '#/king-picks' })
    await button.waitFor()
    await page.reload({waitUntil:'networkidle'})
    assert.equal(await scanCount(), 0)
    assert.equal(await count('qa-latest-calls'), latestCalls)
    checks.push('Navigation and restart preserve the displayed scan without fetching newer results')
    await button.click()
    await page.getByText('手动扫描1', {exact:true}).waitFor()
    assert.equal(await scanCount(), 1)
    await page.getByRole('button', {name:'历史',exact:true}).click()
    await page.getByRole('button', {name:'查看',exact:true}).click()
    await page.getByText('历史快照', {exact:true}).waitFor()
    await page.getByRole('button', {name:'返回当前',exact:true}).click()
    await page.getByText('手动扫描1', {exact:true}).waitFor()
    assert.equal(await scanCount(), 1)
    checks.push('One explicit refresh forces both groups; viewing and returning from history does not scan or overwrite it')
    await page.evaluate(() => localStorage.setItem('qa-fail', '1'))
    await button.click()
    await page.getByText('更新失败 · 当前为旧结果', {exact:true}).waitFor()
    assert.equal(await page.getByText('手动扫描1', {exact:true}).count(), 1)
    assert.equal(await scanCount(), 2)
    checks.push('Failed refresh preserves the last successful result')
    await page.evaluate(() => { localStorage.removeItem('stock-king:picks:snapshot:v2.3'); localStorage.removeItem('qa-fail'); localStorage.setItem('qa-empty', '1') })
    await page.reload({waitUntil:'networkidle'})
    await page.getByText('选择模型，点击刷新全部', {exact:true}).waitFor()
    assert.equal(await scanCount(), 2)
    await button.evaluate(el => { el.click(); el.click() })
    await page.getByText('手动扫描3', {exact:true}).waitFor()
    assert.equal(await scanCount(), 3)
    checks.push('Empty state waits for manual refresh; rapid duplicate clicks create one request')
    assert.deepEqual(errors, [])
    await page.locator('.n-spin-body').waitFor({state:'hidden'})
    await page.screenshot({path:path.join(out,'manual-refresh.png'), fullPage:true})
    fs.writeFileSync(path.join(out,'results.json'), JSON.stringify({checks,errors,fixtureOnly:true},null,2))
    console.log(JSON.stringify({checks,errors},null,2))
  } finally { await browser.close() }
})().catch(error => { console.error(error); process.exitCode = 1 })
