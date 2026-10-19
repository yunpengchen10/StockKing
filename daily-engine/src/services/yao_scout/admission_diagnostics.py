"""Explain the recorded selection gates without changing admission decisions."""
from collections import Counter

from .local_algorithm import is_entry_eligible, is_research_eligible
from .research_policy import DEFAULT_PARAMETERS
from .precision_policy import execution_quote_data


def build_admission_diagnostics(candidates, selected, quality, *, morning=False):
    rows = list({row['code']: row for row in candidates if row.get('code')}.values())
    total = len(rows)
    formal = DEFAULT_PARAMETERS['history']['formalDays']
    missing = Counter()
    adverse = Counter()
    for row in rows:
        admission = row.get('primaryAdmission') or {}
        missing.update(set(admission.get('missingReasons') or []))
        adverse.update(set(admission.get('reasons') or []))

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
        passed = sum(check(row) for row in rows)
        checks.append({'key': key, 'label': label, 'passed': passed,
                       'blocked': total - passed, 'total': total, 'action': action})
    research = [row for row in rows if is_research_eligible(row)]
    admitted = [row for row in research if is_entry_eligible(row)]
    model_blocked = sum((row.get('morningSignal') or {}).get('selectionEligible') is not True
                        for row in research) if morning else 0
    model_unavailable = sum((row.get('morningSignal') or {}).get('learningStatus') != 'active'
                            for row in research) if morning else 0
    blocked_checks = [check for check in checks if check['blocked']]
    if selected:
        state = 'selected'
    elif blocked_checks or missing:
        state = 'data_incomplete'
    elif research and model_unavailable:
        state = 'model_not_ready'
    elif research and not admitted:
        state = 'model_risk_rejected'
    else:
        state = 'conditions_not_met'
    fragments = [f"{check['label']}未通过{check['blocked']}/{total}只"
                 for check in sorted(blocked_checks, key=lambda item: -item['blocked'])[:3]]
    if morning and not admitted:
        fragments.append('上午模型状态见独立验证进度' if model_unavailable or not research
                         else '已验证模型的收益或风险条件未满足')
    summary = f'本轮主推荐{len(selected)}只。'
    if fragments:
        summary += '主要阻断：' + '；'.join(fragments) + '。'
    elif not selected:
        summary += '数据检查已完成，当前方向、价格或风险条件未满足。'
    return {
        'version': 'primary-diagnostics-v1', 'status': state, 'total': total,
        'checks': checks, 'evidenceAndRiskPassed': len(research),
        'modelAdmissionPassed': len(admitted), 'morningModelBlocked': model_blocked,
        'morningModelUnavailable': model_unavailable,
        'selected': len(selected), 'missingReasonCounts': dict(missing),
        'riskReasonCounts': dict(adverse), 'summary': summary,
        'countMeaning': '各项检查独立统计，同一股票可以有多个阻断原因，数量不可相加。',
        'collection': {key: quality.get(key, {}) for key in
                       ('stageIncompleteCounts', 'stageMissingReasons', 'sectorCollection')},
    }
