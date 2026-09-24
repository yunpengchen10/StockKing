"""Read-only MCP adapter for Stock King's existing public quote collector.

No account configuration, portfolio database, trading or AI client is imported.
Run on loopback behind an authenticated MCP tunnel, or use stdio directly.
"""
from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
import sys
import time
from collections import deque
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daily-engine"))
from src.services.public_market_quotes import get_public_market_quotes, normalize_quote_codes


def build_server(port=8766, public_host=None):
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if public_host:
        # A hostname, never a wildcard or URL chosen by a tool caller.
        import re
        if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", public_host):
            raise ValueError("public_host_must_be_an_exact_hostname")
        hosts.append(public_host)
    server = FastMCP(
        "Stock King Quotes", host="127.0.0.1", port=port,
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
        instructions=(
            "Read-only public Shanghai/Shenzhen stock quotes. Request arbitrary six-digit "
            "codes, at most 20 per call. Always cite quote.source_time and source, and "
            "recheck age at decision time. checked_at is NOT the supplier timestamp. "
            "Missing/stale quotes are not trade signals. Opening-auction indicative "
            "price and matched/unmatched quantities are NOT supported. This service "
            "cannot read portfolios, execute orders or call AI models."
        ),
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                           idempotentHint=True, openWorldHint=True))
    async def get_stock_quotes(codes: list[str]) -> dict[str, Any]:
        """Fetch live A-share price and five-level book from Tencent and Sina.

        codes: 1–20 six-digit Shanghai/Shenzhen stock codes, e.g. 600519.
        Results include provider source_time, age, per-stock errors and validity.
        A fresh price is not proof of a fill; inspect asks and trading session.
        No historical reconstruction or 09:20 auction matching fields.
        """
        normalized = normalize_quote_codes(codes)
        return await anyio.to_thread.run_sync(get_public_market_quotes, normalized)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                           idempotentHint=True, openWorldHint=False))
    def get_quote_capabilities() -> dict[str, Any]:
        """Return supported fields and limits; this is not a provider health check."""
        return {
            "providers": ["tencent", "sina"], "max_codes": 20,
            "fields": ["price", "source_time", "volume_shares", "amount_cny", "bids", "asks"],
            "max_age_seconds": 30, "auction_fields_supported": False,
            "historical_snapshot_supported": False, "trading_supported": False,
            "provider_connectivity_verified": False,
            "health_check": "Call get_stock_quotes and inspect each stock's result.",
        }

    return server


class BoundedEndpoint:
    """Limit transport exposure independently of MCP tool validation."""
    def __init__(self, app, bearer_token=""):
        self.app, self.token = app, bearer_token
        self.requests = deque()
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))

        async def reply(status, message):
            await JSONResponse({"error": message}, status_code=status,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)

        if scope["path"] != "/mcp":
            return await reply(404, "not_found")
        if self.token and not hmac.compare_digest(
            headers.get(b"authorization", b""), ("Bearer " + self.token).encode()
        ):
            return await reply(401, "unauthorized")
        if scope["method"] != "POST":
            return await reply(405, "post_required")
        now = time.monotonic()
        while self.requests and self.requests[0] < now - 60:
            self.requests.popleft()
        if len(self.requests) >= 60 or self.active >= 4:
            return await reply(429, "quote_service_busy")
        self.requests.append(now)
        self.active += 1
        try:
            chunks, size = [], 0
            with anyio.move_on_after(5) as deadline:
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    size += len(event.get("body", b""))
                    if size > 16_384:
                        return await reply(413, "request_too_large")
                    chunks.append(event.get("body", b""))
                    if not event.get("more_body", False):
                        break
            if deadline.cancel_called:
                return await reply(408, "request_timeout")
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                return await receive()

            async def no_cache_send(event):
                if event["type"] == "http.response.start":
                    event["headers"] = list(event.get("headers", [])) + [(b"cache-control", b"no-store")]
                await send(event)

            await self.app(scope, bounded_receive, no_cache_send)
        finally:
            self.active -= 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--public-host")
    args = parser.parse_args()
    token = os.environ.get("STOCK_KING_QUOTE_TOKEN", "")
    if args.public_host and len(token) < 32:
        parser.error("Public HTTP requires STOCK_KING_QUOTE_TOKEN (at least 32 characters).")
    mcp = build_server(args.port, args.public_host)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        uvicorn.run(BoundedEndpoint(mcp.streamable_http_app(), token),
                    host="127.0.0.1", port=args.port, access_log=False)
