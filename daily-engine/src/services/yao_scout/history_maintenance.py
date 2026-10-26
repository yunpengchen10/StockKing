"""Bounded history preparation after publication, with a cross-process lease."""
from datetime import datetime, timedelta
import logging
import sqlite3
from threading import Thread
from time import monotonic, time
from uuid import uuid4

from .history_preparation import _write_status, prepare_history
from .intraday_evidence import SHANGHAI


def _claim(directory, owner, *, duration=240):
    with sqlite3.connect(directory / 'history-maintenance-lease.sqlite3', timeout=1) as db:
        db.execute('CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY, owner TEXT, expires REAL)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT expires FROM lease WHERE id=1').fetchone()
        if row and row[0] > time():
            return False
        db.execute('INSERT OR REPLACE INTO lease VALUES(1,?,?)', (owner, time()+duration))
    return True


def _release(directory, owner):
    with sqlite3.connect(directory / 'history-maintenance-lease.sqlite3', timeout=1) as db:
        db.execute('DELETE FROM lease WHERE id=1 AND owner=?', (owner,))


def next_preparation_session(now):
    if now.hour < 15:
        return now.date()
    from .research_factors import _sessions
    days = _sessions(now.date()+timedelta(days=1), now.date()+timedelta(days=15))
    return min(days) if days else now.date()


def run_history_preparation(data_dir, codes, now, *, timeout=120, max_codes=40):
    directory = data_dir / 'minute_history'
    directory.mkdir(parents=True, exist_ok=True)
    owner = uuid4().hex
    if not _claim(directory, owner, duration=max(240, timeout+120)):
        return {'status': 'already_running'}
    try:
        return prepare_history(codes, now, directory, timeout=timeout, max_codes=max_codes,
                               target_session=next_preparation_session(now))
    finally:
        _release(directory, owner)


def _prepare_batches(directory, codes, *, timeout=600, batch_size=20):
    """One bounded pass through the queue, resuming the persisted code cursor.

    Missing providers and quarantined sessions remain explicit failures. A
    worker never spins repeatedly on the same unreachable stock in this pass.
    """
    started = datetime.now(SHANGHAI)
    target_session = next_preparation_session(started)
    deadline = monotonic() + max(0, timeout)
    path = directory / 'history-maintenance-status.json'
    progress = {'version': 'bounded-history-maintenance-v1', 'status': 'running',
        'startedAt': started.isoformat(), 'requested': len(codes), 'processed': 0,
        'batches': 0, 'maxSeconds': timeout, 'maxCodesPerBatch': batch_size,
        'meaning': '后台按持久游标逐批覆盖队列；未补齐的真实缺口保留，不改写历史信号'}
    def checkpoint():
        progress['updatedAt'] = datetime.now(SHANGHAI).isoformat()
        _write_status(path, progress)
    checkpoint()
    try:
        while progress['processed'] < len(codes) and monotonic() < deadline:
            remaining = deadline - monotonic()
            result = prepare_history(codes, datetime.now(SHANGHAI), directory,
                timeout=min(120, remaining), max_codes=min(batch_size, len(codes)-progress['processed']),
                target_session=target_session)
            processed = max(0, min(int(result.get('processedThisRun', 0)),
                                   len(codes)-progress['processed']))
            progress.update(processed=progress['processed']+processed,
                batches=progress['batches']+1, lastCode=result.get('lastCode'),
                preparationStatus=result.get('status'), ready=result.get('ready', 0),
                fullSessionReady=result.get('fullSessionReady', 0),
                preparedWithUsableClocks=result.get('preparedWithUsableClocks', 0),
                pending=result.get('pending', len(codes)), targetSession=result.get('targetSession'))
            checkpoint()
            if not processed or result.get('status') == 'unavailable':
                break
        progress.update(status='budget_exhausted' if monotonic() >= deadline else
            'completed' if progress.get('pending') == 0 else 'partial',
            queueCovered=progress['processed'] >= len(codes),
            remainingUnvisited=max(0, len(codes)-progress['processed']),
            finishedAt=datetime.now(SHANGHAI).isoformat())
    except Exception as error:
        progress.update(status='failed', error=type(error).__name__,
                        finishedAt=datetime.now(SHANGHAI).isoformat())
        raise
    finally:
        checkpoint()
    return progress


def start_history_preparation(data_dir, codes, *, timeout=600, batch_size=20):
    """A scan never waits for historical network I/O or repairs its frozen signal."""
    from src.services.software_market import SoftwareMarketClient
    codes = list(dict.fromkeys(codes))
    if not SoftwareMarketClient.from_environment().available or not codes:
        return {'status': 'not_started', 'reason': 'software_gateway_or_codes_unavailable'}
    directory = data_dir / 'minute_history'
    directory.mkdir(parents=True, exist_ok=True)
    owner = uuid4().hex
    try:
        if not _claim(directory, owner, duration=max(240, timeout+120)):
            return {'status': 'already_running'}
    except sqlite3.Error:
        return {'status': 'deferred', 'reason': 'maintenance_lease_busy'}

    def work():
        try:
            _prepare_batches(directory, codes, timeout=timeout, batch_size=max(1, batch_size))
        except Exception:
            logging.getLogger(__name__).exception('Historical preparation deferred; published scan is unchanged')
        finally:
            _release(directory, owner)

    try:
        Thread(target=work, name='stock-king-history-preparation', daemon=True).start()
    except Exception:
        _release(directory, owner)
        raise
    return {'status': 'started', 'maxSeconds': timeout, 'maxCodes': len(codes),
            'maxCodesPerBatch': max(1, batch_size),
            'meaning': '扫描发布后补齐历史；已发布信号和训练资格保持原样'}
