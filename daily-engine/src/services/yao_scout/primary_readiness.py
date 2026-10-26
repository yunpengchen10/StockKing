"""Small, read-only primary-task diagnostics; never a model or selection gate."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .entry_execution_policy import VERSION as ENTRY_VERSION, model_entry_allowed
from .local_algorithm import is_entry_eligible, is_research_eligible


ACTIVE_STATUSES = {'active', 'model_active', 'validated_model'}
TASK_ID = 'entry_today_exit_next_session'


def admission_stage(row):
    """Classify the first failing gate without changing its decision."""
    if not is_research_eligible(row):
        return 'evidence_blocked'
    prediction = row.get('prediction') or {}
    if prediction.get('status') not in ACTIVE_STATUSES or prediction.get('taskId') != TASK_ID:
        return 'model_not_ready'
    if not model_entry_allowed(row):
        return 'model_rejected'
    if not is_entry_eligible(row):
        return 'entry_plan_blocked'
    return 'eligible'


def cold_start_research(candidates, *, slot='live', official=False, ledger=None):
    """Describe model-independent controls; publication alone is not maturity."""
    from .signal_learning import ENTRY_SLOTS
    rows = list({row['code']: row for row in candidates if row.get('code') and not row.get('dailySignal')}.values())
    research = [row for row in rows if is_research_eligible(row)]
    stages = Counter(admission_stage(row) for row in rows)
    plan_ready = lambda row: (row.get('entryExecution') or {}).get('version') == ENTRY_VERSION and (row.get('entryExecution') or {}).get('priceEligible') is True
    cold = [row for row in research if admission_stage(row) == 'model_not_ready' and plan_ready(row)]
    allowed = bool(official and slot in ENTRY_SLOTS)
    saved_count = ledger.get('trainingEligibleCount') if isinstance(ledger, dict) else None
    items = []
    for row in cold[:20]:
        plan = row['entryExecution']
        frozen = row.get('learningSampleEligibility') or {}
        items.append({'code': row['code'], 'name': row.get('name'),
            'status': 'research_only', 'formalRecommendation': False, 'selectionAuthority': False,
            'modelStatus': (row.get('prediction') or {}).get('status', 'model_missing'),
            'referencePrice': row.get('referencePrice') or row.get('signalPrice'),
            'minimumPrice': plan.get('minimumPrice'), 'maximumPrice': plan.get('maximumPrice'),
            'invalidationPrice': plan.get('invalidationPrice'), 'expiresAt': plan.get('expiresAt'),
            'trainingEligible': frozen.get('eligible'), 'trainingBlockerCodes': frozen.get('blockerCodes'),
            'meaning': '证据和研究买点已通过，模型尚未就绪；仅冻结研究对照，不构成正式推荐'})
    return {'version': 'cold-start-research-v1', 'taskId': TASK_ID,
        'status': 'research_controls_available' if cold else 'research_conditions_not_met',
        'evidenceEligibleCount': len(research), 'entryPlanReadyCount': sum(plan_ready(row) for row in research),
        'modelNotReadyCount': stages['model_not_ready'], 'modelRejectedCount': stages['model_rejected'],
        'entryPlanBlockedCount': stages['entry_plan_blocked'], 'coldStartControlCount': len(cold),
        'trainingEligibleCount': saved_count, 'trainingControlCount': (ledger or {}).get('trainingControlCount'),
        'recordingMode': 'official_frozen' if allowed and saved_count is not None else
            'official_pending_publication' if allowed else 'manual_audit_only',
        'items': items, 'itemsTruncated': len(cold) > len(items),
        'selectionAuthority': False, 'formalRecommendation': False,
        'meaning': '合格对照无需先被模型选中；只有正式时段、发布时证据与数据血缘合格的记录才获训练资格。手动刷新只作审计；待T+1标签核验后才计入成熟样本。'}


def read_primary_readiness(data_dir, now, *, candidates=(), slot='live', official=False, ledger=None, db_path=None):
    """Read one registry row by primary key, with no service construction.

    Old or missing cohort counts remain unknown. A live loaded prediction is
    the only evidence here that a configured model passed current load checks.
    No event table, minute archive, training or readiness refresh is touched.
    """
    from .signal_learning import _at
    from .task_learning import TaskLearner, training_split_readiness
    now = _at(now)
    path = Path(db_path) if db_path is not None else Path(data_dir) / 'signal-learning-v1.sqlite3'
    registry, read_error = {}, None
    if path.exists():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
                row = db.execute("SELECT payload_json FROM learning_registry WHERE registry_key='v11'").fetchone()
                registry = json.loads(row[0]) if row else {}
                if not isinstance(registry, dict):
                    raise ValueError('registry_not_mapping')
        except (sqlite3.Error, ValueError, TypeError):
            registry, read_error = {}, 'primary_registry_unavailable'
    else:
        read_error = 'primary_registry_missing'
    rows = list(candidates)
    flags = [is_research_eligible(row) for row in rows if not row.get('dailySignal')]
    predictions = [row.get('prediction') or {} for row in rows if not row.get('dailySignal')]
    loaded = any(p.get('taskId') == TASK_ID and p.get('status') in ACTIVE_STATUSES for p in predictions)
    checked = loaded or any(p.get('taskId') == TASK_ID for p in predictions)
    # TaskLearner construction is inert; this pure report does not access service.
    learner = TaskLearner(None)
    report = learner._readiness_report(now, registry, current_data_eligibility=flags,
        verified_active=loaded if checked else None)
    current = report.get('currentScan') or {}
    current['meaning'] = '本轮证据与风控资格；发布时血缘、时间及正式时段仍须核验，不等于已入池或已成熟样本'
    report['currentScan'] = current
    minimum = training_split_readiness([], [], TASK_ID)['minimumSignalDaysForAnyTraining']
    report.update(version='primary-readiness-v1', taskId=TASK_ID,
        modelUsableNow=loaded if checked else None,
        minimumSignalDaysForAnyTraining=minimum,
        requiredStages=['signal_time_qualified_official_controls', 'mature_T1_labels',
            'chronological_train_calibration_development', 'sealed_holdout_validation', 'forward_shadow_validation'],
        recommendationMeaning='正式主推荐必须使用已验证T+1净盈利模型；模型未就绪时只保留合格研究对照')
    if loaded:
        report['blockerCodes'] = [code for code in report['blockerCodes'] if code not in
                                  {'validated_model_missing', 'configured_model_not_usable'}]
        if any(flags):
            report.update(status='active', message='本轮已有通过加载核验的T+1模型；正式入场仍须逐只通过收益、风险和冻结买点条件。')
    if read_error:
        report['blockerCodes'] = list(dict.fromkeys([*report['blockerCodes'], read_error]))
    return {'primaryReadiness': report,
            'coldStartResearch': cold_start_research(rows, slot=slot, official=official, ledger=ledger)}
