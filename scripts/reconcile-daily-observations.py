"""Settle only daily observations from existing local evidence.

Default: consistent SQLite-copy dry run. --apply first makes the same backup,
then updates only derived daily labels and their worker checkpoints. Close other
ledger writers before applying; concurrent frozen-row changes fail verification.
There is no historical-as-of CLI option, price fetch, notification or training.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daily-engine'))
from src.services.yao_scout.signal_learning import SignalLearningService

TZ = ZoneInfo('Asia/Shanghai')
DERIVED = {'daily_observation_outcomes', 'daily_observation_review_queue', 'daily_observation_worker_state'}
REQUIRED = {'daily_observation_signals', 'daily_observation_appearances', 'signal_runs',
            'signal_events', 'model_versions', 'forward_predictions', 'daily_observation_outcomes'}


def _ro(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def validate_source_schema(path):
    # The service constructor contains a legacy migration that writes frozen
    # signal rows. Refuse that schema before creating a service or any backup.
    with _ro(path) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(signal_events)')}
    if 'review_eligible' not in columns:
        raise ValueError('Source schema lacks signal_events.review_eligible; '
                         'complete the software database migration before retrying daily reconciliation')


def frozen_snapshot(path):
    """Hash all existing non-daily-derived tables, including exact JSON bytes."""
    with _ro(path) as db:
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not REQUIRED <= tables:
            raise ValueError('Missing required ledger tables: ' + ','.join(sorted(REQUIRED - tables)))
        result = {}
        for table in sorted(tables - DERIVED):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = list(db.execute('PRAGMA table_info(' + quoted + ')'))
            keys = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
            order = ','.join('"' + k.replace('"', '""') + '"' for k in keys) or 'rowid'
            digest, count = sha256(), 0
            for row in db.execute('SELECT * FROM ' + quoted + ' ORDER BY ' + order):
                digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':'),
                                        default=lambda b: {'blobHex': bytes(b).hex()}).encode('utf-8'))
                digest.update(b'\n')
                count += 1
            result[table] = {'rows': count, 'sha256': digest.hexdigest()}
        return result


def terminal_snapshot(path, keys=None):
    with _ro(path) as db:
        result = {}
        for row in db.execute("SELECT * FROM daily_observation_outcomes WHERE status IN ('observed','unverified_reference') ORDER BY observation_id,horizon"):
            key = row['observation_id'] + ':' + str(row['horizon'])
            if keys is None or key in keys:
                result[key] = sha256(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        return result


def due_attempt_audit(service, cutoff):
    """Distinguish attempted-but-missing evidence from unattempted due work."""
    calendars = {}
    summary = dict(dueLabels=0, terminalLabels=0, attemptedDueLabels=0,
                   attemptedThisReconciliation=0, unattemptedDueLabels=0,
                   failedDueLabels=0, calendarUnknownLabels=0, missingCloseLabels=0)
    missing = {}
    with service._connect() as db:
        rows = db.execute('''SELECT s.code,s.trading_day,s.reference_verified,h.horizon,
            o.status,o.label_end_at,o.reviewed_at,q.last_attempt_at,q.last_error
            FROM daily_observation_signals s CROSS JOIN
                 (SELECT 1 horizon UNION ALL SELECT 3 UNION ALL SELECT 5) h
            LEFT JOIN daily_observation_outcomes o ON o.observation_id=s.observation_id
                 AND o.horizon=h.horizon AND o.reviewed_at<=?
            LEFT JOIN daily_observation_review_queue q ON q.observation_id=s.observation_id AND q.horizon=h.horizon
            WHERE s.created_at<=?''', (cutoff.isoformat(), cutoff.isoformat())).fetchall()
    for row in rows:
        end = datetime.fromisoformat(row['label_end_at']) if row['label_end_at'] else None
        if end is None:
            day = date.fromisoformat(row['trading_day'])
            if day not in calendars:
                calendars[day] = service._sessions(service.calendar_provider, day, day + timedelta(days=60))
            sessions = calendars[day]
            if sessions and sessions[0] == day and len(sessions) > row['horizon']:
                end = datetime.combine(sessions[row['horizon']], time(15), TZ)
        if end is None:
            summary['calendarUnknownLabels'] += 1
            continue
        if end > cutoff:
            continue
        summary['dueLabels'] += 1
        if row['status'] in {'observed', 'unverified_reference'}:
            summary['terminalLabels'] += 1
            continue
        attempted = datetime.fromisoformat(row['last_attempt_at']) if row['last_attempt_at'] else None
        if attempted and end <= attempted <= cutoff:
            summary['attemptedDueLabels'] += 1
            summary['attemptedThisReconciliation'] += int(attempted == cutoff)
            summary['failedDueLabels'] += int(bool(row['last_error']))
        else:
            summary['unattemptedDueLabels'] += 1
        if row['status'] in {'missing_close', 'unverified_close'}:
            summary['missingCloseLabels'] += 1
            if row['reference_verified']:
                missing.setdefault(row['code'], set()).add(end.date().isoformat())
    return {**summary, 'allDueAttemptedOrTerminal': summary['unattemptedDueLabels'] == 0,
            'localEvidenceGaps': {code: sorted(days) for code, days in sorted(missing.items())},
            'meaning': 'A successful attempt with missing or quarantined close evidence is not an observed return.'}


def _network_forbidden(*_args, **_kwargs):
    raise RuntimeError('Network access is forbidden during daily-only reconciliation')


def reconcile(data_dir, output_dir, *, apply=False, max_batches=200,
              batch_labels=300, batch_seconds=4.0, clock=None, calendar_provider=None):
    root, output = Path(data_dir).resolve(), Path(output_dir).resolve()
    source = root / 'signal-learning-v1.sqlite3'
    if not source.is_file():
        raise FileNotFoundError(source)
    validate_source_schema(source)
    if not 1 <= int(max_batches) <= 200:
        raise ValueError('max_batches must be in 1..200')
    cutoff = (clock or (lambda: datetime.now(TZ)))()
    if cutoff.tzinfo is None:
        raise ValueError('Reconciliation clock must be timezone-aware')
    cutoff = cutoff.astimezone(TZ)
    output.mkdir(parents=True, exist_ok=True)
    tag = cutoff.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8]
    backup = output / (('daily-observation-backup-' if apply else 'daily-observation-copy-') + tag + '.sqlite3')
    # Backup reads the source through mode=ro, including its committed WAL.
    with _ro(source) as src, sqlite3.connect(backup) as dest:
        src.backup(dest)
    target = source if apply else backup
    before_frozen = frozen_snapshot(target)
    terminal_before = terminal_snapshot(target)
    batches = []
    stop_reason = 'batch_limit'
    failure = None
    before = after = audit = None
    with ExitStack() as guard:
        # Protect against a future accidental fetch added below this entrypoint.
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guard.enter_context(patch(name, _network_forbidden))
        service = SignalLearningService(root, db_path=target, clock=lambda: cutoff,
            calendar_provider=calendar_provider, bar_fetcher=_network_forbidden)
        if frozen_snapshot(target) != before_frozen:
            failure = 'Frozen rows changed during schema initialization'
        else:
            before = service.daily_observation_status(cutoff)
            try:
                for _ in range(int(max_batches)):
                    batch = service.review_daily_observations(cutoff, max_labels=batch_labels, budget_seconds=batch_seconds)
                    batches.append(batch)
                    after = service.daily_observation_status(cutoff)
                    if batch.get('status') == 'busy':
                        stop_reason = 'worker_busy'
                        break
                    if batch.get('checked', 0) == 0:
                        stop_reason = 'no_due_attempt_available'
                        break
                    if after['unreviewedDueCount'] == 0:
                        stop_reason = 'all_due_labels_reviewed'
                        break
            except Exception as exc:
                failure = type(exc).__name__ + ': ' + str(exc)[:240]
                stop_reason = 'exception'
            after = service.daily_observation_status(cutoff)
            audit = due_attempt_audit(service, cutoff)
    after_frozen = frozen_snapshot(target)
    terminal_after = terminal_snapshot(target, set(terminal_before))
    protected = before_frozen == after_frozen and terminal_before == terminal_after
    if not protected:
        failure = failure or 'Frozen rows or previously terminal labels changed; check concurrent writers'
    receipt = {'mode': 'applied' if apply else 'dry_run_copy', 'asOf': cutoff.isoformat(),
        'source': str(source), 'target': str(target), 'backup': str(backup),
        'backupRole': 'pre_apply_snapshot' if apply else 'consistent_working_copy',
        'beforeHealth': before, 'afterHealth': after, 'dueAttemptAudit': audit,
        'batchCount': len(batches), 'updated': sum(b.get('updated', 0) for b in batches),
        'checked': sum(b.get('checked', 0) for b in batches),
        'errors': sum(b.get('errors', 0) for b in batches), 'stopReason': stop_reason,
        'failure': failure, 'batches': batches,
        'frozenProtection': {'verified': protected, 'before': before_frozen, 'after': after_frozen,
                            'existingTerminalLabelsChecked': len(terminal_before),
                            'existingTerminalLabelsUnchanged': terminal_before == terminal_after},
        'limitations': ['Only existing local verified close evidence is used.',
                       'Missing or quarantined closes remain unknown; no current quotes replace historical closes.',
                       'Invalid frozen references remain invalid; they are never repaired with later prices.',
                       'No price downloads, notifications, executable reviews, training, or model publication.']}
    path = output / ('daily-observation-reconciliation-' + tag + '.json')
    receipt['receipt'] = str(path)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-batches', type=int, default=200)
    args = parser.parse_args(argv)
    result = reconcile(args.data_dir, args.output_dir, apply=args.apply, max_batches=args.max_batches)
    print(json.dumps({key: result[key] for key in ('mode', 'asOf', 'batchCount', 'updated', 'checked',
          'errors', 'stopReason', 'failure', 'beforeHealth', 'afterHealth', 'dueAttemptAudit', 'receipt')}, ensure_ascii=False))
    incomplete = (result['failure'] or result['errors'] or not result['frozenProtection']['verified']
                  or not (result['dueAttemptAudit'] or {}).get('allDueAttemptedOrTerminal')
                  or (result['afterHealth'] or {}).get('calendarUnknownCount'))
    return 1 if incomplete else 0


if __name__ == '__main__':
    raise SystemExit(main())
