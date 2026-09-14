// Production frontend, isolated Wails bridge, all external requests blocked.
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict')
const {chromium} = require(path.join(require('os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
const origin = process.env.QA_ORIGIN || 'http://127.0.0.1:5174'
const out = path.join(__dirname, '../artifacts/v2.4.4/markdown-qa')
const source = fs.readFileSync(path.join(__dirname, 'ai-workspace-ui-qa.cjs'), 'utf8').replaceAll('\r\n', '\n')
const fixture = source.slice(source.indexOf('    const preferences'), source.indexOf('\n  })\n  const page'))
fs.mkdirSync(out, {recursive:true})
;(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true})
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1100}})
    await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort())
    const raw = JSON.parse(fs.readFileSync(path.join(__dirname, '../tmp/markdown-real-fixture.json'), 'utf8').replace(/^\uFEFF/, '')).raw
    await context.addInitScript(({raw}) => {
      const report = {schema:'stockking.ai-research.v1',stockCode:'600903.SH',source:'api',sourceName:'本机历史报告排版验证',raw,markdown:raw,format:'text',warnings:[]}
      localStorage.setItem('qa-ai-history', JSON.stringify([{ID:1,stockCode:'600903.SH',stockName:'贵州燃气',modelName:'排版测试',CreatedAt:'2026-09-08T11:22:00+08:00',content:JSON.stringify(report)}]))
    }, {raw})
    await context.addInitScript({content:fixture})
    const page = await context.newPage(), errors = [], checks = []
    page.on('pageerror', e => errors.push(e.message))
    await page.goto(origin+'/#/ai-advice?code=600903.SH&name=贵州燃气', {waitUntil:'networkidle'})
    await page.locator('.history-list button').first().click()
    await page.locator('.report-panel .safe-markdown table').waitFor()
    assert.equal(await page.locator('.report-panel .safe-markdown h3').count(), 4)
    assert.equal(await page.locator('.report-panel .safe-markdown').getByText('"markdown":', {exact:true}).count(), 0)
    await page.getByRole('button', {name:'查看原文',exact:true}).click()
    assert.equal(await page.getByLabel('保存的原始报告').inputValue(), raw.replaceAll('\r\n', '\n'))
    assert.equal(await page.evaluate(()=>JSON.parse(window.__qaHistory[0].content).raw), raw)
    await page.getByRole('button', {name:'阅读报告',exact:true}).click()
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', {name:'导出',exact:true}).click()])
    const exported = fs.readFileSync(await download.path(), 'utf8')
    assert.ok(download.suggestedFilename().endsWith('.md'))
    assert.ok(exported.includes('\n### 观察条件简表'))
    assert.ok(!exported.trim().startsWith('{'))
    checks.push('Existing malformed JSON report renders 1 table and 4 headings; exact original remains available')
    checks.push('Markdown export contains the readable document, not the malformed JSON envelope')
    await page.screenshot({path:path.join(out,'existing-report.png'),fullPage:true})
    await page.goto(origin+'/#/research?name=AI分析报告', {waitUntil:'networkidle'})
    await page.getByRole('button',{name:'查看',exact:true}).first().click()
    await page.locator('.safe-report-preview table').waitFor()
    await page.waitForTimeout(400) // Let the modal entrance transition finish.
    checks.push('Research archive renders the same old report without regenerating it')
    await page.screenshot({path:path.join(out,'history-report.png'),fullPage:true,animations:'disabled'})
    assert.equal(await page.evaluate(()=>window.__qaCalls.filter(c=>c.method==='RunStockKingAIResearch').length), 0)
    assert.deepEqual(errors, [])
    fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({checks,errors},null,2))
    console.log(JSON.stringify({checks,errors},null,2))
  } finally { await browser.close() }
})().catch(e=>{console.error(e);process.exitCode=1})
