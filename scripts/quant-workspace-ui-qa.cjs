// Local browser interaction QA only. All Wails responses are explicit fixtures.
// This script never runs Python training, an AI API, or the production database.
// Run against an existing Vite server; it neither starts nor stops that server.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || require('path').join(require('os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const out = path.join(__dirname, '../artifacts/v2.4.0/quant-workspace-qa')
fs.mkdirSync(out, { recursive: true })

;(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1 })
  const checks = [], errors = []
  let page, failure = null
  await context.addInitScript(() => {
    // Match Wails' JSON serialization boundary, including Vue reactive proxies.
    const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
    const calls = []
    const reports = JSON.parse(localStorage.getItem('qa-quant-reports') || '{}')
    const tasks = []
    window.__qaCalls = calls
    window.__qaQuantReports = reports
    const fixture = {
      dataset_name: 'QA 合成演示 · 非策略业绩', provenance: 'synthetic_demo', point_in_time_confirmed: true,
      calendar_source: 'QA synthetic calendar', price_basis: 'unadjusted', benchmark_name: 'QA 合成基准',
      calendar: ['2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06', '2026-08-07'],
      signals: [{ signal_id: 'qa-signal', symbol: '600000.SH', signal_date: '2026-08-03', available_at: '2026-08-03T16:00:00+08:00', source: 'QA synthetic fixture', side: 'buy' }],
      bars: ['2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06', '2026-08-07'].map(date => ({ symbol: '600000.SH', date, open: 10, high: 10.5, low: 9.5, close: 10, volume: 1000000, limit_up: 11, limit_down: 9 })),
      benchmark: ['2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06', '2026-08-07'].map(date => ({ date, close: 1000 })),
      config: { initial_cash: 100000, delay_sessions: 0, holding_sessions: 1, position_weight: 0.2, max_positions: 5, commission_rate: 0.0003, minimum_commission: 5, stamp_tax_rate: null, transfer_fee_rate: 0.00001, slippage_bps: 10, stop_loss_pct: null, take_profit_pct: null }
    }
    function makeResult(input) {
      const delayed = input.config.delay_sessions > 0
      const net = delayed ? 0.0045 : 0.0123
      const reportId = (delayed ? 'b' : 'a').repeat(64)
      return {
        engine_version: 'QA-MOCK-NOT-A-BACKTEST', input_sha256: reportId, report_id: reportId,
        report_path: `QA fixture only / ${reportId}.json`, saved_at: '2026-09-07T10:00:00Z',
        dataset_name: input.dataset_name, provenance: 'synthetic_demo', performance_kind: 'synthetic_demo', verified_live_performance: false,
        config: clone(input.config), benchmark_name: 'QA 合成基准', calendar_source: 'QA synthetic calendar',
        summary: { from: '2026-08-03', through: '2026-08-07', sessions: 5, initial_cash: 100000,
          final_equity: 100000 * (1 + net), net_return: net, benchmark_return: 0, excess_return: net,
          max_drawdown: -0.002, total_fees: 20, realized_pnl: 100000 * net, closed_trades: 1,
          win_rate: 1, entry_fill_rate: 1, open_positions: 0, unrecovered_drawdown: false, longest_drawdown_sessions: 1 },
        equity_curve: fixture.calendar.map((date, i) => ({ date, cash: 100000 * (1 + net * i / 4), market_value: 0,
          equity: 100000 * (1 + net * i / 4), nav: 1 + net * i / 4, benchmark_nav: 1, drawdown: i === 2 ? -0.002 : 0, positions: 0 })),
        trades: [{ date: delayed ? '2026-08-05' : '2026-08-04', symbol: '600000.SH', side: 'buy', quantity: 100,
          price: 10, notional: 1000, fees: { total: 5 }, cash_after: 98995, signal_id: 'qa-signal', reason: 'recorded_signal' }],
        closed_positions: [], rejected_orders: [], open_positions: [],
        limitations: ['QA 显式模拟响应，仅验证操作流程，不代表引擎计算或策略表现。']
      }
    }
    const implementations = {
      GetConfig: () => ({ darkTheme: true, aiConfigs: [], AiConfigs: [] }),
      GetEngineStatus: () => ({ ready: true, state: 'ready', message: 'QA fixture only' }),
      IsTradingTime: () => false, IsHKTradingTime: () => false, IsUSTradingTime: () => false,
      GetModelMetrics: () => ({ models: {}, modelCount: 0, tiers: Object.fromEntries(['conservative', 'regular', 'aggressive'].map(key => [key, { qualified: false, reason: 'qa_not_validated', metadata: {}, metrics: {} }])) }),
      ListQuantTasks: () => ({ tasks: clone(tasks) }),
      ListExecutableBacktests: () => ({ reports: Object.values(reports).map(row => ({ report_id: row.result.report_id,
        dataset_name: row.result.dataset_name, saved_at: row.result.saved_at, performance_kind: row.result.performance_kind,
        summary: row.result.summary, config: row.result.config })) }),
      GetExecutableBacktestTemplate: () => ({ template: clone(fixture), schema: {} }),
      GetExecutableBacktestReport: id => {
        if (!reports[id]) throw new Error('QA report not found')
        return clone(reports[id])
      },
      RunExecutableBacktest: input => {
        const result = makeResult(input)
        reports[result.report_id] = { input: clone(input), result: clone(result) }
        localStorage.setItem('qa-quant-reports', JSON.stringify(reports))
        return result
      },
      StartQuantTrainingScoped: (symbols, objectives) => {
        const task = { task_id: `qa-task-${tasks.length + 1}`, status: 'completed', progress: 100,
          message: 'QA mock only; no real training performed', request: { symbol_count: symbols.length, objectives: clone(objectives) } }
        tasks.unshift(task)
        return { task_id: task.task_id, status: 'completed', deduplicated: false }
      },
      PauseQuantTask: id => ({ task_id: id, status: 'paused' }),
      ResumeQuantTask: id => ({ task_id: id, status: 'completed' })
    }
    window.go = { main: { App: Object.fromEntries(Object.entries(implementations).map(([method, handler]) => [method, async (...args) => {
      calls.push({ method, args: clone(args) })
      return clone(await handler(...args))
    }])) } }
    window.runtime = new Proxy({}, { get: (_, key) => key === 'EventsOnMultiple' ? (event, callback) => {
      if (event === 'loadingMsg') setTimeout(() => callback('done'), 50)
      return () => {}
    } : () => {} })
  })
  try {
    page = await context.newPage()
    page.setDefaultTimeout(12000)
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('http://127.0.0.1:5173/#/quant-models', { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '策略实验室', exact: true }).waitFor()
    await page.waitForFunction(() => window.__qaCalls.filter(call => call.method === 'GetModelMetrics').length >= 2)
    const startupCalls = await page.evaluate(() => window.__qaCalls.map(call => call.method))
    assert.equal(startupCalls.some(method => /^(Start|Run|Train)/.test(method)), false)
    assert.equal(startupCalls.some(method => /AI|Research|Chat|LLM/i.test(method)), false)
    assert.equal(await page.getByRole('button', { name: '运行回测', exact: true }).isDisabled(), true)
    await page.getByRole('heading', { name: '待导入数据', exact: true }).waitFor()
    checks.push({ name: 'default_read_only', passed: true, evidence: 'Opened page and waited for a complete refresh; no training, backtest or AI calls', calls: startupCalls })
    await page.screenshot({ path: path.join(out, '01-default.png'), fullPage: true, animations: 'disabled' })

    await page.locator('input[type=file][accept=".json,application/json"]').setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{ invalid JSON') })
    await page.locator('.n-message').filter({ hasText: '导入失败' }).waitFor()
    await page.waitForFunction(() => {
      const message = [...document.querySelectorAll('.n-message')].find(item => item.textContent.includes('导入失败'))
      if (!message) return false
      for (let node = message; node; node = node.parentElement) if (Number(getComputedStyle(node).opacity) < 0.99) return false
      const box = message.getBoundingClientRect()
      return box.top >= 0 && box.bottom <= innerHeight && box.left >= 0 && box.right <= innerWidth
    })
    assert.equal(await page.getByRole('button', { name: '运行回测', exact: true }).isDisabled(), true)
    assert.equal(await page.evaluate(() => window.__qaCalls.filter(call => call.method === 'RunExecutableBacktest').length), 0)
    checks.push({ name: 'invalid_json_rejected', passed: true, evidence: 'Actual file input reports parse error and cannot run a backtest' })
    await page.screenshot({ path: path.join(out, '02-invalid-json.png'), fullPage: true, animations: 'disabled' })
    await page.locator('.n-message').filter({ hasText: '导入失败' }).waitFor({ state: 'hidden' })

    await page.getByRole('button', { name: '演示', exact: true }).click()
    await page.getByText('合成演示 · 非业绩', { exact: true }).waitFor()
    assert.equal(await page.evaluate(() => window.__qaCalls.filter(call => call.method === 'RunExecutableBacktest').length), 0)
    await page.getByRole('button', { name: '对照延迟 +1 日', exact: true }).click()
    await page.locator('.comparison').waitFor()
    await page.getByText('合成演示，不代表策略收益。', { exact: true }).waitFor()
    const runs = await page.evaluate(() => window.__qaCalls.filter(call => call.method === 'RunExecutableBacktest'))
    assert.deepEqual(runs.map(call => call.args[0].config.delay_sessions), [0, 1])
    assert.equal(runs[0].args[0].provenance, 'synthetic_demo')
    assert.equal(runs[0].args[0].point_in_time_confirmed, true)
    assert.ok((await page.locator('.stat-grid').innerText()).includes('1.23%'))
    assert.ok((await page.locator('.comparison').innerText()).includes('0.45%'))
    await page.locator('.equity-chart canvas').waitFor()
    checks.push({ name: 'synthetic_demo_and_delay_comparison', passed: true, evidence: 'Demo does not auto-run; explicit action sends delays 0 and 1 and displays both mock returns plus chart', mockOnly: true })
    await page.screenshot({ path: path.join(out, '03-demo-comparison.png'), fullPage: true, animations: 'disabled' })

    await page.locator('.n-tabs-tab').filter({ hasText: /^模型验证$/ }).click()
    const symbolInput = page.getByPlaceholder('股票代码，逗号或换行分隔')
    const makeSymbols = count => Array.from({ length: count }, (_, i) => `${600000 + i}.SH`)
    await symbolInput.fill(makeSymbols(19).join(','))
    await page.getByRole('button', { name: '开始训练', exact: true }).click()
    await page.locator('.n-message').filter({ hasText: '20–200' }).waitFor()
    await symbolInput.fill(makeSymbols(201).join('\n'))
    await page.getByRole('button', { name: '开始训练', exact: true }).click()
    assert.equal(await page.evaluate(() => window.__qaCalls.filter(call => call.method === 'StartQuantTrainingScoped').length), 0)
    await page.getByRole('checkbox', { name: '波段', exact: true }).uncheck()
    await page.getByRole('checkbox', { name: '稳健', exact: true }).check()
    await page.getByRole('checkbox', { name: '短期', exact: true }).check()
    await symbolInput.fill(makeSymbols(20).join('，'))
    await page.getByRole('button', { name: '开始训练', exact: true }).click()
    await page.waitForFunction(() => window.__qaCalls.filter(call => call.method === 'StartQuantTrainingScoped').length === 1)
    await page.getByRole('button', { name: '开始训练', exact: true }).waitFor({ state: 'visible' })
    await symbolInput.fill(makeSymbols(200).join('\n'))
    await page.getByRole('button', { name: '开始训练', exact: true }).click()
    await page.waitForFunction(() => window.__qaCalls.filter(call => call.method === 'StartQuantTrainingScoped').length === 2)
    const training = await page.evaluate(() => window.__qaCalls.filter(call => call.method === 'StartQuantTrainingScoped'))
    assert.deepEqual(training.map(call => call.args[0].length), [20, 200])
    assert.deepEqual(training[0].args[1], ['conservative', 'aggressive'])
    assert.deepEqual(training[1].args[1], ['conservative', 'aggressive'])
    assert.equal(await page.getByText('未验证 / 未通过', { exact: true }).count(), 3)
    checks.push({ name: 'training_scope_and_objectives', passed: true, evidence: '19/201 rejected without bridge call; 20/200 accepted with only conservative/aggressive; failed model status remains visible', mockOnly: true })
    await page.screenshot({ path: path.join(out, '04-training-scope.png'), fullPage: true, animations: 'disabled' })

    await page.reload({ waitUntil: 'networkidle' })
    await page.locator('.saved-reports .n-select').waitFor()
    await page.locator('.saved-reports .n-select').click()
    await page.locator('.n-base-select-option').filter({ hasText: '延迟 1 日' }).click()
    await page.locator('.saved-reports').getByRole('button', { name: '打开', exact: true }).click()
    await page.locator('.result-heading').waitFor()
    await page.locator('.equity-chart canvas').waitFor()
    assert.ok((await page.locator('.result-heading').innerText()).includes('延迟 1 日'))
    assert.ok((await page.locator('.stat-grid').innerText()).includes('0.45%'))
    assert.equal(await page.evaluate(() => window.__qaCalls.filter(call => call.method === 'RunExecutableBacktest').length), 0)
    const reopened = await page.evaluate(() => window.__qaCalls.filter(call => call.method === 'GetExecutableBacktestReport'))
    assert.deepEqual(reopened[0].args, ['b'.repeat(64)])
    const delayInput = page.locator('.field-grid > label').filter({ hasText: '延迟 / 交易日' }).locator('input')
    assert.equal(await delayInput.inputValue(), '1')
    checks.push({ name: 'saved_report_restores_after_reload', passed: true, evidence: 'History survives isolated mock reload; selecting saved delay-1 report restores input, summary and chart without rerunning', mockOnly: true })
    await page.screenshot({ path: path.join(out, '05-restored-report.png'), fullPage: true, animations: 'disabled' })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    assert.deepEqual(errors, [])
  } catch (error) {
    failure = { message: error.message, stack: error.stack }
    if (page) failure.diagnostics = await page.evaluate(() => ({ calls: window.__qaCalls, body: document.body.innerText.slice(-6000) })).catch(() => null)
    if (page) await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {})
  } finally {
    const report = { mockOnly: true, scope: 'Real browser UI interactions with isolated explicit Wails fixtures; does not validate real engine accuracy, live execution, or database persistence.', checks, errors, failure }
    if (!failure && !errors.length) fs.rmSync(path.join(out, 'failure.png'), { force: true })
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify(report, null, 2))
    await browser.close()
    console.log(JSON.stringify({ passed: checks.length, checks, errors, failure }, null, 2))
    if (failure || errors.length) process.exitCode = 1
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
