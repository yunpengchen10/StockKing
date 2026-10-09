"""Immutable research defaults; every value is included in the algorithm contract.

These are unvalidated starting points, never fitted or silently promoted online.
Candidate grids are for chronological, held-out comparisons only.
"""
from collections.abc import Mapping
from types import MappingProxyType


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


VALIDATION_STATUS = 'unvalidated_starting_points'
DEFAULT_PARAMETERS = _freeze({
    'history': {'minimumScoreDays': 5, 'formalDays': 20,
                'criticalFactors': ['M', 'V', 'W', 'B']},
    'freshness': {'quoteSeconds': 30, 'minuteSeconds': 300,
                  'researchMaxAgeSeconds': {'C': 86400, 'G': 86400, 'E': 300, 'H': 300}},
    'weights': {
        'early': {'M': .18, 'V': .09, 'T': .05, 'F': .14, 'W': .12, 'S': .12,
                  'B': .10, 'C': .08, 'G': .07, 'L': .05},
        'main': {'F': .18, 'M': .16, 'S': .14, 'B': .13, 'W': .12,
                 'V': .05, 'T': .05, 'G': .07, 'C': .05, 'L': .05},
        'distribution': {'volumeExhaustion': .20, 'priceInefficiency': .20,
                         'vwapBreak': .15, 'lowerHigh': .15, 'sectorDivergence': .10,
                         'activeSell': .10, 'failedBreakout': .10}},
    'score': {
        'centre': 50., 'minimum': 0., 'maximum': 100., 'zScale': 12.5,
        'relativePriceScale': 500., 'vwapSlopePerPct': 50., 'ratioScale': 20.,
        'sectorPctScale': 10., 'activeBuyScale': 2., 'flowPositive': 75., 'flowNegative': 25.,
        'vwapReclaim': 90., 'vwapRetest': 85.,
        'momentumWeights': {'r3': .25, 'r5': .20, 'a3': .30, 'a1': .15, 'break': .10},
        'volumeWeights': {'v3': .35, 'v5': .25, 'va': .20, 'structure': .20},
        'fundWeights': {'active': .5, 'vendor_flow': .5},
        'vwapWeights': {'position': .35, 'slope': .25, 'reclaim': .20, 'retest': .20},
        'sectorWeights': {'relative': .45, 'sector_return': .30, 'breadth': .25},
        'bookDepthWeight': .6, 'bookSpreadWeight': .4, 'bookDepthReferenceCny': 1e6,
        'bookDepthLogScale': 10., 'bookSpreadScale': 150.,
        'riskPenaltyCoefficient': .2, 'riskMissingScore': 100.,
        'riskVolumeRatio': 2., 'exhaustionBase': 40., 'exhaustionScale': 20.,
        'inefficiencyReturnPct': .2, 'inefficiencyBase': 70., 'inefficiencyScale': 10.,
        'vwapBreakRisk': 80., 'sectorDivergenceRisk': 85.,
        'activeSellThreshold': 45., 'activeSellRisk': 80.,
        'missingFactorPolicy': 'fixed_declared_weights_zero_contribution_no_renormalization',
        'missingRiskPolicy': 'fixed_declared_weights_conservative_upper_bound',
        'stageBranches': {'breakout': 'early', 'repair': 'early', 'continuation': 'main'},
    },
    'minute': {'madConsistency': 1.4826, 'relativeMadFloor': .05, 'zClip': 6.,
               'priceMadFloor': .05, 'amountMadFloor': 1., 'vwapRetestTolerance': .002},
    'profiles': {
        'conservative': {'label': '稳健', 'max_bias_pct': 3., 'max_atr_extension': 1.5,
                         'min_reward_risk': 2., 'max_unlock_pct': 3., 'require_financial_quality': True},
        'regular': {'label': '均衡', 'max_bias_pct': 5., 'max_atr_extension': 2.,
                    'min_reward_risk': 1.5, 'max_unlock_pct': 5., 'require_financial_quality': False},
        'aggressive': {'label': '激进', 'max_bias_pct': 7.5, 'max_atr_extension': 2.5,
                       'min_reward_risk': 1.2, 'max_unlock_pct': 8., 'require_financial_quality': False}},
    'entry': {'previousCloseToleranceCny': .011, 'unknownRewardRiskAggressiveBreakoutException': True,
              'eventCoverage': ['forecast', 'unlock'], 'negativeForecastTypes': ['首亏', '续亏', '增亏', '预减']},
    'researchFactors': {
        'C': {'halfLifeSessions': 1., 'neutralScore': 50., 'positiveScale': 50.,
              'positiveTypes': {'预增': 1., '扭亏': 1., '略增': .5}},
        'G': {'lookbackSessions': 120, 'minEvents': 20, 'limitPct': 10.,
              'continuationWeight': .5, 'meanReturnWeight': .3, 'reversalWeight': .2,
              'meanReturnScale': 10., 'reversalScale': 10., 'priceTick': .01},
        'E': {'historySessions': 60, 'lowPercentile': 20., 'highPercentile': 80., 'slotMinutes': 5,
              'maxSourceAgeSeconds': 300, 'maxReceiptLagSeconds': 300,
              'minimumCoverage': .8, 'minimumUniverse': 300, 'weakExposure': .5, 'hotExposure': .75},
        'H': {'minPeers': 5, 'minimumCoverage': .8, 'minimumBreadthPct': 60.}},
    'validation': {'minimumMatureDays': 120, 'minimumValidSamples': 1000, 'purgeDays': 5,
        'shadowDays': 20, 'minimumHoldoutDays': 60, 'minimumHoldoutTrades': 50,
        'minimumShadowTrades': 1, 'executionModel': 'king-v1.2-execution-model',
        'splitPolicy': 'reserve_max_60_days_or_20pct_holdout_then_75pct_pre_holdout_training',
        'holdoutFraction': .20, 'preHoldoutTrainFraction': .75,
        'minimumCalibrationDays': 15, 'minimumTrainSamples': 300,
        'purgePolicy': 'actual_label_end_strictly_before_next_split',
        'exitPolicy': 'target_close_then_first_legal_completed_minute_close_proxy'},
})
CANDIDATE_GRIDS = _freeze({
    'riskPenaltyCoefficient': [.1, .2, .3, .4],
    'vwapSlopePerPct': [25., 50., 100.],
    'turnoverWeightFromVolume': [0., .05, .10],
    'eventHalfLifeSessions': [1, 3, 5],
    'emotionLowMultiplier': [.8, .9, 1.],
    'stagePolicy': ['stage_selected', 'mean', 'max_legacy'],
    'emotionUse': ['downward_regime_only', 'shadow_only'],
})


def parameter_contract():
    return {'validationStatus': VALIDATION_STATUS,
            'defaults': _plain(DEFAULT_PARAMETERS), 'candidateGrids': _plain(CANDIDATE_GRIDS)}
