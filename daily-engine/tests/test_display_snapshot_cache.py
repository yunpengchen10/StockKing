"""Offline boundaries for metadata-only display reconciliation and file caching."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace

from src.services.stock_king_display import LocalPicksDisplayStore, _MAX_HISTORY_READS


def test_custom_screen_uses_all_distinct_frozen_quotes_and_only_verified_members():
    from src.services.stock_king_display import _display_response
    at = '2026-11-03T10:30:00+08:00'
    def quote(code):
        return {'code': code, 'name': code, 'provider_timestamp': at, 'source': 'fixture',
                'price': 10.3, 'high': 10.4, 'pre_close': 10, 'limit_up': 11,
                'order_book': {'source_time': at, 'source': 'fixture',
                               'asks': [{'price': 10.31, 'volume_shares': 300}]}}
    quotes = {str(code): quote(str(code)) for code in range(600010, 600023)}
    quotes['600020']['high'] = 11
    quotes['600021'].update(price=11, high=11)
    quotes['600022']['order_book']['asks'] = []
    watches = [{'code': '600010', 'quote': quotes['600010'], 'nextDaySignal': {
        'queue': 'first_board', 'touchedLimitToday': False, 'sourceTime': at,
        'selected': True, 'referencePrice': 10.3, 'branches': ['平台突破蓄势']}}]
    run = {'generatedAt': at, 'dailyObservationQuotes': quotes, 'nextDayWatchlist': watches,
           'controls': watches, 'themeBoard': {'themes': [
               {'name': name, 'memberCodes': [code], 'membershipEligible': eligible, 'sourceTime': at}
               for name, code, eligible in [('机器人', '600011', True), ('未核实', '600010', False)]]}}
    shown = _display_response({'adaptive': run})['adaptive']
    screen = shown['customScreen']
    assert screen['checkedCount'] == 13 and screen['eligibleCount'] == 10
    assert screen['excludedCounts'] == {'at_limit': 1, 'touched_limit': 1, 'unavailable': 1}
    selected, other = screen['rows'][:2]
    assert selected['code'] == '600010' and selected['firstBoardSelected'] and not selected['formalSelected']
    assert selected['reasons'] == ['平台突破蓄势'] and selected['themes'] == []
    assert other['code'] == '600011' and other['themes'] == ['机器人'] and not other['firstBoardSelected']
    assert other['changePct'] == 3 and other['limitEntry']['askVolumeShares'] == 300
    assert 'dailyObservationQuotes' not in shown and 'quote' not in other


def test_custom_screen_resolves_latest_quotes_and_rejects_conflicts():
    from datetime import datetime, timedelta
    from src.services.stock_king_display import _custom_screen
    now = datetime.fromisoformat('2026-11-03T10:30:00+08:00')
    def quote(code, offset=0, **changes):
        at = (now+timedelta(seconds=offset)).isoformat()
        return {'code': code, 'name': code, 'source': 'fixture', 'source_time': at,
                'price': 10, 'high': 10.5, 'pre_close': 9.9, 'limit_up': 10.89,
                'order_book': {'source': 'fixture', 'source_time': at,
                               'asks': [{'price': 10.01, 'volume_shares': 100}]}, **changes}
    codes = [str(code) for code in range(600010, 600016)]
    older = [quote(codes[0], -10), quote(codes[1], -10), quote(codes[2]), quote(codes[3]),
             quote(codes[4], -10), quote(codes[5], -10)]
    updates = [quote(codes[0], high=10.89), quote(codes[1], price=10.89, high=10.89),
               quote(codes[2], price=10.2), quote(codes[3], limit_up=10.9),
               quote(codes[4], order_book={}), quote(codes[5], 2, price=12)]
    for a, b in ((older, updates), (updates, older)):
        run = {'controls': [{'code': q['code'], 'quote': q} for q in a],
               'dailyObservationQuotes': {q['code']: q for q in b}}
        screen = _custom_screen(run, now)
        assert [row['code'] for row in screen['rows']] == [codes[5]]
        assert screen['excludedCounts'] == {'touched_limit': 1, 'at_limit': 1, 'conflicting': 2, 'unavailable': 1}
        assert screen['rows'][0]['limitEntry']['sourceTime'] == older[5]['source_time']


def scan(index, *, valid=True):
    return {'run_id': f'cache-{index}', 'mode': 'king_live', 'scanSlot': 'live',
            'status': 'completed_observations',
            'generatedAt': f'2026-11-03T10:{index:02d}:00+08:00',
            'candidates': [{'code': f'{600000+index}'}],
            'dataQuality': {'snapshot_count': 3000 if valid else 0}}


def database(scans):
    reads = []
    headers = [{'id': index+1, 'run_id': run['run_id'], 'mode': run['mode'],
                'status': run['status'], 'as_of': run['generatedAt']}
               for index, run in enumerate(scans)]
    def fetch(run_id):
        reads.append(run_id)
        return {'result': deepcopy(next(run for run in scans if run['run_id'] == run_id))}
    return SimpleNamespace(display_history_identity='offline-fixture-database',
                           list_yao_run_headers=lambda **_: list(reversed(headers)),
                           get_yao_run=fetch), reads


def test_invalid_recent_rows_have_a_bounded_read_budget_and_do_not_hide_older_valid_result(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    valid = scan(1)
    db, reads = database([valid, *[scan(i, valid=False) for i in range(2, 8)]])
    store = LocalPicksDisplayStore(db)
    assert store.read().get('displaySyncError')
    assert len(reads) == _MAX_HISTORY_READS
    for _ in range(4):
        before = len(reads)
        shown = store.read()
        assert len(reads)-before <= _MAX_HISTORY_READS
        if shown.get('displayRunId'):
            break
    assert shown['displayRunId'] == valid['run_id']
    assert len(reads) == 7
    assert store.read() == shown
    assert len(reads) == 7


def test_missing_snapshot_can_be_restored_again_after_successful_history_read(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    run = scan(1)
    db, reads = database([run])
    store = LocalPicksDisplayStore(db)
    first = store.read()
    store.path.unlink()
    restored = LocalPicksDisplayStore(db).read()
    assert restored['displayRunId'] == first['displayRunId']
    assert reads == [run['run_id'], run['run_id']]


def test_external_atomic_snapshot_replacement_invalidates_shared_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    first = store.save(scan(1))
    frozen = json.loads(store.path.read_text(encoding='utf-8'))
    frozen['adaptive'] = scan(2)
    frozen['generatedAt'] = frozen['adaptive']['generatedAt']
    replacement = tmp_path/'replacement.json'
    replacement.write_text(json.dumps(frozen), encoding='utf-8')
    os.replace(replacement, store.path)
    latest = LocalPicksDisplayStore(None).read()
    assert latest['displayRunId'] != first['displayRunId']
    assert latest['adaptive']['publishedPicks']['status'] == 'unrecorded'
    assert {k: v for k, v in latest['adaptive'].items() if k != 'publishedPicks'} == scan(2)
    assert json.loads(store.path.read_text(encoding='utf-8')) == frozen


def test_insufficient_summary_preserves_admission_and_legacy_trigger_reasons(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    run = scan(1)
    row = {'code': '600009', 'quote': {'price': 10, 'fetched_at': '2026-11-03T10:01:01+08:00',
        'public_quote_metadata': {'status': 'observed', 'raw': 'provider transport'}},
        'primaryAdmission': {'eligible': False,
        'reasons': ['报价未通过'], 'missingReasons': ['缺少历史基准'],
        'largeResearch': {'raw': 'not for display'}},
        'triggerConditions': ['站上昨日高点'], 'upgradeConditions': ['等报价核验'],
        'invalidationConditions': ['跌破支撑'], 'expectedHoldingRange': '1至3个交易日'}
    run['evidenceInsufficient'] = [row]
    store = LocalPicksDisplayStore(None)
    shown = store.save(run)['adaptive']['evidenceInsufficient'][0]
    assert shown['primaryAdmission'] == {'eligible': False,
        'reasons': ['报价未通过'], 'missingReasons': ['缺少历史基准']}
    assert shown['quote']['fetched_at'] == row['quote']['fetched_at']
    assert shown['quote']['public_quote_metadata'] == {'status': 'observed'}
    for key in ('triggerConditions', 'upgradeConditions', 'invalidationConditions', 'expectedHoldingRange'):
        assert shown[key] == row[key]
    assert json.loads(store.path.read_text(encoding='utf-8'))['adaptive']['evidenceInsufficient'] == [row]


def test_recovery_resumes_after_restart_and_finds_newer_completion_behind_older_ids(tmp_path, monkeypatch):
    from src.services import stock_king_display as display
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    newest = scan(1)
    newest['generatedAt'] = '2026-11-03T11:00:00+08:00'
    older = [scan(i) for i in range(2, 7)]
    for run in older:
        run['generatedAt'] = '2026-11-03T10:00:00+08:00'
    db, reads = database([newest, *older])
    first = LocalPicksDisplayStore(db).read()
    assert first['displayRecovery']['remainingHeaders'] == 6-_MAX_HISTORY_READS
    assert len(reads) == _MAX_HISTORY_READS and newest['run_id'] not in reads
    display._history_checked.clear()  # A fresh process must resume from the small checkpoint.
    display._display_cache.clear()
    for _ in range(4):
        before = len(reads)
        restored = LocalPicksDisplayStore(db).read()
        assert len(reads)-before <= _MAX_HISTORY_READS
        if 'displayRecovery' not in restored:
            break
    assert restored['displayRunId'] == newest['run_id']
    assert len(reads) == 6 and 'displayRecovery' not in restored
    assert LocalPicksDisplayStore(db).read() == restored
    assert len(reads) == 6


def test_checkpoint_write_failure_does_not_fail_display(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    db, _ = database([scan(1)])
    store = LocalPicksDisplayStore(db)
    replace = os.replace
    def unavailable(source, target):
        if target == store._history_path:
            raise PermissionError('checkpoint directory is temporarily unavailable')
        return replace(source, target)
    monkeypatch.setattr(os, 'replace', unavailable)
    assert store.read()['displayRunId'] == 'cache-1'
    assert store.path.exists()


def test_database_identity_change_does_not_reuse_old_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    db, reads = database([scan(1), scan(2)])
    store = LocalPicksDisplayStore(db)
    store.read()
    assert len(reads) == 2
    db.display_history_identity = 'replacement-database'
    store.read()
    assert len(reads) == 3  # The displayed row is known; the other row must be checked anew.


def test_other_process_replace_after_save_never_caches_our_payload_under_its_file_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    store = LocalPicksDisplayStore(None)
    store.save(scan(1))
    replacement = json.loads(store.path.read_text(encoding='utf-8'))
    replacement['adaptive'] = scan(3)
    replacement['generatedAt'] = replacement['adaptive']['generatedAt']
    other = tmp_path/'other-process.json'
    other.write_text(json.dumps(replacement), encoding='utf-8')
    replace = os.replace
    def concurrent_replace(source, target):
        replace(source, target)
        if target == store.path and other.exists():
            replace(other, target)
    monkeypatch.setattr(os, 'replace', concurrent_replace)
    assert store.save(scan(2))['displayRunId'] == 'cache-3'
    for _ in range(2):
        assert LocalPicksDisplayStore(None).read()['displayRunId'] == 'cache-3'


def test_record_snapshot_reads_one_exact_scan_without_changing_the_display(tmp_path, monkeypatch):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    run = {**scan(2), 'controls': [{'code': '600099'}]}
    reads = []
    def fetch(key):
        reads.append(key)
        return {'mode': 'king_live', 'run_id': key, 'result': run} if key == run['run_id'] else None
    store = LocalPicksDisplayStore(SimpleNamespace(get_yao_run=fetch))
    item = store.history_snapshot(run['run_id'])
    assert item['result']['run_id'] == item['run_id'] == run['run_id']
    assert item['result']['candidates'] == run['candidates']
    assert 'controls' not in item['result']
    assert store.history_snapshot('missing') is None
    assert reads == [run['run_id'], 'missing']
    assert not store.path.exists()
