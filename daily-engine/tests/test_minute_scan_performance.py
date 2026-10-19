"""Latency regressions must not weaken whole-session or point-in-time checks."""
from datetime import timedelta
import json
import sqlite3
import zlib

import pytest

from src.services.software_market import SoftwareMarketClient
from src.services.yao_scout import minute_history as minute
from tests.test_minute_integrity import CODE, DAY, NOW, anchor, bars


def legacy_archive(directory, rows, code=CODE):
    path = directory / 'minute-bars-v2.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS sessions(code TEXT,day TEXT,payload BLOB,PRIMARY KEY(code,day))')
        by_day = {}
        for row in rows:
            by_day.setdefault(row['end'][:10], []).append(row)
        db.executemany('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', [
            (code, day, zlib.compress(json.dumps(items).encode())) for day, items in by_day.items()])
    return path


def test_legacy_revision_is_hashed_once_per_session_not_per_bar(monkeypatch, tmp_path):
    legacy_archive(tmp_path, bars())
    minute._checked_session_rows.cache_clear()
    calls, original = [], minute.session_revision
    def revision(*args):
        calls.append((args[0], args[1]))
        return original(*args)
    monkeypatch.setattr(minute, 'session_revision', revision)
    rows = minute.read_archive_rows(tmp_path, CODE, NOW)
    assert len(rows) == 240
    assert calls == [(CODE, DAY)]
    assert len({row['raw_data_revision'] for row in rows}) == 1
    assert {row['data_quality_status'] for row in rows} == {'unknown'}


def test_cached_rows_are_isolated_and_payload_changes_invalidate(monkeypatch, tmp_path):
    path = legacy_archive(tmp_path, bars())
    minute._checked_session_rows.cache_clear()
    calls, original = [], minute.inspect_session
    def inspect(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(minute, 'inspect_session', inspect)
    first = minute.read_archive_rows(tmp_path, CODE, NOW)
    count = len(calls)
    first[0]['identity_evidence']['response_code'] = '001336'
    second = minute.read_archive_rows(tmp_path, CODE, NOW)
    assert len(calls) == count
    assert second[0]['identity_evidence']['response_code'] == CODE
    # Detect legacy bytes changed by another process, even with no revision row.
    changed = bars()
    changed[-1]['identity_evidence'] = {**changed[-1]['identity_evidence'], 'response_code':'001336'}
    with sqlite3.connect(path) as db:
        db.execute('UPDATE sessions SET payload=?', (zlib.compress(json.dumps(changed).encode()),))
    assert minute.read_archive_rows(tmp_path, CODE, NOW) == []
    assert len(calls) > count


def test_cache_rechecks_anchor_updates_and_quarantine(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(), NOW)
    minute._checked_session_rows.cache_clear()
    assert {r['data_quality_status'] for r in archive.read(CODE, NOW)} == {'unknown'}
    archive.save(CODE, bars(), NOW, verification=anchor())
    assert {r['data_quality_status'] for r in archive.read(CODE, NOW)} == {'verified'}
    archive.quarantine(CODE, DAY, 'external_conflict')
    assert archive.read(CODE, NOW) == []


def test_same_day_later_conflict_invalidates_a_cached_earlier_prefix(tmp_path):
    path = legacy_archive(tmp_path, bars())
    cutoff = minute._datetime(bars()[10]['end'])
    assert len(minute.read_archive_rows(tmp_path, CODE, cutoff)) == 11
    changed = bars()
    changed[-1]['conflicting_observations'] = [{'source':'other'}]
    with sqlite3.connect(path) as db:
        db.execute('UPDATE sessions SET payload=?', (zlib.compress(json.dumps(changed).encode()),))
    assert minute.read_archive_rows(tmp_path, CODE, cutoff) == []


def test_cache_cannot_import_other_session_or_future_minutes(tmp_path):
    changed = bars()
    changed[-1]['end'] = changed[-1]['end'].replace(DAY, '2026-10-09')
    archive = minute.MinuteArchive(tmp_path)
    # Deliberately corrupt a legacy day payload under the wrong primary key.
    with archive.connect() as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?)',
                   (CODE, DAY, zlib.compress(json.dumps(changed).encode())))
    assert minute.read_archive_rows(tmp_path, CODE, NOW) == []


def test_read_only_recompute_skips_unusable_history_and_never_creates_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(minute, '_prior_sessions', lambda *_: pytest.fail('history accessed without current minutes'))
    result = minute.read_bar_evidence(CODE, NOW, tmp_path)
    assert result['asOf'] is None and result['dataLineage']['status'] == 'unknown'
    assert not list(tmp_path.iterdir())


def test_scan_window_matches_full_history_formula_and_cutoff(monkeypatch, tmp_path):
    cutoff = minute._datetime(bars()[59]['end'])
    days = [cutoff.date()-timedelta(days=i) for i in range(1, 21)]
    rows = bars() + [{**row, 'end':row['end'].replace(DAY, day.isoformat())}
                     for day in days for row in bars()]
    legacy_archive(tmp_path, rows)
    monkeypatch.setattr(minute, '_prior_sessions', lambda *_: days)
    expected = minute.compute_bar_evidence(minute.read_archive_rows(tmp_path, CODE, cutoff),
                                          cutoff, expected_dates=days)
    result = minute.read_bar_evidence(CODE, cutoff, tmp_path)
    for key in ('metrics', 'v11Inputs', 'v11Coverage', 'baselineDates', 'dependencyDays',
                'historyDays', 'priceHistoryDays', 'asOf', 'currentPrice', 'featureHistoryDays'):
        assert result[key] == expected[key], key
    assert result['asOf'] == cutoff.isoformat()
    assert result['dataLineage']['status'] == 'unknown'  # No anchor invented.
    assert len(result['dataLineage']['sessions']) == 21


def test_incremental_scan_fetches_before_history_and_reads_each_dependency_once(monkeypatch, tmp_path):
    cutoff = minute._datetime(bars()[59]['end'])
    calls = []
    original = minute.read_archive_rows
    def read(*args, **kwargs):
        calls.append(('read', kwargs))
        return original(*args, **kwargs)
    class Client:
        available = True
        def bars(self, code, **kwargs):
            calls.append(('fetch', kwargs))
            return {'bars':bars()[:60], 'time_semantics':'bar_end'}
    monkeypatch.setattr(minute, 'read_archive_rows', read)
    monkeypatch.setattr(minute, '_prior_sessions', lambda *_: [])
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda:Client())
    monkeypatch.setattr(minute, 'fetch_sina_bars', lambda *a, **k:pytest.fail('unexpected fallback'))
    result = minute.fetch_bar_evidence(CODE, cutoff, tmp_path, backfill=False, software_count=240)
    assert calls == [('fetch', {'period':'1', 'count':240}), ('read', {'lookback_days':0})]
    assert result['asOf'] == cutoff.isoformat()
    assert result['metrics']['amount_3m'] == pytest.approx(1350)
    assert result['historyBackfill']['status'] == 'disabled_during_scan'
    assert result['dataLineage']['status'] == 'unknown'


def test_expired_scan_reads_cache_without_constructing_writable_archive(monkeypatch, tmp_path):
    legacy_archive(tmp_path, bars())
    cutoff = minute._datetime(bars()[59]['end'])
    monkeypatch.setattr(minute, '_prior_sessions', lambda *_: [])
    monkeypatch.setattr(minute, 'MinuteArchive', lambda *_:pytest.fail('write-capable archive constructed'))
    result = minute.fetch_bar_evidence(CODE, cutoff, tmp_path, backfill=False,
                                       software_count=240, deadline=minute.monotonic()-1)
    assert result['asOf'] == cutoff.isoformat()
    assert result['softwareMarketUsed'] is False
