"""Explain the recorded selection gates without changing admission decisions."""
from collections import Counter

from .local_algorithm import is_entry_eligible, is_research_eligible
from .research_policy import DEFAULT_PARAMETERS
from .precision_policy import execution_quote_data
from .primary_readiness import admission_stage
from .entry_execution_policy import model_entry_allowed


def build_admission_diagnostics(candidates, selected, quality, *, morning=False):
    rows = list({row['code']: row for row in candidates if row.get('code')}.values())
    total = len(rows)
    formal = DEFAULT_PARAMETERS['history']['formalDays']
    missing = Counter()
    adverse = Counter()
    warnings = Counter()
    for row in rows:
        admission = row.get('primaryAdmission') or {}
        missing.update(set(admission.get('missingReasons') or []))
        adverse.update(set(admission.get('reasons') or []))
        warnings.update(set(admission.get('warnings') or []))

    def warming_up(row):
        return (row.get('baselineEvidence') or {}).get('baselineStatus') == 'session_warmup'

    def current_data_unavailable(row):
        return (row.get('baselineEvidence') or {}).get('baselineStatus') in (
            'current_minutes_unavailable', 'current_minutes_stale', 'current_window_unavailable')

    def history_pending(row):
        return warming_up(row) or current_data_unavailable(row)

    def current_window_ready(row):
        baseline = row.get('baselineEvidence') or {}
        if baseline.get('baselineStatus') in ('session_warmup', 'current_minutes_unavailable',
                'current_minutes_stale', 'current_window_unavailable'):
            return False
        window = baseline.get('sessionWindow') or {}
        return window.get('price5mWindowElapsed') is not False

    def sufficient_history(row):
        eligibility = row.get('dataEligibility') or {}
        amount = row.get('baselineHistoryDays', eligibility.get('amountHistoryDays'))
        price = row.get('priceHistoricalCoverageDays', eligibility.get('priceHistoryDays'))
        return (isinstance(amount, (int, float)) and amount >= formal
                and isinstance(price, (int, float)) and price >= formal)

    def source_passed(row, reason):
        admission = row.get('primaryAdmission') or {}
        return bool(admission) and reason not in (admission.get('missingReasons') or [])

    checks = []
    definitions = [
        ('current_window', '开盘后完整分钟窗口', current_window_ready,
         '等待本交易时段形成完整窗口；不跨午休拼接分钟。'),
        ('history', f'{formal}日同刻量价基准', sufficient_history,
         '后台补齐缺失交易日并核验历史分钟数据；重复刷新只更新当前分钟。'),
        ('lineage', '分钟来源与历史核验',
         lambda row: (row.get('dataLineage') or {}).get('status') == 'verified',
         '核验各依赖交易日的证券身份和独立价格证据；冲突数据继续隔离。'),
        ('minute', '分钟窗口时效',
         lambda row: source_passed(row, 'minute_window_missing_future_or_stale'),
         '采集同一截止窗口的完整分钟，保留已经按时完成的股票。'),
        ('sector', '同窗板块数据',
         lambda row: source_passed(row, 'S_source_missing_stale_or_misaligned'),
         '按行业共享采集并保留部分结果；覆盖不足的行业继续补采。'),
        ('fund', '同窗资金数据',
         lambda row: source_passed(row, 'F_source_missing_stale_or_misaligned'),
         '补齐与个股分钟一致的资金窗口。'),
        ('active_flow', '主动资金分类',
         lambda row: source_passed(row, 'active_flow_classification_missing_or_stale'),
         '刷新带实际来源时间的主动买卖分类数据。'),
        ('execution_quote', '交易报价字段',
         lambda row: execution_quote_data(row)['status'] == 'complete',
         '补齐并核验前收盘、涨跌停价格及特殊交易状态。'),
        ('breadth', '全市场广度数据',
         lambda row: source_passed(row, 'whole_market_breadth_unavailable_or_stale'),
         '刷新具有实际来源时间和完整覆盖的市场快照。'),
    ]
    for key, label, check, action in definitions:
        # A not-yet-formed current window cannot measure historical coverage.
        # Show it as pending, never as 20 missing archive sessions.
        pending = sum(history_pending(row) for row in rows) if key == 'history' else 0
        passed = sum(check(row) for row in rows if key != 'history' or not history_pending(row))
        checks.append({'key': key, 'label': label, 'passed': passed,
                       'blocked': total - passed - pending, 'pending': pending,
                       'pendingReason': ('当前分钟尚未取得或窗口尚未形成，历史基准待核验，不计为历史缺失'
                                         if key == 'history' and pending else None),
                       'total': total, 'action': action})
    research = [row for row in rows if is_research_eligible(row)]
    admitted = [row for row in research if is_entry_eligible(row)]
    stages = Counter(admission_stage(row) for row in rows)
    model_unavailable = stages['model_not_ready']
    model_rejected = stages['model_rejected']
    entry_blocked = stages['entry_plan_blocked']
    model_passed = sum(model_entry_allowed(row) for row in research)
    # Morning fields are retained as independent direction-study diagnostics;
    # they have no authority over the executable T+1 primary admission.
    morning_blocked = sum((row.get('morningSignal') or {}).get('selectionEligible') is not True
                          for row in research) if morning else 0
    morning_unavailable = sum((row.get('morningSignal') or {}).get('learningStatus') != 'active'
                              for row in research) if morning else 0
    blocked_checks = [check for check in checks if check['blocked']]
    if selected:
        state = 'selected'
    elif total and all(warming_up(row) for row in rows):
        state = 'session_warmup'
    elif blocked_checks or missing:
        state = 'data_incomplete'
    elif research and model_unavailable:
        state = 'model_not_ready'
    elif model_rejected:
        state = 'model_risk_rejected'
    elif entry_blocked:
        state = 'entry_plan_blocked'
    else:
        state = 'conditions_not_met'
    fragments = [f"{check['label']}未通过{check['blocked']}/{total}只"
                 for check in sorted(blocked_checks, key=lambda item: -item['blocked'])[:3]]
    if model_unavailable:
        fragments.append(f'{model_unavailable}只证据已通过，T+1净盈利模型尚未就绪，保留研究对照')
    if model_rejected:
        fragments.append(f'{model_rejected}只被已验证T+1模型的收益或风险条件拒绝')
    if entry_blocked:
        fragments.append(f'{entry_blocked}只模型已通过，但冻结买点失效、超时或价格不在区间内')
    summary = f'本轮主推荐{len(selected)}只。'
    warmup_count = sum(warming_up(row) for row in rows)
    if warmup_count:
        summary += f'{warmup_count}只正在等待开盘后的完整分钟窗口，不能据此判定历史缺失。'
    current_missing_count = sum(current_data_unavailable(row) for row in rows)
    if current_missing_count:
        summary += f'{current_missing_count}只当前分钟未采集齐，历史基准待核验，不计为历史缺失。'
    if fragments:
        summary += '主要阻断：' + '；'.join(fragments) + '。'
    elif not selected:
        summary += '数据检查已完成，当前方向、价格或风险条件未满足。'
    return {
        'version': 'primary-diagnostics-v3', 'status': state, 'total': total,
        'sessionWarmupCount': warmup_count,
        'currentMinuteUnavailableCount': current_missing_count,
        'sessionWindows': {row['code']: (row.get('baselineEvidence') or {}).get('sessionWindow')
                           for row in rows if warming_up(row)},
        'checks': checks, 'evidenceAndRiskPassed': len(research),
        'modelAdmissionPassed': model_passed, 'entryAdmissionPassed': len(admitted),
        'modelNotReady': model_unavailable, 'modelRejected': model_rejected,
        'entryPlanBlocked': entry_blocked, 'stageCounts': dict(stages),
        'morningModelBlocked': morning_blocked, 'morningModelUnavailable': morning_unavailable,
        'morningModelRole': 'independent_direction_research_only',
        'selected': len(selected), 'missingReasonCounts': dict(missing),
        'riskReasonCounts': dict(adverse), 'riskWarningCounts': dict(warnings), 'summary': summary,
        'countMeaning': '各项检查独立统计，同一股票可以有多个阻断原因，数量不可相加。',
        'collection': {key: quality.get(key, {}) for key in
                       ('stageIncompleteCounts', 'stageMissingReasons', 'sectorCollection', 'fundCollection')},
    }
