// Isolated startup shell test: no Go bridge, API keys or remote services.
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(require('node:os').homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))
;(async () => {
  const html = fs.readFileSync(path.join(__dirname, '../desktop/frontend/index.html'), 'utf8')
    .replace(/<link[^>]+>/g, '')
    .replace(/<script src="[^\"]+" type="module"><\/script>/, '')
  const browser = await chromium.launch({ headless: true, channel: 'msedge' })
  try {
    const page = await browser.newPage()
    await page.setContent(html)
    await page.evaluate(() => window.dispatchEvent(new Event('error')))
    assert.match(await page.locator('#startup-message').innerText(), /界面加载失败/)
    assert.equal(await page.locator('#startup-retry').isVisible(), true)
    await page.evaluate(() => window.stockKingStartupComplete())
    assert.equal(await page.locator('#startup-status').count(), 0)
    await page.evaluate(() => window.dispatchEvent(new Event('error')))
    assert.equal(await page.locator('#startup-status').count(), 0)
    console.log('PASS: startup errors show recovery; successful startup removes overlay; later errors do not restore it')
  } finally { await browser.close() }
})().catch(error => { console.error(error); process.exitCode = 1 })
