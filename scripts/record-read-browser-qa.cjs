// Browser regression for native Window timer receiver failures in PicksLedger.
// This mounts the real Vue SFC with Naive UI and an isolated Wails fixture.
// No application data, engine, market requests, or installed files are touched.
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const root = path.resolve(__dirname, '..')
const frontend = path.join(root, 'desktop/frontend')
const output = path.join(root, 'output/recommendation-fix-20261012-illegal-invocation/browser-qa')
const { build } = require(path.join(frontend, 'node_modules/esbuild'))
const { parse, compileScript } = require(path.join(frontend, 'node_modules/@vue/compiler-sfc'))
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(require('node:os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const createFixture = require('./workspace-tour-fixture.cjs')
const fixture = createFixture(JSON.parse(fs.readFileSync(path.join(root, 'docs/research/stockking-v11-market-sample.json'), 'utf8')))
// Review API rows explicitly identify selected samples in the current contract.
fixture.fixed.GetStockKingDelayedReviews.items.forEach(row => { row.selected = true })
const adapter = /setTimeout: \(callback, delay\) => setTimeout\(callback, delay\),\s*clearTimeout: timer => clearTimeout\(timer\),/
const checks = []
const errors = []
let server, browser
async function bundle(legacy) {
  const outfile = path.join(output, legacy ? 'legacy.js' : 'fixed.js')
  await build({
    stdin: { contents: `
      import { createApp, h } from 'vue';
      import naive from 'naive-ui';
      import PicksLedger from './src/components/PicksLedger.vue';
      window.__ledgerApp = createApp({ render: () => h(PicksLedger, { mode: new URLSearchParams(location.search).get('mode') || 'records' }) });
      window.__ledgerApp.use(naive).mount('#app');
    `, resolveDir: frontend, sourcefile: 'record-read-browser-harness.js' },
    bundle: true, format: 'iife', platform: 'browser', outfile,
    define: { 'process.env.NODE_ENV': '"production"', __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
    plugins: [{ name: 'actual-picks-ledger-sfc', setup(api) {
      api.onLoad({ filter: /\.vue$/ }, async args => {
        let source = fs.readFileSync(args.path, 'utf8')
        assert.equal(path.basename(args.path), 'PicksLedger.vue')
        assert.match(source, adapter, 'the browser test requires the fixed production adapter')
        // Reconstruct only the reported bad adapter for a controlled comparison.
        if (legacy) source = source.replace(adapter, 'setTimeout, clearTimeout,')
        const { descriptor, errors: compileErrors } = parse(source, { filename: args.path })
        assert.deepEqual(compileErrors, [])
        return { contents: compileScript(descriptor, { id: 'ledger-browser-qa', inlineTemplate: true }).content, loader: 'js', resolveDir: path.dirname(args.path) }
      })
    } }],
  })
  return outfile
}
function installFixture({ fixed }) {
  window.__ledgerQA = { calls: [], behavior: 'success', failOnce: false }
  window.go = { main: { App: new Proxy({}, { get: (_, key) => (...args) => {
    const state = window.__ledgerQA
    state.calls.push({ method: String(key), args })
    if (key === 'GetStockKingRecommendationHistory') {
      if (state.behavior === 'hang') return new Promise(() => {})
      if (state.behavior === 'reject') return Promise.reject(new Error('隔离测试：桥接读取失败'))
      if (state.behavior === 'empty') return Promise.resolve({ items: [] })
    }
    if (!Object.hasOwn(fixed, key)) return Promise.reject(new Error('Unexpected fixture call: ' + String(key)))
    return Promise.resolve(structuredClone(fixed[key]))
  } }) } }
}
;(async () => {
  fs.mkdirSync(output, { recursive: true })
  await bundle(true)
  await bundle(false)
  server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname
    if (pathname === '/legacy.js' || pathname === '/fixed.js') {
      res.setHeader('Content-Type', 'text/javascript')
      fs.createReadStream(path.join(output, pathname.slice(1))).pipe(res)
      return
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8')
    const script = pathname === '/legacy' ? 'legacy.js' : 'fixed.js'
    res.end('<!doctype html><html><body><div id="app"></div><script src="/' + script + '"></script></body></html>')
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = 'http://127.0.0.1:' + server.address().port
  browser = await chromium.launch({ headless: true, channel: 'msedge' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } })
  await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort())
  await context.addInitScript(installFixture, fixture)
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(origin + '/legacy')
  await page.getByText(/读取失败：Illegal invocation/).waitFor()
  assert.equal(await page.locator('.ledger-group').count(), 0)
  await page.screenshot({ path: path.join(output, 'legacy-illegal-invocation.png'), fullPage: true })
  checks.push('The pre-fix timer adapter reproduces the exact Illegal invocation error in a real Chromium Window, with the same PicksLedger SFC and fixture.')
  const native = await page.evaluate(() => ({
    setTimeout: Function.prototype.toString.call(window.setTimeout),
    clearTimeout: Function.prototype.toString.call(window.clearTimeout),
  }))
  assert.match(native.setTimeout, /\[native code\]/)
  assert.match(native.clearTimeout, /\[native code\]/)
  await page.goto(origin + '/fixed')
  await page.locator('.ledger-group').first().waitFor()
  assert.equal(await page.getByText(/读取失败/).count(), 0)
  const rows = await page.locator('.signal-row').count()
  assert.ok(rows > 0)
  assert.equal(await page.locator('.n-empty').count(), 0, 'loaded records must not also display an empty-state placeholder')
  assert.equal(await page.locator('.n-button--loading').count(), 0)
  checks.push('The fixed real SFC renders recommendation rows with untouched native browser timers and clears the loading indicator.')
  await page.evaluate(() => { window.__ledgerQA.behavior = 'hang' })
  const query = page.getByRole('button', { name: '查询记录', exact: true })
  const start = Date.now()
  await query.click()
  await page.locator('.n-button--loading').first().waitFor()
  await page.getByText(/读取失败：记录读取超时/).waitFor({ timeout: 26000 })
  const elapsed = Date.now() - start
  assert.ok(elapsed >= 19000 && elapsed < 26000, 'the actual 20-second browser deadline must expire')
  assert.equal(await page.locator('.n-button--loading').count(), 0)
  assert.equal(await page.locator('.signal-row').count(), rows)
  assert.equal(await page.locator('.n-empty').count(), 0, 'retained rows after a timeout must not be described as empty')
  await page.screenshot({ path: path.join(output, 'timeout-keeps-records.png'), fullPage: true })
  checks.push('A hung Wails promise times out after the production 20-second deadline, stops spinning, and preserves the already loaded records.')
  await page.evaluate(() => { window.__ledgerQA.behavior = 'success' })
  await query.click()
  await page.getByText(/读取失败/).waitFor({ state: 'hidden' })
  assert.equal(await page.locator('.signal-row').count(), rows)
  assert.equal(await page.locator('.n-button--loading').count(), 0)
  await page.screenshot({ path: path.join(output, 'fixed-success-after-retry.png'), fullPage: true })
  checks.push('The retry button succeeds after a timed-out bridge request, clears the error, and displays the records.')
  await page.evaluate(() => { window.__ledgerQA.behavior = 'empty' })
  await query.click()
  await page.locator('.n-empty').waitFor()
  assert.equal(await page.locator('.signal-row').count(), 0)
  await page.screenshot({ path: path.join(output, 'empty-records-state.png'), fullPage: true })
  checks.push('Loaded or timeout-retained records have no empty placeholder; a successful empty response displays the empty state and removes rows.')
  await page.goto(origin + '/fixed?mode=reviews')
  await page.locator('.review-table').first().waitFor()
  assert.equal(await page.getByText(/读取失败/).count(), 0)
  await page.goto(origin + '/fixed?mode=learning')
  await page.locator('.learning-header').waitFor()
  assert.equal(await page.getByText(/读取失败/).count(), 0)
  checks.push('Delayed review and learning-state modes also load through the corrected shared timeout adapter.')
  assert.deepEqual(errors, [])
  const result = { status: 'passed', browser: await browser.version(), fixtureOnly: true, nativeTimersUnmodified: true, sourceComponent: 'desktop/frontend/src/components/PicksLedger.vue', sourceSha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(frontend, 'src/components/PicksLedger.vue'))).digest('hex'), timeoutObservedMs: elapsed, checks, errors }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
})().catch(error => { console.error(error); process.exitCode = 1 }).finally(async () => {
  if (browser) await browser.close()
  if (server) await new Promise(resolve => server.close(resolve))
})
