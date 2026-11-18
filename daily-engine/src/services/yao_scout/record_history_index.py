"""Compact, rebuildable projections of immutable publication lists for record reads."""
import json
import sqlite3
import zlib


SAVED_LISTS_SQL = """json_extract(scan_json,'$.result.run_id','$.result.runId','$.sourceRunId',
    '$.result.nextDayWatchlist','$.result.nextDayContinuationWatchlist',
    '$.result.opportunityChannels.recommendations','$.result.marketBrief.asOf',
    '$.result.marketBrief.focusStocks')"""


def init_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS signal_history_lists_v1(
            run_id TEXT PRIMARY KEY, display_id TEXT, lists BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS signal_history_display_v1 ON signal_history_lists_v1(display_id,run_id);
        CREATE TABLE IF NOT EXISTS signal_history_codes_v1(
            code TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(code,run_id)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS signal_history_codes_run_v1 ON signal_history_codes_v1(run_id);
        CREATE TRIGGER IF NOT EXISTS invalidate_signal_history_update_v1 AFTER UPDATE OF scan_json ON signal_runs
        BEGIN
            DELETE FROM signal_history_lists_v1 WHERE run_id=old.run_id;
            DELETE FROM signal_history_codes_v1 WHERE run_id=old.run_id;
        END;
        CREATE TRIGGER IF NOT EXISTS invalidate_signal_history_delete_v1 AFTER DELETE ON signal_runs
        BEGIN
            DELETE FROM signal_history_lists_v1 WHERE run_id=old.run_id;
            DELETE FROM signal_history_codes_v1 WHERE run_id=old.run_id;
        END;
    ''')


def available(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_history_lists_v1'").fetchone())


def save(db, run_id, lists, keys, encode):
    original_id, legacy_id, source_id, watch, continuation, opportunities, brief_at, focus = lists
    codes = {str(row['code'])[:6] for rows in (watch, continuation, opportunities, focus)
             for row in (rows or []) if isinstance(row, dict) and row.get('code')}
    def compact(rows, names):
        return [{k: row[k] for k in names if k in row} for row in rows if isinstance(row, dict)] if rows else rows
    data = [original_id, legacy_id, source_id, compact(watch, keys), compact(continuation, keys),
            compact(opportunities, (*keys, 'opportunity')), brief_at, focus]
    db.execute('INSERT INTO signal_history_lists_v1 VALUES(?,?,?)',
               (run_id, original_id or legacy_id or source_id, zlib.compress(encode(data).encode(), level=1)))
    db.executemany('INSERT INTO signal_history_codes_v1 VALUES(?,?)', ((code, run_id) for code in codes))


def backfill(service, keys, encode, *, limit=500):
    """Commit one existing publication at a time. Repeated calls resume after interruption."""
    with service._connect() as db:
        missing = db.execute('SELECT r.run_id FROM signal_runs r WHERE NOT EXISTS('
            'SELECT 1 FROM signal_history_lists_v1 h WHERE h.run_id=r.run_id) ORDER BY r.as_of DESC LIMIT ?',
            (max(1, int(limit)),)).fetchall()
        saved, failed = 0, []
        for (run_id,) in missing:
            try:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT ' + SAVED_LISTS_SQL + ' FROM signal_runs WHERE run_id=? AND NOT EXISTS('
                        'SELECT 1 FROM signal_history_lists_v1 WHERE run_id=?)', (run_id, run_id)).fetchone()
                    if row:
                        save(db, run_id, json.loads(row[0]), keys, encode)
                        saved += 1
            except (sqlite3.OperationalError, ValueError, TypeError) as error:
                failed.append({'runId': run_id, 'error': str(error)})
    return {'indexed': saved, 'failed': failed}


def read(db, run_id, indexed):
    if indexed:
        found = db.execute('SELECT lists FROM signal_history_lists_v1 WHERE run_id=?', (run_id,)).fetchone()
        if found:
            return json.loads(zlib.decompress(found[0]))
    row = db.execute('SELECT ' + SAVED_LISTS_SQL + ' FROM signal_runs WHERE run_id=?', (run_id,)).fetchone()
    return json.loads(row[0])
