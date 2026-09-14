import numpy as np
import pandas as pd
import pytest

from src.quant.validation import block_mean_interval, holding_period_report
from src.quant.tier_models import nested_meta_gate_masks, TierModelService


def test_nested_validation_purges_labels_crossing_gate_even_with_missing_bars():
    dates = pd.Series(pd.bdate_range('2023-01-02', periods=400).repeat(8))
    # Trading suspensions can push actual label maturity past a calendar purge.
    ends = dates + pd.offsets.BDay(80)
    meta, gate = nested_meta_gate_masks(dates, minimum_dates=100, purge=20, label_end_dates=ends)
    assert meta.any() and gate.any()
    assert ends[meta].max() < dates[gate].min()
    assert not (meta & gate).any()
    assert dates[gate].nunique() >= 100


def test_nested_validation_rejects_short_history():
    meta, gate = nested_meta_gate_masks(pd.bdate_range('2024-01-01', periods=210), minimum_dates=100, purge=20)
    assert not meta.any() and not gate.any()


def test_block_interval_is_deterministic_and_preserves_persistent_risk():
    values = np.repeat([-.1, .1, -.1, .1, -.1, .1, -.1, .1], 20)
    iid = block_mean_interval(values, block_size=1)
    dependent = block_mean_interval(values, block_size=20)
    assert dependent == block_mean_interval(values, block_size=20)
    assert dependent[1] - dependent[0] > 2 * (iid[1] - iid[0])
    assert block_mean_interval(np.ones(99), block_size=20) is None
    assert block_mean_interval([np.nan] * 100, block_size=20) is None


def panel_report(*, skill=True, days=120, flat_market=False):
    dates = pd.bdate_range('2024-01-01', periods=days).repeat(10)
    targets = np.tile(np.arange(10), days)
    returns = np.full(len(dates), .05) if flat_market else .001 + targets * .01
    return holding_period_report(dates, targets, returns, targets if skill else -targets,
                                 horizon=20, cost=.0015)


def test_gate_distinguishes_stock_selection_from_a_rising_market():
    assert panel_report()['qualified']
    weak = panel_report(skill=False)
    assert not weak['qualified']
    assert weak['top5NetReturn'] > 0
    assert 'no_selection_advantage' in weak['failures']
    flat = panel_report(flat_market=True)
    assert not flat['qualified']
    assert 'no_selection_advantage' in flat['failures']


def test_missing_blocks_do_not_create_a_zero_confidence_bound():
    report = panel_report(days=80)
    assert not report['qualified']
    assert report['netReturnInterval95'] is None
    assert report['failures'] == ['insufficient_independent_blocks']


def test_holding_report_is_invariant_to_row_order():
    dates = pd.bdate_range('2024-01-01', periods=120).repeat(10)
    y = np.tile(np.arange(10), 120)
    args = [dates, y, .001 + y * .01, y]
    order = np.random.default_rng(10).permutation(len(y))
    first = holding_period_report(*args, horizon=20, cost=.0015)
    shuffled = holding_period_report(*[np.asarray(arg)[order] for arg in args], horizon=20, cost=.0015)
    assert first['qualified'] == shuffled['qualified']
    for key in ('rankIc', 'top5NetReturn', 'top5ExcessReturn', 'excessReturnInterval95'):
        assert first[key] == pytest.approx(shuffled[key])


def test_maturity_column_never_becomes_a_model_feature():
    panel = pd.DataFrame({'ret_1': [.1], 'label_end_20': [pd.Timestamp('2026-01-01')]})
    assert TierModelService._feature_names(panel) == ['ret_1']


def test_regular_training_produces_both_horizon_reports(tmp_path, monkeypatch):
    """Exercise the actual ensemble/registry contract with small LightGBM fits."""
    import lightgbm as lgb
    import src.quant.tier_models as module
    monkeypatch.setattr(module, '_lgbm_ranker', lambda seed: lgb.LGBMRanker(
        n_estimators=3, num_leaves=4, n_jobs=1, verbosity=-1, random_state=seed))
    service = TierModelService(tmp_path / 'models', tmp_path / 'cache')
    monkeypatch.setattr(service, '_master_oos_vector', lambda data, horizon: (np.full(len(data), np.nan), None))
    days, stocks = 800, 12
    dates = pd.bdate_range('2021-01-01', periods=days)
    rng = np.random.default_rng(18)
    feature = rng.normal(size=days * stocks)
    panel = pd.DataFrame({
        'date': dates.repeat(stocks), 'symbol': np.tile([f'{i:06}.SH' for i in range(stocks)], days),
        'industry': np.tile(['A'] * 6 + ['B'] * 6, days),
        'log_size': rng.normal(size=days * stocks), 'beta_60': rng.uniform(.5, 1.5, days * stocks),
        'ret_1': feature, 'volatility_20': .02,
        'forward_return_5': .02 + feature * .02 + rng.normal(0, .01, days * stocks),
        'forward_return_20': .04 + feature * .03 + rng.normal(0, .015, days * stocks),
        'label_end_20': (dates + pd.offsets.BDay(20)).repeat(stocks),
    })
    artifact = service._train_regular(panel)
    assert set(artifact.metrics['holdingPeriods']) == {'5', '20'}
    assert artifact.metadata['validationVersion'] == 'balancedrank-block-v1'
    assert artifact.metadata['nestedValidation']['purgeTradingDates'] == 20
    assert 'label_end_20' not in artifact.feature_names
    assert artifact.metrics['holdingPeriods']['20']['evaluationDays'] >= 100
    candidates = [{'code': f'{i:06}.SH'} for i in range(stocks)]
    service._score_regular(artifact, candidates, panel[artifact.feature_names].iloc[-stocks:].to_numpy())
    assert all('excessRank5d' in item and 'excessRank20d' in item for item in candidates)
