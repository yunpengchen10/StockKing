// Standalone public quotes: no account, database, session or credential imports.
export const MAX_QUOTE_CODES = 20;
export const MAX_AGE_SECONDS = 30;
const REQUEST_TIMEOUT_MS = 1_800;
const MAX_PAYLOAD_BYTES = 128_000;
const MAX_CONCURRENT_REQUESTS = 4;
let activeRequests = 0;

                                   
                                  
                                                                                                                                
                                                                                                                               
                 
                                                                    
                                                              
                                                                 
                                                          
  
                                                                                                                 
                                                                                                                              
                              

export class QuoteGatewayBusyError extends Error {}

export function normalizeQuoteCodes(input        )           {
  if (!input || input.length > MAX_QUOTE_CODES * 7) throw new Error("codes must contain 1 to 20 Shanghai/Shenzhen A-share codes");
  const codes = input.split(",").map((code) => code.trim());
  if (codes.length > MAX_QUOTE_CODES || codes.some((code) => !/^[036]\d{5}$/.test(code))) {
    throw new Error("codes must contain 1 to 20 comma-separated six-digit codes starting with 0, 3 or 6");
  }
  return [...new Set(codes)];
}

function chinaTime(time        )         {
  return new Date(time + 8 * 3_600_000).toISOString().slice(0, 19) + "+08:00";
}

function sourceTime(value        )         {
  const compact = value.replace(/[- :T]/g, "");
  if (!/^\d{14}$/.test(compact)) throw new Error("invalid_source_time");
  const iso = `${compact.slice(0, 4)}-${compact.slice(4, 6)}-${compact.slice(6, 8)}T${compact.slice(8, 10)}:${compact.slice(10, 12)}:${compact.slice(12, 14)}+08:00`;
  const time = Date.parse(iso);
  if (!Number.isFinite(time) || chinaTime(time) !== iso) throw new Error("invalid_source_time");
  return iso;
}

function requiredNumber(value                    , positive = false)         {
  if (value === undefined || !/^-?\d+(?:\.\d+)?$/.test(value.trim())) throw new Error("invalid_numeric_field");
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0 || (positive && parsed === 0)) throw new Error("invalid_numeric_field");
  return parsed;
}

export function marketPhase(time        )        {
  const clock = time.slice(11, 19);
  if (clock < "09:15:00") return "pre_open";
  if (clock < "09:25:00") return "opening_auction";
  if (clock < "09:30:00") return "opening_pause";
  if (clock < "11:30:00") return "continuous";
  if (clock < "13:00:00") return "lunch_break";
  if (clock < "14:57:00") return "continuous";
  if (clock < "15:00:00") return "closing_auction";
  return "post_close";
}

function readBook(fields          , source          ) {
  const bids          = [];
  const asks          = [];
  let valid = true;
  for (const [side, start] of [[bids, source === "tencent" ? 9 : 10], [asks, source === "tencent" ? 19 : 20]]         ) {
    for (let level = 0; level < 5; level++) {
      try {
        const offset = start + level * 2;
        const price = requiredNumber(fields[offset + (source === "tencent" ? 0 : 1)]);
        const volume = requiredNumber(fields[offset + (source === "tencent" ? 1 : 0)]);
        if (!Number.isSafeInteger(volume) || (price === 0) !== (volume === 0)) valid = false;
        side.push({ price, volume_shares: volume * (source === "tencent" ? 100 : 1), source_volume: volume, source_volume_unit: source === "tencent" ? "lots_100_shares" : "shares" });
      } catch {
        valid = false;
      }
    }
  }
  // Retain the actual zero levels and crossed books, but never qualify them as buyability evidence.
  const positiveBids = bids.filter((level) => level.price > 0);
  const positiveAsks = asks.filter((level) => level.price > 0);
  if (positiveBids.some((level, index) => index > 0 && level.price > positiveBids[index - 1].price)) valid = false;
  if (positiveAsks.some((level, index) => index > 0 && level.price < positiveAsks[index - 1].price)) valid = false;
  if (positiveBids.length && positiveAsks.length && positiveBids[0].price >= positiveAsks[0].price) valid = false;
  return { bids, asks, order_book_valid: valid };
}

export function parseQuotePayload(payload        , source          , requestedCodes          ) {
  const quotes = new Map                  ();
  const errors = new Map                ();
  const expression = source === "tencent" ? /v_(sh|sz)(\d{6})="([^"]*)"/g : /hq_str_(sh|sz)(\d{6})="([^"]*)"/g;
  for (const match of payload.matchAll(expression)) {
    const [, exchange, code, body] = match;
    if (!requestedCodes.includes(code) || exchange !== (code.startsWith("6") ? "sh" : "sz")) continue;
    if (quotes.has(code) || errors.has(code)) {
      quotes.delete(code);
      errors.set(code, "duplicate_quote");
      continue;
    }
    try {
      const fields = body.split(source === "tencent" ? "~" : ",");
      if (fields.length < (source === "tencent" ? 38 : 32)) throw new Error("empty_or_incomplete_quote");
      if (source === "tencent" && fields[2] !== code) throw new Error("code_mismatch");
      const name = fields[source === "tencent" ? 1 : 0].trim();
      if (!name) throw new Error("missing_name");
      const price = requiredNumber(fields[3], true);
      const previousClose = requiredNumber(fields[source === "tencent" ? 4 : 2], true);
      const open = requiredNumber(fields[source === "tencent" ? 5 : 1]);
      const high = requiredNumber(fields[source === "tencent" ? 33 : 4]);
      const low = requiredNumber(fields[source === "tencent" ? 34 : 5]);
      const stamp = sourceTime(source === "tencent" ? fields[30] : fields[30] + " " + fields[31]);
      if (marketPhase(stamp) === "continuous" && (low <= 0 || high < low || price < low || price > high || open < low || open > high)) throw new Error("inconsistent_price_fields");
      quotes.set(code, {
        code, name, price, previous_close: previousClose, open, high, low,
        change_pct: Number(((price / previousClose - 1) * 100).toFixed(4)),
        volume_shares: requiredNumber(fields[source === "tencent" ? 36 : 8]) * (source === "tencent" ? 100 : 1),
        amount_cny: requiredNumber(fields[source === "tencent" ? 37 : 9]) * (source === "tencent" ? 10_000 : 1),
        source_time: stamp, ...readBook(fields, source),
      });
    } catch (error) {
      errors.set(code, error instanceof Error ? error.message : "invalid_quote");
    }
  }
  return { quotes, errors };
}

async function readLimitedBody(response          )                  {
  if (!response.body) throw new Error("empty_response");
  const announcedSize = Number(response.headers.get("content-length"));
  if (announcedSize > MAX_PAYLOAD_BYTES) {
    await response.body.cancel();
    throw new Error("payload_too_large");
  }
  const reader = response.body.getReader();
  const chunks               = [];
  let size = 0;
  try {
    for (;;) {
      const result = await reader.read();
      if (result.done) break;
      size += result.value.byteLength;
      if (size > MAX_PAYLOAD_BYTES) throw new Error("payload_too_large");
      chunks.push(result.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder("gb18030").decode(bytes);
}

async function fetchPublicSource(source          , codes          , fetcher           , now              ) {
  const symbols = codes.map((code) => (code.startsWith("6") ? "sh" : "sz") + code).join(",");
  const hostPath = source === "tencent" ? `qt.gtimg.cn/q=${symbols}` : `hq.sinajs.cn/list=${symbols}`;
  const observations                = [];
  const attempts            = [];
  const errors = new Map                ();
  for (const transport of ["https", "http"]         ) {
    const url = `${transport}://${hostPath}`;
    const controller = new AbortController();
    let timer                                           ;
    try {
      // Bound the whole fetch AND body read, even if an upstream stops streaming.
      const payload = await Promise.race([
        (async () => {
          const response = await fetcher(url, {
            cache: "no-store", credentials: "omit", redirect: "manual", signal: controller.signal,
            headers: { "user-agent": "StockKingPublicQuotes/1.0", referer: source === "tencent" ? "https://gu.qq.com/" : "https://finance.sina.com.cn/" },
          });
          if (!response.ok) throw new Error(`upstream_http_${response.status}`);
          return readLimitedBody(response);
        })(),
        new Promise       ((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error("upstream_timeout")); }, REQUEST_TIMEOUT_MS); }),
      ]);
      const fetchedAt = now();
      const parsed = parseQuotePayload(payload, source, codes);
      for (const [code, error] of parsed.errors) errors.set(code, error);
      for (const quote of parsed.quotes.values()) {
        // A future source time is rejected, never disguised with the local fetch time.
        if (Date.parse(quote.source_time) > fetchedAt) { errors.set(quote.code, "future_source_time"); continue; }
        observations.push({ ...quote, source, transport, fetched_at: chinaTime(fetchedAt), source_url: url });
      }
      const received = observations.filter((quote) => quote.transport === transport);
      attempts.push({ source, transport, status: received.length ? "ok" : "failed", codes_received: received.map((quote) => quote.code), ...(!received.length ? { error: "no_valid_quotes" } : {}) });
      // Try HTTP only when HTTPS was unavailable, incomplete or stale. It sends public codes only.
      if (codes.every((code) => received.some((quote) => quote.code === code && fetchedAt - Date.parse(quote.source_time) <= MAX_AGE_SECONDS * 1000 && quote.source_time.slice(0, 10) === chinaTime(fetchedAt).slice(0, 10)))) break;
    } catch (error) {
      const message = error instanceof Error && /^upstream_http_\d+$|^upstream_timeout$|^payload_too_large$|^empty_response$/.test(error.message) ? error.message : "upstream_unavailable";
      attempts.push({ source, transport, status: "failed", error: message });
    } finally {
      if (timer) clearTimeout(timer);
      controller.abort();
    }
  }
  return { observations, attempts, errors };
}

export async function getPublicMarketQuotes(codes          , options                                              = {}) {
  // Validate here too, so non-HTTP callers cannot introduce a different host or an unbounded batch.
  const requested = normalizeQuoteCodes(codes.join(","));
  if (activeRequests >= MAX_CONCURRENT_REQUESTS) throw new QuoteGatewayBusyError("quote_gateway_busy");
  activeRequests++;
  try {
    const now = options.now ?? Date.now;
    const results = await Promise.all((["tencent", "sina"]         ).map((source) => fetchPublicSource(source, requested, options.fetcher ?? fetch, now)));
    const checkedAt = now();
    const checkedChina = chinaTime(checkedAt);
    const phase = marketPhase(checkedChina);
    const quotes = requested.map((code) => {
      const observations = results.flatMap((result) => result.observations.filter((quote) => quote.code === code));
      const ranked = observations.sort((a, b) => Date.parse(b.source_time) - Date.parse(a.source_time) || (a.transport === "https" ? -1 : 1));
      const quote = ranked[0];
      if (!quote) return {
        code, status: "missing_data"         , quote: null, execution_verified: false,
        issues: [...new Set(results.map((result) => result.errors.get(code) ?? "source_quote_unavailable"))],
      };
      const ageMilliseconds = checkedAt - Date.parse(quote.source_time);
      const ageSeconds = Math.round(ageMilliseconds / 100) / 10;
      const sameDay = quote.source_time.slice(0, 10) === checkedChina.slice(0, 10);
      // Rounding is presentation only: 30.049 seconds must not pass a 30-second policy.
      const fresh = sameDay && ageMilliseconds >= 0 && ageMilliseconds <= MAX_AGE_SECONDS * 1000;
      const sourcePhase = marketPhase(quote.source_time);
      const issues           = [];
      if (!sameDay) issues.push("different_market_date");
      if (!fresh) issues.push("stale_quote");
      if (phase !== "continuous") issues.push(`request_phase_${phase}`);
      if (sourcePhase !== "continuous") issues.push(`source_phase_${sourcePhase}`);
      if (phase === "post_close" || sourcePhase === "post_close") issues.push("post_close_cannot_reconstruct_1455");
      if (phase === "opening_auction" || sourcePhase === "opening_auction") issues.push("auction_fields_not_supported");
      if (quote.transport === "http") issues.push("unencrypted_public_source_fallback");
      if (!quote.order_book_valid) issues.push("invalid_order_book");
      const peer = ranked.find((row) => row.source !== quote.source && row.source_time.slice(0, 10) === checkedChina.slice(0, 10) && checkedAt - Date.parse(row.source_time) <= MAX_AGE_SECONDS * 1000);
      const sourceTimeDifferenceSeconds = peer ? Math.abs(Date.parse(peer.source_time) - Date.parse(quote.source_time)) / 1000 : null;
      // A price change between two valid observations is not a source conflict.
      // Compare prices only when the providers report the exact same source timestamp.
      const synchronized = peer ? sourceTimeDifferenceSeconds === 0 : false;
      const pricesAgree = peer && synchronized ? Math.abs(peer.price - quote.price) <= 0.011 : null;
      if (pricesAgree === false) issues.push("cross_source_price_conflict");
      if (peer && !synchronized) issues.push("cross_source_asynchronous_reference");
      const priceCheckUsable = fresh && phase === "continuous" && sourcePhase === "continuous" && pricesAgree !== false;
      // Select a whole observed book independently. Never copy a peer's levels into the quote.
      // Require its own timestamp, validity and exactly the same quoted price.
      const bookObservation = priceCheckUsable ? ranked.find((row) => {
        const bookAge = checkedAt - Date.parse(row.source_time);
        return row.order_book_valid && Math.abs(row.price - quote.price) < 1e-8
          && row.source_time.slice(0, 10) === checkedChina.slice(0, 10)
          && bookAge >= 0 && bookAge <= MAX_AGE_SECONDS * 1000
          && marketPhase(row.source_time) === "continuous";
      }) : undefined;
      const orderBook = bookObservation ? {
        source: bookObservation.source, source_time: bookObservation.source_time,
        fetched_at: bookObservation.fetched_at, source_url: bookObservation.source_url,
        transport: bookObservation.transport, observed_quote_price: bookObservation.price,
        age_milliseconds: checkedAt - Date.parse(bookObservation.source_time),
        bids: bookObservation.bids, asks: bookObservation.asks,
      } : null;
      if (bookObservation && bookObservation !== quote) issues.push("order_book_from_separate_observation");
      if (bookObservation?.transport === "http" && bookObservation !== quote) issues.push("order_book_unencrypted_public_source_fallback");
      const bestAsk = orderBook?.asks.find((level) => level.price > 0 && level.volume_shares > 0);
      return {
        code, status: priceCheckUsable ? "fresh"          : "reference_only"         ,
        quote, age_seconds: ageSeconds, age_milliseconds: ageMilliseconds, same_market_date: sameDay, phase: sourcePhase,
        quote_usable_for_current_price_check: priceCheckUsable,
        order_book: orderBook,
        buy_side_liquidity_observed: Boolean(bestAsk),
        execution_verified: false,
        cross_source_check: peer ? {
          source: peer.source, source_time: peer.source_time, price: peer.price,
          time_difference_seconds: sourceTimeDifferenceSeconds, synchronized, prices_agree: pricesAgree,
        } : null,
        issues,
      };
    });
    return {
      schema_version: 1, checked_at: checkedChina, max_age_seconds: MAX_AGE_SECONDS,
      source_time_meaning: "provider_quote_update_time", exchange_execution_time_verified: false,
      historical_snapshot_supported: false, auction_fields_supported: false, trading_calendar_verified: false,
      quotes, attempts: results.flatMap((result) => result.attempts),
    };
  } finally {
    activeRequests--;
  }
}


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
const arraySchema = (items         ) => ({ type: 'array', items });
const nullableSchema = (schema         ) => ({ anyOf: [schema, { type: 'null' }] });
const objectSchema = (properties                         , required = Object.keys(properties)) => ({ type: 'object', properties, required, additionalProperties: false });
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
const json = (body         , status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
export default {
  async fetch(request         )                    {
    const url = new URL(request.url);
    if (url.pathname === '/health' && request.method === 'GET') return json({ service: 'Stock King Quotes', ...capabilities });
    if (url.pathname === '/quotes' || url.pathname.startsWith('/quotes/')) {
      if (request.method !== 'GET') {
        const response = json({ error: 'method_not_allowed' }, 405);
        response.headers.set('Allow', 'GET');
        return response;
      }
      let codes          ;
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
    let message     ;
    try {
      const reader = request.body?.getReader();
      if (!reader) throw new Error('empty');
      let size = 0;
      const chunks               = [];
      const deadline = Date.now() + 5000;
      try {
        while (true) {
          const remaining = deadline - Date.now();
          if (remaining <= 0) throw new Error('timeout');
          let timer                                           ;
          const part = await Promise.race([reader.read(), new Promise       ((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), remaining); })]).finally(() => clearTimeout(timer));
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
    const reply = (result         ) => json({ jsonrpc: '2.0', id: message.id, result });
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
