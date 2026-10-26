"""Auditable learning compatibility without rewriting frozen publications.

Only the exclusions below are compatible. Unknown fields and any change to
features, eligibility, execution or labels fail closed. A legacy hash alone is
never treated as evidence that an old publication used compatible rules.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json


VERSION = 'learning-compatibility-v1'
# These fields describe other lanes, presentation, or model release policy.
# The actual numeric features and prospective admission/portfolio policies stay
# in the projection. Model/label compatibility is checked separately per task.
IGNORED_ALGORITHM_FIELDS = frozenset({
    'contractId', 'dailyPolicy', 'dailyFallbackPolicy', 'learningTaskContracts',
    'primaryTask', 'entryConditions', 'samplePolicy', 'rankingPolicy',
    'rankingTieBreakPolicy',
})
LABEL_FIELDS = (
    'taskId', 'queue', 'targetHorizon', 'labelVersion', 'labelSizing',
    'returnMeaning', 'costPolicy', 'unknownFillPolicy', 'referencePolicy',
    'riskTarget', 'riskPathPolicy', 'minuteProxyPolicy', 'largeDropThreshold',
    'sameDayExitAllowed', 'dedupePolicy',
)


def _hash(prefix, value):
    return prefix + sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()[:20]


def verified_algorithm_manifest(value, expected_id=None):
    if not isinstance(value, dict) or not value.get('contractId'):
        return None
    payload = {key: item for key, item in value.items() if key != 'contractId'}
    actual = _hash('local-picks-', payload)
    if actual != value['contractId'] or (expected_id and actual != expected_id):
        return None
    return value


def algorithm_learning_projection(algorithm, task_id='entry_today_exit_next_session'):
    payload = {key: deepcopy(value) for key, value in algorithm.items()
               if key not in IGNORED_ALGORITHM_FIELDS}
    primary = payload.get('primaryAdmissionPolicy')
    if isinstance(primary, dict):
        # Historical metadata listed the morning model as a primary condition,
        # although research admission itself was always evidence-only. Model
        # release gates cannot erase rejected but prospectively valid controls.
        primary.pop('modelAdmission', None)
        primary.pop('meaning', None)
        if isinstance(primary.get('requires'), list):
            primary['requires'] = [item for item in primary['requires'] if item != 'morning_validated_model']
    if task_id != 'entry_today_exit_next_session':
        # Price observations have no order band, portfolio sizing or fill label.
        # Their frozen data/eligibility and own label contract remain required.
        for key in ('entryExecutionPolicy', 'portfolioPolicyVersion', 'portfolioParameters'):
            payload.pop(key, None)
    parameters = payload.get('parameters')
    if isinstance(parameters, dict):
        parameters.pop('candidateGrids', None)  # Unfitted research search space.
        parameters.pop('validationStatus', None)
        defaults = parameters.get('defaults') or {}
        validation = defaults.get('validation')
        if isinstance(validation, dict):
            # These two affect executable labels; split/release sizes do not.
            release_only = {'minimumMatureDays', 'minimumValidSamples', 'purgeDays',
                'shadowDays', 'minimumHoldoutDays', 'minimumHoldoutTrades',
                'minimumShadowTrades', 'splitPolicy', 'holdoutFraction',
                'preHoldoutTrainFraction', 'minimumCalibrationDays',
                'minimumTrainSamples', 'purgePolicy'}
            defaults['validation'] = {key: value for key, value in validation.items()
                                      if key not in release_only}
            if task_id != 'entry_today_exit_next_session':
                defaults['validation'].pop('executionModel', None)
                defaults['validation'].pop('exitPolicy', None)
    return {'version': VERSION, 'taskScope': task_id, 'algorithm': payload}


def learning_algorithm_contract(algorithm=None, task_id='entry_today_exit_next_session'):
    if algorithm is None:
        from .local_algorithm import algorithm_contract
        algorithm = algorithm_contract()
    payload = algorithm_learning_projection(algorithm, task_id)
    return {**payload, 'contractId': _hash('learning-algorithm-', payload)}


def compatibility(source, current=None, task_id=None):
    """Accept an exact current hash or a verified, equivalent legacy manifest.

    ``source`` may be a registry/artifact, a run envelope, or an event augmented
    with its run's manifest. This function does not grant training eligibility.
    The caller must still require the original immutable eligibility flag.
    """
    from .local_algorithm import algorithm_contract
    current = current or algorithm_contract()
    task_id = task_id or source.get('taskId') or 'entry_today_exit_next_session'
    source_id = source.get('algorithmContractId')
    current_learning = learning_algorithm_contract(current, task_id)['contractId']
    result = {'version': VERSION, 'compatible': False, 'sourceAlgorithmContractId': source_id,
              'currentAlgorithmContractId': current['contractId'],
              'learningAlgorithmContractId': current_learning, 'taskId': task_id,
              'reason': 'unverified_legacy_contract', 'excludedFields': sorted(IGNORED_ALGORITHM_FIELDS - {'contractId'})}
    frozen_learning = (source.get('learningAlgorithmContractId') if source.get('taskId') == task_id
                       or task_id == 'entry_today_exit_next_session' else None)
    if frozen_learning and frozen_learning != current_learning:
        return {**result, 'reason': 'learning_contract_changed'}
    if source_id == current['contractId']:
        return {**result, 'compatible': True, 'reason': 'exact_algorithm_contract'}
    manifest = source.get('algorithmManifest') or source.get('algorithm') or (source.get('result') or {}).get('algorithm')
    manifest = verified_algorithm_manifest(manifest, source_id) if source_id else None
    if manifest is None:
        return result
    if algorithm_learning_projection(manifest, task_id) != algorithm_learning_projection(current, task_id):
        return {**result, 'reason': 'learning_policy_or_feature_changed'}
    return {**result, 'compatible': True, 'reason': 'verified_manifest_equivalent_learning_policy'}


def learning_contracts(task, algorithm=None):
    """Independent data/feature/label/model fingerprints for new artifacts."""
    from .local_algorithm import algorithm_contract
    algorithm = algorithm or algorithm_contract()
    learned = learning_algorithm_contract(algorithm, task['taskId'])
    label = {key: task[key] for key in LABEL_FIELDS if key in task}
    data = {'qualityVersion': task['dataQualityVersion'],
            'qualityPolicy': algorithm.get('dataQualityPolicy'),
            'eligibility': task['signalQualityEligibility']}
    features = {'schema': task['featureSchemaVersion'],
                'algorithmLearningContractId': learned['contractId']}
    ids = {'dataContractId': _hash('data-', data), 'featureContractId': _hash('features-', features),
           'labelContractId': _hash('labels-', label), 'modelContractId': task['taskContractId'],
           'learningAlgorithmContractId': learned['contractId']}
    return {**ids, 'taskId': task['taskId'], 'learningDataContractId': _hash('learning-data-', {key: ids[key] for key in
        ('dataContractId', 'featureContractId', 'labelContractId')}),
        'compatibilityPolicy': VERSION, 'algorithmManifest': algorithm}


def frozen_learning_fields(task, algorithm=None):
    """Small per-event fields; the verified full manifest is stored once/run."""
    return {key: value for key, value in learning_contracts(task, algorithm).items()
            if key not in {'algorithmManifest', 'taskId'}}


def task_data_compatible(signal, task, algorithm=None):
    expected = learning_contracts(task, algorithm)
    if signal.get('learningDataContractId'):
        return signal['learningDataContractId'] == expected['learningDataContractId']
    # Legacy publications did not persist independent label manifests. Only an
    # exact known task hash can establish their label identity, never a guess.
    return signal.get('learningTaskContractId') == task['taskContractId']


def run_task_data_compatible(source, task, algorithm=None):
    """Count empty signal days only when their task labels are compatible too."""
    from .local_algorithm import algorithm_contract
    algorithm = algorithm or algorithm_contract()
    if source.get('learningDataContractId'):
        return task_data_compatible(source, task, algorithm)
    manifest = source.get('algorithmManifest') or (source.get('result') or {}).get('algorithm')
    manifest = verified_algorithm_manifest(manifest, source.get('algorithmContractId'))
    if manifest:
        return task['taskContractId'] in (manifest.get('learningTaskContracts') or [])
    return source.get('algorithmContractId') == algorithm['contractId']
