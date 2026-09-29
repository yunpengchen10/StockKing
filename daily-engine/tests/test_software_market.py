from datetime import datetime
from zoneinfo import ZoneInfo
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from src.services.software_market import SoftwareMarketClient
from src.services.yao_scout.scan_claim import run_once


def test_gateway_rejects_non_loopback_and_credentials():
    for value in ('https://example.com', 'http://localhost:9000', 'http://127.0.0.1:9000/redirect', 'http://user:pass@127.0.0.1:9000'):
        with pytest.raises(ValueError):
            SoftwareMarketClient(value, 'secret')


def test_gateway_bar_contract_preserves_missing_amount():
    client = SoftwareMarketClient('http://127.0.0.1:9000', 'secret')
    bar = {'end': '2026-09-29T09:40:00+08:00', 'open': 10., 'high': 10.2, 'low': 9.9,
           'close': 10.1, 'volume_shares': 100., 'amount_cny': None}
    with patch.object(client, '_request', return_value={'results': {'600000': {'bars': [bar], 'adjustment': 'none'}}}) as request:
        result = client.bars('600000')
    assert result['bars'][0]['amount_cny'] is None
    assert result['bars'][0]['volume_shares'] == 100
    assert request.call_args.args[1]['period'] == '1'


def test_official_scan_claim_is_shared_across_instances_and_manual_excluded(tmp_path):
    now = datetime(2026, 9, 29, 9, 40, tzinfo=ZoneInfo('Asia/Shanghai'))
    calls = []
    def service():
        return SimpleNamespace(persistent_claims=True, yao=SimpleNamespace(data_dir=tmp_path),
            clock=lambda: now, _run=lambda slot, **kw: calls.append(slot) or {'status': 'completed', 'candidates': []})
    first = run_once(service(), '0940', {'official': True})
    second = run_once(service(), '0940', {'official': True})
    assert first['status'] == 'completed' and second['idempotentReplay']
    assert calls == ['0940']
    run_once(service(), 'live', {'official': False})
    run_once(service(), 'live', {'official': False})
    assert calls == ['0940', 'live', 'live']
