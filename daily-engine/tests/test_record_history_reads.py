"""History pages read only selected frozen scans, including legacy databases."""
import json
import sqlite3

import pytest

from src.services.yao_scout.signal_learning import SignalLearningService


def insert_run(db, run_id, day, result=None, *, version='v1', malformed=False):
    raw = '{unreadable old scan' if malformed else json.dumps({'result': result or {}})
    db.execute('INSERT INTO signal_runs VALUES(?,?,?,?,?,?,?,?,?)',
               (run_id, 'live', day + 'T10:30:00+08:00', day, 0, version,
                'completed_observations', raw, day + 'T10:30:00+08:00'))


def readonly_service(path):
    class ReadOnlyLedger(SignalLearningService):
        def _connect(self):
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            connection.row_factory = sqlite3.Row
            return connection
    return ReadOnlyLedger.__new__(ReadOnlyLedger)


@pytest.mark.parametrize('compact', [False, True])
def test_latest_page_does_not_read_discarded_scans_without_migrating_legacy_database(tmp_path, compact):
    service = SignalLearningService(tmp_path)
    with service._connect() as db:
        # A legacy read-only installation has no new metadata indexes. The
        # page boundary must still precede JSON parsing, independent of plan.
        db.execute('DROP INDEX signal_runs_history_order')
        db.execute('DROP INDEX signal_runs_history_day')
        insert_run(db, 'old', '2026-10-28', malformed=True)
        insert_run(db, 'latest', '2026-10-30', {'run_id': 'display-latest'})
    before = service.db_path.read_bytes()
    result = readonly_service(service.db_path).history({'compact': compact, 'limit': 1})
    assert [(row['runId'], row['displayRunId']) for row in result['items']] == [('latest', 'display-latest')]
    assert service.db_path.read_bytes() == before


@pytest.mark.parametrize('compact', [False, True])
def test_alias_and_direct_id_union_filters_and_limits_remain_consistent(tmp_path, compact):
    service = SignalLearningService(tmp_path)
    with service._connect() as db:
        insert_run(db, 'older-alias', '2026-10-27', {'run_id': 'shared'})
        insert_run(db, 'shared', '2026-10-28', {'run_id': 'different'})
        insert_run(db, 'newer-alias', '2026-10-29', {'run_id': 'shared'}, version='v2')
        insert_run(db, 'unrelated-newest', '2026-10-30', {'run_id': 'other'})
    read = readonly_service(service.db_path)
    def ids(**filters):
        return [row['runId'] for row in read.history({'compact': compact, 'run_id': 'shared', **filters})['items']]
    assert ids() == ['newer-alias', 'shared', 'older-alias']
    assert ids(limit=1) == ['newer-alias']
    assert ids(date='2026-10-28') == ['shared']
    assert ids(version='v1') == ['shared', 'older-alias']
    assert ids(date='2026-10-30') == []


def test_compact_lists_share_one_scan_read_and_keep_unknown_nested_evidence(tmp_path):
    service = SignalLearningService(tmp_path)
    watch = {'code': '600001', 'name': 'watch', 'referencePrice': 10,
             'nextDaySignal': {'selected': True, 'queue': 'first_board', 'unknown': None},
             'rawMinuteEvidence': ['large evidence omitted from transport'] * 100}
    opportunity = {'code': '600002', 'name': 'opportunity', 'referencePrice': 12,
                   'opportunity': {'grade': 'E-A', 'verified': False, 'missing': None},
                   'rawMinuteEvidence': watch['rawMinuteEvidence']}
    with service._connect() as db:
        insert_run(db, 'one', '2026-10-30', {'run_id': 'display-one', 'nextDayWatchlist': [watch],
                   'opportunityChannels': {'recommendations': [opportunity]}})
    calls = []
    original_connect = service._connect
    def connect():
        connection = original_connect()
        connection.set_trace_callback(calls.append)
        return connection
    service._connect = connect
    before = service.db_path.read_bytes()
    result = service.history({'compact': True, 'include_controls': False})
    rows = {row['code']: row for row in result['items'][0]['signals']}
    assert rows['600001']['nextDaySelected'] is True
    assert rows['600001']['snapshot']['nextDaySignal']['unknown'] is None
    assert rows['600002']['opportunitySelected'] is True
    assert rows['600002']['snapshot']['opportunity'] == opportunity['opportunity']
    assert all('rawMinuteEvidence' not in row['snapshot'] for row in rows.values())
    scan_reads = [sql for sql in calls if 'scan_json' in sql]
    assert len(scan_reads) == 1, 'one page row must not repeatedly read/parse its large universe snapshot'
    assert service.db_path.read_bytes() == before
