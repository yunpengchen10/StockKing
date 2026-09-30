// Render the current production Vue UI with an isolated, offline Wails bridge.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const assert = require('node:assert/strict');
const createFixture = require('./workspace-tour-fixture.cjs');
const root = path.resolve(__dirname, '..');
const frontend = path.join(root, 'desktop/frontend');
const out = path.join(root, 'artifacts/workspace-tour');
const build = path.join(out, 'build');
const runtime = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies');

function encodeTour(source, destination) {
  const python = process.env.TOUR_PYTHON || (fs.existsSync(path.join(runtime, 'python/python.exe')) ? path.join(runtime, 'python/python.exe') : 'python');
  const encoded = spawnSync(python, [path.join(__dirname, 'encode-workspace-tour.py'), source], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
  if (encoded.error) throw encoded.error;
  if (encoded.status !== 0) throw Error(`Tour encoding failed: ${encoded.stderr}`);
  const { files, ...encoding } = JSON.parse(encoded.stdout.trim());
  const expectedMedia = new Set(['stockking-workspace-tour.gif', 'stockking-workspace-tour.png']);
  const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
  for (const file of files) {
    assert.equal(path.basename(file.name), file.name, 'Encoded filename must be a basename');
    assert(['media', 'review'].includes(file.target), 'Unknown encoded output target');
    if (file.target === 'media') assert(expectedMedia.delete(file.name), 'Unexpected or repeated media output');
    else assert(/^review-[\w.-]+\.png$/.test(file.name), 'Unexpected review output');
    const bytes = Buffer.from(file.base64, 'base64');
    const signature = file.name.endsWith('.gif') ? Buffer.from('GIF89a') : Buffer.from('89504e470d0a1a0a', 'hex');
    assert(bytes.subarray(0, signature.length).equals(signature), `Invalid image signature: ${file.name}`);
    assert.equal(bytes.length, file.bytes, `Encoded byte count mismatch: ${file.name}`);
    assert.equal(sha256(bytes), file.sha256, `Encoded hash mismatch: ${file.name}`);
    if (file.name.endsWith('.gif')) assert(bytes.length <= 5 * 1024 * 1024, 'GIF is larger than 5 MiB');
    const directory = file.target === 'media' ? destination : source;
    fs.mkdirSync(directory, { recursive: true });
    const outputPath = path.join(directory, file.name);
    fs.writeFileSync(outputPath, bytes);
    const written = fs.readFileSync(outputPath);
    assert(written.subarray(0, signature.length).equals(signature), `Written image signature changed: ${file.name}`);
    assert.equal(written.length, file.bytes, `Written byte count changed: ${file.name}`);
    assert.equal(sha256(written), file.sha256, `Written hash changed: ${file.name}`);
  }
  assert.equal(expectedMedia.size, 0, 'Encoder omitted a required media file');
  encoding.outputs = files.filter(file => file.target === 'media').map(({ name, bytes, sha256 }) => ({ name, bytes, sha256 }));
  return encoding;
}

const encodeOnlyIndex = process.argv.indexOf('--encode-only');
if (encodeOnlyIndex !== -1) {
  const source = process.argv[encodeOnlyIndex + 1], destination = process.argv[encodeOnlyIndex + 2];
  if (!source || !destination) throw Error('Usage: --encode-only <frames-directory> <output-directory>');
  console.log(JSON.stringify(encodeTour(path.resolve(source), path.resolve(destination)), null, 2));
  process.exit(0);
}

let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch { playwright = require(path.join(runtime, 'node/node_modules/playwright')); }
const samplePath = path.join(root, 'docs/research/stockking-v11-market-sample.json');
const sample = JSON.parse(fs.readFileSync(samplePath, 'utf8'));
const fixture = createFixture(sample);
fs.mkdirSync(out, { recursive: true });
if (!process.argv.includes('--skip-build')) {
  const result = spawnSync(process.execPath, [path.join(frontend, 'node_modules/vite/bin/vite.js'), 'build', '--outDir', build, '--emptyOutDir'], { cwd: frontend, encoding: 'utf8' });
  fs.writeFileSync(path.join(out, 'build.log'), result.stdout + result.stderr);
  if (result.status !== 0) throw Error(`Frontend build failed. See ${out}/build.log`);
}
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.woff': 'font/woff' };
const server = http.createServer((req, res) => {
  const requestPath = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  const file = path.resolve(build, `.${requestPath === '/' ? '/index.html' : requestPath}`);
  if (!file.startsWith(build + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
});
const steps = [
  ['自选观察', '按研究方向分组，保留报价时间'],
  ['图表研究', '从自选进入 K 线，打开指标解释'],
  ['本地精选', '查看候选、历史时点与证据覆盖'],
  ['入选依据', '展开原因、风险与失效条件'],
  ['推荐记录', '保留每轮信号，回看原始快照'],
  ['延后复盘', '区分观察与模拟，缺证据就留空'],
];
;(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await playwright.chromium.launch({ headless: true, ...(process.env.TOUR_BROWSER_CHANNEL === 'chromium' ? {} : { channel: process.env.TOUR_BROWSER_CHANNEL || 'msedge' }) });
  const errors = [], blocked = [], frames = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 780 }, deviceScaleFactor: 1, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      blocked.push(route.request().url()); return route.abort();
    });
    await context.addInitScript(createFixture.install, fixture);
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    let step = 0, pointer = null;
    const snap = async (label, duration = 1800, click = false) => {
      const file = `${String(frames.length).padStart(3, '0')}-${label}.png`;
      await page.screenshot({ path: path.join(out, file), animations: 'disabled' });
      frames.push({ file, step, title: steps[step][0], subtitle: steps[step][1], duration, pointer, click });
    };
    const click = async (locator, label, duration = 1800) => {
      await locator.scrollIntoViewIfNeeded();
      const box = await locator.boundingBox(); assert(box, `No bounding box for ${label}`);
      pointer = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      await page.mouse.move(pointer.x, pointer.y);
      await snap(`${label}-click`, 350, true);
      await locator.click();
      await page.waitForTimeout(400);
      // The checked-in market sample is unadjusted; reflect that in the actual UI.
      if (label === 'chart') {
        await page.getByRole('button', { name: '不复权', exact: true }).click();
        await page.waitForTimeout(350);
      }
      if (label === 'macd') {
        await page.locator('.sk-page-content').evaluate(el => { el.scrollTop = 240; });
        await page.waitForTimeout(350);
      }
      await page.mouse.move(0, 0); pointer = null;
      await page.waitForTimeout(400); // Let tooltips dismiss before the held frame.
      await snap(label, duration);
    };
    await page.goto(origin + '/#/stock', { waitUntil: 'networkidle' });
    await page.getByRole('heading', { name: '自选', exact: true }).waitFor();
    await page.getByText('浦发银行', { exact: true }).waitFor();
    await snap('watchlist', 2300);
    await click(page.getByRole('button', { name: '银行观察', exact: true }), 'group', 1500);
    step = 1;
    await click(page.getByRole('button', { name: '研究', exact: true }).first(), 'chart', 2100);
    await page.getByRole('button', { name: 'MACD 图表指标', exact: true }).waitFor();
    assert(await page.locator('.lw-kline-chart canvas').count() > 0);
    await click(page.getByRole('button', { name: 'MACD 图表指标', exact: true }), 'macd', 2300);
    fs.copyFileSync(path.join(out, frames.at(-1).file), path.join(out, 'poster-source.png'));
    await click(page.getByRole('button', { name: 'MA 信号说明', exact: true }), 'indicator-guide', 1900);
    await page.keyboard.press('Escape'); await page.locator('.n-drawer').waitFor({ state: 'hidden' });
    step = 2;
    await click(page.getByRole('link', { name: '精选', exact: true }), 'picks', 2600);
    await page.locator('.pick-card').first().waitFor();
    step = 3;
    await click(page.locator('.pick-why').first(), 'pick-reason', 2800);
    await page.keyboard.press('Escape'); await page.locator('.n-drawer').waitFor({ state: 'hidden' });
    step = 4;
    await click(page.getByText('推荐记录', { exact: true }), 'records', 1700);
    await click(page.locator('.signal-row summary').first(), 'record-snapshot', 2600);
    step = 5;
    await click(page.getByText('延后复盘', { exact: true }), 'reviews', 3000);
    assert.equal(await page.locator('.review-table tbody tr').count(), 3);
    const calls = await page.evaluate(() => window.__tourCalls);
    assert(!calls.some(c => c.method === 'StartKingPicksRefresh'), 'Tour must not initiate a scan');
    assert.equal(errors.length, 0, errors.join('\n'));
    assert.equal(blocked.length, 0, `Unexpected external requests: ${blocked.join(', ')}`);
    fs.writeFileSync(path.join(out, 'frames.json'), JSON.stringify({ frames, steps, width: 1280, height: 780 }, null, 2));
    const provenance = {
      schemaVersion: 1, capturedAt: new Date().toISOString(), frontend: 'Current Vue frontend built by Vite; real browser clicks and screenshots',
      frontendSourceCommit: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(),
      viewport: { width: 1280, height: 780 }, fixture: 'scripts/workspace-tour-fixture.cjs',
      marketSample: 'docs/research/stockking-v11-market-sample.json', marketAsOf: sample.asOfMarketDate,
      disclosure: 'UI demo fixtures plus historical prices; no real recommendation, current quote, executed trade, return, model performance or user data.',
      isolatedBrowser: true, network: 'Loopback static assets only; all other requests blocked', backendStarted: false,
      calls: [...new Set(calls.map(c => c.method))], errors, blockedRequests: blocked,
      durationMs: frames.reduce((sum, frame) => sum + frame.duration, 0), screenshotCount: frames.length,
    };
    const encoding = encodeTour(out, path.join(root, 'docs/media'));
    provenance.gifFrameCount = encoding.frames;
    provenance.outputDimensions = encoding.size;
    provenance.loop = 0;
    provenance.gifBytes = fs.statSync(path.join(root, 'docs/media/stockking-workspace-tour.gif')).size;
    provenance.media = encoding.outputs;
    assert(provenance.gifBytes <= 5 * 1024 * 1024, 'GIF is larger than 5 MiB');
    fs.writeFileSync(path.join(root, 'docs/media/stockking-workspace-tour.provenance.json'), JSON.stringify(provenance, null, 2) + '\n');
    console.log(JSON.stringify(provenance, null, 2));
  } finally { await browser.close(); server.close(); }
})().catch(error => { server.close(); console.error(error); process.exitCode = 1; });
