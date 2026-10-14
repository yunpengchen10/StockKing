// Tests the built production UI with an isolated Wails fixture; no user data or market calls.
const fs = require('node:fs'), path = require('node:path'), http = require('node:http'), assert = require('node:assert/strict')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(require('node:os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const root = path.join(__dirname, '../desktop/frontend/dist')
const out = path.join(__dirname, '../artifacts/chart-loading-qa')
const fixtureSource = fs.readFileSync(path.join(__dirname, 'research-experience-ui-qa.cjs'), 'utf8').replaceAll('\r\n', '\n')
const fixture = fixtureSource.slice(fixtureSource.indexOf('      window.__qaCalls=[]'), fixtureSource.indexOf('\n    })\n    const page='))
const mime = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2' }
const server = http.createServer((request, response) => {
  const target = path.resolve(root, '.' + decodeURIComponent(new URL(request.url, 'http://localhost').pathname))
  if (!target.startsWith(path.resolve(root) + path.sep) && target !== path.resolve(root)) { response.writeHead(403).end(); return }
  const file = target === path.resolve(root) ? path.join(root, 'index.html') : target
  fs.readFile(file, (error, data) => {
    if (error) { response.writeHead(404).end(); return }
    response.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream'); response.end(data)
  })
})
;(async () => {
  assert(fs.existsSync(path.join(root, 'index.html')), 'Run npm run build first')
  fs.mkdirSync(out, { recursive: true })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${server.address().port}`
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const errors = [], checks = []
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } })
    await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort())
    await context.addInitScript({ content: fixture })
    await context.addInitScript(() => {
      const original = window.go.main.App
      window.__chartCalls = []; window.__chartRequests = []; window.__preferencesResolved = false
      const preferences = new Promise(resolve => { window.__releasePreferences = () => { window.__preferencesResolved = true; resolve('[]') } })
      window.__releaseChart = (index, source, price) => {
        const bars = Array.from({ length: 180 }, (_, i) => ({ day: new Date(Date.UTC(2026, 0, i + 1)).toISOString().slice(0, 10), open: price - 0.1, close: price, high: price + 0.2, low: price - 0.2, volume: 100000 + i, amount: price * (100000 + i) }))
        window.__chartRequests[index].resolve({ data: bars, source })
      }
      window.go.main.App = new Proxy({}, { get: (_, key) => async (...args) => {
        window.__chartCalls.push({ method: String(key), args, at: performance.now() })
        if (key === 'GetDisplayedKingPicks') return { ...await original.GetKingPicks(), displaySnapshotVersion: 1 }
        if (key === 'GetStockKingPreference' && args[0] === 'kline.recentSymbols') return preferences
        if (key === 'GetStockKLineWithFallback') return new Promise(resolve => window.__chartRequests.push({ args, resolve }))
        if (key === 'GetForecast') return { horizon: args[1], available: false, unavailableReason: '隔离测试' }
        return original[key](...args)
      } })
    })
    const page = await context.newPage()
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(origin + '/#/king-picks', { waitUntil: 'networkidle' })
    await page.getByRole('button', { name: '图表', exact: true }).first().click()
    await page.waitForFunction(() => window.__chartRequests.length === 1)
    assert.equal(await page.evaluate(() => window.__preferencesResolved), false)
    assert.equal(await page.evaluate(() => window.__chartCalls.filter(c => ['GetForecast', 'GetResearchNote'].includes(c.method)).length), 0)
    assert(await page.locator('.lw-kline-chart canvas').count() > 0)
    await page.evaluate(() => window.__releaseChart(0, 'fixture-initial', 12))
    await page.locator('.lw-kline-source-tag').filter({ hasText: 'fixture-initial' }).waitFor()
    checks.push('精选图表在偏好读取仍挂起时开始请求并绘制；隐藏笔记/预测调用为 0')

    await page.getByRole('button', { name: '5分', exact: true }).click()
    await page.waitForFunction(() => window.__chartRequests.length === 2)
    await page.getByRole('button', { name: '日K', exact: true }).click()
    await page.waitForFunction(() => window.__chartRequests.length === 3)
    await page.evaluate(() => window.__releaseChart(2, 'fixture-current', 15))
    await page.locator('.lw-kline-source-tag').filter({ hasText: 'fixture-current' }).waitFor()
    await page.evaluate(() => window.__releaseChart(1, 'fixture-stale', 10))
    await page.waitForFunction(() => document.querySelector('.lw-kline-source-tag')?.textContent.trim() === 'fixture-current')
    assert((await page.locator('.lw-kline-crosshair-strip').innerText()).includes('15.00'))
    checks.push('日K→5分→日K 后延迟旧周期结果不会覆盖最新图表')

    const input = page.locator('.search-box input')
    await input.fill('000001.SZ'); await page.getByRole('button', { name: '打开', exact: true }).click()
    await page.waitForFunction(() => window.__chartRequests.length === 4)
    await input.fill('600001.SH'); await page.getByRole('button', { name: '打开', exact: true }).click()
    await page.waitForFunction(() => window.__chartRequests.length === 5)
    await page.evaluate(() => { window.__releaseChart(4, 'fixture-selected', 18); window.__releaseChart(3, 'fixture-old-stock', 9) })
    await page.locator('.lw-kline-source-tag').filter({ hasText: 'fixture-selected' }).waitFor()
    assert((await page.locator('.lw-kline-crosshair-strip').innerText()).includes('18.00'))
    assert.equal(await page.locator('.lw-kline-root').count(), 1)
    assert.equal(await page.evaluate(() => window.__chartCalls.filter(c => ['GetForecast', 'GetResearchNote'].includes(c.method)).length), 0)
    checks.push('快速切换股票后旧请求不能恢复旧图表或覆盖所选股票')

    await page.getByRole('button', { name: '笔记', exact: true }).click()
    await page.waitForFunction(() => window.__chartCalls.filter(c => c.method === 'GetForecast').length === 3)
    assert.equal(await page.evaluate(() => window.__chartCalls.filter(c => c.method === 'GetResearchNote').length), 1)
    await page.evaluate(() => window.__releasePreferences())
    await page.locator('.notebook .n-spin-body').waitFor({ state: 'hidden' })
    await page.screenshot({ path: path.join(out, 'chart-and-on-demand-notebook.png'), fullPage: true })
    checks.push('展开笔记后才读取笔记及 1/5/20 日预测')
    assert.deepEqual(errors, [])
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify({ fixtureOnly: true, productionBuild: true, checks, errors }, null, 2))
    console.log(JSON.stringify({ checks, errors }, null, 2))
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)) }
})().catch(error => { server.close(); console.error(error); process.exitCode = 1 })
