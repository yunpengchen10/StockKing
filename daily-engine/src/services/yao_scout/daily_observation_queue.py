"""Bounded settlement, durable retry checkpoints and read-only health.

Intraday runs use local evidence. The dedicated close review can obtain unadjusted
daily bars for previously published recommendations.
The label write and its retry checkpoint commit together; process interruption
therefore loses at most the in-flight label, not the completed batch.
"""
from datetime import date, datetime, time, timedelta
from time import monotonic
from uuid import uuid4


def init_schema(db):
    db.executescript('''
        CREATE INDEX IF NOT EXISTS daily_observation_published
            ON daily_observation_signals(first_category,observation_id);
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


def _tencent_daily_confirmation(code, days, cutoff):
    """Require Tencent's explicit security key and unadjusted day array."""
    from .history_preparation import symbol, SHANGHAI
    import requests
    security = symbol(code)
    response = requests.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
        params={'param': f'{security},day,{min(days)},{max(days)},640,'},
        headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
    response.raise_for_status()
    result = response.json()
    if result.get('code') not in (0, '0'):
        return {}
    observed = datetime.now(SHANGHAI).isoformat()
    anchors = {}
    for row in (result.get('data') or {}).get(security, {}).get('day') or []:
        if isinstance(row, list) and len(row) >= 6 and row[0] in days:
            day = row[0]
            if day in anchors:
                anchors[day] = None  # ambiguous duplicates never confirm a close
            else:
                anchors[day] = dict(kind='daily_ohlc', code=code, day=day, source='tencent daily unadjusted',
                    price_basis='unadjusted', complete=True, observed_at=observed,
                    open=row[1], close=row[2], high=row[3], low=row[4])
    return anchors


def review_published_closes(service, *, daily_fetcher=None, confirmation_fetcher=None, timeout=30, max_labels=1000):
    """Fetch missing target closes only in the explicit current-time review."""
    from .history_preparation import fetch_daily_anchors
    return run(service, service.clock(), max_labels=max_labels, budget_seconds=30,
               daily_fetcher=daily_fetcher or fetch_daily_anchors, fetch_timeout=timeout,
               confirmation_fetcher=confirmation_fetcher or _tencent_daily_confirmation)


def _fetch_closes(service, rows, cutoff, calendars, closes, fetcher, confirm, timeout):
    from .daily_observation_outcomes import evaluate_reference, _at, TZ
    from .history_preparation import validated_daily_anchor
    from .minute_history import bounded_fetch_map
    requested, conflicted = {}, {}
    for row in rows:
        label = evaluate_reference(service, row, row['horizon'], cutoff, calendars, closes)
        if label[3] in {'missing_close', 'unverified_close'}:
            local = closes[row['code'], label[2]]
            if local.get('gaps') == ['session_quality_tainted']:
                conflicted.setdefault(row['code'], set()).add(label[2])
            if (local['status'] == 'missing_close' or local.get('gaps') in (
                    ['session_quality_not_verified'], ['session_quality_tainted'],
                    ['verification_not_known_at_review_cutoff'])):
                requested.setdefault(row['code'], set()).add(label[2])
    def fetch(code):
        anchors = fetcher(code, requested[code], cutoff)
        corroboration = confirm(code, conflicted[code], cutoff) if code in conflicted else {}
        return code, anchors, corroboration
    responses, incomplete = bounded_fetch_map(fetch, requested, workers=6, timeout=timeout)
    checked_at = _at(service.clock())
    for code, anchors, corroboration in responses:
        for day in requested[code]:
            raw = anchors.get(day) if isinstance(anchors, dict) else None
            anchor = validated_daily_anchor(raw, code, day, checked_at)
            second = None
            if anchor and day in conflicted.get(code, ()):
                second = validated_daily_anchor(corroboration.get(day), code, day, checked_at)
                if (not second or second['source'] == anchor['source']
                        or float(second['close']) != float(anchor['close'])):
                    closes[code, day]['gaps'].append('independent_daily_close_not_corroborated')
                    continue
            if anchor:
                closes[code, day] = {'status': 'verified', 'close': float(anchor['close']),
                    'source': anchor['source'], 'source_time': anchor.get('source_time'),
                    'known_at': checked_at.isoformat(), 'close_at': datetime.combine(date.fromisoformat(day), time(15), TZ).isoformat(),
                    'evidence': 'verified_complete_daily_ohlc', 'price_basis': 'unadjusted', 'verification': anchor, 'gaps': []}
                if second:
                    closes[code, day].update(corroboration=second, minuteArchiveStatus='tainted')
            else:
                closes[code, day]['gaps'].append('provider_daily_anchor_invalid' if raw else 'provider_target_daily_bar_missing')
    for code in incomplete:
        for day in requested[code]:
            closes[code, day]['gaps'].append('daily_anchor_request_failed_or_timed_out')
    return checked_at


def run(service, as_of, *, max_labels=300, budget_seconds=4.0, retry_seconds=300,
        daily_fetcher=None, confirmation_fetcher=None, fetch_timeout=0):
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
            (owner, (wall + timedelta(seconds=budget + fetch_timeout + 60)).isoformat(), stamp, wall.isoformat())).rowcount
    if not claimed:
        return {'status': 'busy', 'updated': 0, 'checked': 0, 'asOf': stamp, 'trainingEligible': False}
    updated = checked = errors = 0
    exhausted = False
    last_error = None
    calendars, closes = {}, {}
    try:
        with service._connect() as db:
            # Queue rows are inserted together for each immutable signal. A
            # count mismatch also recovers interrupted/legacy queue creation.
            signal_count = db.execute('SELECT COUNT(*) FROM daily_observation_signals').fetchone()[0]
            queued_count = db.execute('SELECT COUNT(*) FROM daily_observation_review_queue').fetchone()[0]
            if queued_count != 3 * signal_count:
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
            due_query = '''SELECT s.observation_id,s.code,s.trading_day,s.reference_price,
                s.reference_verified,s.reference_gaps_json,q.horizon FROM {signals_and_queue}
                LEFT JOIN daily_observation_outcomes o
                  ON o.observation_id=q.observation_id AND o.horizon=q.horizon
                WHERE q.terminal=0 AND s.created_at<=? AND (o.reviewed_at IS NULL OR o.reviewed_at<=?)'''
            order = ''' ORDER BY CASE WHEN o.status='not_matured' AND o.label_end_at<=? THEN 0
                                   WHEN q.attempts=0 THEN 1 ELSE 2 END,
                         q.next_attempt_at,q.last_attempt_at,s.trading_day,s.reference_at,s.code,q.horizon
                LIMIT ?'''
            # Use the small published cohort index before sorting tens of
            # thousands of due control labels. New independent evidence may
            # resolve a recent local miss before its ordinary retry cooldown.
            due = (" AND (q.next_attempt_at<=? OR o.status IN ('missing_close','unverified_close','calendar_unavailable'))"
                   if daily_fetcher else " AND q.next_attempt_at<=?")
            published_query = due_query.format(signals_and_queue='daily_observation_signals s CROSS JOIN '
                                               'daily_observation_review_queue q USING(observation_id)')
            batch = db.execute(published_query + " AND s.first_category IN ('opportunity_recommendation','selected','profile_selected')"
                               + due + order, (stamp, stamp, stamp, stamp, max_labels + 1)).fetchall()
            priority_batch = bool(batch)
            general_query = due_query.format(signals_and_queue='daily_observation_review_queue q JOIN '
                                             'daily_observation_signals s USING(observation_id)')
            if not batch and not daily_fetcher:
                batch = db.execute(general_query + due + order, (stamp, stamp, stamp, stamp, max_labels + 1)).fetchall()
        exhausted = len(batch) > max_labels
        if daily_fetcher and batch:
            cutoff = _fetch_closes(service, batch[:max_labels], cutoff, calendars, closes, daily_fetcher,
                                   confirmation_fetcher, fetch_timeout)
            stamp = cutoff.isoformat()
            started = monotonic()  # independent write budget after bounded public reads
        def pending_rows():
            nonlocal exhausted
            yield from batch[:max_labels]
            if not priority_batch or daily_fetcher or len(batch) >= max_labels:
                return
            if monotonic() - started >= budget:
                exhausted = True
                return
            remaining = max_labels - len(batch)
            with service._connect() as db:
                controls = db.execute(general_query +
                    " AND s.first_category NOT IN ('opportunity_recommendation','selected','profile_selected')" +
                    due + order, (stamp, stamp, stamp, stamp, remaining + 1)).fetchall()
            exhausted = len(controls) > remaining
            yield from controls[:remaining]
        for row in pending_rows():
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
