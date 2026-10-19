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
    parser.add_argument("--expected-recommendations", type=int)
    parser.add_argument("--expected-premarket", type=int)
    parser.add_argument("--expected-daily", type=int)
    args = parser.parse_args()
    source = args.history_db.resolve(strict=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daily-engine"))
    from src.services.stock_king_display import LocalPicksDisplayStore
    from src.services.yao_scout.local_algorithm import algorithm_contract

    class History:
        def list_yao_display_runs(self, *, modes, statuses, limit=100):
            # Match the production header API while keeping the source read-only.
            mode_slots = ','.join('?' for _ in modes)
            status_slots = ','.join('?' for _ in statuses)
            query = f"""select run_id, mode,
                json_extract(result_json, '$.generatedAt', '$.as_of', '$.cutoff_at',
                    '$.scanSlot', '$.status', '$.dataQuality', '$.data_quality', '$.premarketAssessment',
                    '$.primarySelectionMode', '$.primaryObjective', '$.dailyAssessment'),
                json_type(result_json, '$.candidates'),
                json_array_length(result_json, '$.nextDayWatchlist'),
                json_array_length(result_json, '$.nextDayContinuationWatchlist')
                from yao_runs where id in (select id from yao_runs
                where mode in ({mode_slots}) and status in ({status_slots})
                order by as_of_at desc,id desc limit ?)"""
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
                headers = db.execute(query, (*modes, *statuses, max(1, min(limit, 100)))).fetchall()
            rows = []
            for run_id, mode, raw, candidate_type, next_day_count, continuation_count in headers:
                metadata = dict(zip(('generatedAt', 'as_of', 'cutoff_at', 'scanSlot',
                    'status', 'dataQuality', 'data_quality', 'premarketAssessment',
                    'primarySelectionMode', 'primaryObjective', 'dailyAssessment'), json.loads(raw)))
                metadata.update(candidates=[] if candidate_type == 'array' else None,
                    nextDayWatchlist=[True] if next_day_count else [],
                    nextDayContinuationWatchlist=[True] if continuation_count else [])
                rows.append({'run_id': run_id, 'mode': mode, 'result': metadata})
            return rows

        def get_yao_run(self, run_id):
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute('select mode,result_json from yao_runs where run_id=?',
                                 (run_id,)).fetchone()
            return {'run_id': run_id, 'mode': row[0], 'result': json.loads(row[1])} if row else None

    original_data_dir = os.environ.get("YAO_SCOUT_DATA_DIR")
    with tempfile.TemporaryDirectory(prefix="stock-king-picks-smoke-") as directory:
        root = Path(directory)
        os.environ["YAO_SCOUT_DATA_DIR"] = str(root / "picks")
        expected = LocalPicksDisplayStore(History()).read()
        assert expected.get("adaptive"), "No valid historical result for migration"
        if args.expected_recommendations is not None:
            assert expected.get("savedRecommendationCount") == args.expected_recommendations, "Saved research recommendation count is incorrect"
        if args.expected_premarket is not None:
            assert expected.get("premarketCandidateCount") == args.expected_premarket, "Saved opening plan count is incorrect"
            assert expected['adaptive']['candidates'] == [], "Opening plans must not become intraday entries"
        if args.expected_daily is not None:
            assert expected.get("dailyCandidateCount") == args.expected_daily, "Saved daily candidate count is incorrect"
            assert expected['adaptive'].get('primarySelectionMode') == 'daily_conditional', "Daily branch identity was lost"
            assert expected.get('tradeCandidateCount') == args.expected_daily
            assert expected.get('savedRecommendationCount') == args.expected_daily
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
                assert learning["taskId"] == "entry_today_exit_next_session"
                assert len(learning["taskStates"]) == 7, "Primary, two next-day and four morning queues need independent state"
                morning_states = [state for state in learning['taskStates']
                                  if state.get('taskId') == 'morning_signal_to_same_session_close']
                assert len(morning_states) == 4, 'Morning registries must stay independent'
                assert all(not state.get('championVersion') for state in morning_states), 'Cold profile must not promote a morning model'
                assert learning.get("lastFitCompletedAt") is None, "A check cannot masquerade as fitting"
                assert learning.get("activatedAt") is None, "Isolated cold start must not activate a model"
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
            "nextDayCandidateCount": len(expected["adaptive"].get("nextDayWatchlist") or []),
            "continuationCandidateCount": len(expected["adaptive"].get("nextDayContinuationWatchlist") or []),
            "savedRecommendationCount": expected.get("savedRecommendationCount"),
            "premarketCandidateCount": expected.get("premarketCandidateCount"),
            "dailyCandidateCount": expected.get("dailyCandidateCount"),
            "primarySelectionMode": expected['adaptive'].get('primarySelectionMode'),
            "generatedAt": expected.get("generatedAt"), "displayReadMs": durations,
            "validationContract": validated_contract,
            "historyDatabaseReadOnly": True, "profileIsolated": True}, ensure_ascii=False))
    if original_data_dir is None:
        os.environ.pop("YAO_SCOUT_DATA_DIR", None)
    else:
        os.environ["YAO_SCOUT_DATA_DIR"] = original_data_dir


if __name__ == "__main__":
    main()
