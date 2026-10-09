from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.services.yao_scout.theme_evidence import theme_factor

NOW = datetime(2026, 9, 30, 10, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


def frame():
    rows = [{'code': '600000', 'concepts': '题材A;题材B', 'change_pct': 2}]
    rows += [{'code': f'60000{i}', 'concepts': '题材A', 'change_pct': -1} for i in range(1, 6)]
    rows += [{'code': f'60001{i}', 'concepts': '题材B', 'change_pct': 9} for i in range(1, 6)]
    data = pd.DataFrame([{**row, 'source_time': NOW.isoformat()} for row in rows])
    data['concepts'] = data.concepts.astype(object)
    data.attrs['source'] = 'frozen-provider-snapshot'
    return data


def test_theme_is_frozen_before_returns_and_excludes_target():
    result = theme_factor(frame(), '600000', NOW, received_at=NOW)
    assert result['status'] == 'observed'
    assert result['theme'] == '题材A' and result['score'] == 0
    assert result['mode'] == 'shadow'
    assert result['features']['peer_count'] == 5
    assert '600000' not in result['members']


def test_missing_timestamps_and_late_mapping_cannot_fake_breadth():
    data = frame()
    data.loc[data.concepts == '题材A', 'source_time'] = None
    data.attrs['fetched_at'] = NOW.isoformat()
    result = theme_factor(data, '600000', NOW, received_at=NOW)
    assert result['score'] is None and result['status'] == 'insufficient'
    result = theme_factor(frame(), '600000', NOW, received_at=NOW + timedelta(seconds=1))
    assert result['score'] is None


def test_source_coverage_counts_all_theme_peers_not_only_valid_risers():
    data = frame()
    data.loc[data.code == '600001', 'source_time'] = (NOW + timedelta(seconds=1)).isoformat()
    result = theme_factor(data, '600000', NOW, received_at=NOW)
    assert result['features']['peer_coverage_pct'] == 80
    assert result['features']['peer_count'] == 4
    assert result['status'] == 'insufficient'


def test_primary_industry_cannot_stand_in_for_missing_theme_mapping():
    data = frame().rename(columns={'concepts': 'industry'})
    assert theme_factor(data, '600000', NOW, received_at=NOW)['status'] == 'unavailable'


def test_future_target_mapping_cannot_select_a_theme_with_valid_peers():
    data = frame()
    data.loc[data.code == '600000', 'source_time'] = (NOW + timedelta(seconds=1)).isoformat()
    assert theme_factor(data, '600000', NOW, received_at=NOW)['score'] is None
    data = frame()
    data.attrs['complete'] = False
    assert theme_factor(data, '600000', NOW, received_at=NOW)['score'] is None


@pytest.mark.parametrize('missing', [None, float('nan'), '', '  ', [], [None], ['题材B', None]])
def test_missing_universe_mapping_cannot_be_excluded_from_the_denominator(missing):
    data = frame()
    for index in data.index[data.concepts == '题材B']:
        data.at[index, 'concepts'] = missing
    result = theme_factor(data, '600000', NOW, received_at=NOW)
    assert result['status'] == 'insufficient' and result['score'] is None
    assert result['features']['mapping_universe_count'] == 11
    assert result['features']['mapping_known_count'] == 6
    assert result['features']['mapping_unknown_count'] == 5
    assert result['features']['mapping_coverage_pct'] == pytest.approx(600/11)


def test_explicit_empty_arrays_require_provider_complete_mapping_declaration():
    data = frame()
    for index in data.index[data.concepts == '题材B']:
        data.at[index, 'concepts'] = []
    # Complete quote pagination does not assert complete theme membership.
    data.attrs['complete'] = True
    assert theme_factor(data, '600000', NOW, received_at=NOW)['status'] == 'insufficient'
    data.attrs['theme_mapping_complete'] = True
    result = theme_factor(data, '600000', NOW, received_at=NOW)
    assert result['status'] == 'observed'
    assert result['features']['mapping_coverage_pct'] == 100
    assert result['features']['peer_count'] == 5


def test_complete_declaration_never_converts_null_or_blank_to_no_theme():
    data = frame()
    data.attrs['theme_mapping_complete'] = True
    for index, value in zip(data.index[data.concepts == '题材B'], [None, float('nan'), '', ' ', [None]]):
        data.at[index, 'concepts'] = value
    result = theme_factor(data, '600000', NOW, received_at=NOW)
    assert result['status'] == 'insufficient'
    assert result['features']['mapping_known_count'] == 6


def test_explicit_no_theme_and_unknown_target_remain_distinct():
    data = frame()
    data.at[0, 'concepts'] = []
    unknown = theme_factor(data, '600000', NOW, received_at=NOW)
    assert unknown['score'] is None and '未知' in unknown['gaps'][-1]
    assert unknown['features']['mapping_known_count'] == 10
    data.attrs['theme_mapping_complete'] = True
    empty = theme_factor(data, '600000', NOW, received_at=NOW)
    assert empty['score'] is None and '明确' in empty['gaps'][-1]
    assert empty['features']['mapping_known_count'] == 11


def test_mapping_coverage_reuses_the_eighty_percent_boundary():
    data = frame().iloc[:10].copy()
    data.loc[[8, 9], 'concepts'] = None
    enough = theme_factor(data, '600000', NOW, received_at=NOW)
    assert enough['status'] == 'observed'
    assert enough['features']['mapping_coverage_pct'] == 80
    data.loc[7, 'concepts'] = None
    assert theme_factor(data, '600000', NOW, received_at=NOW)['status'] == 'insufficient'


def test_future_nonmember_mappings_do_not_count_as_known_universe_membership():
    data = frame()
    data.loc[data.concepts == '题材B', 'source_time'] = (NOW + timedelta(seconds=1)).isoformat()
    result = theme_factor(data, '600000', NOW, received_at=NOW)
    assert result['status'] == 'insufficient'
    assert result['features']['mapping_known_count'] == 6
