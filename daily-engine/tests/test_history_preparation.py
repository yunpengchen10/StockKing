from datetime import datetime, time, timedelta
import json
import time as wall
import zlib

import pytest

from src.services.yao_scout import history_preparation as prep
from src.services.yao_scout import minute_history as minute
from src.services.yao_scout.minute_integrity import inspect_session, session_revision


NOW = datetime(2026, 10, 16, 13, 8, tzinfo=minute.SHANGHAI)
# The exchange calendar is injected: exactly these twenty sessions are required.
DAYS = [f'2026-09-{day:02d}' for day in range(1, 21)]
CODE = '600000'


def session(day, code=CODE, *, identity=True, source='tdx-mac'):
    date = datetime.fromisoformat(day).date()
    starts = [datetime.combine(date, time(9, 31), minute.SHANGHAI),
              datetime.combine(date, time(13, 1), minute.SHANGHAI)]
    ident = {'status': 'provider_reply_checked', 'requested_code': code, 'response_code': code,
        'requested_market': 1, 'response_market': 1, 'period': '1', 'page_cursor_checked': True}
    return [{'end': (start + timedelta(minutes=i)).isoformat(), 'open': 10, 'close': 10,
        'high': 10.01, 'low': 9.99, 'volume_shares': 100, 'amount_cny': 1000,
        'source': source, **({'identity_evidence': ident} if identity else {})}
        for start in starts for i in range(120)]


def anchor(day, code=CODE, source='sina daily unadjusted'):
    return {'kind': 'daily_ohlc', 'code': code, 'day': day, 'source': source,
        'price_basis': 'unadjusted', 'complete': True, 'observed_at': NOW.isoformat(),
        'open': 10, 'close': 10, 'high': 10.01, 'low': 9.99}


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    monkeypatch.setattr(prep, '_prior_sessions', lambda day: DAYS)


def daily(code, days, cutoff, **kwargs):
    return {day: anchor(day, code) for day in days}


def historical(code, *, end, **kwargs):
    return [row for day in DAYS if day.replace('-', '') <= end[:8] for row in session(day, code)]


def run(tmp_path, **kwargs):
    return prep.prepare_history([CODE], NOW, tmp_path, timeout=30,
        minute_fetcher=kwargs.pop('minute_fetcher', historical),
        daily_fetcher=kwargs.pop('daily_fetcher', daily), **kwargs)


def test_nineteen_days_requires_exact_missing_session_and_remains_incremental(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    for day in DAYS[1:]:
        archive.save(CODE, session(day), NOW, verification=anchor(day))
    # An older twenty-first session cannot substitute for the missing exact day.
    archive.save(CODE, session('2026-08-31'), NOW, verification=anchor('2026-08-31'))
    calls = []
    def fetch(code, **kw):
        calls.append(kw['end'])
        return session(DAYS[0])
    result = run(tmp_path, minute_fetcher=fetch)
    assert result['ready'] == 1 and result['details'][CODE]['verifiedDays'] == DAYS
    assert calls == ['20260901150000']
    assert result['details'][CODE]['repairedDays'] == [DAYS[0]]
    def forbidden(*a, **k):
        pytest.fail('prepared session triggered network')
    again = run(tmp_path, minute_fetcher=forbidden, daily_fetcher=forbidden)
    assert again['ready'] == 1 and again['details'][CODE]['pages'] == 0


def test_legacy_unknown_identity_requires_real_new_session_and_preserves_old_revision(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, session(DAYS[0], identity=False), NOW)
    old = archive.quality(CODE, NOW, [DAYS[0]])
    result = run(tmp_path)
    assert result['ready'] == 1
    current = archive.quality(CODE, NOW, [DAYS[0]])
    assert current['status'] == 'verified'
    assert current['sessions'][0]['revision'] != old['sessions'][0]['revision']
    assert not minute.validate_archive_lineage(tmp_path, old)['valid']
    with archive.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM session_evidence WHERE kind='before_verified_rebuild'").fetchone()[0] == 1


@pytest.mark.parametrize('bad', ['identity', 'same_source', 'quote', 'price_conflict', 'missing_amount', 'preclose_anchor'])
def test_incomplete_or_unverified_history_cannot_be_certified(tmp_path, bad):
    def bars(code, **kwargs):
        rows = historical(code, **kwargs)
        if bad == 'identity':
            rows = [{key: value for key, value in row.items() if key != 'identity_evidence'} for row in rows]
        if bad == 'same_source':
            rows = [{**row, 'source': 'sina minute'} for row in rows]
        if bad == 'missing_amount':
            rows = [{**row, 'amount_cny': None} for row in rows]
        return rows
    def anchors(code, days, cutoff, **kwargs):
        result = daily(code, days, cutoff)
        if bad == 'quote':
            result = {day: {**item, 'kind': 'quote_prefix'} for day, item in result.items()}
        if bad == 'price_conflict':
            result = {day: {**item, 'open': 9.95, 'close': 9.95, 'high': 9.96, 'low': 9.94}
                      for day, item in result.items()}
        if bad == 'preclose_anchor':
            result = {day: {**item, 'observed_at': day + 'T10:00:00+08:00'} for day, item in result.items()}
        return result
    result = run(tmp_path, minute_fetcher=bars, daily_fetcher=anchors, max_pages_per_code=1)
    assert result['ready'] == 0
    if bad != 'missing_amount':
        assert result['details'][CODE]['repairedDays'] == []


def test_tainted_history_requires_explicit_rebuild_and_success_keeps_audit(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, session(DAYS[0]), NOW)
    archive.quarantine(CODE, DAYS[0], 'test_conflict')
    result = run(tmp_path)
    assert result['ready'] == 0 and result['details'][CODE]['taintedDays'] == DAYS[:1]
    repaired = run(tmp_path, allow_tainted_rebuild=True)
    assert repaired['ready'] == 1 and repaired['details'][CODE]['repairedDays'] == DAYS[:1]


def test_quarantine_between_audit_and_rebuild_cannot_bypass_explicit_opt_in(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = session(DAYS[0])
    archive.save(CODE, rows, NOW)
    expected = session_revision(CODE, DAYS[0], list(minute.normalize_bars(rows, NOW).values()))
    archive.quarantine(CODE, DAYS[0], 'concurrent_quarantine')
    with pytest.raises(ValueError, match='explicit authorization'):
        archive.rebuild(CODE, DAYS[0], rows, NOW, verification=anchor(DAYS[0]),
                        expected_revision=expected, allow_tainted=False)


@pytest.mark.parametrize('missing_clock,ready', [('11:06', True), ('13:07', False)])
def test_verified_legacy_partial_day_only_admits_genuinely_complete_clock_windows(tmp_path, missing_clock, ready):
    archive = minute.MinuteArchive(tmp_path)
    for day in DAYS:
        archive.save(CODE, session(day, identity=False), NOW)
    def partial(code, **kwargs):
        return [row for row in historical(code, **kwargs) if row['end'][11:16] != missing_clock]
    result = run(tmp_path, minute_fetcher=partial, max_pages_per_code=1)
    detail = result['details'][CODE]
    assert detail['ready'] is ready and detail['fullSessionReady'] is False
    assert len(detail['verifiedDays']) == 20
    assert len(detail['windowReadyDays']) == (20 if ready else 0)
    assert len(archive.read(CODE, NOW)) == 20 * 239
    with archive.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM session_evidence WHERE kind='before_verified_legacy_replacement'").fetchone()[0] == 20


def test_partial_identity_refresh_never_repairs_quarantine(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = session(DAYS[0])
    archive.save(CODE, rows, NOW)
    archive.quarantine(CODE, DAYS[0], 'must_keep_quarantine')
    expected = archive.quality(CODE, NOW, [DAYS[0]])['sessions'][0]['revision']
    with pytest.raises(ValueError, match='full verified rebuild'):
        archive.replace_unknown_session(CODE, DAYS[0], rows[:-1], NOW,
            verification=anchor(DAYS[0]), expected_revision=expected)


def test_rejected_provider_row_is_retained_but_never_used_as_a_minute(tmp_path):
    def malformed(code, **kwargs):
        return [{**row, 'amount_cny': 1} if row['end'][11:16] == '11:06' else row
                for row in historical(code, **kwargs)]
    result = run(tmp_path, minute_fetcher=malformed, max_pages_per_code=1)
    assert result['ready'] == 1 and not result['details'][CODE]['fullSessionReady']
    rejected = next(e for e in result['details'][CODE]['errors'] if e.get('day') == DAYS[0])
    assert rejected['sourceMinuteCount'] == 240
    assert rejected['normalizationRejectedMinuteTimes'] == [DAYS[0]+'T11:06:00+08:00']
    archive = minute.MinuteArchive(tmp_path)
    assert len(archive.read(CODE, NOW)) == 20 * 239
    with archive.connect() as db:
        saved = db.execute("SELECT payload FROM session_evidence WHERE code=? AND day=? "
            "AND kind='historical_preparation_source'", (CODE, DAYS[0])).fetchone()
    rows = json.loads(zlib.decompress(saved[0]))
    assert len(rows) == 240
    assert next(row for row in rows if row['end'][11:16] == '11:06')['amount_cny'] == 1


def test_same_source_secondary_provenance_is_not_independent():
    rows = [{**row, 'sources': ['tdx-mac', 'sina minute']} for row in session(DAYS[0])]
    assert inspect_session(CODE, DAYS[0], rows, anchor(DAYS[0]))['status'] != 'verified'


def test_bounded_batch_rotates_and_readiness_is_single_file(tmp_path, monkeypatch):
    calls = []
    def empty(code, **kw):
        calls.append(code)
        return []
    args = dict(timeout=30, max_codes=1, max_pages_per_code=1, minute_fetcher=empty, daily_fetcher=daily)
    first = prep.prepare_history([CODE, '600001'], NOW, tmp_path, **args)
    second = prep.prepare_history([CODE, '600001'], NOW, tmp_path, **args)
    assert calls == [CODE, '600001']
    assert first['statusPath'] == second['statusPath'] == str(tmp_path / prep.STATUS_FILE)
    monkeypatch.setattr(prep, '_read_sessions', lambda *a: pytest.fail('status reads database'))
    assert len(prep.read_preparation_status(tmp_path, NOW)['details']) == 2
    assert prep.read_preparation_status(tmp_path, NOW + timedelta(days=1))['status'] == 'stale'


def test_new_trading_day_invalidates_readiness_without_starving_later_codes(tmp_path):
    calls = []
    def empty(code, **kw):
        calls.append(code)
        return []
    args = dict(timeout=30, max_codes=1, max_pages_per_code=1, minute_fetcher=empty, daily_fetcher=daily)
    prep.prepare_history([CODE, '600001'], NOW, tmp_path, **args)
    second = prep.prepare_history([CODE, '600001'], NOW + timedelta(days=1), tmp_path, **args)
    assert calls == [CODE, '600001']
    assert set(second['details']) == {'600001'}


def test_next_session_window_uses_real_close_not_future_evidence_clock(tmp_path, monkeypatch):
    close = NOW.replace(hour=15, minute=30)
    target = '2026-10-19'
    tomorrow_days = DAYS[1:] + [close.date().isoformat()]
    monkeypatch.setattr(prep, '_prior_sessions', lambda day: tomorrow_days)
    seen = []
    def fetch(code, **kwargs):
        return [row for day in tomorrow_days for row in session(day, code)]
    def anchors(code, days, cutoff, **kwargs):
        seen.append(cutoff)
        return {day: {**packet, 'observed_at': cutoff.isoformat()}
                for day, packet in daily(code, days, cutoff).items()}
    result = prep.prepare_history([CODE], close, tmp_path, target_session=target,
        minute_fetcher=fetch, daily_fetcher=anchors)
    assert result['ready'] == 1 and result['targetSession'] == target
    assert seen == [close] and result['evidenceCutoffAt'] == close.isoformat()
    assert prep.read_preparation_status(tmp_path, datetime.fromisoformat(target+'T09:00:00+08:00'))['ready'] == 1
    early = prep.prepare_history([CODE], NOW, tmp_path, target_session=target,
        minute_fetcher=lambda *a, **k: pytest.fail('future history fetch'))
    assert early['status'] == 'unavailable' and early['reason'] == 'required_historical_session_not_closed_at_real_cutoff'


def test_timed_out_read_cannot_mutate_archive_after_return(tmp_path):
    def slow(code, **kw):
        wall.sleep(.2)
        return historical(code, **kw)
    result = prep.prepare_history([CODE], NOW, tmp_path, timeout=.04,
        minute_fetcher=slow, daily_fetcher=daily)
    assert result['ready'] == 0 and result['budgetExhausted']
    wall.sleep(.25)
    with minute.MinuteArchive(tmp_path).connect() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_status_atomic_replace_retries_transient_windows_read_lock(tmp_path, monkeypatch):
    original, calls = prep.os.replace, []
    def locked_once(source, target):
        calls.append(True)
        if len(calls) < 3:
            raise PermissionError('temporary reader lock')
        return original(source, target)
    monkeypatch.setattr(prep.os, 'replace', locked_once)
    monkeypatch.setattr(prep, 'sleep', lambda _: None)
    prep._write_status(tmp_path / prep.STATUS_FILE, {'version': prep.VERSION, 'ready': 0})
    assert len(calls) == 3 and prep.read_preparation_status(tmp_path)['ready'] == 0


def test_daily_adapter_preserves_historical_dates_and_rejects_duplicate_conflict():
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'result': {'data': [
                {'day': DAYS[0], 'open': 10, 'high': 10.01, 'low': 9.99, 'close': 10},
                {'day': DAYS[0], 'open': 10, 'high': 11, 'low': 9.99, 'close': 10},
                {'day': DAYS[1], 'open': 10, 'high': 10.01, 'low': 9.99, 'close': 10},
                {'day': '2026-10-16', 'open': 10, 'high': 10.01, 'low': 9.99, 'close': 10}]}}
    packet = prep.fetch_daily_anchors(CODE, DAYS, NOW, getter=lambda *a, **k: Response())
    assert set(packet) == {DAYS[1]} and packet[DAYS[1]]['kind'] == 'daily_ohlc'


def test_ready_clocks_follow_real_windows_instead_of_worker_clock(tmp_path):
    def partial(code, **kwargs):
        return [row for row in historical(code, **kwargs) if row['end'][11:16] != '13:07']
    result = run(tmp_path, minute_fetcher=partial, max_pages_per_code=1)
    detail = result['details'][CODE]
    assert not detail['ready'] and not detail['fullSessionReady']
    assert detail['verifiedDayCount'] == 20
    assert '10:30:00' in detail['readyClocks']
    assert '13:01:00' not in detail['readyClocks']
    assert '13:04:00' in detail['readyClocks']
    assert all(f'13:{minute:02d}:00' not in detail['readyClocks'] for minute in range(7, 11))
    assert '13:11:00' in detail['readyClocks']
    assert result['preparedWithUsableClocks'] == 1


def test_preparation_warmup_does_not_erase_complete_historical_sessions(tmp_path):
    cutoff = NOW.replace(hour=13, minute=1)
    result = prep.prepare_history([CODE], cutoff, tmp_path,
        minute_fetcher=historical, daily_fetcher=daily)
    detail = result['details'][CODE]
    assert detail['fullSessionReady'] and not detail['ready']
    assert detail['sessionWindow']['status'] == 'session_warmup'
    assert detail['sessionWindow']['baselineReadyAt'].endswith('13:04:00+08:00')
    assert detail['readyClocks'][0] == '09:34:00'
    assert '13:04:00' in detail['readyClocks']
    assert result['fullSessionReady'] == 1


def test_verified_partial_sessions_can_gain_compatible_real_minutes_with_cas(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    day = DAYS[0]
    previous = [row for row in session(day) if row['end'][11:16] != '13:07']
    fresh = [row for row in session(day) if row['end'][11:16] != '13:08']
    archive.save(CODE, previous, NOW, verification=anchor(day))
    lineage = archive.quality(CODE, NOW, [day])
    revision = lineage['sessions'][0]['revision']
    result = archive.supplement_verified_session(CODE, day, fresh, NOW,
        verification=anchor(day), expected_revision=revision)
    assert result['addedMinutes'] == 1 and result['actualMinutes'] == 240
    assert result['revision'] != revision and not result['fullSessionRebuild']
    assert len(archive.read(CODE, NOW)) == 240
    assert not minute.validate_archive_lineage(tmp_path, lineage)['valid']
    with archive.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM session_evidence WHERE kind='before_verified_supplement'").fetchone()[0] == 1
    with pytest.raises(ValueError, match='revision changed'):
        archive.supplement_verified_session(CODE, day, fresh, NOW,
            verification=anchor(day), expected_revision=revision)


@pytest.mark.parametrize('failure', ['overlap', 'identity', 'anchor', 'quarantine'])
def test_partial_supplement_rejects_conflict_unverified_or_quarantined_bytes(tmp_path, failure):
    archive = minute.MinuteArchive(tmp_path)
    day = DAYS[0]
    archive.save(CODE, session(day)[:-1], NOW, verification=anchor(day))
    if failure == 'quarantine':
        archive.quarantine(CODE, day, 'test_conflict')
    with archive.connect() as db:
        original = db.execute('SELECT payload FROM sessions WHERE code=? AND day=?', (CODE, day)).fetchone()[0]
    old = json.loads(zlib.decompress(original))
    revision = session_revision(CODE, day, old)
    fresh = session(day)[1:]
    if failure == 'overlap':
        fresh[0] = {**fresh[0], 'close':10.005}
    if failure == 'identity':
        fresh[0] = {**fresh[0], 'identity_evidence':{}}
    verification = anchor(day)
    if failure == 'anchor':
        verification = {**verification, 'source':'tdx-mac'}
    with pytest.raises(ValueError):
        archive.supplement_verified_session(CODE, day, fresh, NOW,
            verification=verification, expected_revision=revision)
    with archive.connect() as db:
        assert db.execute('SELECT payload FROM sessions WHERE code=? AND day=?', (CODE, day)).fetchone()[0] == original


def test_partial_supplement_preserves_complete_history_and_unchanged_partial_revision(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    day = DAYS[0]
    for rows, expected in [(session(day)[:-1], 'no_new_verified_minutes'),
                           (session(day), 'complete_session_preserved')]:
        archive.save(CODE, rows, NOW, verification=anchor(day))
        lineage = archive.quality(CODE, NOW, [day])
        revision = lineage['sessions'][0]['revision']
        result = archive.supplement_verified_session(CODE, day, rows[1:], NOW,
            verification=anchor(day), expected_revision=revision)
        assert result['unchanged'] and result['reason'] == expected
        assert result['revision'] == revision
        assert minute.validate_archive_lineage(tmp_path, lineage)['valid']


def test_preparation_merges_complementary_partial_pages_without_repeating_same_page(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    for day in DAYS:
        rows = session(day)
        archive.save(CODE, rows[:-1] if day == DAYS[0] else rows, NOW, verification=anchor(day))
    result = run(tmp_path, minute_fetcher=lambda *a, **kw: session(DAYS[0])[1:])
    assert result['details'][CODE]['fullSessionReady']
    assert result['details'][CODE]['pages'] == 1
    assert result['details'][CODE]['repairedDays'] == DAYS[:1]


def test_preparation_same_partial_page_is_no_progress_instead_of_write_failure(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    for day in DAYS:
        archive.save(CODE, session(day)[:-1], NOW, verification=anchor(day))
    result = run(tmp_path, minute_fetcher=lambda *a, **kw: [row for day in DAYS for row in session(day)[:-1]])
    detail = result['details'][CODE]
    assert detail['pages'] == 1 and not detail['fullSessionReady']
    assert {e['reason'] for e in detail['errors']} == {'historical_minute_session_incomplete', 'no_new_verified_minutes'}
