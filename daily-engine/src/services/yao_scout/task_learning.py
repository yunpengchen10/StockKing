"""Task-specific point-in-time learning. Nothing in this module trains on import.

Price-pattern probabilities and executable returns have separate contracts and
registries. All parameter values below are declared research defaults, not fitted
claims. A model cannot affect live selection before holdout AND forward gates.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from functools import wraps
from contextvars import ContextVar
import json
import math

import numpy as np

from .morning_task import MORNING_TASK_ID, MORNING_QUEUES, MORNING_VERSION, LARGE_DROP_THRESHOLD

ENTRY_TASK_ID = 'entry_today_exit_next_session'
NEXT_DAY_TASK_ID = 'next_session_price_event'
DEFERRED_ENTRY_TASK_ID = 'entry_next_session_exit_following_session'
TASK_MODEL_VERSION = 'king-task-learning-v1'
DATA_QUALITY_VERSION = 'minute-integrity-v1'
BIG_RETURN_THRESHOLD = .05
TAIL_ALPHA = .10
RISK_COEFFICIENT = 1.
WALK_FORWARD_DAYS = 20
WALK_FORWARD_FOLDS = 3
COHORT_WINDOW_DAYS = 240
_LINEAGE_CACHE = ContextVar('stock_king_lineage_validation_cache', default=None)


def task_contract(task_id=ENTRY_TASK_ID, queue=None):
    if task_id not in {ENTRY_TASK_ID, NEXT_DAY_TASK_ID, MORNING_TASK_ID}:
        # Buying tomorrow cannot reuse a tomorrow-close label or artifact.
        raise ValueError('unsupported_task_requires_independent_T_plus_2_execution_labels')
    if task_id == NEXT_DAY_TASK_ID and queue not in {'first_board', 'continuation'}:
        raise ValueError('next_day_queue_required')
    if task_id == MORNING_TASK_ID and queue not in MORNING_QUEUES:
        raise ValueError('morning_time_stratum_required')
    payload = {'taskId': task_id, 'queue': queue, 'targetHorizon': 1,
        'version': TASK_MODEL_VERSION, 'dataQualityVersion': DATA_QUALITY_VERSION,
        'featureSchemaVersion': 'frozen-numeric-with-missing-indicators-v1',
        'labelVersion': 'executable-T1-net-frozen-cap-v1' if task_id == ENTRY_TASK_ID else 'T1-close-price-pattern-v1',
        'labelSizing': 'frozen_single_name_cap_fee_inclusive_nominal_account' if task_id == ENTRY_TASK_ID else 'none_price_observation',
        'bigReturnThreshold': BIG_RETURN_THRESHOLD, 'tailAlpha': TAIL_ALPHA,
        'riskCoefficient': RISK_COEFFICIENT, 'parameterStatus': 'declared_unvalidated_research_defaults',
        'calibration': 'disjoint_chronological_sigmoid',
        'returnMeaning': 'after_cost_execution_proxy' if task_id == ENTRY_TASK_ID else 'price_observation_not_trading_return',
        'classMeaning': 'net_return_at_least_5pct' if task_id == ENTRY_TASK_ID else 'next_session_close_10pct_price_pattern',
        'costPolicy': 'already_in_net_label_do_not_subtract_again',
        'unknownFillPolicy': 'unknown_not_cash_zero',
        'folds': WALK_FORWARD_FOLDS, 'foldDays': WALK_FORWARD_DAYS,
        'cohortWindowSignalDays': COHORT_WINDOW_DAYS,
        'signalQualityEligibility': 'verified_and_known_no_later_than_publication_immutable',
        'minimumShadowCompletedObservations': 50}
    if task_id == MORNING_TASK_ID:
        payload.update(targetHorizon=0, labelVersion=MORNING_VERSION,
            classMeaning='same_session_close_above_frozen_published_signal_reference_price',
            referencePolicy='immutable_published_signal_price_with_matching_fresh_known_quote',
            riskTarget='complete_post_publication_minutes_mae_relative_to_frozen_signal_price',
            riskPathPolicy='first_full_minute_start_strictly_after_publication_excludes_partial_publication_minute',
            minuteProxyPolicy='next_complete_minute_open_return_separate_not_training_target',
            minimumUpProbability=.5,
            upAccuracyGate='selected_up_rate_not_below_both_baselines_and_trading_day_block_95_lower_above_0.5',
            utilityPolicy='mean_price_return_minus_max_negative_q10_and_expected_mae',
            largeDropThreshold=LARGE_DROP_THRESHOLD,
            costPolicy='price_observation_no_fabricated_trade_or_cost',
            sameDayExitAllowed=False, T1TaskRequiredSeparately=True,
            dedupePolicy='first_selected_stock_day_reporting_balanced_stock_day_weights_within_time_stratum')
        payload.pop('bigReturnThreshold', None)
    payload['taskContractId'] = 'task-' + sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    return payload


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def scoped_lineage_validation(method):
    """Reuse exact prefix checks only within one public operation, never later."""
    @wraps(method)
    def wrapped(service, *args, **kwargs):
        token = _LINEAGE_CACHE.set({}) if _LINEAGE_CACHE.get() is None else None
        try:
            return method(service, *args, **kwargs)
        finally:
            if token is not None:
                _LINEAGE_CACHE.reset(token)
    return wrapped


def _validated_lineage(service, lineage):
    if not isinstance(lineage, dict) or not lineage.get('sessions'):
        return {'valid':False,'sessions':[]}
    from .minute_history import validate_archive_lineage
    cache = _LINEAGE_CACHE.get()
    sessions, valid = [], True
    for session in lineage['sessions']:
        prefix = {name:session.get(name) for name in ('code','day','frozenThrough','revision')}
        dependency = {'data_quality_version':lineage.get('data_quality_version'),'sessions':[prefix]}
        key = (str(service.data_dir),json.dumps(dependency,sort_keys=True,separators=(',',':')))
        result = cache.get(key) if cache is not None else None
        if result is None:
            result = validate_archive_lineage(service.data_dir / 'minute_history',dependency)
            if cache is not None:
                cache[key] = result
        sessions.extend(result.get('sessions') or [])
        valid = valid and result.get('valid') is True
    return {'valid':valid,'sessions':sessions}


def lineage_valid(service, lineage):
    return _validated_lineage(service,lineage).get('valid') is True


def lineage_known_at(service, lineage):
    """Knowledge time comes from the audit log, never from the price timestamp."""
    checked = _validated_lineage(service,lineage)
    sessions = checked.get('sessions') or []
    if checked.get('valid') is not True or not sessions or any(not row.get('qualityKnownAt') for row in sessions):
        return None
    return max(row['qualityKnownAt'] for row in sessions)


def combine_lineages(lineages):
    sessions = {}
    for lineage in lineages:
        if not lineage or not lineage.get('sessions'):
            raise ValueError('training_lineage_unverified')
        for session in lineage.get('sessions', []):
            key = (session['code'], session['day'], session.get('endAt') or session.get('frozenThrough') or '')
            if key in sessions and sessions[key].get('revision') != session.get('revision'):
                raise ValueError('conflicting_training_data_revisions')
            sessions[key] = session
    if not sessions:
        raise ValueError('training_lineage_sessions_missing')
    return {'status': 'verified', 'data_quality_version': DATA_QUALITY_VERSION,
            'sessions': [sessions[key] for key in sorted(sessions)]}


def balanced_sample_weights(samples):
    """One market day, then one stock-day, gets equal total weight."""
    repeats = Counter((row['day'], row['code']) for row in samples)
    stocks = Counter(day for day, _ in repeats)
    weights = np.asarray([1. / stocks[row['day']] / repeats[(row['day'], row['code'])]
                          for row in samples], dtype=float)
    return weights * len(weights) / weights.sum()


def feature_matrix(samples, features, medians):
    values = np.asarray([[finite(row['features'].get(name)) for name in features]
                         for row in samples], dtype=float)
    missing = ~np.isfinite(values)
    return np.column_stack((np.where(missing, np.asarray(medians), values), missing.astype(float)))


def class_target(row, task_id):
    if task_id == ENTRY_TASK_ID:
        return int(row['net'] >= BIG_RETURN_THRESHOLD)
    if task_id == MORNING_TASK_ID:
        return int(row['net'] > 0)
    return int(row['eventLabel'])


def fit_model(train, calibration, task_id, queue=None):
    """Fit only on train; fit probability calibration only on later labels."""
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.preprocessing import StandardScaler
    from lightgbm import LGBMClassifier, LGBMRegressor
    present = Counter(name for row in train for name, value in row['features'].items()
                      if finite(value) is not None)
    features = sorted(name for name, count in present.items() if count >= len(train) * .6)
    if not features:
        raise ValueError('features_insufficient')
    medians = [float(np.median([float(row['features'][name]) for row in train
                              if finite(row['features'].get(name)) is not None])) for name in features]
    x = feature_matrix(train, features, medians)
    xc = feature_matrix(calibration, features, medians)
    y = np.asarray([class_target(row, task_id) for row in train])
    yc = np.asarray([class_target(row, task_id) for row in calibration])
    if len(np.unique(y)) != 2 or len(np.unique(yc)) != 2:
        raise ValueError('two_classes_required_in_train_and_calibration')
    w, wc = balanced_sample_weights(train), balanced_sample_weights(calibration)
    scaler = StandardScaler().fit(x, sample_weight=w)
    xs = scaler.transform(x)
    logistic = LogisticRegression(C=1., max_iter=1000, random_state=23).fit(xs, y, sample_weight=w)
    returns = np.asarray([row['net'] for row in train], dtype=float)
    ridge = Ridge(alpha=8.).fit(xs, returns, sample_weight=w)
    args = dict(n_estimators=80, num_leaves=7, min_child_samples=30, learning_rate=.04,
                max_depth=4, random_state=23, verbosity=-1, n_jobs=1)
    net_model = LGBMRegressor(**args).fit(x, returns, sample_weight=w)
    tail_model = LGBMRegressor(objective='quantile', alpha=TAIL_ALPHA, **args).fit(x, returns, sample_weight=w)
    classifier = LGBMClassifier(**args).fit(x, y, sample_weight=w)
    raw = np.clip(classifier.predict_proba(xc)[:, 1], 1e-6, 1-1e-6)
    logits = np.log(raw / (1-raw)).reshape(-1, 1)
    calibrator = LogisticRegression(C=1., max_iter=500, random_state=23).fit(logits, yc, sample_weight=wc)
    artifact = {**task_contract(task_id, queue), 'features': features, 'medians': medians,
        'netRegressor': net_model.booster_.model_to_string(),
        'tailRegressor': tail_model.booster_.model_to_string(),
        'eventClassifier': classifier.booster_.model_to_string(),
        'calibrationCoef': float(calibrator.coef_[0, 0]), 'calibrationIntercept': float(calibrator.intercept_[0]),
        'logisticCoef': logistic.coef_[0].tolist(), 'logisticIntercept': float(logistic.intercept_[0]),
        'ridgeCoef': ridge.coef_.tolist(), 'ridgeIntercept': float(ridge.intercept_),
        'logisticMean': scaler.mean_.tolist(), 'logisticScale': scaler.scale_.tolist(),
        'trainingPrevalence': float(np.average(y, weights=w)),
        'trainingMeanReturn': float(np.average(returns, weights=w)),
        'rankMeaning': 'calibrated_event_probability_with_positive_return_utility_gate',
        'trainedThrough': max(row['day'] for row in train),
        'calibrationThrough': max(row['day'] for row in calibration), 'modelParameters': args}
    if task_id == MORNING_TASK_ID:
        adverse = np.asarray([finite(row.get('observedMae')) for row in train], dtype=float)
        if not np.all(np.isfinite(adverse)) or np.any(adverse < 0):
            raise ValueError('morning_adverse_excursion_labels_missing')
        mae_model = LGBMRegressor(**args).fit(x, adverse, sample_weight=w)
        artifact['maeRegressor'] = mae_model.booster_.model_to_string()
    return artifact


def predict_model(artifact, samples):
    from lightgbm import Booster
    x = feature_matrix(samples, artifact['features'], artifact['medians'])
    net = np.asarray(Booster(model_str=artifact['netRegressor']).predict(x, num_threads=1), dtype=float)
    tail = np.asarray(Booster(model_str=artifact['tailRegressor']).predict(x, num_threads=1), dtype=float)
    raw = np.clip(Booster(model_str=artifact['eventClassifier']).predict(x, num_threads=1), 1e-6, 1-1e-6)
    logit = np.log(raw/(1-raw)) * artifact['calibrationCoef'] + artifact['calibrationIntercept']
    probability = 1 / (1 + np.exp(-np.clip(logit, -30, 30)))
    standardized = (x - artifact['logisticMean']) / artifact['logisticScale']
    logistic = 1 / (1 + np.exp(-np.clip(standardized @ artifact['logisticCoef'] + artifact['logisticIntercept'], -30, 30)))
    ridge = standardized @ artifact['ridgeCoef'] + artifact['ridgeIntercept']
    risk = np.maximum(0., -tail)
    adverse = None
    if artifact['taskId'] == MORNING_TASK_ID:
        adverse = np.maximum(0., np.asarray(Booster(model_str=artifact['maeRegressor']).predict(x, num_threads=1), dtype=float))
        risk = np.maximum(risk, adverse)
    utility = net - artifact['riskCoefficient'] * risk
    eligible = (net > 0) & (utility > 0)
    if artifact['taskId'] == MORNING_TASK_ID:
        eligible &= probability > artifact['minimumUpProbability']
    result = {'net': net, 'tail': tail, 'utility': utility, 'probability': probability,
              'rank': np.where(eligible, probability*100, 0.), 'eligible': eligible,
              'logistic': logistic, 'ridge': ridge,
              'baselineRank': np.where(ridge > 0, logistic*100, 0.),
              'trainingPrevalence': np.full(len(samples), artifact['trainingPrevalence']),
              'trainingMeanReturn': np.full(len(samples), artifact['trainingMeanReturn'])}
    if adverse is not None:
        result['expectedMae'] = adverse
    if any(not np.all(np.isfinite(value)) for value in result.values()):
        raise ValueError('nonfinite_model_prediction')
    return result


def diagnostics(samples, prediction, artifact):
    from sklearn.metrics import average_precision_score
    y = np.asarray([class_target(row, artifact['taskId']) for row in samples])
    returns = np.asarray([row['net'] for row in samples], dtype=float)
    p = np.clip(prediction['probability'], 1e-6, 1-1e-6)
    weights = balanced_sample_weights(samples)
    brier = float(np.average((p-y)**2, weights=weights))
    base = float(np.average((prediction.get('trainingPrevalence', artifact['trainingPrevalence'])-y)**2, weights=weights))
    residual = returns-prediction['net']
    denominator = float(np.average((returns-prediction.get('trainingMeanReturn', artifact['trainingMeanReturn']))**2, weights=weights))
    buckets = []
    for low in np.arange(0, 1, .2):
        mask = (p >= low) & (p < low+.2 if low < .8 else p <= 1)
        if mask.any():
            buckets.append({'lower': float(low), 'count': int(mask.sum()),
                'meanProbability': float(np.average(p[mask], weights=weights[mask])),
                'observedFrequency': float(np.average(y[mask], weights=weights[mask]))})
    result = {'brier': brier, 'baselineBrier': base, 'brierSkill': 1-brier/base if base > 0 else None,
        'logLoss': float(np.average(-(y*np.log(p)+(1-y)*np.log(1-p)), weights=weights)),
        'prAuc': float(average_precision_score(y, p, sample_weight=weights)) if len(np.unique(y)) == 2 else None,
        'mae': float(np.average(np.abs(residual), weights=weights)),
        'rmse': float(np.sqrt(np.average(residual**2, weights=weights))),
        'outOfSampleR2': 1-float(np.average(residual**2, weights=weights))/denominator if denominator > 0 else None,
        'q10Coverage': float(np.average(returns < prediction['tail'], weights=weights)),
        'calibrationBuckets': buckets, 'positiveEvents': int(y.sum()),
        'stockDays': len({(row['day'], row['code']) for row in samples}),
        'independentTradingDays': len({row['day'] for row in samples}),
        'recommendationCoverage': float(np.average(prediction['eligible'], weights=weights)),
        'returnMeaning': artifact['returnMeaning']}
    if artifact['taskId'] == MORNING_TASK_ID:
        adverse = np.asarray([row['observedMae'] for row in samples], dtype=float)
        result.update(meanObservedMae=float(np.average(adverse, weights=weights)),
            maePredictionAbsoluteError=float(np.average(np.abs(adverse-prediction['expectedMae']), weights=weights)),
            largeDropRate=float(np.average(adverse >= LARGE_DROP_THRESHOLD, weights=weights)),
            labelMeaning='same_day_price_change_and_adverse_excursion_not_realized_profit')
    return result


def partition(samples, days, boundary, calibration_days=20, purge_days=5):
    """All label intervals must finish before the next chronological block."""
    calibration_start = boundary-calibration_days-purge_days
    train_end = calibration_start-purge_days
    if train_end <= 0:
        return [], []
    train_dates = set(days[:train_end])
    calibration_dates = set(days[calibration_start:boundary-purge_days])
    train = [row for row in samples if row['day'] in train_dates
             and row.get('labelEndAt', '9999') < days[calibration_start]
             and row.get('labelKnownAt', '9999') < days[calibration_start]
             and row.get('dataQualityKnownAt', '9999') < days[calibration_start]]
    calibration = [row for row in samples if row['day'] in calibration_dates
                   and row.get('labelEndAt', '9999') < days[boundary]
                   and row.get('labelKnownAt', '9999') < days[boundary]
                   and row.get('dataQualityKnownAt', '9999') < days[boundary]]
    return train, calibration


def load_artifact(service, version, active_only=False):
    if not version:
        return None
    with service._connect() as db:
        row = db.execute('SELECT artifact_json,stage,gates_json FROM model_versions WHERE version=?', (version,)).fetchone()
    if not row:
        return None
    try:
        artifact, gates = json.loads(row[0]), json.loads(row[2])
        from .local_algorithm import algorithm_contract
        contract = task_contract(artifact.get('taskId'), artifact.get('queue'))
        if (row[1] not in ({'active'} if active_only else {'active', 'shadow'})
                or gates.get('passed') is not True or artifact.get('version') != version
                or artifact.get('taskContractId') != contract['taskContractId']
                or artifact.get('algorithmContractId') != algorithm_contract()['contractId']
                or not lineage_valid(service, artifact.get('dataLineage'))
                or not all(key in artifact for key in ('netRegressor', 'tailRegressor', 'eventClassifier',
                    'calibrationCoef', 'calibrationIntercept', 'ridgeCoef', 'features', 'medians',
                    'ridgeIntercept', 'logisticMean', 'logisticScale', 'logisticCoef', 'logisticIntercept'))):
            return None
        if artifact.get('taskId') == MORNING_TASK_ID and 'maeRegressor' not in artifact:
            return None
        # A release row is not usable unless all serialized heads actually load.
        # Cache the probe by exact artifact content; lineage is still checked on
        # every load so a subsequently quarantined dependency invalidates it.
        digest = sha256(row[0].encode()).hexdigest()
        checked = getattr(service, '_checked_task_artifacts', set())
        if digest not in checked:
            predict_model(artifact, [{'features': dict(zip(artifact['features'], artifact['medians']))}])
            checked.add(digest)
            service._checked_task_artifacts = checked
        return artifact
    except Exception:
        return None


class TaskLearner:
    """One independent registry per prediction target and observation queue."""
    def __init__(self, service, task_id=ENTRY_TASK_ID, queue=None):
        self.service, self.task_id, self.queue = service, task_id, queue
        self.contract = task_contract(task_id, queue)
        self.key = 'v11' if task_id == ENTRY_TASK_ID else f'task:{task_id}:{queue}'

    def registry(self):
        with self.service._connect() as db:
            row = db.execute('SELECT payload_json FROM learning_registry WHERE registry_key=?', (self.key,)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    def save_registry(self, registry, now):
        from .signal_learning import _json
        with self.service._connect() as db:
            db.execute('INSERT INTO learning_registry VALUES(?,?,?) ON CONFLICT(registry_key) DO UPDATE '
                       'SET payload_json=excluded.payload_json,updated_at=excluded.updated_at',
                       (self.key, _json(registry), now.isoformat()))

    def _readiness_snapshot(self, now, data):
        """Summarize an already checked cohort; never run a second ledger scan."""
        from .local_algorithm import algorithm_contract
        samples, days, complete = data
        return {'version': 'task-readiness-v1', 'checkedAt': now.isoformat(),
            'taskContractId': self.contract['taskContractId'],
            'algorithmContractId': algorithm_contract()['contractId'],
            'matureDays': len(days), 'validSampleDays': len({row['day'] for row in samples}),
            'validSamples': len({(row['day'], row['code']) for row in samples}),
            'rawSampleCount': len(samples), 'completeQueue': bool(complete)}

    def refresh_readiness(self, now):
        """Explicit background/close-time cohort audit. Does not fit or promote a model.

        This is deliberately separate from ``readiness``: historical JSON and
        minute lineage checks can be expensive and must not run on each scan.
        """
        from .signal_learning import _at
        now = _at(now)
        registry = self.registry()
        registry['readinessSnapshot'] = self._readiness_snapshot(now, self.data(now))
        self.save_registry(registry, now)
        return self._readiness_report(now, registry)

    def readiness(self, now, *, current_candidates=None, current_data_eligibility=None):
        """Cheap diagnostic read, not an admission decision or a training trigger.

        Only the small registry row is read. Missing/stale audit counts remain
        unknown; they are never replaced by fabricated zeroes. Callers may pass
        their already computed data-only eligibility flags before publication.
        """
        from .signal_learning import _at
        return self._readiness_report(_at(now), self.registry(),
            current_candidates=current_candidates, current_data_eligibility=current_data_eligibility)

    def _readiness_report(self, now, registry, *, snapshot=None, current_candidates=None,
                          current_data_eligibility=None, verified_active=None):
        from .local_algorithm import algorithm_contract
        from .signal_learning import MIN_MATURE_DAYS, MIN_VALID_SAMPLES, MIN_HOLDOUT_DAYS, MIN_HOLDOUT_TRADES, SHADOW_DAYS
        algorithm_id = algorithm_contract()['contractId']
        audit = snapshot if snapshot is not None else registry.get('readinessSnapshot') or {}
        contract_matches = (audit.get('version') == 'task-readiness-v1'
                    and audit.get('taskContractId') == self.contract['taskContractId']
                    and audit.get('algorithmContractId') == algorithm_id)
        future_audit = bool(audit.get('checkedAt') and audit['checkedAt'] > now.isoformat())
        matching = bool(contract_matches and audit.get('checkedAt') and not future_audit)
        checked = audit.get('checkedAt') if matching else None
        fresh = bool(checked and checked[:10] == now.date().isoformat() and checked <= now.isoformat())
        counts = {key: audit.get(key) if matching else None for key in
                  ('matureDays', 'validSampleDays', 'validSamples', 'rawSampleCount', 'completeQueue')}
        registry_matches = (registry.get('taskContractId') == self.contract['taskContractId']
                            and registry.get('algorithmContractId') == algorithm_id)
        reason = registry.get('reason') if registry_matches else None
        champion = registry.get('championVersion') if registry_matches else None
        shadow = registry.get('shadowVersion') if registry_matches else None
        gates = (registry.get('gates') or {}) if registry_matches else {}
        current = None
        if current_candidates is not None or current_data_eligibility is not None:
            candidates = list(current_candidates or [])
            if current_data_eligibility is not None:
                flags = list(current_data_eligibility)
            else:
                flags = []
                for row in candidates:
                    eligibility = row.get('learningDataEligibility')
                    if isinstance(eligibility, dict):
                        eligibility = eligibility.get('eligible')
                    if eligibility is None:
                        signal = row.get('morningSignal') or {}
                        eligibility = signal.get('trainingEligibleAtSignal', signal.get('researchEligibleAtDecision'))
                    flags.append(eligibility)
            current = {'candidateCount': len(flags), 'dataEligibleCount': sum(value is True for value in flags),
                       'dataRejectedCount': sum(value is False for value in flags),
                       'dataUnknownCount': sum(value is not True and value is not False for value in flags),
                       'meaning': '本轮研究样本资格（含数据、方向和风控）；不等于历史成熟样本或模型准入'}
        blockers = []
        if current and current['candidateCount'] and current['dataEligibleCount'] == 0:
            blockers.append('current_research_eligibility_failed')
        if not champion:
            blockers.append('validated_model_missing')
        elif verified_active is False:
            blockers.append('configured_model_not_usable')
        if not matching:
            blockers.append('cohort_progress_unknown' if not audit else
                            'cohort_audit_after_as_of' if future_audit else 'cohort_contract_changed')
        else:
            if not fresh:
                blockers.append('cohort_progress_stale')
            if counts['validSamples'] == 0:
                blockers.append('no_eligible_mature_samples')
            if counts['matureDays'] < MIN_MATURE_DAYS:
                blockers.append('insufficient_mature_days')
            if counts['validSamples'] < MIN_VALID_SAMPLES:
                blockers.append('insufficient_valid_samples')
            if not counts['completeQueue']:
                blockers.append('incomplete_cohort_or_lineage')
        status, message, action = 'progress_unknown', '尚未审计当前任务的有效学习样本，进度未知。', '收盘复盘后刷新学习进度；盘中扫描不会自动训练。'
        if matching:
            status = 'collecting_samples'
            message = f"已核验 {counts['matureDays']}/{MIN_MATURE_DAYS} 个成熟日期、{counts['validSamples']}/{MIN_VALID_SAMPLES} 个有效股票日样本。"
            action = '继续保存发布时数据合格的正式扫描，并在收盘后核验完整分时标签。'
            if counts['validSamples'] == 0:
                status, action = 'no_eligible_samples', '先检查发布时的数据、方向与风控资格；事后补数据不能改写原信号的训练资格。'
            elif counts['matureDays'] >= MIN_MATURE_DAYS and counts['validSamples'] >= MIN_VALID_SAMPLES:
                status = 'ready_for_training' if counts['completeQueue'] else 'cohort_incomplete'
                action = ('后台执行学习检查；训练切分、留出验证和前瞻影子验证仍须分别通过。'
                          if counts['completeQueue'] else '补齐已冻结合格队列的标签与数据血缘，再重新核验。')
            if not fresh:
                message += ' 以上为上次核验结果，当前进度待刷新。'
        if reason and any(part in reason for part in ('failed', 'unavailable', 'invalid', 'too_small', 'history_insufficient')):
            status = 'validation_blocked'
            message += ' 最近学习检查未通过，尚未产生可用正式模型。'
            blockers.append(reason)
            action = '按最近学习检查原因处理数据、切分或验证问题；不降低验证门槛。'
        if shadow:
            status, message = 'shadow_collecting', f"模型处于前瞻影子验证：{registry.get('shadowDays', 0)}/{SHADOW_DAYS} 个成熟日期。"
            action = '继续冻结前瞻预测并收盘复盘；只有影子验证通过才可激活。'
        if champion:
            status = 'active' if verified_active is True else 'model_unusable' if verified_active is False else 'model_configured'
            message = ('当前任务已有通过本次加载核验的模型。' if verified_active is True else
                       '已配置的模型未通过本次加载核验，继续保持研究状态。' if verified_active is False else
                       '已有已激活模型记录；盘中使用时仍会核验模型、契约和数据血缘。')
            action = '继续按实时证据与模型准入结果筛选。' if verified_active is not False else '检查模型文件、任务契约及数据血缘；重新核验前不发布正式推荐。'
        elif not shadow:
            message += ' 当前任务尚无已验证模型。'
        if current and current['candidateCount'] and current['dataEligibleCount'] == 0:
            status = 'data_blocked'
            message = f"本轮 {current['candidateCount']} 只候选中，研究样本资格通过 0 只；不能形成新的合格训练样本。 " + message
            action = '先查看本轮数据、方向和风控的实际阻断原因；补齐缺失证据，等待未满足的市场条件，重复扫描不会绕过这些条件。'
        progress = {**counts, 'minimumMatureDays': MIN_MATURE_DAYS, 'minimumValidSamples': MIN_VALID_SAMPLES,
                    'minimumHoldoutDays': MIN_HOLDOUT_DAYS, 'minimumHoldoutObservations': MIN_HOLDOUT_TRADES,
                    'shadowDays': registry.get('shadowDays', 0) if shadow else 0, 'shadowRequiredDays': SHADOW_DAYS}
        return {'version': 'task-readiness-v1', 'taskId': self.task_id, 'queue': self.queue,
            'taskContractId': self.contract['taskContractId'], 'algorithmContractId': algorithm_id,
            'status': status, 'message': message, 'action': action, 'blockerCodes': list(dict.fromkeys(blockers)),
            'progress': progress, 'progressKnown': matching, 'progressFresh': fresh, 'countsAsOf': checked,
            'currentScan': current, 'lastLearningReason': reason,
            'lastLearningCheckAt': registry.get('lastLearningCheckAt'),
            'lastFitCompletedAt': registry.get('lastFitCompletedAt'),
            'lastValidationAt': registry.get('lastValidationAt'),
            'configuredChampionVersion': champion, 'shadowVersion': shadow,
            'validationGates': {key: {'passed': value.get('passed'), 'reason': value.get('reason')}
                for key, value in gates.items() if isinstance(value, dict)},
            'diagnosticOnly': True, 'selectionAuthority': False}

    def _next_events(self, now):
        from .signal_learning import ENTRY_SLOTS, _at, _feature_values
        from .local_algorithm import algorithm_contract
        with self.service._connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='next_day_outcomes'").fetchone()
            outcome = 'o.result_json,o.reviewed_at' if exists else 'NULL result_json,NULL reviewed_at'
            rows = db.execute('SELECT e.signal_id,e.code,e.decision_at,e.snapshot_json,r.run_id,r.trading_day,r.slot,'
                'r.created_at,' + outcome + ' FROM signal_events e JOIN signal_runs r ON r.run_id=e.run_id '
                + ('LEFT JOIN next_day_outcomes o ON o.signal_id=e.signal_id ' if exists else '')
                + "WHERE r.official=1 AND r.trading_day<=? AND json_extract(e.snapshot_json,'$.nextDaySignal.queue')=? "
                  "AND json_extract(e.snapshot_json,'$.algorithmContractId')=? "
                  "AND json_extract(e.snapshot_json,'$.nextDaySignal.learningTaskContractId')=? "
                  "AND json_extract(e.snapshot_json,'$.nextDaySignal.trainingEligibleAtSignal')=1 "
                  'ORDER BY r.trading_day,e.code,e.decision_at',
                  (now.date().isoformat(), self.queue,algorithm_contract()['contractId'],self.contract['taskContractId'])).fetchall()
        result = []
        for row in rows:
            snapshot = json.loads(row['snapshot_json'])
            signal = snapshot.get('nextDaySignal') or {}
            if (row['slot'] not in ENTRY_SLOTS or _at(row['created_at']) > now
                    or snapshot.get('algorithmContractId') != algorithm_contract()['contractId']
                    or signal.get('learningTaskContractId') != self.contract['taskContractId']
                    or signal.get('trainingEligibleAtSignal') is not True):
                continue
            outcome = json.loads(row['result_json']) if row['result_json'] else {}
            target = signal.get('targetSession')
            if not target or target >= now.date().isoformat() and now.time() < time(15, 1):
                continue
            # Target date comparisons use an aware timestamp, not natural-day arithmetic.
            due = datetime.combine(date.fromisoformat(target), time(15, 1), now.tzinfo)
            if due > now:
                continue
            features = _feature_values(snapshot)
            features.update({'next_' + key: float(value) for key, value in (signal.get('features') or {}).items()
                             if finite(value) is not None})
            features['ruleScore'] = finite(signal.get('score')) or 0.
            record = {'signalId': row['signal_id'], 'runId': row['run_id'],
                'day': row['trading_day'], 'code': row['code'], 'decisionAt': row['decision_at'],
                'features': features, 'selected': signal.get('selected') is True, 'selectionRank': signal.get('rank'),
                'dataLineage': snapshot.get('dataLineage'), 'labelDataLineage': outcome.get('dataLineage'),
                'targetSession': target, 'labelEndAt': outcome.get('labelEndAt'),
                'labelKnownAt': row['reviewed_at'], 'outcome': outcome, 'complete': False}
            ret = finite(outcome.get('observedReturnFromSignalPct'))
            if (outcome.get('status') == 'mature_price_pattern' and ret is not None
                    and outcome.get('targetSession') == target and outcome.get('version') == signal.get('version')
                    and isinstance(outcome.get('closeAtLimit'), bool)
                    and record['labelEndAt'] and _at(record['labelEndAt']) <= now
                    and row['reviewed_at'] and _at(row['reviewed_at']) <= now):
                record.update(net=ret/100, eventLabel=int(outcome['closeAtLimit']), complete=True)
            result.append(record)
        return result

    def data(self, now):
        if self.task_id == ENTRY_TASK_ID:
            days = self.service._evaluation_days(now)
            rows = self.service._dataset(now, dedupe=False)
            expected = None
        elif self.task_id == MORNING_TASK_ID:
            from .morning_task import morning_dataset
            expected, days = morning_dataset(self.service, now, self.contract, self.queue)
            rows = [row for row in expected if row['complete']]
        else:
            expected = self._next_events(now)
            days = sorted({row['day'] for row in expected})
            rows = [row for row in expected if row['complete']]
        # Calendar-based, predeclared window: never choose a start date by
        # surviving outcomes or returns. Unknown members fail the whole cohort
        # until repaired or naturally outside this fixed window.
        days = days[-COHORT_WINDOW_DAYS:]
        if expected is not None:
            expected = [row for row in expected if row['day'] in set(days)]
        usable = []
        for row in rows:
            if row['day'] not in set(days) or (self.task_id == ENTRY_TASK_ID and
                    (finite(row.get('portfolioWeight')) is None or not row.get('portfolioPolicy'))):
                continue
            known = [lineage_known_at(self.service, row.get(key)) for key in ('dataLineage', 'labelDataLineage')]
            if all(known) and max(known) <= now.isoformat():
                usable.append({**row, 'dataQualityKnownAt': max(known)})
        complete = (self.service._complete_universe(set(days), usable) if expected is None else
                    bool(days) and {r['signalId'] for r in expected} == {r['signalId'] for r in usable})
        return usable, days, complete

    def artifact(self, registry, active=True):
        versions = (registry.get('championVersion'), registry.get('rollbackVersion')) if active else (registry.get('shadowVersion'),)
        for version in versions:
            artifact = load_artifact(self.service, version, active_only=active)
            if artifact and artifact['taskContractId'] == self.contract['taskContractId']:
                return artifact
        return None

    def state(self, now, data=None):
        from .signal_learning import MIN_MATURE_DAYS, MIN_VALID_SAMPLES, MIN_HOLDOUT_DAYS, MIN_HOLDOUT_TRADES, SHADOW_DAYS
        from .local_algorithm import algorithm_contract
        samples, days, complete = data or self.data(now)
        registry = self.registry()
        active, shadow = self.artifact(registry), self.artifact(registry, active=False)
        same = registry.get('taskContractId') == self.contract['taskContractId'] and registry.get('algorithmContractId') == algorithm_contract()['contractId']
        reason = registry.get('reason') if same else 'task_contract_has_no_current_validation'
        stock_days = len({(r['day'], r['code']) for r in samples})
        if len(days) < MIN_MATURE_DAYS or stock_days < MIN_VALID_SAMPLES:
            reason = f'待积累独立成熟交易日/有效股票日：{len(days)}/{MIN_MATURE_DAYS} 天，{stock_days}/{MIN_VALID_SAMPLES} 条'
        return {**self.contract, 'algorithm': algorithm_contract(),
            'stage': 'active' if active else 'shadow' if shadow else 'rules_cold_start',
            'championVersion': active['version'] if active else None, 'shadowVersion': shadow['version'] if shadow else None,
            'configuredChampionVersion': registry.get('championVersion'),
            'effectiveRankVersion': active['version'] if active else 'rules_v1.2' if self.task_id == ENTRY_TASK_ID else
                'morning_rules_unvalidated' if self.task_id == MORNING_TASK_ID else 'next_day_rules_unvalidated',
            'effectiveRankSource': 'champion' if active else 'rules',
            'rankingFallbackReason': '模型缺失、数据血缘失效或契约不匹配，保持未验证研究规则' if registry.get('championVersion') and not active else None,
            'matureDays': len(days), 'validSamples': stock_days, 'rawSampleCount': len(samples),
            'minimumMatureDays': MIN_MATURE_DAYS, 'minimumValidSamples': MIN_VALID_SAMPLES,
            'minimumHoldoutDays': MIN_HOLDOUT_DAYS, 'minimumHoldoutCompletedTrades': MIN_HOLDOUT_TRADES,
            'shadowDays': registry.get('shadowDays', 0) if shadow else 0, 'shadowRequiredDays': SHADOW_DAYS,
            'lastLearningCheckAt': registry.get('lastLearningCheckAt'),
            'lastFitCompletedAt': registry.get('lastFitCompletedAt'), 'lastTrainingAt': registry.get('lastFitCompletedAt'),
            'lastValidationAt': registry.get('lastValidationAt'), 'activatedAt': registry.get('activatedAt'),
            'gates': registry.get('gates', {}) if same else {}, 'reason': reason,
            'validationContractId': algorithm_contract()['contractId'] if same else None,
            'historicalValidationAvailable': bool(registry.get('priorValidation') or registry.get('gates') and not same),
            'cohortAudit': {'completeQueue': complete, 'frozenSignalDays': days, 'completedSignalCount': len(samples)},
            'splitAudit': registry.get('splitAudit') if same else None,
            'validationAttempts': len(registry.get('validationHistory') or []),
            'rollbackVersion': registry.get('rollbackVersion'),
            'readiness': self._readiness_report(now, registry,
                snapshot=self._readiness_snapshot(now, (samples, days, complete)), verified_active=bool(active))}

    def gate(self, rows, prediction, artifact, days, complete, minimum_days=60):
        from .signal_learning import MIN_HOLDOUT_TRADES, PURGE_DAYS
        if not rows or not days:
            return {'passed': False, 'reason': 'empty_validation_cohort'}
        metric = diagnostics(rows, prediction, artifact)
        if self.task_id == ENTRY_TASK_ID:
            result = self.service._gates(rows, prediction['rank'], prediction['baselineRank'], seed=317,
                config=self.service.execution_config, complete_universe=complete,
                calendar_days=self.service._gate_calendar(rows, days), evaluation_days=days,
                minimum_days=minimum_days, minimum_completed_trades=MIN_HOLDOUT_TRADES)
        else:
            groups = defaultdict(list)
            for index, row in enumerate(rows):
                groups[row['day']].append((index, row))
            observed, baseline, simple, count = [], [], [], 0
            adverse_groups = {'model': [], 'frozen': [], 'simple': []}
            daily_up_counts = {key: [] for key in adverse_groups}
            daily_selected_counts = {key: [] for key in adverse_groups}
            for day in days:
                group = groups[day]
                # Decisions from a later scan cannot compete retrospectively
                # with morning names. Freeze the first eligible five unique
                # codes in chronological scan order for each daily watch policy.
                runs = defaultdict(list)
                for pair in group:
                    runs[pair[1]['runId']].append(pair)
                ordered_runs = sorted(runs.values(), key=lambda pairs: min(row['decisionAt'] for _, row in pairs))
                def choose(mode):
                    selected, seen = [], set()
                    for scan in ordered_runs:
                        ordered = (sorted(scan, key=lambda pair: (-prediction[mode][pair[0]], pair[1]['code'])) if mode
                                   else sorted(scan, key=lambda pair: (finite(pair[1].get(
                                       'baselineRank' if self.task_id == MORNING_TASK_ID else 'selectionRank')) or math.inf, pair[1]['code'])))
                        for index, row in ordered:
                            if row['code'] in seen or (prediction[mode][index] <= 0 if mode else not row.get(
                                    'baselineSelected' if self.task_id == MORNING_TASK_ID else 'selected')):
                                continue
                            selected.append(row)
                            seen.add(row['code'])
                            if len(selected) == 5:
                                return selected
                    return selected
                selected, current, benchmark = choose('rank'), choose(None), choose('baselineRank')
                if self.task_id == MORNING_TASK_ID:
                    for key, chosen in (('model', selected), ('frozen', current), ('simple', benchmark)):
                        adverse_groups[key].extend(row['observedMae'] for row in chosen)
                        daily_up_counts[key].append(sum(row['net'] > 0 for row in chosen))
                        daily_selected_counts[key].append(len(chosen))
                observed.append(sum(row['net'] for row in selected)/5)
                baseline.append(sum(row['net'] for row in current)/5)
                simple.append(sum(row['net'] for row in benchmark)/5)
                count += len(selected)
            values, control = np.asarray(observed), np.asarray(baseline)
            block = max(PURGE_DAYS, 5)
            rng = np.random.default_rng(317)
            starts = rng.integers(0, max(1, len(values)-block+1), size=(2000, math.ceil(len(values)/block)))
            idx = np.clip((starts[:, :, None]+np.arange(block)).reshape(2000, -1)[:, :len(values)], 0, len(values)-1)
            lower = float(np.quantile(np.mean(values[idx], axis=1), .05))
            advantage = float(np.quantile(np.mean((values-control)[idx], axis=1), .05))
            simple_advantage = float(np.quantile(np.mean((values-np.asarray(simple))[idx], axis=1), .05))
            result = {'passed': bool(complete and len(days) >= minimum_days and count >= MIN_HOLDOUT_TRADES
                                    and lower > 0 and advantage > 0 and simple_advantage > 0),
                      'independentSignalDays': len(days), 'selectedPriceObservations': count,
                      'meanPriceReturn95Lower': lower, 'priceReturnAdvantage95Lower': advantage,
                      'ridgeLogisticPriceAdvantage95Lower': simple_advantage,
                      'meaning': '固定每日最多5个观察名额的价格变化比较；非账户净值、非成交收益'}
            if self.task_id == MORNING_TASK_ID:
                risks = {key: {'count': len(values),
                    'meanMae': float(np.mean(values)) if values else None,
                    'largeDropRate': float(np.mean(np.asarray(values) >= LARGE_DROP_THRESHOLD)) if values else None}
                    for key, values in adverse_groups.items()}
                # No attractive average return may hide worse observed downside.
                downside = bool(all(value['count'] for value in risks.values()) and all(
                    risks['model']['meanMae'] <= risks[key]['meanMae']
                    and risks['model']['largeDropRate'] <= risks[key]['largeDropRate']
                    for key in ('frozen', 'simple')))
                result.update(downsideComparison=risks, downsideNotWorse=downside,
                              largeDropThreshold=LARGE_DROP_THRESHOLD)
                accuracy = {key: {'upCount': sum(daily_up_counts[key]),
                    'count': sum(daily_selected_counts[key]),
                    'upRate': sum(daily_up_counts[key])/sum(daily_selected_counts[key])
                        if sum(daily_selected_counts[key]) else None} for key in adverse_groups}
                # Resample whole trading-day blocks, preserving all correlated
                # stocks within a day. Repeated scans are never independent.
                resampled_counts = np.sum(np.asarray(daily_selected_counts['model'])[idx], axis=1)
                resampled_ups = np.sum(np.asarray(daily_up_counts['model'])[idx], axis=1)
                up_lower = float(np.quantile(np.divide(resampled_ups, resampled_counts,
                    out=np.zeros_like(resampled_ups, dtype=float), where=resampled_counts > 0), .05))
                accuracy_pass = bool(all(value['count'] for value in accuracy.values())
                    and up_lower > self.contract['minimumUpProbability'] and all(
                        accuracy['model']['upRate'] >= accuracy[key]['upRate'] for key in ('frozen', 'simple')))
                result.update(upAccuracyComparison=accuracy, selectedUpRate95Lower=up_lower,
                    upAccuracyPassed=accuracy_pass, upAccuracyUncertainty='trading_day_moving_block_bootstrap')
                result['passed'] = bool(result['passed'] and downside and accuracy_pass)
        result['predictionMetrics'] = metric
        result['probabilityCalibrated'] = bool(metric['brierSkill'] is not None and metric['brierSkill'] > 0
                                             and metric['positiveEvents'] > 0)
        result['passed'] = bool(result.get('passed') and result['probabilityCalibrated'])
        return result

    def train(self, now):
        from .signal_learning import MIN_MATURE_DAYS, MIN_VALID_SAMPLES, MIN_HOLDOUT_DAYS, SHADOW_DAYS, PURGE_DAYS, _VALIDATION, _json
        from .local_algorithm import algorithm_contract
        samples, days, complete = data = self.data(now)
        registry = self.registry()
        algorithm_id = algorithm_contract()['contractId']
        if (registry.get('taskContractId') != self.contract['taskContractId']
                or registry.get('algorithmContractId') != algorithm_id):
            registry['priorValidation'] = {key: registry.get(key) for key in
                ('algorithmContractId', 'taskContractId', 'gates', 'lastFitCompletedAt', 'lastTrainingAt')}
            registry.update(taskContractId=self.contract['taskContractId'], algorithmContractId=algorithm_id,
                            gates={}, shadowDays=0)
        registry['lastLearningCheckAt'] = now.isoformat()
        registry['cohortAudit'] = {'frozenSignalDays': days, 'completeQueue': complete,
            'completedSignalCount': len(samples), 'knownThrough': now.isoformat(),
            'cohortSource': 'immutable_official_scan_ledger_not_surviving_outcomes'}
        registry['readinessSnapshot'] = self._readiness_snapshot(now, data)
        attempt = None

        def finish(reason):
            registry['reason'] = reason
            if attempt is not None:
                attempt.update(finishedAt=now.isoformat(), reason=reason)
            self.save_registry(registry, now)
            return self.state(now, data)

        if len(days) < MIN_MATURE_DAYS or len({(r['day'], r['code']) for r in samples}) < MIN_VALID_SAMPLES:
            return finish('insufficient_independent_mature_days_or_samples')
        if not complete:
            return finish('incomplete_frozen_cohort_or_invalid_data_lineage')
        active = self.artifact(registry)
        if registry.get('championVersion') and not active:
            registry.update(championVersion=None, rollbackVersion=None)
            return finish('active_artifact_or_data_lineage_invalid')
        # Every promotion/rollback uses prospectively saved scores, never an
        # artifact fitted after those future outcomes were already available.
        for key, started, minimum in (('championVersion', 'activeAfterDay', SHADOW_DAYS),
                                      ('shadowVersion', 'shadowAfterDay', SHADOW_DAYS)):
            version = registry.get(key)
            if not version:
                continue
            artifact = load_artifact(self.service, version, active_only=key == 'championVersion')
            if not artifact:
                registry[key] = None
                return finish('artifact_or_data_lineage_invalid')
            future_days = [day for day in days if day > registry.get(started, '9999')]
            if key == 'championVersion':
                future_days = future_days[-SHADOW_DAYS:]
                if registry.get('lastEvaluatedThrough') == (future_days[-1] if future_days else None):
                    continue
            else:
                registry['shadowDays'] = len(future_days)
            if len(future_days) < minimum:
                if key == 'shadowVersion':
                    return finish('shadow_collecting_forward_mature_days')
                continue
            future = [row for row in samples if row['day'] in set(future_days)]
            forward = self.service._forward_scores(version, future)
            if forward is None:
                return finish('forward_predictions_missing')
            try:
                prediction = predict_model(artifact, future)
                if not np.allclose(prediction['rank'], forward, rtol=1e-10, atol=1e-10):
                    return finish('forward_prediction_replay_mismatch')
                gates = self.gate(future, prediction, artifact, future_days, complete, minimum)
            except (ValueError, ImportError, KeyError):
                return finish('forward_prediction_or_validation_failed')
            registry['lastValidationAt'] = now.isoformat()
            registry.setdefault('gates', {})['liveRolling' if key == 'championVersion' else 'shadow'] = gates
            if key == 'championVersion':
                registry['lastEvaluatedThrough'] = future_days[-1]
                if not gates['passed']:
                    rollback = load_artifact(self.service, registry.get('rollbackVersion'), active_only=True)
                    registry['championVersion'] = rollback['version'] if rollback else None
                    registry['rollbackVersion'] = None
                    with self.service._connect() as db:
                        db.execute("UPDATE model_versions SET stage='rolled_back' WHERE version=?", (version,))
                    return finish('active_gate_failed_automatic_rollback')
            else:
                # Not enough forward fills is not evidence for rejection; keep collecting.
                completed = gates.get('completedTrades', gates.get('selectedPriceObservations', 0))
                if completed < _VALIDATION['minimumHoldoutTrades']:
                    return finish('shadow_collecting_completed_observations')
                with self.service._connect() as db:
                    db.execute('UPDATE model_versions SET stage=? WHERE version=?',
                               ('active' if gates['passed'] else 'rejected', version))
                registry['shadowVersion'] = None
                if gates['passed']:
                    registry.update(rollbackVersion=registry.get('championVersion'), championVersion=version,
                                    activeAfterDay=now.date().isoformat(), activatedAt=now.isoformat())
                    return finish('shadow_passed_promoted')
                return finish('shadow_failed_kept_prior_champion_or_rules')
        holdout_size = max(MIN_HOLDOUT_DAYS, math.ceil(len(days)*_VALIDATION['holdoutFraction']))
        boundary = len(days)-holdout_size
        holdout_days = days[boundary:]
        consumed = set(registry.get('consumedHoldoutDays') or [])
        if consumed.intersection(holdout_days):
            return finish('sealed_holdout_already_observed_wait_for_new_holdout')
        attempt = {'at':now.isoformat(),'taskContractId':self.contract['taskContractId'],
                   'fitCount':0,'reason':'started',
                   'datasetHash':sha256(_json([(r['signalId'],r['net'],r.get('labelKnownAt'),r.get('dataQualityKnownAt'))
                                              for r in samples]).encode()).hexdigest()}
        registry.setdefault('validationHistory',[]).append(attempt)
        self.save_registry(registry,now)
        development_rows, development_predictions, development_artifacts, folds = [], [], [], []
        try:
            # Three chronologically later, non-overlapping test windows. Each
            # independently refits train and calibrator. Label purge still applies.
            development_end = boundary-PURGE_DAYS
            for fold in range(WALK_FORWARD_FOLDS):
                end = development_end-(WALK_FORWARD_FOLDS-1-fold)*WALK_FORWARD_DAYS
                start = end-WALK_FORWARD_DAYS
                if start <= 0:
                    return finish('rolling_development_history_insufficient')
                train, calibration = partition(samples, days, start,
                    calibration_days=max(20, _VALIDATION['minimumCalibrationDays']), purge_days=PURGE_DAYS)
                if len(train) < _VALIDATION['minimumTrainSamples'] or len({r['day'] for r in calibration}) < _VALIDATION['minimumCalibrationDays']:
                    return finish('rolling_train_calibration_split_too_small')
                test = [r for r in samples if r['day'] in set(days[start:end])]
                if not test or any(r.get('labelEndAt', '9999') >= days[boundary]
                                   or r.get('labelKnownAt', '9999') >= days[boundary]
                                   or r.get('dataQualityKnownAt', '9999') >= days[boundary] for r in test):
                    return finish('rolling_test_labels_incomplete')
                model = fit_model(train, calibration, self.task_id, self.queue)
                attempt['fitCount'] += 1
                registry['lastFitCompletedAt'] = now.isoformat()
                predicted = predict_model(model, test)
                development_rows.extend(test)
                development_predictions.append(predicted)
                development_artifacts.append(model)
                folds.append({'trainThrough': model['trainedThrough'], 'calibrationThrough': model['calibrationThrough'],
                              'testDays': days[start:end], 'testSamples': len(test),
                              'metrics': diagnostics(test, predicted, model)})
            # Per-fold calibration skill is required, preventing an aggregate
            # good regime from hiding a probability head that failed a fold.
            if any((fold['metrics']['brierSkill'] or -1) <= 0 for fold in folds):
                registry['developmentFolds'] = folds
                registry['lastFitCompletedAt'] = now.isoformat()
                registry['lastValidationAt'] = now.isoformat()
                attempt['developmentFolds'] = folds
                return finish('rolling_probability_calibration_failed')
            aggregate = {name: np.concatenate([p[name] for p in development_predictions]) for name in development_predictions[0]}
            # Empty official scans are cash days, not missing days to erase.
            dev_days = sorted({day for fold in folds for day in fold['testDays']})
            dev_gate = self.gate(development_rows, aggregate, development_artifacts[-1], dev_days, complete, WALK_FORWARD_DAYS*WALK_FORWARD_FOLDS)
            registry['developmentFolds'] = folds
            registry['gates']['development'] = dev_gate
            registry['lastFitCompletedAt'] = now.isoformat()
            registry['lastValidationAt'] = now.isoformat()
            if not dev_gate['passed']:
                return finish('rolling_development_gate_failed')
            train, calibration = partition(samples, days, boundary,
                calibration_days=max(20, _VALIDATION['minimumCalibrationDays']), purge_days=PURGE_DAYS)
            holdout = [r for r in samples if r['day'] in set(holdout_days)]
            artifact = fit_model(train, calibration, self.task_id, self.queue)
            attempt['fitCount'] += 1
            registry['lastFitCompletedAt'] = now.isoformat()
            # Mark consumption before evaluation so a rejected model cannot
            # repeatedly peek at the same holdout after parameter edits.
            registry['consumedHoldoutDays'] = sorted(consumed | set(holdout_days))
            self.save_registry(registry, now)
            prediction = predict_model(artifact, holdout)
            gates = self.gate(holdout, prediction, artifact, holdout_days, complete)
        except (ValueError, ImportError, KeyError) as exc:
            return finish('task_training_unavailable:' + str(exc))
        registry['lastValidationAt'] = now.isoformat()
        registry['gates']['holdout'] = gates
        registry['splitAudit'] = {'trainDays': sorted({r['day'] for r in train}),
            'calibrationDays': sorted({r['day'] for r in calibration}), 'holdoutDays': holdout_days,
            'holdoutSignalIds': [r['signalId'] for r in holdout], 'minimumGapSessions': PURGE_DAYS,
            'purgePolicy': 'actual_label_end_before_next_split', 'developmentFolds': folds}
        attempt['gates'] = gates
        attempt['developmentFolds'] = folds
        if not gates['passed']:
            return finish('challenger_holdout_gate_failed')
        try:
            lineage = combine_lineages([r[key] for r in samples for key in ('dataLineage', 'labelDataLineage')])
        except (KeyError, ValueError):
            return finish('inconsistent_training_data_lineage')
        artifact.update(version=f'{TASK_MODEL_VERSION}-{self.queue or "entry"}-{now.strftime("%Y%m%d%H%M%S%f")}',
            algorithmContractId=algorithm_id, holdoutThrough=holdout_days[-1],
            executionModel='morning-price-observation-T0' if self.task_id == MORNING_TASK_ID else 'task-specific-T1', dataLineage=lineage)
        with self.service._connect() as db:
            db.execute('INSERT INTO model_versions VALUES(?,?,?,?,?)',
                       (artifact['version'], 'shadow', _json(artifact), _json(gates), now.isoformat()))
        registry.update(shadowVersion=artifact['version'], shadowAfterDay=now.date().isoformat(),
                        shadowStartedAt=now.isoformat(), shadowDays=0)
        return finish('holdout_passed_shadow_started')
