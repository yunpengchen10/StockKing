import { getPublicMarketQuotes, normalizeQuoteCodes, QuoteGatewayBusyError } from './quotes.ts';

const capabilities = { providers: ['tencent', 'sina'], max_codes: 20, max_age_seconds: 30,
  auction_fields_supported: false, historical_snapshot_supported: false, trading_supported: false };
const annotations = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true };
const securitySchemes = [{ type: 'noauth' }];
const stringSchema = { type: 'string' };
const numberSchema = { type: 'number' };
const booleanSchema = { type: 'boolean' };
const timestampSchema = { type: 'string', format: 'date-time' };
const codeSchema = { type: 'string', pattern: '^[036][0-9]{5}$' };
const sourceSchema = { type: 'string', enum: ['tencent', 'sina'] };
const transportSchema = { type: 'string', enum: ['https', 'http'] };
const phaseSchema = { type: 'string', enum: ['pre_open', 'opening_auction', 'opening_pause', 'continuous', 'lunch_break', 'closing_auction', 'post_close'] };
const arraySchema = (items: unknown) => ({ type: 'array', items });
const nullableSchema = (schema: unknown) => ({ anyOf: [schema, { type: 'null' }] });
const objectSchema = (properties: Record<string, unknown>, required = Object.keys(properties)) => ({ type: 'object', properties, required, additionalProperties: false });
const levelSchema = objectSchema({ price: numberSchema, volume_shares: numberSchema, source_volume: numberSchema,
  source_volume_unit: { type: 'string', enum: ['lots_100_shares', 'shares'] } });
const observationProperties = { source: sourceSchema, transport: transportSchema, source_time: timestampSchema,
  fetched_at: timestampSchema, source_url: { type: 'string', format: 'uri' } };
const quoteSchema = objectSchema({ code: codeSchema, name: stringSchema, price: numberSchema, previous_close: numberSchema,
  open: numberSchema, high: numberSchema, low: numberSchema, change_pct: numberSchema, volume_shares: numberSchema,
  amount_cny: numberSchema, bids: arraySchema(levelSchema), asks: arraySchema(levelSchema), order_book_valid: booleanSchema,
  ...observationProperties });
const orderBookSchema = objectSchema({ ...observationProperties, observed_quote_price: numberSchema,
  age_milliseconds: numberSchema, bids: arraySchema(levelSchema), asks: arraySchema(levelSchema) });
const crossSourceSchema = objectSchema({ source: sourceSchema, source_time: timestampSchema, price: numberSchema,
  time_difference_seconds: numberSchema, synchronized: booleanSchema, prices_agree: nullableSchema(booleanSchema) });
const rowProperties = { code: codeSchema, execution_verified: { type: 'boolean', const: false }, issues: arraySchema(stringSchema) };
const quoteRowSchema = { anyOf: [
  objectSchema({ ...rowProperties, status: { type: 'string', const: 'missing_data' }, quote: { type: 'null' } }),
  objectSchema({ ...rowProperties, status: { type: 'string', enum: ['fresh', 'reference_only'] }, quote: quoteSchema,
    age_seconds: numberSchema, age_milliseconds: numberSchema, same_market_date: booleanSchema, phase: phaseSchema,
    quote_usable_for_current_price_check: booleanSchema, order_book: nullableSchema(orderBookSchema),
    buy_side_liquidity_observed: booleanSchema, cross_source_check: nullableSchema(crossSourceSchema) })
] };
const quotesOutputSchema = objectSchema({ schema_version: { type: 'integer', const: 1 }, checked_at: timestampSchema,
  max_age_seconds: numberSchema, source_time_meaning: { type: 'string', const: 'provider_quote_update_time' },
  exchange_execution_time_verified: { type: 'boolean', const: false }, historical_snapshot_supported: { type: 'boolean', const: false },
  auction_fields_supported: { type: 'boolean', const: false }, trading_calendar_verified: { type: 'boolean', const: false },
  quotes: arraySchema(quoteRowSchema), attempts: arraySchema(objectSchema({ source: sourceSchema, transport: transportSchema,
    status: { type: 'string', enum: ['ok', 'failed'] }, error: stringSchema, codes_received: arraySchema(codeSchema) }, ['source', 'transport', 'status'])) });
const capabilitiesOutputSchema = objectSchema({ providers: arraySchema(sourceSchema), max_codes: { type: 'integer' },
  max_age_seconds: numberSchema, auction_fields_supported: { type: 'boolean', const: false },
  historical_snapshot_supported: { type: 'boolean', const: false }, trading_supported: { type: 'boolean', const: false } });
const tools = [
  { name: 'get_stock_quotes', title: 'Get current A-share quotes', description: 'Fetch current public A-share prices and five-level order books from Tencent and Sina. Always inspect source_time and freshness. No auction matching data or historical snapshots. A quote does not prove execution.',
    inputSchema: { type: 'object', properties: { codes: { type: 'array', items: codeSchema, minItems: 1, maxItems: 20 } }, required: ['codes'], additionalProperties: false },
    outputSchema: quotesOutputSchema, securitySchemes, _meta: { securitySchemes }, annotations },
  { name: 'get_quote_capabilities', title: 'Get quote service capabilities', description: 'Supported fields and limitations; not a connectivity check.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false }, outputSchema: capabilitiesOutputSchema,
    securitySchemes, _meta: { securitySchemes }, annotations: { ...annotations, openWorldHint: false } }
];
const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
export default {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/health' && request.method === 'GET') return json({ service: 'Stock King Quotes', ...capabilities });
    if (url.pathname === '/quotes' || url.pathname.startsWith('/quotes/')) {
      if (request.method !== 'GET') {
        const response = json({ error: 'method_not_allowed' }, 405);
        response.headers.set('Allow', 'GET');
        return response;
      }
      let codes: string[];
      try {
        const input = url.pathname === '/quotes' ? url.searchParams.get('codes') ?? '' : decodeURIComponent(url.pathname.slice('/quotes/'.length));
        codes = normalizeQuoteCodes(input);
      } catch { return json({ error: 'invalid_codes', message: 'Provide 1–20 comma-separated six-digit A-share codes starting with 0, 3 or 6.' }, 400); }
      try { return json(await getPublicMarketQuotes(codes)); }
      catch (error) { return json({ error: error instanceof QuoteGatewayBusyError ? 'quote_gateway_busy' : 'quote_unavailable' }, 503); }
    }
    if (url.pathname !== '/mcp') return json({ error: 'not_found' }, 404);
    if (request.method !== 'POST') return new Response(null, { status: 405, headers: { Allow: 'POST' } });
    const origin = request.headers.get('origin');
    if (origin && origin !== url.origin && origin !== 'https://chatgpt.com') return json({ error: 'origin_denied' }, 403);
    if (!request.headers.get('content-type')?.includes('application/json')) return json({ error: 'json_required' }, 415);
    let message: any;
    try {
      const reader = request.body?.getReader();
      if (!reader) throw new Error('empty');
      let size = 0;
      const chunks: Uint8Array[] = [];
      const deadline = Date.now() + 5000;
      try {
        while (true) {
          const remaining = deadline - Date.now();
          if (remaining <= 0) throw new Error('timeout');
          let timer: ReturnType<typeof setTimeout> | undefined;
          const part = await Promise.race([reader.read(), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), remaining); })]).finally(() => clearTimeout(timer));
          if (part.done) break;
          size += part.value.length;
          if (size > 16384) return json({ error: 'body_too_large' }, 413);
          chunks.push(part.value);
        }
      } finally { await reader.cancel().catch(() => undefined); }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      message = JSON.parse(new TextDecoder().decode(bytes));
    } catch { return json({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Invalid JSON' } }, 400); }
    if (!message || Array.isArray(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string'
      || (message.id !== undefined && typeof message.id !== 'string' && !(typeof message.id === 'number' && Number.isFinite(message.id)))
      || (message.params !== undefined && (!message.params || typeof message.params !== 'object' || Array.isArray(message.params)))) {
      return json({ jsonrpc: '2.0', id: null, error: { code: -32600, message: 'Invalid request' } }, 400);
    }
    if (message.id === undefined) return new Response(null, { status: 202 });
    const reply = (result: unknown) => json({ jsonrpc: '2.0', id: message.id, result });
    if (message.method === 'initialize') return reply({ protocolVersion: ['2024-11-05', '2025-03-26', '2025-06-18'].includes(message.params?.protocolVersion) ? message.params.protocolVersion : '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'Stock King Quotes', version: '1.0.0' }, instructions: 'Public market data only. Cite provider source_time; checked_at is not quote time. Recheck freshness at decision time. Never treat missing or stale data as a trade signal.' });
    if (message.method === 'ping') return reply({});
    if (message.method === 'tools/list') return reply({ tools });
    if (message.method === 'tools/call') {
      try {
        let result;
        if (message.params?.name === 'get_quote_capabilities') result = capabilities;
        else if (message.params?.name === 'get_stock_quotes') {
          const codes = message.params.arguments?.codes;
          if (!Array.isArray(codes) || codes.length < 1 || codes.length > 20 || !codes.every(c => typeof c === 'string' && /^[036]\d{5}$/.test(c))) throw new Error('codes must be an array of 1–20 six-digit A-share codes');
          result = await getPublicMarketQuotes(normalizeQuoteCodes(codes.join(',')));
        } else return json({ jsonrpc: '2.0', id: message.id, error: { code: -32602, message: 'Unknown tool' } });
        return reply({ content: [{ type: 'text', text: JSON.stringify(result) }], structuredContent: result, isError: false });
      } catch (error) { return reply({ content: [{ type: 'text', text: error instanceof Error ? error.message : 'quote_unavailable' }], isError: true }); }
    }
    return json({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'Method not found' } });
  }
};
