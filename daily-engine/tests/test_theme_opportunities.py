"""Theme-first entry point, using the existing independent flow fixtures."""
from copy import deepcopy
from datetime import timedelta

import pytest

from src.services.yao_scout.theme_opportunities import build_theme_opportunities, VERSION
from src.services.stock_king_display import _display_response, displayable_run
from tests.test_flow_opportunity_policy import NOW, inputs


def theme_board(codes=('600001',), now=NOW):
    return {'version': 'theme-rotation-v1', 'status': 'observed', 'asOf': now.isoformat(),
        'sourceTime': now.isoformat(), 'source': 'fixture_concepts', 'gaps': [],
        'selection': {'maximumThemes': 3, 'selectedIds': ['T1']},
        'themes': [{'id': 'T1', 'name': '测试主线', 'rank': 1, 'heatScore': .9,
            'changePct': 1., 'breadthPct': 90., 'relativeStrengthPct': .8,
            'stage': 'observed', 'selected': True, 'membershipEligible': True,
            'sourceTime': now.isoformat(), 'memberCodes': list(codes),
            'memberQuotes': {code: {'changePct': 1.5, 'valid': True, 'sourceTime': now.isoformat()} for code in codes},
            'coverage': {'fraction': 1.}}]}


def build(*packets, board=None, now=NOW, **kwargs):
    packets = packets or [inputs(now=now)]
    return build_theme_opportunities([p[0] for p in packets], now, theme_board=board,
        minute_packets={p[0]['code']: p[1] for p in packets},
        daily_packets={p[0]['code']: p[2] for p in packets},
        sector_flow_packets={p[0]['code']: p[3] for p in packets}, **kwargs)


def test_missing_theme_cannot_fall_back_to_qualified_industry_flow():
    result = build()
    assert result['recommendations'] == []
    assert 'theme_market_evidence_unavailable' in result['observations'][0]['opportunity']['failedConditions']


def test_selected_core_theme_keeps_all_flow_evidence():
    result = build(board=theme_board())
    row = result['recommendations'][0]
    assert row['opportunity']['version'] == VERSION
    assert row['opportunity']['theme']['coreEligible'] is True
    assert row['opportunity']['flowEvidence']['sector']['coverage']['excludedSelf']
    assert not row['formalEligible'] and not row['executable'] and not row['trainingEligible']


@pytest.mark.parametrize('mutation', ['stale', 'future', 'no_source', 'unselected', 'weak_member', 'unverified_member'])
def test_bad_or_nonleading_theme_remains_observation(mutation):
    board = theme_board()
    theme = board['themes'][0]
    if mutation == 'stale':
        board['sourceTime'] = (NOW-timedelta(seconds=301)).isoformat()
    elif mutation == 'future':
        board['asOf'] = (NOW+timedelta(seconds=1)).isoformat()
    elif mutation == 'no_source':
        board['source'] = None
    elif mutation == 'unselected':
        theme['selected'] = False
    elif mutation == 'weak_member':
        theme['memberQuotes']['600001']['changePct'] = -.5
    else:
        theme['memberQuotes']['600001']['valid'] = False
    assert build(board=board)['recommendations'] == []


def test_hot_theme_never_overrides_negative_funds_or_bad_book():
    row, minute, daily, sector = inputs(flow_delta=-1000)
    assert build((row, minute, daily, sector), board=theme_board())['recommendations'] == []
    row, minute, daily, sector = inputs()
    row['quote']['order_book']['asks'] = []
    assert build((row, minute, daily, sector), board=theme_board())['recommendations'] == []


def test_theme_order_precedes_better_flow_rank_and_has_per_theme_limit():
    codes = ['600001', '600002', '600003', '600004']
    board = theme_board(codes[:3])
    second = deepcopy(theme_board(codes[3:])['themes'][0])
    second.update(id='T2', name='第二主线', rank=2, heatScore=.8)
    board['themes'].append(second)
    board['selection']['selectedIds'].append('T2')
    packets = [inputs(code, sector=code, flow_delta=1000*(i+1)) for i, code in enumerate(codes)]
    result = build(*packets, board=board)
    assert [row['code'] for row in result['recommendations']] == ['600003', '600002', '600004']
    assert result['observations'][0]['opportunity']['selectionExclusion'] == 'theme_concentration_limit'


def test_leader_rank_precedes_flow_quality_within_same_theme():
    board = theme_board(['600001', '600002'])
    board['themes'][0]['memberQuotes']['600001']['changePct'] = 3
    result = build(inputs('600001', flow_delta=1000), inputs('600002', flow_delta=5000), board=board)
    assert [row['code'] for row in result['recommendations']] == ['600001', '600002']


def test_new_contract_preserves_first_detection_without_rewriting_inputs():
    first = build(board=theme_board())
    later = NOW+timedelta(seconds=5)
    current = build(inputs(now=later), board=theme_board(now=later), now=later, previous=first)
    assert current['recommendations'][0]['opportunity']['firstDetectionAt'] == NOW.isoformat()
    assert first['recommendations'][0]['opportunity']['version'] == VERSION


def test_display_omits_large_universe_and_member_quotes_but_keeps_feedback():
    run = {'themeBoard': theme_board(), 'themeResearchUniverse': [{'code': '600001'}],
           'themeFeedback': {'summary': '昨日主线仍需跟踪'}}
    shown = _display_response({'adaptive': run})['adaptive']
    assert 'themeResearchUniverse' not in shown
    assert 'memberQuotes' not in shown['themeBoard']['themes'][0]
    assert shown['themeFeedback'] == run['themeFeedback']
