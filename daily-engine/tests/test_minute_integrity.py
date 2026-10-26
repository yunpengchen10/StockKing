from datetime import datetime, time, timedelta
import json
import sqlite3
import importlib.util
from pathlib import Path
import sys
import zlib

import pytest

from src.services.yao_scout import minute_history as minute


DAY = '2026-10-08'
NOW = datetime(2026, 10, 9, 15, 30, tzinfo=minute.SHANGHAI)
CODE = '000692'


def identity(code=CODE):
    return {'status':'provider_reply_checked', 'requested_code':code, 'response_code':code,
            'requested_market':0, 'response_market':0, 'period':'1', 'page_cursor_checked':True}


def bars(price=4.5):
    day = datetime.fromisoformat(DAY).date()
    starts = [datetime.combine(day, time(9, 31), minute.SHANGHAI),
              datetime.combine(day, time(13, 1), minute.SHANGHAI)]
    return [{'end':(start+timedelta(minutes=i)).isoformat(), 'open':price, 'close':price,
             'high':price+.01, 'low':price-.01, 'amount_cny':price*100, 'volume_shares':100,
             'source':'tdx-mac', 'identity_evidence':identity()}
            for start in starts for i in range(120)]


def anchor(price=4.5):
    return {'kind':'daily_ohlc', 'code':CODE, 'day':DAY, 'source':'sina daily unadjusted',
            'price_basis':'unadjusted', 'complete':True, 'observed_at':NOW.isoformat(),
            'open':price, 'close':price, 'high':price+.01, 'low':price-.01}


def test_existing_archive_gets_indexed_anchor_lookup_without_rewriting_data(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(), NOW, verification=anchor())
    with archive.connect() as db:
        db.execute('DROP INDEX idx_session_evidence_code_day_kind')
        before = db.execute('SELECT payload FROM sessions WHERE code=? AND day=?', (CODE, DAY)).fetchone()[0]
        evidence = db.execute('SELECT evidence_id FROM session_evidence ORDER BY evidence_id').fetchall()
    minute.MinuteArchive(tmp_path)
    with archive.connect() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT s.code,s.day,s.metadata_json FROM session_evidence s "
            "WHERE s.kind='independent_price_anchor' AND s.code=? AND s.day IN (?)", (CODE, DAY)).fetchall()
        assert any('SEARCH' in row[3] and 'idx_session_evidence_code_day_kind' in row[3] for row in plan)
        assert db.execute('SELECT payload FROM sessions WHERE code=? AND day=?', (CODE, DAY)).fetchone()[0] == before
        assert db.execute('SELECT evidence_id FROM session_evidence ORDER BY evidence_id').fetchall() == evidence


def test_batched_validation_reuses_sessions_but_checks_each_prefix_and_revision(tmp_path, monkeypatch):
    from src.services.yao_scout import minute_integrity as integrity
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(), NOW, verification=anchor())
    other_day = '2026-10-09'
    other_rows = [{**row, 'end':row['end'].replace(DAY, other_day)} for row in bars()]
    archive.save(CODE, other_rows, NOW, verification={**anchor(), 'day':other_day})
    prefixes = [minute.read_archive_lineage(tmp_path, CODE, [DAY], cutoff=bars()[59]['end']),
                minute.read_archive_lineage(tmp_path, CODE, [DAY]),
                minute.read_archive_lineage(tmp_path, CODE, [other_day])]
    lineage = {'data_quality_version':integrity.DATA_QUALITY_VERSION,
               'sessions':[part['sessions'][0] for part in prefixes]}
    original = integrity._read_archive_records
    reads = []
    def counted(*args, **kwargs):
        reads.append((args[1], set(args[2])))
        return original(*args, **kwargs)
    monkeypatch.setattr(integrity, '_read_archive_records', counted)
    checked = integrity.validate_archive_dependencies(tmp_path, lineage)
    assert len(reads) == 1 and reads[0] == (CODE, {DAY, other_day})
    assert all(item['valid'] for item in checked)
    assert [item['sessions'] for item in checked] == [part['sessions'] for part in prefixes]
    corrupt = {**lineage, 'sessions':[dict(row) for row in lineage['sessions']]}
    corrupt['sessions'][1]['revision'] = 'wrong_revision'
    assert [item['valid'] for item in integrity.validate_archive_dependencies(tmp_path, corrupt)] == [True, False, True]
    archive.quarantine(CODE, DAY, 'independent_review_conflict')
    assert [item['valid'] for item in integrity.validate_archive_dependencies(tmp_path, lineage)] == [False, False, True]


def test_mismatched_security_is_quarantined_and_raw_evidence_retained(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = [{**row, 'identity_evidence':identity('001336')} for row in bars()]
    result = archive.save(CODE, rows, NOW)
    assert result['quarantinedDays'] == [DAY]
    assert not archive.read(CODE, NOW)
    quality = minute.read_archive_lineage(tmp_path, CODE, [DAY])
    assert quality['status'] == 'tainted'
    assert 'security_identity_mismatch' in quality['reasonCodes']
    with archive.connect() as db:
        evidence = db.execute("SELECT payload FROM session_evidence WHERE kind='quarantined_ingest'").fetchone()
    assert json.loads(zlib.decompress(evidence[0]))[0]['identity_evidence']['response_code'] == '001336'


def test_nonoverlapping_price_scale_fragments_never_make_a_complete_usable_day(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars()[:12], NOW)
    archive.save(CODE, bars(122.43)[12:], NOW)
    assert not archive.read(CODE, NOW)
    assert not archive.read_recent(CODE, NOW.replace(day=8))
    quality = archive.quality(CODE, NOW)
    assert quality['status'] == 'tainted'
    # A later normal fetch must not silently clear quarantine.
    archive.save(CODE, bars(), NOW, verification=anchor())
    assert archive.quality(CODE, NOW)['status'] == 'tainted'


def test_legacy_mixed_day_is_blocked_on_read_without_rewriting_payload(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = bars()[:12] + bars(115.39)[12:]
    payload = zlib.compress(json.dumps(rows).encode())
    with archive.connect() as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?)', (CODE, DAY, payload))
    assert not archive.read(CODE, NOW)
    assert minute.read_archive_lineage(tmp_path, CODE, [DAY])['status'] == 'tainted'
    with archive.connect() as db:
        assert db.execute('SELECT payload FROM sessions').fetchone()[0] == payload
        assert db.execute('SELECT COUNT(*) FROM session_quality').fetchone()[0] == 0


def test_read_only_label_reader_never_creates_schema_and_filters_whole_tainted_day(tmp_path):
    assert minute.read_archive_rows(tmp_path,CODE,NOW) == []
    assert not list(tmp_path.iterdir())
    path = tmp_path/'minute-bars-v2.sqlite3'
    db = sqlite3.connect(path)
    try:
        db.execute('CREATE TABLE sessions(code TEXT,day TEXT,payload BLOB,PRIMARY KEY(code,day))')
        db.execute('INSERT INTO sessions VALUES(?,?,?)',(CODE,DAY,zlib.compress(json.dumps(bars()).encode())))
        db.commit()
    finally:
        db.close()
    before = path.read_bytes()
    assert len(minute.read_archive_rows(tmp_path,CODE,NOW)) == 240
    assert path.read_bytes() == before
    archive = minute.MinuteArchive(tmp_path)
    archive.quarantine(CODE,DAY,'same_day_conflict')
    assert minute.read_archive_rows(tmp_path,CODE,NOW) == []


def test_identity_alone_does_not_claim_independent_price_verification(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(), NOW)
    state = archive.quality(CODE, NOW)
    assert state['status'] == 'unknown'
    assert not minute.validate_archive_lineage(tmp_path, state)['valid']
    archive.save(CODE, bars(), NOW, verification={**anchor(), 'source':'software:go/tdx-mac'})
    assert archive.quality(CODE, NOW)['status'] == 'unknown'


def test_independent_reference_catches_smooth_wrong_security_day(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(31.53), NOW, verification=anchor())
    assert archive.quality(CODE, NOW)['status'] == 'tainted'
    assert not archive.read(CODE, NOW)


def test_verified_rebuild_preserves_old_evidence_and_invalidates_old_model_dependencies(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(), NOW, verification=anchor())
    original = archive.quality(CODE, NOW)
    assert original['status'] == 'verified'
    assert minute.validate_archive_lineage(tmp_path, original)['valid']
    archive.quarantine(CODE, DAY, 'independent_review_conflict')
    assert not minute.validate_archive_lineage(tmp_path, original)['valid']
    revision = archive.quality(CODE, NOW)['sessions'][0]['revision']
    repaired = archive.rebuild(CODE, DAY, bars(), NOW, verification=anchor(), expected_revision=revision)
    assert repaired['revision'] != revision
    assert not minute.validate_archive_lineage(tmp_path, original)['valid']
    current = archive.quality(CODE, NOW)
    assert minute.validate_archive_lineage(tmp_path, current)['valid']
    assert len(archive.read(CODE, NOW)) == 240
    with archive.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM session_evidence WHERE kind='before_verified_rebuild'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM minute_quality_events WHERE event='verified_rebuild'").fetchone()[0] == 1


def test_repair_rejects_partial_day_stale_revision_or_missing_independent_evidence(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(), NOW)
    revision = archive.quality(CODE, NOW)['sessions'][0]['revision']
    for rows, evidence, expected in ((bars()[:-1], anchor(), revision),
                                     (bars(), {}, revision), (bars(), anchor(), 'stale')):
        with pytest.raises(ValueError):
            archive.rebuild(CODE, DAY, rows, NOW, verification=evidence, expected_revision=expected)
    assert archive.quality(CODE, NOW)['status'] == 'unknown'


def test_frozen_prefix_survives_legitimate_same_day_appends_but_not_repair_or_quarantine(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = bars()
    morning = datetime.fromisoformat(rows[59]['end'])
    archive.save(CODE, rows[:60], morning, verification=anchor())
    frozen = archive.quality(CODE, morning)
    assert frozen['sessions'][0]['frozenThrough'] == rows[59]['end']
    assert minute.validate_archive_lineage(tmp_path, frozen)['valid']
    archive.save(CODE, rows[60:], NOW, verification=anchor())
    assert minute.validate_archive_lineage(tmp_path, frozen)['valid']
    assert archive.quality(CODE, morning)['fingerprint'] == frozen['fingerprint']
    assert archive.quality(CODE, NOW)['fingerprint'] != frozen['fingerprint']
    # Even a conflict outside the used prefix invalidates the whole session;
    # the day cannot be selectively presented as clean.
    outside = {**rows[-1], 'close':4.505, 'source':'sina'}
    archive.save(CODE, [outside], NOW)
    assert not minute.validate_archive_lineage(tmp_path, frozen)['valid']
    current = archive.quality(CODE, NOW)
    archive.rebuild(CODE, DAY, rows, NOW, verification=anchor(),
                    expected_revision=current['sessions'][0]['archiveRevision'])
    assert not minute.validate_archive_lineage(tmp_path, frozen)['valid']


def test_large_legal_intraday_move_needs_independent_evidence_not_fixed_ten_percent_rule(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = bars()
    rows[120:] = bars(8.0)[120:]
    evidence = {**anchor(), 'close':8.0, 'high':8.01, 'low':4.49}
    archive.save(CODE, rows, NOW, verification=evidence)
    assert archive.quality(CODE, NOW)['status'] == 'verified'
    assert len(archive.read(CODE, NOW)) == 240


def quote(at, price=4.5, source='Tencent quote'):
    return {'code':CODE,'source':source,'provider_timestamp':at.isoformat(),
            'open_price':price,'high':price+.01,'low':price-.01,'price':price,'pre_close':price}


def test_quote_verifies_only_covered_prefix_and_later_anchor_keeps_first_known_at(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = bars()
    morning = datetime.fromisoformat(rows[59]['end'])
    archive.save(CODE, rows[:60], morning)
    state = archive.verify_quote(CODE,quote(morning+timedelta(seconds=15)),morning+timedelta(seconds=20))
    assert state['status'] == 'verified'
    frozen = archive.quality(CODE,morning,[DAY])
    assert frozen['sessions'][0]['qualityKnownAt']
    assert frozen['sessions'][0]['verifiedThrough'] == rows[59]['end']
    archive.save(CODE,rows[60:61],morning+timedelta(minutes=1))
    assert archive.quality(CODE,morning+timedelta(minutes=1),[DAY])['status'] == 'unknown'
    assert minute.validate_archive_lineage(tmp_path,frozen)['valid']
    archive.verify_quote(CODE,quote(morning+timedelta(minutes=1)),morning+timedelta(minutes=1,seconds=3))
    assert archive.quality(CODE,morning+timedelta(minutes=1),[DAY])['status'] == 'verified'
    assert archive.quality(CODE,morning,[DAY])['sessions'][0]['qualityKnownAt'] == frozen['sessions'][0]['qualityKnownAt']


def test_close_quote_automatically_verifies_day_without_blessing_other_days_or_clearing_taint(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = bars()
    close = datetime.fromisoformat(rows[-1]['end'])
    archive.save(CODE,rows,NOW)
    assert archive.verify_quote(CODE,quote(close),close+timedelta(minutes=30))['status'] == 'verified'
    assert archive.quality(CODE,NOW,[DAY])['status'] == 'verified'
    archive.quarantine(CODE,DAY,'external_conflict')
    assert archive.verify_quote(CODE,quote(close),close+timedelta(minutes=31))['status'] == 'tainted'


def test_quote_wrong_security_same_provider_future_or_unrelated_day_does_not_verify(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    rows = bars()
    at = datetime.fromisoformat(rows[59]['end'])
    archive.save(CODE,rows[:60],at)
    invalid = [quote(at,source='tdx'),quote(at,source='unidentified_gateway'),
               {**quote(at),'code':'001336'},quote(at+timedelta(minutes=1)),quote(at-timedelta(days=1))]
    for item in invalid:
        assert archive.verify_quote(CODE,item,at)['status'] == 'unknown'
    archive.verify_quote(CODE,quote(at,price=31.53),at)
    assert archive.quality(CODE,at,[DAY])['status'] == 'tainted'


def test_expired_deadline_reads_cache_without_network(monkeypatch, tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, bars(), NOW)
    monkeypatch.setattr(minute, 'monotonic', lambda: 100.)
    monkeypatch.setattr(minute, '_prior_sessions', lambda *args: [])
    monkeypatch.setattr(minute, 'fetch_sina_bars', lambda *a, **k: pytest.fail('late network request'))
    result = minute.fetch_bar_evidence(CODE, NOW, tmp_path, deadline=99., backfill=False, software_count=240)
    assert result['dataLineage']['status'] == 'unknown'


def test_feature_lineage_does_not_depend_on_unrelated_older_archive_days(monkeypatch,tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE,bars(),NOW,verification=anchor())
    old = [{**row,'end':row['end'].replace(DAY,'2026-09-01')} for row in bars()]
    archive.save(CODE,old,NOW)
    cutoff = datetime.fromisoformat(bars()[-1]['end'])+timedelta(seconds=30)
    monkeypatch.setattr(minute,'_prior_sessions',lambda *_:[])
    monkeypatch.setattr(minute,'monotonic',lambda:100.)
    result = minute.fetch_bar_evidence(CODE,cutoff,tmp_path,deadline=99.,backfill=False)
    assert result['dependencyDays'] == [DAY]
    assert result['dataLineage']['status'] == 'verified'
    assert [row['day'] for row in result['dataLineage']['sessions']] == [DAY]


def test_cross_source_conflict_cannot_be_used_directly_as_label_bars(tmp_path):
    first = bars()
    second = [{**row, 'close':4.505, 'source':'sina'} for row in first]
    audit = {}
    merged = minute.supplement_bars(first, second, NOW, diagnostics=audit)
    assert audit['conflictingOverlapMinutes'] == 240
    assert not minute.normalize_bars(merged, NOW)
    assert not minute.minute_coverage(merged, NOW, [DAY])['pricePathComplete']
    archive = minute.MinuteArchive(tmp_path)
    archive.save(CODE, merged, NOW)
    assert archive.quality(CODE, NOW)['status'] == 'tainted'
    assert not archive.read(CODE, NOW)


def test_recent_fetch_deadline_does_not_start_fallback(monkeypatch):
    clock = [10.]
    monkeypatch.setattr(minute, 'monotonic', lambda: clock[0])
    def software(*args, **kwargs):
        assert kwargs['timeout'] == 1.
        clock[0] = 12.
        return bars()[:2]
    monkeypatch.setattr(minute, 'fetch_software_bars', software)
    monkeypatch.setattr(minute, 'fetch_sina_bars', lambda *a, **k: pytest.fail('fallback started after deadline'))
    assert len(minute.fetch_recent_bars(CODE, count=20, cutoff=NOW, deadline=11.)) == 2


def test_longer_return_and_rebound_features_use_only_completed_window():
    start = datetime(2026, 10, 8, 9, 31, tzinfo=minute.SHANGHAI)
    prices = [100-i*.3 for i in range(28)] + [92., 92.1, 92.2]
    rows = {start+timedelta(minutes=i):{'close':price,'amount_cny':1000,'volume_shares':10}
            for i, price in enumerate(prices)}
    result = minute._window_features(rows, max(rows))
    assert result['r30_pct'] == pytest.approx(-7.8)
    assert result['r15_pct'] < 0 and result['r3_pct'] > 0
    assert result['short_rebound_in_long_weak'] == 1.
    short = minute._window_features(dict(list(rows.items())[-5:]), max(rows))
    assert short['r30_pct'] is None and short['short_rebound_in_long_weak'] is None


def test_repair_cli_dry_run_leaves_live_bytes_unchanged_and_apply_keeps_backup(tmp_path, monkeypatch, capsys):
    archive_dir = tmp_path / 'archive'
    archive = minute.MinuteArchive(archive_dir)
    archive.save(CODE, bars(), NOW)
    before = archive.path.read_bytes()
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'as_of':NOW.isoformat(), 'operations':[{
        'operation':'quarantine', 'code':CODE, 'day':DAY, 'reason':'test-independent-conflict'}]}), encoding='utf-8')
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'minute-integrity.py'
    spec = importlib.util.spec_from_file_location('minute_integrity_cli', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = [str(script), '--archive-dir', str(archive_dir), '--plan', str(plan)]
    monkeypatch.setattr(sys, 'argv', args)
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == 'dry_run' and not result['applied']
    assert archive.path.read_bytes() == before
    monkeypatch.setattr(sys, 'argv', args + ['--apply', '--backup-dir', str(tmp_path / 'backups')])
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result['applied'] and Path(result['backup']).is_file()
    assert archive.quality(CODE, NOW)['status'] == 'tainted'
