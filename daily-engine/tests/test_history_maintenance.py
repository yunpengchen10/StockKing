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
