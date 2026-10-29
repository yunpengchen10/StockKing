"""Frozen opportunity evidence remains readable across rule-version upgrades."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from src.services.stock_king_display import (
    LocalPicksDisplayStore, _opportunity_assessment_valid,
    _opportunity_header_valid, displayable_run,
)
from src.services.yao_scout.opportunity_channels import VERSION


LEGACY = 'opportunity-em-20261028-v1'
NOW = datetime(2026, 10, 29, 10, 30, 15, tzinfo=timezone(timedelta(hours=8)))


def frozen_run(version=VERSION, *, empty=False, now=NOW):
    quote = {'code': '600001', 'source': 'frozen_fixture', 'source_time': now.isoformat(),
             'price': 10., 'pre_close': 10., 'open_price': 10., 'high': 10.2,
             'low': 9.8, 'change_pct': 0.}
    item = {'code': '600001', 'quote': quote, 'ruleRecommendation': not empty,
            'opportunity': {'version': version, 'grade': 'B' if empty else 'E-A',
                            'detectedAt': now.isoformat(), 'hardVetoes': [],
                            'buyWindow': {'status': 'observed'},
                            'sourceEvidence': {'minuteSource': 'frozen_fixture',
                                               'minuteAsOf': (now-timedelta(minutes=1)).isoformat()},
                            'metrics': {'r1_pct': -.2 if empty else .3}}}
    return {'run_id': f'{version}-{now.isoformat()}', 'mode': 'king_live', 'scanSlot': 'live',
            'status': 'completed_observations', 'generatedAt': now.isoformat(),
            'candidates': [], 'primarySelectionMode': 'daily_observation',
            'dataQuality': {'snapshot_count': 1, 'stale': True},
            'opportunityChannels': {'version': version, 'asOf': now.isoformat(),
                'policy': {'version': version}, 'researchCandidates': [deepcopy(item)],
                'recommendations': [] if empty else [deepcopy(item)],
                'observations': [deepcopy(item)] if empty else [],
                'diagnostics': {'researchedCount': 1, 'recommendationCount': 0 if empty else 1,
                                'observationCount': 1 if empty else 0}}}


@pytest.mark.parametrize('empty', [False, True])
def test_only_known_legacy_contract_can_be_read_but_not_newly_published(monkeypatch, tmp_path, empty):
    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path))
    legacy = frozen_run(LEGACY, empty=empty)
    assert _opportunity_assessment_valid(legacy, historical=True)
    assert displayable_run(legacy, historical=True)
    assert not displayable_run(legacy)
    store = LocalPicksDisplayStore(None)
    with pytest.raises(ValueError):
        store.save(legacy)
    store.save(legacy, source='history')
    before = store.path.read_bytes()
    restored = store.read()
    assert restored['adaptive']['opportunityChannels']['version'] == LEGACY
    assert restored['opportunityRecommendationCount'] == (0 if empty else 1)
    assert store.path.read_bytes() == before
    current = frozen_run(now=NOW+timedelta(minutes=2), empty=empty)
    assert displayable_run(current)
    assert store.save(current)['adaptive']['opportunityChannels']['version'] == VERSION


@pytest.mark.parametrize('version', [LEGACY, 'opportunity-em-future'])
def test_other_display_branch_cannot_publish_unsupported_opportunity_version(version):
    run = frozen_run(version)
    # The legacy generic branch would otherwise admit this as a completed run.
    run.pop('primarySelectionMode')
    run['dataQuality']['stale'] = False
    assert not displayable_run(run)
    if version != LEGACY:
        assert not displayable_run(run, historical=True)


@pytest.mark.parametrize('empty', [False, True])
def test_unknown_and_mixed_versions_do_not_inherit_legacy_qualification(empty):
    run = frozen_run('opportunity-em-future', empty=empty)
    assert not _opportunity_assessment_valid(run, historical=True)
    run = frozen_run(LEGACY, empty=empty)
    rows = run['opportunityChannels']['researchCandidates' if empty else 'recommendations']
    rows[0]['opportunity']['version'] = VERSION
    assert not _opportunity_assessment_valid(run, historical=True)


@pytest.mark.parametrize('version', [LEGACY, VERSION, 'opportunity-em-future'])
def test_header_versions_follow_explicit_history_policy(version):
    run = frozen_run(version, empty=True)
    packet = run['opportunityChannels']
    header = {'result': run, 'opportunity_recommendation_count': 0,
              'opportunity_assessment': {'version': version, 'policyVersion': version,
                  'asOf': packet['asOf'], 'diagnostics': packet['diagnostics'], 'researchRowCount': 1}}
    assert _opportunity_header_valid(header) is (version == VERSION)
    assert _opportunity_header_valid(header, historical=True) is (version in {LEGACY, VERSION})


def test_sql_history_legacy_empty_round_replaces_previous_picks_after_upgrade(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.storage import DatabaseManager, YaoRun

    monkeypatch.setenv('YAO_SCOUT_DATA_DIR', str(tmp_path/'display'))
    engine = create_engine(f"sqlite:///{(tmp_path/'history.sqlite').as_posix()}")
    YaoRun.__table__.create(engine)
    db = object.__new__(DatabaseManager)
    db._initialized, db._SessionLocal = True, sessionmaker(bind=engine)
    first, empty = frozen_run(LEGACY), frozen_run(LEGACY, empty=True, now=NOW+timedelta(minutes=2))
    try:
        store = LocalPicksDisplayStore(db)
        store.save(first, source='history')
        with engine.begin() as connection:
            connection.execute(YaoRun.__table__.insert(), {
                'run_id': empty['run_id'], 'mode': 'king_live', 'status': empty['status'],
                'as_of_at': datetime.fromisoformat(empty['generatedAt']).replace(tzinfo=None),
                'cutoff_at': datetime.fromisoformat(empty['generatedAt']).replace(tzinfo=None),
                'model_version': 'fixture', 'result_json': json.dumps(empty)})
        shown = store.read()
        assert shown['displayRunId'] == empty['run_id']
        assert shown['opportunityRecommendationCount'] == 0
        assert shown['adaptive']['opportunityChannels']['version'] == LEGACY
        assert 'displaySyncError' not in shown
        before = store.path.read_bytes()
        assert store.read()['displayRunId'] == empty['run_id']
        assert store.path.read_bytes() == before
    finally:
        engine.dispose()
