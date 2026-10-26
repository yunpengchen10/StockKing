"""Maintenance CLI uses a copy by default and changes only derived daily rows."""
from datetime import date, datetime
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3

import pytest

from src.services.yao_scout import daily_observation_outcomes as labels
from src.services.yao_scout.signal_learning import SignalLearningService

SPEC = importlib.util.spec_from_file_location('reconcile_daily_script',
    Path(__file__).resolve().parents[2] / 'scripts' / 'reconcile-daily-observations.py')
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)
NOW = datetime(2026, 10, 22, 10, 30, tzinfo=labels.TZ)
END = NOW.replace(day=23, hour=16)
DAYS = [date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26),
        date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)]


def calendar(start, end):
    return [day for day in DAYS if start <= day <= end]


def prepare(root):
    svc = SignalLearningService(root, calendar_provider=calendar, clock=lambda: NOW)
    rows = [{'code': code, 'referencePrice': 10., 'observationOnly': True,
             'quote': {'code': code, 'price': 10., 'source': 'fixture', 'source_time': NOW.isoformat()},
             'dailySignal': {'taskId': 'daily_structure_to_next_session', 'version': 'king-daily-primary-v5'}}
            for code in ('600001', '600002', '600003')]
    rows[2]['quote'].pop('source')
    svc.persist_scan({'candidates': [], 'dailyCandidates': rows, 'primarySelectionMode': 'daily_observation'},
                     '1030', NOW, True)
    labels.review(svc, NOW)
    return svc


def verified(_root, code, _day, _as_of):
    return ({'status': 'verified', 'close': 11., 'known_at': END.isoformat(), 'gaps': []}
            if code == '600001' else {'status': 'missing_close', 'gaps': ['fixture_missing_close']})


def test_default_copy_dry_run_settles_backlog_without_changing_source(tmp_path, monkeypatch):
    svc = prepare(tmp_path / 'live')
    source_bytes = svc.db_path.read_bytes()
    source_frozen = SCRIPT.frozen_snapshot(svc.db_path)
    monkeypatch.setattr(labels, 'read_verified_close', verified)
    result = SCRIPT.reconcile(svc.data_dir, tmp_path / 'report', clock=lambda: END,
                              calendar_provider=calendar, batch_labels=1)
    assert result['mode'] == 'dry_run_copy' and result['failure'] is None
    assert result['batchCount'] == result['updated'] == 3
    assert result['beforeHealth']['unreviewedDueCount'] == 3
    assert result['afterHealth']['observedCount'] == 1
    assert result['afterHealth']['invalidReferenceCount'] == 1
    assert result['afterHealth']['missingDataCount'] == 1
    assert result['afterHealth']['dueCount'] == 1  # Not mislabeled as success.
    assert result['dueAttemptAudit']['allDueAttemptedOrTerminal']
    assert result['dueAttemptAudit']['localEvidenceGaps'] == {'600002': ['2026-10-23']}
    assert svc.db_path.read_bytes() == source_bytes
    assert SCRIPT.frozen_snapshot(svc.db_path) == source_frozen
    assert result['frozenProtection']['verified']
    assert Path(result['backup']).is_file() and Path(result['receipt']).is_file()


def test_apply_backs_up_then_updates_only_daily_outcomes_and_preserves_existing_terminals(tmp_path, monkeypatch):
    svc = prepare(tmp_path / 'live')
    monkeypatch.setattr(labels, 'read_verified_close', verified)
    labels.review(svc, END, max_labels=1)
    frozen = SCRIPT.frozen_snapshot(svc.db_path)
    terminal = SCRIPT.terminal_snapshot(svc.db_path)
    result = SCRIPT.reconcile(svc.data_dir, tmp_path / 'report', apply=True, clock=lambda: END,
                              calendar_provider=calendar, batch_labels=1)
    assert result['mode'] == 'applied' and result['frozenProtection']['verified']
    assert SCRIPT.frozen_snapshot(svc.db_path) == frozen
    assert SCRIPT.terminal_snapshot(svc.db_path, set(terminal)) == terminal
    with sqlite3.connect(result['backup']) as db:
        assert db.execute("SELECT COUNT(*) FROM daily_observation_outcomes WHERE status='not_matured' AND horizon=1").fetchone()[0] == 2
    with svc._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM daily_observation_outcomes WHERE status='not_matured' AND horizon=1").fetchone()[0] == 0


def test_batch_limit_and_network_guard_are_visible_and_do_not_modify_source(tmp_path, monkeypatch):
    svc = prepare(tmp_path / 'live')
    source_bytes = svc.db_path.read_bytes()
    attempts = []
    def forbidden_fetch(*_args):
        attempts.append(1)
        socket.create_connection(('example.invalid', 443))
    monkeypatch.setattr(labels, 'read_verified_close', forbidden_fetch)
    result = SCRIPT.reconcile(svc.data_dir, tmp_path / 'report', clock=lambda: END,
                              calendar_provider=calendar, max_batches=1, batch_labels=1)
    assert attempts == [1]
    assert result['errors'] == 1 and result['batches'][0]['lastError'] == 'RuntimeError'
    assert result['stopReason'] == 'batch_limit'
    assert result['dueAttemptAudit']['failedDueLabels'] == 1
    assert not result['dueAttemptAudit']['allDueAttemptedOrTerminal']
    assert svc.db_path.read_bytes() == source_bytes


def test_missing_source_never_creates_production_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        SCRIPT.reconcile(tmp_path / 'missing', tmp_path / 'report')
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('apply', [False, True])
def test_legacy_schema_is_rejected_before_source_migration_or_backup(tmp_path, monkeypatch, apply):
    svc = prepare(tmp_path / 'legacy')
    with svc._connect() as db:
        db.execute('ALTER TABLE signal_events DROP COLUMN review_eligible')
    source_bytes = svc.db_path.read_bytes()
    frozen = SCRIPT.frozen_snapshot(svc.db_path)
    monkeypatch.setattr(SCRIPT, 'SignalLearningService', lambda *args, **kwargs:
                        pytest.fail('Service constructor must not run on a legacy source'))
    report_dir = tmp_path / 'report'
    with pytest.raises(ValueError, match='complete the software database migration'):
        SCRIPT.reconcile(svc.data_dir, report_dir, apply=apply)
    assert svc.db_path.read_bytes() == source_bytes
    assert SCRIPT.frozen_snapshot(svc.db_path) == frozen
    assert not report_dir.exists()
    with svc._connect() as db:
        assert 'review_eligible' not in {row[1] for row in db.execute('PRAGMA table_info(signal_events)')}


def test_cli_uses_actual_clock_and_exits_success_with_reported_missing_data(tmp_path, monkeypatch, capsys):
    svc = prepare(tmp_path / 'live')
    monkeypatch.setattr(labels, 'read_verified_close', verified)
    original = SCRIPT.reconcile
    monkeypatch.setattr(SCRIPT, 'reconcile', lambda *args, **kwargs: original(*args, **kwargs,
                         clock=lambda: END, calendar_provider=calendar))
    assert SCRIPT.main(['--data-dir', str(svc.data_dir), '--output-dir', str(tmp_path / 'report')]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['asOf'] == END.isoformat()
    assert report['afterHealth']['missingDataCount'] == 1
    assert report['afterHealth']['observedCount'] == 1
