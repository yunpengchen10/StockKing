"""Offline boundaries for metadata-only display reconciliation and file caching."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace

from src.services.stock_king_display import LocalPicksDisplayStore, _MAX_HISTORY_READS


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
    assert latest['adaptive'] == scan(2)
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
