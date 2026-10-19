"""Bounded history preparation after publication, with a cross-process lease."""
from datetime import datetime, timedelta
import logging
import sqlite3
from threading import Thread
from time import time
from uuid import uuid4

from .history_preparation import prepare_history
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


def start_history_preparation(data_dir, codes):
    """A scan never waits for historical network I/O or repairs its frozen signal."""
    from src.services.software_market import SoftwareMarketClient
    if not SoftwareMarketClient.from_environment().available or not codes:
        return {'status': 'not_started', 'reason': 'software_gateway_or_codes_unavailable'}
    directory = data_dir / 'minute_history'
    directory.mkdir(parents=True, exist_ok=True)
    owner = uuid4().hex
    try:
        if not _claim(directory, owner):
            return {'status': 'already_running'}
    except sqlite3.Error:
        return {'status': 'deferred', 'reason': 'maintenance_lease_busy'}

    def work():
        try:
            now = datetime.now(SHANGHAI)
            prepare_history(codes, now, directory, timeout=120, max_codes=20,
                            target_session=next_preparation_session(now))
        except Exception:
            logging.getLogger(__name__).exception('Historical preparation deferred; published scan is unchanged')
        finally:
            _release(directory, owner)

    try:
        Thread(target=work, name='stock-king-history-preparation', daemon=True).start()
    except Exception:
        _release(directory, owner)
        raise
    return {'status': 'started', 'maxSeconds': 120, 'maxCodes': 20,
            'meaning': '扫描发布后补齐历史；已发布信号和训练资格保持原样'}
