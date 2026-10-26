from pathlib import Path
from unittest.mock import Mock

from src.services.yao_scout import history_maintenance as maintenance


def test_cross_process_lease_serializes_repairs_and_only_owner_releases(tmp_path):
    assert maintenance._claim(tmp_path, 'first')
    assert not maintenance._claim(tmp_path, 'second')
    maintenance._release(tmp_path, 'second')
    assert not maintenance._claim(tmp_path, 'third')
    maintenance._release(tmp_path, 'first')
    assert maintenance._claim(tmp_path, 'second')


def test_offline_scan_never_starts_network_or_creates_maintenance_files(tmp_path, monkeypatch):
    from src.services.software_market import SoftwareMarketClient
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Mock(available=False))
    monkeypatch.setattr(maintenance, 'prepare_history', Mock(side_effect=AssertionError('unexpected network')))
    assert maintenance.start_history_preparation(tmp_path, ['600000'])['status'] == 'not_started'
    assert not list(tmp_path.iterdir())


def test_preparation_is_scheduled_without_waiting_and_failure_releases_lease(tmp_path, monkeypatch):
    from src.services.software_market import SoftwareMarketClient
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Mock(available=True))
    jobs = []
    class DeferredThread:
        def __init__(self, *, target, name, daemon):
            assert daemon
            self.target = target
        def start(self):
            jobs.append(self.target)
    monkeypatch.setattr(maintenance, 'Thread', DeferredThread)
    work = Mock(side_effect=TimeoutError('provider timeout'))
    monkeypatch.setattr(maintenance, 'prepare_history', work)
    assert maintenance.start_history_preparation(tmp_path, ['600000'])['status'] == 'started'
    assert not work.called
    assert maintenance.start_history_preparation(tmp_path, ['600000'])['status'] == 'already_running'
    from datetime import datetime
    assert maintenance.run_history_preparation(tmp_path, ['600000'], datetime.now(maintenance.SHANGHAI))['status'] == 'already_running'
    assert not work.called
    jobs[0]()
    assert maintenance.start_history_preparation(tmp_path, ['600000'])['status'] == 'started'


def test_background_pass_covers_multiple_batches_and_persists_unresolved_gaps(tmp_path, monkeypatch):
    import json
    codes = [f'600{index:03d}' for index in range(45)]
    calls = []
    def prepare(pool, now, directory, **kwargs):
        assert pool == codes and directory == tmp_path
        calls.append(kwargs)
        return {'processedThisRun': kwargs['max_codes'], 'status': 'partial',
                'ready': 8, 'pending': 37, 'lastCode': codes[sum(c['max_codes'] for c in calls)-1]}
    monkeypatch.setattr(maintenance, 'prepare_history', prepare)
    result = maintenance._prepare_batches(tmp_path, codes)
    assert [call['max_codes'] for call in calls] == [20, 20, 5]
    assert all(0 < call['timeout'] <= 120 for call in calls)
    assert result['status'] == 'partial' and result['queueCovered']
    assert result['processed'] == 45 and result['pending'] == 37
    assert json.loads((tmp_path/'history-maintenance-status.json').read_text('utf-8')) == result


def test_background_total_deadline_stops_after_partial_batch(tmp_path, monkeypatch):
    times = iter([0, 0, 0, 6, 6])
    monkeypatch.setattr(maintenance, 'monotonic', lambda: next(times))
    work = Mock(return_value={'processedThisRun': 4, 'status': 'partial', 'pending': 26})
    monkeypatch.setattr(maintenance, 'prepare_history', work)
    result = maintenance._prepare_batches(tmp_path, [f'600{i:03d}' for i in range(30)], timeout=5)
    assert work.call_count == 1 and work.call_args.kwargs['timeout'] == 5
    assert result['status'] == 'budget_exhausted'
    assert not result['queueCovered'] and result['remainingUnvisited'] == 26


def test_unavailable_window_cannot_spin_or_claim_completed(tmp_path, monkeypatch):
    work = Mock(return_value={'processedThisRun': 0, 'status': 'unavailable'})
    monkeypatch.setattr(maintenance, 'prepare_history', work)
    result = maintenance._prepare_batches(tmp_path, ['600000'])
    assert work.call_count == 1 and result['status'] == 'partial'
    assert result['remainingUnvisited'] == 1


def test_one_background_pass_keeps_the_same_target_session_across_close(tmp_path, monkeypatch):
    from datetime import datetime
    class Clock(datetime):
        calls = 0
        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return datetime(2026, 10, 22, 14 if cls.calls == 1 else 15, 59 if cls.calls == 1 else 1, tzinfo=tz)
    monkeypatch.setattr(maintenance, 'datetime', Clock)
    monkeypatch.setattr(maintenance, 'next_preparation_session', lambda now:
                        '2026-10-22' if now.hour < 15 else '2026-10-23')
    work = Mock(return_value={'processedThisRun': 1, 'status': 'partial', 'pending': 2})
    monkeypatch.setattr(maintenance, 'prepare_history', work)
    maintenance._prepare_batches(tmp_path, ['600000', '600001'], batch_size=1)
    assert work.call_count == 2
    assert {call.kwargs['target_session'] for call in work.call_args_list} == {'2026-10-22'}
