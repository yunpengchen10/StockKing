import test from 'node:test';
import assert from 'node:assert/strict';
import worker from './worker.mjs';
const rpc = (method, params = {}, extra = {}) => worker.fetch(new Request('https://quotes.example/mcp', {
  method: 'POST', headers: { 'Content-Type': 'application/json', ...extra }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params })
}));
test('MCP initialization and tool discovery', async () => {
  assert.equal((await (await rpc('initialize', { protocolVersion: '2025-06-18' })).json()).result.protocolVersion, '2025-06-18');
  const tools = (await (await rpc('tools/list')).json()).result.tools;
  assert.deepEqual(tools.map(t => t.name), ['get_stock_quotes', 'get_quote_capabilities']);
  for (const tool of tools) {
    assert.ok(tool.title);
    assert.deepEqual(tool.securitySchemes, [{ type: 'noauth' }]);
    assert.deepEqual(tool._meta.securitySchemes, tool.securitySchemes);
    assert.equal(tool.outputSchema.type, 'object');
    assert.ok(tool.outputSchema.required.length);
  }
});
test('invalid codes never make upstream requests', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('must not fetch'); };
  try {
    for (const codes of [['https://internal'], [], ['600519,000001'], Array(21).fill('600519')]) {
      const result = (await (await rpc('tools/call', { name: 'get_stock_quotes', arguments: { codes } })).json()).result;
      assert.equal(result.isError, true);
      assert.doesNotMatch(result.content[0].text, /must not fetch/);
    }
  } finally { globalThis.fetch = original; }
});
test('transport rejects foreign origins, oversized bodies and writes', async () => {
  assert.equal((await rpc('tools/list', {}, { Origin: 'https://evil.example' })).status, 403);
  const response = await worker.fetch(new Request('https://quotes.example/mcp', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: 'x'.repeat(17000) }));
  assert.equal(response.status, 413);
  assert.equal((await (await rpc('tools/call', { name: 'place_order' })).json()).error.code, -32602);
});

test('malformed RPC envelopes are rejected before dispatch; notifications are accepted', async () => {
  for (const extra of [{ id: null }, { id: {} }, { id: [] }, { params: null }, { params: [] }, { params: 'invalid' }]) {
    const response = await worker.fetch(new Request('https://quotes.example/mcp', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', ...extra })
    }));
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, -32600);
  }
  const notification = await worker.fetch(new Request('https://quotes.example/mcp', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })
  }));
  assert.equal(notification.status, 202);
  assert.equal(await notification.text(), '');
});

test('body read deadline does not reset when another chunk arrives', async () => {
  const originalNow = Date.now;
  let elapsed = 0;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('{"jsonrpc":"2.0","id":1,'));
      controller.enqueue(encoder.encode('"method":"tools/list"}'));
      controller.close();
    }
  });
  Date.now = () => (elapsed += 3000);
  try {
    const response = await worker.fetch(new Request('https://quotes.example/mcp', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: stream, duplex: 'half'
    }));
    assert.equal(response.status, 400);
  } finally { Date.now = originalNow; }
});

test('public GET quote URLs return the same provider data and source timestamps', async () => {
  const originalFetch = globalThis.fetch;
  const originalNow = Date.now;
  const fields = Array(38).fill('0');
  Object.assign(fields, { 1: 'Test', 2: '600519', 3: '10', 4: '9', 5: '9', 19: '10.01', 20: '1',
    30: '20260915103000', 33: '11', 34: '8', 36: '10', 37: '1' });
  globalThis.fetch = async () => new Response(`v_sh600519="${fields.join('~')}";`);
  Date.now = () => Date.parse('2026-09-15T10:30:01+08:00');
  try {
    const bodies = [];
    for (const path of ['/quotes?codes=600519,000001', '/quotes/600519,000001']) {
      const response = await worker.fetch(new Request('https://quotes.example' + path));
      assert.equal(response.status, 200);
      assert.match(response.headers.get('content-type'), /application\/json/);
      assert.equal(response.headers.get('cache-control'), 'no-store');
      bodies.push(await response.json());
    }
    assert.deepEqual(bodies[0], bodies[1]);
    assert.equal(bodies[0].quotes[0].quote.price, 10);
    assert.equal(bodies[0].quotes[0].quote.source_time, '2026-09-15T10:30:00+08:00');
    assert.equal(bodies[0].quotes[0].status, 'fresh');
    assert.equal(bodies[0].quotes[1].status, 'missing_data');
  } finally { globalThis.fetch = originalFetch; Date.now = originalNow; }
});

test('public GET rejects invalid batches before requesting upstream data', async () => {
  const original = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => { requests++; throw new Error('must not fetch'); };
  try {
    for (const path of ['/quotes', '/quotes/', '/quotes?codes=https://internal', '/quotes/600519%2F000001',
      '/quotes/%ZZ', '/quotes?codes=' + Array(21).fill('600519').join(',')]) {
      const response = await worker.fetch(new Request('https://quotes.example' + path));
      assert.equal(response.status, 400, path);
      assert.equal((await response.json()).error, 'invalid_codes');
    }
    assert.equal(requests, 0);
  } finally { globalThis.fetch = original; }
});

test('public quote endpoints only accept GET', async () => {
  for (const path of ['/quotes?codes=600519', '/quotes/600519']) {
    const response = await worker.fetch(new Request('https://quotes.example' + path, { method: 'POST' }));
    assert.equal(response.status, 405);
    assert.equal(response.headers.get('allow'), 'GET');
    assert.equal(response.headers.get('cache-control'), 'no-store');
  }
});
