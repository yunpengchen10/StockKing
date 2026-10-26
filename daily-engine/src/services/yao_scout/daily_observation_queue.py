"""Bounded local-only settlement, durable retry checkpoints and read-only health.

This queue never downloads prices, simulates execution or grants training status.
The label write and its retry checkpoint commit together; process interruption
therefore loses at most the in-flight label, not the completed batch.
"""
from datetime import date, datetime, time, timedelta
from time import monotonic
from uuid import uuid4


def init_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS daily_observation_review_queue(
            observation_id TEXT NOT NULL, horizon INTEGER NOT NULL,
            next_attempt_at TEXT NOT NULL, last_attempt_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, terminal INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, PRIMARY KEY(observation_id,horizon)
        );
        CREATE INDEX IF NOT EXISTS daily_observation_queue_due
            ON daily_observation_review_queue(terminal,next_attempt_at,last_attempt_at);
        CREATE TABLE IF NOT EXISTS daily_observation_worker_state(
            id INTEGER PRIMARY KEY CHECK(id=1), lease_owner TEXT, lease_until TEXT,
            last_started_at TEXT, last_completed_at TEXT, last_success_at TEXT,
            last_checkpoint_at TEXT, last_error TEXT, checked_count INTEGER NOT NULL DEFAULT 0
        );
    ''')


def _outcome_counts(service, cutoff):
    with service._connect() as db:
        signals = db.execute('SELECT COUNT(*) FROM daily_observation_signals WHERE created_at<=?', (cutoff,)).fetchone()[0]
        counts = {r[0]: r[1] for r in db.execute('SELECT status,COUNT(*) FROM daily_observation_outcomes '
                  'WHERE reviewed_at<=? GROUP BY status', (cutoff,))}
    return signals, counts


def run(service, as_of, *, max_labels=300, budget_seconds=4.0, retry_seconds=300):
    from .daily_observation_outcomes import _at, evaluate_reference, MEANING, TZ
    cutoff = _at(as_of)
    stamp = cutoff.isoformat()
    max_labels = max(1, min(int(max_labels), 5000))
    budget = max(0.01, min(float(budget_seconds), 30.0))
    retry_seconds = max(0, min(float(retry_seconds), 3600.0))
    started = monotonic()
    owner = uuid4().hex
    wall = datetime.now(TZ)
    with service._connect() as db:
        previous = db.execute('SELECT last_completed_at FROM daily_observation_worker_state WHERE id=1').fetchone()
    if previous and previous[0] and previous[0] > stamp:
        signals, counts = _outcome_counts(service, stamp)
        return {'status': 'historical_cutoff_skipped', 'signals': signals, 'updated': 0, 'checked': 0,
                'statusCounts': counts, 'asOf': stamp, 'trainingEligible': False, 'netReturn': None}
    with service._connect() as db:
        db.execute('INSERT OR IGNORE INTO daily_observation_worker_state(id) VALUES(1)')
        claimed = db.execute('UPDATE daily_observation_worker_state SET lease_owner=?,lease_until=?,last_started_at=? '
            'WHERE id=1 AND (lease_until IS NULL OR lease_until<=?)',
            (owner, (wall + timedelta(seconds=budget + 60)).isoformat(), stamp, wall.isoformat())).rowcount
    if not claimed:
        return {'status': 'busy', 'updated': 0, 'checked': 0, 'asOf': stamp, 'trainingEligible': False}
    updated = checked = errors = 0
    exhausted = False
    last_error = None
    calendars, closes = {}, {}
    try:
        with service._connect() as db:
            # Seed only new queue entries. Never change old references or labels.
            # This also recovers databases upgraded before a queue existed.
            db.execute('''INSERT OR IGNORE INTO daily_observation_review_queue
                (observation_id,horizon,next_attempt_at,terminal)
                SELECT s.observation_id,h.horizon,
                       CASE WHEN o.status='not_matured' AND o.label_end_at IS NOT NULL
                            THEN o.label_end_at ELSE s.created_at END,
                       CASE WHEN o.status IN ('observed','unverified_reference') THEN 1 ELSE 0 END
                FROM daily_observation_signals s CROSS JOIN
                     (SELECT 1 horizon UNION ALL SELECT 3 UNION ALL SELECT 5) h
                LEFT JOIN daily_observation_outcomes o
                  ON o.observation_id=s.observation_id AND o.horizon=h.horizon
                WHERE s.created_at<=?''', (stamp,))
            batch = db.execute('''SELECT s.observation_id,s.code,s.trading_day,s.reference_price,
                s.reference_verified,s.reference_gaps_json,q.horizon FROM daily_observation_review_queue q
                JOIN daily_observation_signals s USING(observation_id)
                LEFT JOIN daily_observation_outcomes o
                  ON o.observation_id=q.observation_id AND o.horizon=q.horizon
                WHERE q.terminal=0 AND q.next_attempt_at<=? AND s.created_at<=?
                  AND (o.reviewed_at IS NULL OR o.reviewed_at<=?)
                ORDER BY CASE WHEN o.label_end_at<=? THEN 0 ELSE 1 END,
                         q.next_attempt_at,q.last_attempt_at,s.trading_day,s.reference_at,s.code,q.horizon
                LIMIT ?''', (stamp, stamp, stamp, stamp, max_labels + 1)).fetchall()
        exhausted = len(batch) > max_labels
        for row in batch[:max_labels]:
            if monotonic() - started >= budget:
                exhausted = True
                break
            checked += 1
            label = error = None
            try:
                label = evaluate_reference(service, row, row['horizon'], cutoff, calendars, closes)
            except Exception as exc:
                # A corrupt session/calendar must not block other stocks.
                error = type(exc).__name__
                errors += 1
                last_error = error
            terminal = bool(label and label[3] in {'observed', 'unverified_reference'})
            next_attempt = (label[7] if label and label[3] == 'not_matured' else
                            (cutoff + timedelta(seconds=retry_seconds)).isoformat())
            label_written = False
            with service._connect() as db:
                old = db.execute('SELECT * FROM daily_observation_outcomes WHERE observation_id=? AND horizon=?',
                                 (row['observation_id'], row['horizon'])).fetchone()
                # Concurrent/historical calls cannot replace a known label.
                if old and (old['reviewed_at'] > stamp or old['status'] in {'observed', 'unverified_reference'}):
                    continue
                if label and (not old or tuple(old)[:9] + tuple(old)[10:] != label[:9] + label[10:]):
                    db.execute('''INSERT INTO daily_observation_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(observation_id,horizon) DO UPDATE SET
                        target_day=excluded.target_day,status=excluded.status,observed_close=excluded.observed_close,
                        gross_reference_change_pct=excluded.gross_reference_change_pct,net_return=NULL,
                        label_end_at=excluded.label_end_at,label_known_at=excluded.label_known_at,
                        reviewed_at=excluded.reviewed_at,details_json=excluded.details_json
                        WHERE daily_observation_outcomes.reviewed_at<=excluded.reviewed_at
                          AND daily_observation_outcomes.status NOT IN ('observed','unverified_reference')''', label)
                    label_written = True
                db.execute('UPDATE daily_observation_review_queue SET next_attempt_at=?,last_attempt_at=?, '
                           'attempts=attempts+1,terminal=?,last_error=? WHERE observation_id=? AND horizon=?',
                           (next_attempt, stamp, int(terminal), error, row['observation_id'], row['horizon']))
                db.execute('UPDATE daily_observation_worker_state SET last_checkpoint_at=? WHERE id=1 AND lease_owner=?',
                           (stamp, owner))
            updated += int(label_written)
    except Exception as exc:
        errors += 1
        last_error = type(exc).__name__
        exhausted = True
    finally:
        with service._connect() as db:
            db.execute('UPDATE daily_observation_worker_state SET lease_owner=NULL,lease_until=NULL,last_completed_at=?, '
                       'last_success_at=CASE WHEN ?=0 THEN ? ELSE last_success_at END,last_error=?,checked_count=? '
                       'WHERE id=1 AND lease_owner=?', (stamp, errors, stamp, last_error, checked, owner))
    signals, counts = _outcome_counts(service, stamp)
    return {'status': 'partial_error' if errors else 'budget_exhausted' if exhausted else 'completed',
            'signals': signals, 'updated': updated, 'checked': checked, 'errors': errors,
            'lastError': last_error, 'budgetExhausted': exhausted, 'statusCounts': counts,
            'asOf': stamp, 'elapsedSeconds': round(monotonic() - started, 3),
            'meaning': MEANING, 'trainingEligible': False, 'netReturn': None}


def status(service, as_of=None):
    """Read only. Recompute due counts even when the worker has stopped running."""
    from .daily_observation_outcomes import _at, TZ
    cutoff = _at(as_of or service.clock())
    stamp = cutoff.isoformat()
    with service._connect() as db:
        groups = db.execute('''SELECT s.trading_day,h.horizon,o.status,o.label_end_at,COUNT(*) n
            FROM daily_observation_signals s CROSS JOIN
                 (SELECT 1 horizon UNION ALL SELECT 3 UNION ALL SELECT 5) h
            LEFT JOIN daily_observation_outcomes o ON o.observation_id=s.observation_id
                 AND o.horizon=h.horizon AND o.reviewed_at<=?
            WHERE s.created_at<=? GROUP BY s.trading_day,h.horizon,o.status,o.label_end_at''', (stamp, stamp)).fetchall()
        state = db.execute('SELECT * FROM daily_observation_worker_state WHERE id=1').fetchone()
        refs = db.execute('SELECT COUNT(*),COALESCE(SUM(reference_verified),0),COUNT(DISTINCT trading_day) '
                          'FROM daily_observation_signals WHERE created_at<=?', (stamp,)).fetchone()
    counts = dict(maturedCount=0, settledCount=0, observedCount=0, dueCount=0, overdueCount=0,
                  missingDataCount=0, unreviewedDueCount=0, invalidReferenceCount=0,
                  pendingCount=0, calendarUnknownCount=0)
    calendars = {}
    oldest_due = None
    for row in groups:
        end = _at(row['label_end_at']) if row['label_end_at'] else None
        if end is None:
            day = date.fromisoformat(row['trading_day'])
            if day not in calendars:
                calendars[day] = service._sessions(service.calendar_provider, day, day + timedelta(days=60))
            sessions = calendars[day]
            if sessions and sessions[0] == day and len(sessions) > row['horizon']:
                end = datetime.combine(sessions[row['horizon']], time(15), TZ)
        n = row['n']
        if end is None:
            counts['calendarUnknownCount'] += n
        elif end > cutoff:
            counts['pendingCount'] += n
        else:
            counts['maturedCount'] += n
            if row['status'] in {'observed', 'unverified_reference'}:
                counts['settledCount'] += n
                counts['observedCount' if row['status'] == 'observed' else 'invalidReferenceCount'] += n
            else:
                counts['dueCount'] += n
                counts['overdueCount'] += n if end + timedelta(minutes=15) < cutoff else 0
                counts['missingDataCount'] += n if row['status'] in {'missing_close', 'unverified_close'} else 0
                counts['unreviewedDueCount'] += n if row['status'] in {None, 'not_matured', 'calendar_unavailable'} else 0
                oldest_due = min(oldest_due, end) if oldest_due else end
    state = dict(state) if state else {}
    # No label or worker metadata from the future in an as-of report.
    visible_state = {k: v for k, v in state.items() if k not in {'id', 'lease_owner', 'lease_until'}}
    if state.get('last_started_at') and state['last_started_at'] > stamp:
        visible_state = {}
    return {'asOf': stamp, **counts, 'oldestDueAt': oldest_due.isoformat() if oldest_due else None,
            'signalCount': refs[0], 'referenceVerifiedCount': refs[1], 'independentTradingDays': refs[2],
            'lastStartedAt': visible_state.get('last_started_at'),
            'lastCompletedAt': visible_state.get('last_completed_at'),
            'lastSuccessAt': visible_state.get('last_success_at'),
            'lastCheckpointAt': visible_state.get('last_checkpoint_at'),
            'lastError': visible_state.get('last_error'),
            'lastCheckedCount': visible_state.get('checked_count'),
            'meaning': '到期未结算、缺收盘证据及执行积压分别计数；成功执行不表示全部观察已有结果',
            'readOnly': True, 'trainingEligible': False}
