"""Read-only history migration and frozen-engine restart check in an isolated profile."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--history-db", required=True, type=Path)
    args = parser.parse_args()
    source = args.history_db.resolve(strict=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        rows = [{"mode": mode, "result": json.loads(raw)} for mode, raw in db.execute(
            "select mode,result_json from yao_runs order by as_of_at desc,id desc limit 100")]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daily-engine"))
    from src.services.stock_king_display import LocalPicksDisplayStore
    from src.services.yao_scout.local_algorithm import algorithm_contract

    class History:
        def list_yao_runs(self, **kwargs):
            return rows

    original_data_dir = os.environ.get("YAO_SCOUT_DATA_DIR")
    with tempfile.TemporaryDirectory(prefix="stock-king-picks-smoke-") as directory:
        root = Path(directory)
        os.environ["YAO_SCOUT_DATA_DIR"] = str(root / "picks")
        expected = LocalPicksDisplayStore(History()).read()
        assert expected.get("adaptive"), "No valid historical result for migration"
        token = secrets.token_urlsafe(32)
        environment = os.environ.copy()
        environment.update({
            "DATABASE_PATH": str(root / "test.db"), "LOG_DIR": str(root / "logs"),
            "SCREENING_DATA_DIR": str(root / "screening"), "MODEL_DIR": str(root / "models"),
            "ENV_FILE": str(root / ".env"), "SCHEDULE_ENABLED": "false",
            "SCHEDULE_RUN_IMMEDIATELY": "false", "STOCK_KING_SIDECAR_TOKEN": token,
            "STOCK_KING_MARKET_URL": "", "STOCK_KING_MARKET_TOKEN": "",
        })
        environment.pop("DSA_PACKAGED_IMPORT_PROBE", None)
        (root / ".env").write_text("SCHEDULE_ENABLED=false\n", encoding="utf-8")
        durations = []
        validated_contract = None
        for _ in range(2):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            command = [str(args.engine.resolve()), "--serve-only", "--host", "127.0.0.1", "--port", str(port)]
            process = subprocess.Popen(command, cwd=root, env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def get(path):
                request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                    headers={"X-Stock-King-Token": token})
                with opener.open(request, timeout=3) as response:
                    return json.load(response)

            try:
                deadline = time.monotonic() + 60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Frozen engine exited: {process.returncode}")
                    try:
                        get("/api/v1/health")
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Frozen engine did not become ready")
                        time.sleep(.25)
                started = time.monotonic()
                actual = get("/api/v1/stock-king/picks/display")
                durations.append(round((time.monotonic() - started) * 1000))
                assert actual == expected, "Display changed after process restart"
                learning = get("/api/v1/stock-king/picks/learning")
                assert learning["algorithm"] == algorithm_contract(), "Frozen validation contract differs from live algorithm"
                assert learning["effectiveRankSource"] == "rules", "Isolated profile must use rules without a trained model"
                assert learning["validSamples"] == 0, "Restoring display must not fabricate training samples"
                validated_contract = learning["algorithm"]["contractId"]
                try:
                    get("/api/v1/stock-king/picks/refresh/tasks/missing")
                    raise AssertionError("Unknown task should return 404")
                except urllib.error.HTTPError as error:
                    assert error.code == 404
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        print(json.dumps({"frozenEngine": str(args.engine), "restartPreserved": True,
            "candidateCount": len(expected["adaptive"]["candidates"]),
            "generatedAt": expected.get("generatedAt"), "displayReadMs": durations,
            "validationContract": validated_contract,
            "historyDatabaseReadOnly": True, "profileIsolated": True}, ensure_ascii=False))
    if original_data_dir is None:
        os.environ.pop("YAO_SCOUT_DATA_DIR", None)
    else:
        os.environ["YAO_SCOUT_DATA_DIR"] = original_data_dir


if __name__ == "__main__":
    main()
