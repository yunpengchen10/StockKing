"""Exercise the actual MCP transport and optionally save its response."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


async def probe(codes, output=None, url=None):
    async with AsyncExitStack() as stack:
        if url:
            reader, writer, _ = await stack.enter_async_context(streamable_http_client(url))
            transport = "streamable_http_mcp"
        else:
            params = StdioServerParameters(command=sys.executable,
                                          args=[str(Path(__file__).with_name("server.py"))])
            reader, writer = await stack.enter_async_context(stdio_client(params))
            transport = "stdio_mcp"
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("get_stock_quotes", {"codes": codes})
            if result.isError:
                raise RuntimeError(str(result.content))
            payload = result.structuredContent
            if payload is None:
                payload = json.loads(result.content[0].text)
            if output:
                Path(output).parent.mkdir(parents=True, exist_ok=True)
                Path(output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({
                "transport": transport, "tools": [tool.name for tool in tools.tools],
                "checked_at": payload["checked_at"],
                "quotes": [{"code": row["code"], "status": row["status"],
                            "price": (row["quote"] or {}).get("price"),
                            "source_time": (row["quote"] or {}).get("source_time"),
                            "source": (row["quote"] or {}).get("source"),
                            "age_seconds": row.get("age_seconds")}
                           for row in payload["quotes"]],
            }, ensure_ascii=False))
            if not any(row["quote"] for row in payload["quotes"]):
                raise RuntimeError("All providers failed; inspect the saved response")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="+", help="Six-digit stock codes")
    parser.add_argument("--output")
    parser.add_argument("--url", help="Remote Streamable HTTP MCP endpoint; omit to test the local stdio server")
    args = parser.parse_args()
    asyncio.run(probe(args.codes, args.output, args.url))
