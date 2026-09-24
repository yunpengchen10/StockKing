import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from server import BoundedEndpoint, build_server


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.server = build_server()
        self.client = TestClient(BoundedEndpoint(self.server.streamable_http_app(), "test-token"),
                                 base_url="http://127.0.0.1:8766")
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer test-token",
                        "Accept": "application/json, text/event-stream"}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def rpc(self, method, params=None):
        return self.client.post("/mcp", headers=self.headers,
                                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_transport_and_read_only_tool_list(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(init.status_code, 200)
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"get_stock_quotes", "get_quote_capabilities"})
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))

    def test_auth_and_route_isolation(self):
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        self.assertEqual(self.client.get("/files/.env", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/mcp", headers=self.headers).status_code, 405)

    def test_body_and_host_limits(self):
        response = self.client.post("/mcp", headers=self.headers, content=b"x" * 16385)
        self.assertEqual(response.status_code, 413)
        headers = {**self.headers, "Host": "untrusted.example"}
        self.assertEqual(self.client.post("/mcp", headers=headers, json={}).status_code, 421)

    def test_invalid_codes_never_reach_network(self):
        with patch("server.get_public_market_quotes") as collector:
            for codes in ([], ["https://example.com"], ["600519"] * 21):
                result = self.rpc("tools/call", {"name": "get_stock_quotes", "arguments": {"codes": codes}})
                self.assertTrue(result.json()["result"]["isError"])
            collector.assert_not_called()

    def test_provider_time_and_failure_are_preserved(self):
        snapshot = {"checked_at": "2026-09-15T13:20:00+08:00", "quotes": [
            {"code": "600519", "quote": {"price": 1277, "source_time": "2026-09-15T13:19:57+08:00"}},
            {"code": "002487", "quote": None, "issues": ["upstream_timeout"]}]}
        with patch("server.get_public_market_quotes", return_value=snapshot):
            result = self.rpc("tools/call", {"name": "get_stock_quotes", "arguments": {"codes": ["600519", "002487"]}})
        self.assertEqual(result.json()["result"]["structuredContent"], snapshot)
        self.assertEqual(result.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
