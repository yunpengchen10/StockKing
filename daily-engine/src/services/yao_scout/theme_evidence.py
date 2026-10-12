"""Point-in-time theme breadth from an explicitly supplied snapshot mapping.

The first provider-listed theme is frozen before looking at its returns. The
result is a shadow feature, not a license to select the best-performing theme
after the fact. A primary industry is not silently relabelled as a hot theme.
"""
from __future__ import annotations

from datetime import datetime
import math
import re


def _time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _number(value):
    try:
        output = float(value)
        return output if math.isfinite(output) else None
    except (TypeError, ValueError):
        return None


def _mapping(value, *, complete):
    """An explicit empty provider array may mean no theme; null never does."""
    if isinstance(value, (list, tuple)):
        if not value:
            return [], complete
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return [], False
        values = value
    elif isinstance(value, str):
        values = [item for item in re.split(r'[,;|、，；]', value) if item.strip()]
    else:
        return [], False
    themes = list(dict.fromkeys(item.strip() for item in values))
    return themes, bool(themes)


def theme_factor(snapshot, code, cutoff, *, received_at):
    from .research_policy import DEFAULT_PARAMETERS
    # Keep the audit thresholds in the algorithm contract even while the input
    # is shadow-only. Defaults support older callers during a rolling update.
    policy = DEFAULT_PARAMETERS['researchFactors']['H']
    minimum_peers = int(policy['minPeers'])
    minimum_coverage = float(policy['minimumCoverage']) * 100
    confirm_breadth = float(policy['minimumBreadthPct'])
    now, received = _time(cutoff), _time(received_at)
    result = {'status': 'unavailable', 'score': None, 'asOf': None,
              'source': None, 'features': {}, 'gaps': [], 'mode': 'shadow',
              'mappingPolicy': 'first_provider_theme_before_returns',
              'thresholds': {'minimumPeers': minimum_peers, 'coveragePct': minimum_coverage,
                             'mappingCoveragePct': minimum_coverage,
                             'confirmationBreadthPct': confirm_breadth}}
    if now is None or received is None or received > now:
        result['gaps'].append('题材映射可知时间无效或晚于决策')
        return result
    if snapshot is None or snapshot.empty or not {'code', 'concepts', 'change_pct'}.issubset(snapshot.columns):
        result['gaps'].append('未提供事前题材成分；行业不能代替热点题材')
        return result
    source = snapshot.attrs.get('snapshot_source') or snapshot.attrs.get('source')
    if not source or snapshot.attrs.get('stale') or snapshot.attrs.get('complete') is False:
        result['gaps'].append('题材快照来源缺失或陈旧')
        return result
    rows = snapshot.drop_duplicates('code').to_dict('records')
    complete_mapping = snapshot.attrs.get('theme_mapping_complete') is True
    def row_time(row):
        raw = row.get('source_time')
        return _time(raw if raw is not None else snapshot.attrs.get('source_time'))
    def known_at_decision(row):
        stamp = row_time(row)
        return bool(stamp and 0 <= (now - stamp).total_seconds() <= 300 and stamp <= received)
    target = next((row for row in rows if str(row['code']) == str(code)), None)
    if not known_at_decision(target or {}):
        result['gaps'].append('候选股票的题材映射源时间缺失、超前或过期')
        return result
    mappings = [(row, *_mapping(row.get('concepts'), complete=complete_mapping)) for row in rows]
    known_count = sum(known and known_at_decision(row) for row, _, known in mappings)
    mapping_coverage = 100 * known_count / len(rows) if rows else 0
    result.update(source=source, observedAt=received.isoformat(),
                  mappingCompleteDeclared=complete_mapping)
    result['features'].update(mapping_universe_count=len(rows), mapping_known_count=known_count,
                              mapping_unknown_count=len(rows)-known_count,
                              mapping_coverage_pct=mapping_coverage)
    if mapping_coverage < minimum_coverage:
        result['status'] = 'insufficient'
        result['gaps'].append('全池题材映射覆盖不足；未知归属不能当作已排除的非成员')
        return result
    themes, target_known = _mapping((target or {}).get('concepts'), complete=complete_mapping)
    if not themes:
        result['gaps'].append('来源明确该股票无题材归属' if target_known else '当前股票题材归属未知')
        return result
    theme = themes[0]
    members = [row for row, mapped, known in mappings
               if known and str(row['code']) != str(code) and theme in mapped]
    times, returns = [], []
    for row in members:
        stamp = row_time(row)
        value = _number(row.get('change_pct'))
        if stamp and 0 <= (now - stamp).total_seconds() <= 300 and stamp <= received and value is not None:
            times.append(stamp)
            returns.append(value)
    coverage = len(returns) / len(members) * 100 if members else 0
    result.update(source=source, theme=theme, observedAt=received.isoformat(),
                  members=[str(row['code']) for row in members])
    result['features'].update(peer_count=len(returns), peer_coverage_pct=coverage)
    if len(returns) < minimum_peers or coverage < minimum_coverage:
        result['status'] = 'insufficient'
        result['gaps'].append('同题材有效成分或真实源时间覆盖不足')
        return result
    breadth = sum(value > 0 for value in returns) / len(returns) * 100
    average = sum(returns) / len(returns)
    result.update(status='observed', score=round(breadth, 4), asOf=min(times).isoformat(),
                  confirmed=breadth >= confirm_breadth and average > 0)
    result['features'].update(breadth_pct=breadth, mean_day_return_pct=average)
    return result
