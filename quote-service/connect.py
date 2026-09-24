"""Run the official private tunnel for this read-only quote service.

The runtime key is read by tunnel-client from a private local file, never printed
or placed in command arguments. No OpenAI model inference is requested here.
"""
import argparse
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ARTIFACTS / "quote-tunnel.json")
    parser.add_argument("--key-file", type=Path, default=ARTIFACTS / "quote-tunnel.key")
    parser.add_argument("--runtime", type=Path, default=ARTIFACTS / "tunnel-client" /
                        ("tunnel-client-runtime.exe" if sys.platform == "win32" else "tunnel-client-runtime"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
    tunnel_id = settings["tunnel_id"]
    if not re.fullmatch(r"tunnel_[a-zA-Z0-9]+", tunnel_id):
        parser.error("Invalid tunnel ID")
    if not args.runtime.is_file():
        parser.error("Official tunnel-client runtime has not been installed")
    if not args.key_file.is_file() or not args.key_file.read_text(encoding="utf-8-sig").strip():
        parser.error(f"Save the tunnel runtime key to {args.key_file}; do not paste it into chat")
    if args.check:
        print("Runtime, tunnel configuration and key file are present; cloud connection is not yet verified.")
        return
    ARTIFACTS.mkdir(exist_ok=True)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    children = []
    session = requests.Session()
    session.trust_env = False
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "get_quote_capabilities", "arguments": {}}}

    def quote_ready():
        try:
            response = session.post("http://127.0.0.1:8766/mcp", json=rpc,
                                    headers={"Accept": "application/json, text/event-stream"}, timeout=2)
            result = response.json()["result"]["structuredContent"]
            return response.ok and result.get("providers") == ["tencent", "sina"]
        except (requests.RequestException, ValueError, KeyError):
            return False

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        if not quote_ready():
            service = subprocess.Popen([sys.executable, str(Path(__file__).with_name("server.py")),
                                        "--transport", "http"], creationflags=creationflags)
            children.append(service)
            for _ in range(20):
                if service.poll() is not None:
                    raise RuntimeError("Quote service failed to start")
                if quote_ready():
                    break
                time.sleep(.25)
            else:
                raise RuntimeError("Quote service did not become ready")
        command = [str(args.runtime.resolve()), "run",
                   "--control-plane.tunnel-id", tunnel_id,
                   "--control-plane.api-key", "file:" + str(args.key_file.resolve()),
                   "--mcp.server-url", "http://127.0.0.1:8766/mcp",
                   "--mcp.max-concurrent-requests", "4",
                   "--health.listen-addr", "127.0.0.1:8767",
                   "--log.file", str(ARTIFACTS / "quote-tunnel.log"),
                   "--pid.file", str(ARTIFACTS / "quote-tunnel.pid")]
        print("Starting Stock King Quotes tunnel; readiness is at http://127.0.0.1:8767/readyz", flush=True)
        client = subprocess.Popen(command, creationflags=creationflags)
        children.append(client)
        status = client.wait()
        if status:
            raise RuntimeError(f"Tunnel client exited with status {status}; inspect the redacted runtime log")
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()


if __name__ == "__main__":
    main()
