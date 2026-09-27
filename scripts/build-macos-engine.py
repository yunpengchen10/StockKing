"""Native macOS build; runtime import checks fail the build before packaging."""
import os
import platform
from pathlib import Path
import subprocess
import sys
import secrets
import socket
import tempfile
import time
import urllib.error
import urllib.request

root = Path(__file__).resolve().parents[1] / "daily-engine"
if sys.platform != "darwin" or platform.machine() != "arm64":
    raise SystemExit("This build requires Apple Silicon and native ARM64 Python")
args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "stock_analysis", "--onedir", "--distpath", "dist/backend",
        "--runtime-hook", "scripts/pyinstaller_runtime_compat.py"]
for directory in ("resources", "strategies", "src/assets/share_image"):
    args += ["--add-data", f"{directory}:{directory}"]
for package in ("api", "src.services", "src.quant", "futu", "lightgbm"):
    args += ["--collect-all", package]
for package in ("litellm", "tiktoken", "akshare"):
    args += ["--collect-data", package]
for module in ("multipart", "multipart.multipart", "orjson", "json_repair",
               "tiktoken_ext.openai_public", "uvicorn.logging",
               "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
               "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"):
    args += ["--hidden-import", module]
args += ['--hidden-import', 'torch']
subprocess.run(args + ["main.py"], cwd=root, check=True)
executable = root / "dist/backend/stock_analysis/stock_analysis"
modules = ("api.app", "src.services.yao_scout.local_opportunities",
           "src.services.yao_scout.local_observation_review", "src.quant.service",
           "src.services.screening.pipeline", "lightgbm", "futu", "orjson", "torch")
with tempfile.TemporaryDirectory(prefix="stock-king-import-probes-") as probe_directory:
    for index, module in enumerate(modules):
        # The first launch may spend several minutes initializing the large
        # PyInstaller bundle on a cold macOS filesystem. Subsequent probes still
        # have a bounded timeout, and every import must pass.
        timeout = 300 if index == 0 else 120
        print(f"Checking packaged import {index + 1}/{len(modules)}: {module} (up to {timeout}s)", flush=True)
        try:
            subprocess.run([str(executable)], cwd=root, check=True, timeout=timeout,
                           env={**os.environ, "DSA_PACKAGED_IMPORT_PROBE": module,
                                "SCREENING_DATA_DIR": probe_directory})
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Packaged import {module} exceeded {timeout}s") from error

with tempfile.TemporaryDirectory(prefix="stock-king-smoke-") as directory:
    state = Path(directory)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = secrets.token_hex(32)
    env = {**os.environ, "STOCK_KING_SIDECAR_TOKEN": token,
           "DATABASE_PATH": str(state / "research.db"), "LOG_DIR": str(state / "logs"),
           "SCREENING_DATA_DIR": str(state / "cache"), "YAO_SCOUT_DATA_DIR": str(state / "picks"),
           "MODEL_DIR": str(state / "models"), "CORS_ALLOW_ALL": "false"}
    # Exercise the normal desktop environment. The inherited CLI intentionally
    # suppresses web serving when GITHUB_ACTIONS=true.
    env.pop("GITHUB_ACTIONS", None)
    env["USE_PROXY"] = "false"
    # Health checks must connect directly to loopback even when the build
    # shell exports a proxy for dependency downloads.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (state / "engine.log").open("w+") as log:
        process = subprocess.Popen([str(executable), "--serve-only", "--host", "127.0.0.1",
                                    "--port", str(port)], cwd=state, env=env, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}/api/v1/health"
            deadline = time.monotonic() + 90
            while True:
                try:
                    request = urllib.request.Request(url, headers={"X-Stock-King-Token": token})
                    with opener.open(request, timeout=2) as response:
                        assert response.status == 200
                    break
                except (OSError, urllib.error.URLError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise RuntimeError("Packaged engine did not start:\n" + log.read()[-8000:])
                    time.sleep(1)
            try:
                opener.open(url, timeout=2)
            except urllib.error.HTTPError as error:
                assert error.code == 401, error.code
            else:
                raise RuntimeError("Packaged engine accepted an unauthenticated request")
            print("Packaged engine: authenticated health 200, unauthenticated 401")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
