// Exercise the built Vue application against an isolated Wails bridge.
// Fixtures are UI test data, never market predictions or user records.
const fs = require('node:fs'), path = require('node:path'), http = require('node:http')
const assert = require('node:assert/strict')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(require('node:os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const createFixture = require('./workspace-tour-fixture.cjs')
const root = path.resolve(__dirname, '..'), build = path.join(root, 'desktop/frontend/dist')
const output = path.resolve(root, process.env.QA_OUTPUT_DIR || 'output/validation/navigation-retention')
const fixture = createFixture(JSON.parse(fs.readFileSync(path.join(root, 'docs/research/stockking-v11-market-sample.json'), 'utf8')))
const researchRecord = fixture.fixed.GetStockKingRecommendationHistory.items[0]
researchRecord.selected = false
researchRecord.nextDaySelected = true
researchRecord.snapshot.nextDaySignal = { selected:true, version:'next-day-limit-watch-v2', queue:'first_board', queueLabel:'未触板潜伏', touchedLimitToday:false, score:71,
  signalSession:'2026-09-29', targetSession:'2026-09-30', probability:null,
  reasons:['隔离测试：次日观察记录'], gaps:['事件覆盖待补'], scoreMeaning:'未验证的研究排序分',
  features:{ touchedLimitToday:false, changePct:3, limitDistancePct:6.7961 } }
fixture.fixed.GetStockKingDelayedReviews.nextDayReview = { items:[{
  signalId:researchRecord.signalId, code:researchRecord.code, selected:true,
  targetSession:'2026-09-30', status:'incomplete', touchLimit:null, closeAtLimit:null,
  reason:'隔离测试：目标日分钟未齐全',
}] }
const mime = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.png':'image/png', '.woff2':'font/woff2' }
const server = http.createServer((req, res) => {
  const file = path.resolve(build, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname))
  if (file !== build && !file.startsWith(build + path.sep)) { res.writeHead(403); res.end(); return }
  const target = file === build ? path.join(build, 'index.html') : file
  if (!fs.existsSync(target) || !fs.statSync(target).isFile()) { res.writeHead(404); res.end(); return }
  res.setHeader('Content-Type', mime[path.extname(target)] || 'application/octet-stream')
  fs.createReadStream(target).pipe(res)
})
;(async () => {
  fs.mkdirSync(output, { recursive:true })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${server.address().port}`
  const browser = await chromium.launch({ headless:true, channel:'msedge' })
  const checks = [], errors = []
  try {
    const context = await browser.newContext({ viewport:{ width:1440, height:850 } })
    await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort())
    await context.addInitScript(createFixture.install, fixture)
    await context.addInitScript(({ fixed }) => {
      window.__retention = { calls:[], scan:0, fail:false, quotePrice:99 }
      const original = window.go.main.App
      const makeSnapshot = () => {
        const value = structuredClone(fixed.GetDisplayedKingPicks)
        const pick = value.adaptive.candidates[0]
        pick.name = '手动刷新测试' + window.__retention.scan
        pick.currentChange = 3
        pick.nextDaySignal = { version:'next-day-limit-watch-v2', queue:'first_board', queueLabel:'未触板潜伏', score:72, probability:null,
          signalSession:'2026-10-08', targetSession:'2026-10-09', branches:['平台突破蓄势'],
          reasons:['隔离UI测试：当日高位承接'], gaps:['缺少20日同刻样本，保留观察'],
          scoreMeaning:'未验证的研究排序分，不是涨停概率', selected:true, touchedLimitToday:false,
          features:{ touchedLimitToday:false, changePct:3, limitDistancePct:6.7961 } }
        const continuation = structuredClone(pick)
        continuation.code = '600099'
        continuation.name = '已触板延续测试' + window.__retention.scan
        continuation.currentChange = 10
        continuation.nextDaySignal = { ...continuation.nextDaySignal, queue:'continuation', queueLabel:'已触板延续', touchedLimitToday:true,
          branches:['触板形态延续（10%价位）'], features:{ touchedLimitToday:true, changePct:10, limitDistancePct:0 } }
        value.adaptive.nextDayWatchlist = [pick]
        value.adaptive.nextDayContinuationWatchlist = [continuation]
        value.adaptive.nextDayResearch = { version:'next-day-limit-watch-v2', selectedCount:1, count:2,
          firstBoardCount:1, continuationCount:1, continuationSelectedCount:1, status:'unvalidated_rules' }
        value.adaptive.message = '隔离界面测试数据，不是实际推荐。'
        value.adaptive.candidates = []
        value.candidateCount = 0
        value.tradeCandidateCount = 0
        value.nextDayCandidateCount = 1
        value.continuationCandidateCount = 1
        value.savedRecommendationCount = 1
        value.displaySnapshotVersion = 1
        value.displaySource = 'manual'
        return value
      }
      window.go.main.App = new Proxy({}, { get:(_, key) => async (...args) => {
        const state = window.__retention
        state.calls.push({ method:String(key), args:structuredClone(args) })
        if (key === 'StartKingPicksRefresh') { state.scan++; return { task_id:'manual-' + state.scan, status:'pending' } }
        if (key === 'GetKingPicksRefreshTask') return state.fail
          ? { status:'failed', error:'测试请求失败，保留原名单' }
          : { status:'completed', progress:100, result:makeSnapshot() }
        if (key === 'GetKingPicksHistory') {
          const history = makeSnapshot().adaptive
          history.nextDayWatchlist[0].name = '历史次日观察测试'
          return { items:[{ run_id:'isolated-history', mode:'king_live', result:history }] }
        }
        if (key === 'GetStockKingRecordQuotes') return { quotes:Object.fromEntries(args[0].map(code => [String(code).replace(/\D/g, '').slice(0, 6), {
          price:state.quotePrice, sourceTime:new Date().toISOString(), source:'UI fixture', status:'fresh',
        }])) }
        return original[key](...args)
      } })
    }, fixture)
    const page = await context.newPage()
    page.on('pageerror', error => errors.push(error.message))
    const calls = method => page.evaluate(key => window.__retention.calls.filter(call => call.method === key).length, method)
    const sidebar = name => page.locator('.sk-primary-nav').getByRole('link', { name, exact:true })
    const tab = name => page.locator('.view-tabs').getByText(name, { exact:true })
    await page.goto(origin + '/#/king-picks', { waitUntil:'networkidle' })
    await page.locator('.pick-grid').first().waitFor()
    assert.equal(await calls('StartKingPicksRefresh'), 0)
    const originalNames = await page.locator('.pick-title strong').allTextContents()
    const displayReads = await calls('GetDisplayedKingPicks')
    await tab('推荐记录').click()
    await page.locator('.ledger-group').first().waitFor()
    await page.locator('.signal-row').getByText('未触板潜伏观察', { exact:true }).first().waitFor()
    const firstHistoryReads = await calls('GetStockKingRecommendationHistory')
    await page.locator('.ledger-filters input[type="date"]').fill('2026-09-29')
    await page.locator('.ledger-filters input').nth(1).fill('600')
    await page.getByRole('button', { name:'查询记录', exact:true }).click()
    await page.waitForFunction(n => window.__retention.calls.filter(c => c.method === 'GetStockKingRecommendationHistory').length === n, firstHistoryReads + 1)
    const recordsReads = await calls('GetStockKingRecommendationHistory')
    await page.locator('.signal-row summary').first().click()
    await page.locator('.ledger-group').first().getByRole('button', { name:'图表', exact:true }).click()
    await page.waitForURL(/kline-analysis/)
    const chartUrl = page.url()
    await sidebar('精选').click()
    await page.locator('.ledger-group').first().waitFor()
    assert.equal(await calls('GetStockKingRecommendationHistory'), recordsReads)
    assert.equal(await page.locator('.ledger-filters input[type="date"]').inputValue(), '2026-09-29')
    assert.equal(await page.locator('.ledger-filters input').nth(1).inputValue(), '600')
    assert.equal(await page.locator('.signal-row').first().getAttribute('open'), '')
    checks.push('图表往返保留推荐记录子页、过滤条件、展开行；不重读名单')
    await sidebar('图表').click()
    await page.waitForURL(chartUrl)
    await sidebar('精选').click()
    checks.push('侧栏图表恢复上次股票查询参数')
    await tab('延后复盘').click()
    await page.locator('.review-table').first().waitFor()
    await page.locator('.next-day-reviews').getByText('证据待补', { exact:true }).waitFor()
    assert.deepEqual(await page.locator('.next-day-reviews tbody tr').first().locator('td').allTextContents().then(cells => cells.slice(4, 7)), ['—', '—', '—'])
    checks.push('次日观察默认列入记录；复盘缺证据保持未知，不冒充预测失败')
    await tab('学习状态').click()
    await page.locator('.learning-header').waitFor()
    await tab('推荐记录').click()
    await page.locator('.ledger-group').first().waitFor()
    assert.equal(await calls('GetStockKingRecommendationHistory'), recordsReads)
    assert.equal(await page.locator('.signal-row').first().getAttribute('open'), '')
    const quoteReads = await calls('GetStockKingRecordQuotes')
    await page.evaluate(() => { window.__retention.quotePrice = 101 })
    await page.getByRole('button', { name:'刷新报价', exact:true }).click()
    await page.waitForFunction(n => window.__retention.calls.filter(c => c.method === 'GetStockKingRecordQuotes').length > n, quoteReads)
    assert.equal(await calls('GetStockKingRecommendationHistory'), recordsReads)
    checks.push('记录、复盘、学习互切保存各自状态；刷新报价不重查推荐')
    await tab('本地精选').click()
    assert.deepEqual(await page.locator('.pick-title strong').allTextContents(), originalNames)
    assert.equal(await calls('GetDisplayedKingPicks'), displayReads)
    await page.getByRole('button', { name:'刷新扫描', exact:true }).click()
    await page.getByText('手动刷新测试1', { exact:true }).waitFor()
    assert.equal(await calls('StartKingPicksRefresh'), 1)
    assert.equal(await page.locator('.next-day-card').count(), 1)
    assert.equal(await page.locator('.tier-adaptive').count(), 0)
    await page.locator('.saved-recommendations').getByText('已保存研究推荐 · 1 只', { exact:true }).waitFor()
    await page.getByText(/未触板潜伏\s*1\s*只.*交易条件候选\s*0\s*只/).waitFor()
    assert.equal(await page.locator('.next-day-primary .next-day-card').count(), 1)
    assert.equal(await page.locator('.next-day-primary .pick-quote').getByText('+3.00%', { exact:true }).count(), 1)
    assert.equal(await page.locator('.next-day-continuation-card').first().isVisible(), false)
    await page.getByRole('button', { name:'展开延续观察', exact:true }).click()
    assert.equal(await page.locator('.next-day-continuation-card').first().isVisible(), true)
    await page.locator('.next-day-continuation-card').getByText(/扫描时已触板/).waitFor()
    await page.getByRole('button', { name:'收起延续观察', exact:true }).click()
    checks.push('普通涨幅潜伏默认可见并计入已保存推荐；触板延续单列折叠且不占主推荐数')
    await page.locator('.next-day-card summary').click()
    await page.locator('.next-day-primary').getByText('缺少20日同刻样本，保留观察', { exact:false }).waitFor()
    checks.push('正式交易候选为零时仍展示独立次日观察候选、目标日与缺口')
    await tab('推荐记录').click()
    await page.getByRole('button', { name:'扫描历史', exact:true }).click()
    await page.locator('.history-head').getByText(/研究推荐\s*1\s*只.*潜伏\s*1\s*只.*交易候选\s*0\s*只.*延续\s*1\s*只/).waitFor()
    await page.locator('.history-candidates').getByText(/历史次日观察测试/).waitFor()
    await page.locator('.history-head').getByRole('button', { name:'查看', exact:true }).click()
    await page.getByText('历史次日观察测试', { exact:true }).waitFor()
    await page.getByRole('button', { name:'返回当前', exact:true }).click()
    await page.getByText('手动刷新测试1', { exact:true }).waitFor()
    assert.equal(await calls('StartKingPicksRefresh'), 1)
    checks.push('扫描历史区分两类候选；从记录查看历史与返回当前均不重扫')
    const readsBeforeExplicitRecords = await calls('GetStockKingRecommendationHistory')
    await page.getByRole('button', { name:'查看最新推荐记录', exact:true }).click()
    await page.waitForFunction(n => window.__retention.calls.filter(c => c.method === 'GetStockKingRecommendationHistory').length === n + 1, readsBeforeExplicitRecords)
    assert.equal(await page.locator('.ledger:visible .ledger-filters input[type="date"]').inputValue(), '')
    assert.equal(await page.locator('.ledger:visible .ledger-filters input').nth(1).inputValue(), '')
    await tab('本地精选').click()
    assert.equal(await calls('StartKingPicksRefresh'), 1)
    checks.push('显式查看最新推荐清除旧记录筛选并只查询一次；普通返回仍不换名单')
    await page.evaluate(() => { window.__retention.fail = true })
    await page.getByRole('button', { name:'刷新扫描', exact:true }).click()
    await page.getByText('测试请求失败，保留原名单', { exact:false }).waitFor()
    assert.equal(await page.getByText('手动刷新测试1', { exact:true }).count(), 1)
    checks.push('仅手动刷新替换名单；刷新失败继续显示上次成功名单')
    assert.deepEqual(errors, [])
    await page.screenshot({ path:path.join(output, 'retained-picks.png'), fullPage:true })
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ checks, errors, fixtureOnly:true }, null, 2))
    console.log(JSON.stringify({ checks, errors, fixtureOnly:true }, null, 2))
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)) }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1 })
