"""Theme-first selection over the independently checked flow/trend evidence.

Theme priority is frozen before looking at candidate outcomes. The flow engine
remains a reusable, versioned assessment; only this entry point publishes the
current opportunity list. Missing themes never fall back to industry labels.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import math

from .opportunity_channels import build_opportunity_channels as build_flow_channels
from .flow_opportunity_policy import VERSION as FLOW_VERSION, PARAMETERS, rank_qualified, policy_contract as flow_contract
from .theme_rotation import theme_for_code

VERSION = 'opportunity-theme-20261113-v1'


def _stamp(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value if value.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def policy_contract():
    contract = flow_contract()
    contract.update(version=VERSION, underlyingFlowVersion=FLOW_VERSION,
        channels={'F': '热门题材中的核心股票，通过资金、持续走势及买入位置核验'},
        ranking='theme_rank_then_member_leadership_then_flow_quality_then_code',
        themePolicy={'maximumThemes': 3, 'maximumPerTheme': 2,
            'maximumAgeSeconds': 300, 'minimumCoreMembers': 3, 'coreFraction': .3,
            'relativeStrengthRequired': True, 'industryFallback': False},
        feedbackPolicy='prior_completed_daily_observations_only; bounded_theme_priority_penalty; no_model_weight_updates',
        meaning='先确定热门题材，再筛选题材核心股，最后核验资金、走势及买入位置；未证明未来收益改善。')
    contract['required'] = ['fresh_selected_theme_with_verified_membership', 'core_member_not_lagging_theme', *contract['required']]
    return contract


def _valid_board(board, now):
    observed, source = _stamp(board.get('asOf')), _stamp(board.get('sourceTime'))
    return bool(board.get('status') in {'observed', 'partial'} and board.get('source')
        and observed and source and source <= observed <= now
        and source.astimezone(now.tzinfo).date() == now.date()
        and 0 <= (now-source).total_seconds() <= 300)


def build_theme_opportunities(candidates, now, *, theme_board=None, **kwargs):
    now = _stamp(now)
    if now is None:
        raise ValueError('theme decision time requires an explicit timezone')
    board = theme_board or {}
    # Only the current theme contract may carry its first-detection evidence
    # forward. The underlying engine checks all other timestamps itself.
    previous = deepcopy(kwargs.pop('previous', None) or {})
    visited = set()
    def convert_prior(value):
        if isinstance(value, (dict, list)):
            if id(value) in visited:
                return
            visited.add(id(value))
        if isinstance(value, dict):
            if isinstance(value.get('opportunity'), dict):
                op = value['opportunity']
                if op.get('version') == VERSION:
                    op['version'] = FLOW_VERSION
                else:
                    op['version'] = 'incompatible_prior_theme_contract'
            for item in value.values():
                convert_prior(item)
        elif isinstance(value, list):
            for item in value:
                convert_prior(item)
    convert_prior(previous)
    packet = build_flow_channels(candidates, now, previous=previous, **kwargs)
    research = packet['researchCandidates']
    valid = _valid_board(board, now)
    for row in research:
        op = row['opportunity']
        row['ruleRecommendation'] = False
        op.update(version=VERSION, rankingComposite=None, rankingComponents={})
        for key in ('rank', 'rankingPeerCount', 'selectionExclusion'):
            op.pop(key, None)
        theme = theme_for_code(board, str(row['code'])) if valid else None
        op['theme'] = deepcopy(theme) if theme else {'status': 'unavailable', 'selected': False}
        failure = None
        if not valid:
            failure = 'theme_market_evidence_unavailable'
            op['missing'].append('当前题材榜缺失、过期或不可核验；行业资金不能代替热点题材')
        elif not theme or theme.get('selected') is not True:
            failure = 'outside_selected_themes'
            op['missing'].append('不在已核验热门题材成分中，保留对照观察')
        else:
            rank, count = theme.get('leaderRank'), theme.get('memberCount')
            strength = theme.get('relativeStrengthPct')
            member_at = _stamp(theme.get('memberSourceTime'))
            quote = row.get('quote') or {}
            try:
                current_change = (float(quote['price'])/float(quote['pre_close'])-1)*100
                current_relative = current_change-float(theme['changePct'])
            except (TypeError, ValueError, KeyError, ZeroDivisionError):
                current_relative = None
            op['theme']['currentRelativeStrengthPct'] = current_relative
            core = (type(rank) is int and type(count) is int and count > 0 and rank > 0
                and rank <= max(3, math.ceil(count*.3))
                and member_at is not None and member_at <= now
                and member_at.astimezone(now.tzinfo).date() == now.date()
                and (now-member_at).total_seconds() <= 300
                and current_relative is not None and math.isfinite(current_relative) and current_relative >= 0
                and type(strength) in (int, float) and math.isfinite(strength) and strength >= 0)
            op['theme']['coreEligible'] = bool(core)
            if not core:
                failure = 'theme_core_not_confirmed'
                op['counterEvidence'].append('题材内领先地位尚未确认；弱后排不补足推荐名额')
            else:
                op['support'].append(f"热门题材「{theme.get('name', '')}」第{theme.get('rank')}位，成分强度排名{rank}/{count}")
        if failure:
            op['failedConditions'].append(failure)
            if op['grade'] != 'C':
                op.update(grade='B', selectionMode='observation')
            op['reasons'] = op['hardVetoes'] or op['failedConditions']
    eligible = rank_qualified([row for row in research if row['opportunity']['grade'] == 'F-A'])
    eligible.sort(key=lambda row: (row['opportunity']['theme']['rank'],
        row['opportunity']['theme']['leaderRank'], -row['opportunity']['rankingComposite'], row['code']))
    limit = max(0, min(5, int(kwargs.get('top_n', 5))))
    recommendations, themes, sectors = [], Counter(), Counter()
    for index, row in enumerate(eligible):
        op = row['opportunity']
        op.update(rank=index+1, rankingPeerCount=len(eligible))
        theme_id, sector = op['theme']['id'], op['sectorKey']
        exclusion = ('outside_maximum_recommendations' if len(recommendations) >= limit else
            'theme_concentration_limit' if themes[theme_id] >= 2 else
            'sector_concentration_limit' if sectors[sector] >= PARAMETERS['maximumPerSector'] else None)
        if exclusion:
            op['selectionExclusion'] = exclusion
        else:
            row['ruleRecommendation'] = True
            recommendations.append(row)
            themes[theme_id] += 1
            sectors[sector] += 1
    observations = [row for row in research if not row['ruleRecommendation']]
    packet.update(version=VERSION, policy=policy_contract(), recommendations=recommendations, observations=observations)
    packet['diagnostics'].update(selectionVersion=VERSION, qualifiedCount=len(eligible),
        recommendationCount=len(recommendations), observationCount=len(observations),
        gradeCounts=dict(Counter(row['opportunity']['grade'] for row in research)),
        failedConditionCounts=dict(Counter(reason for row in research for reason in row['opportunity']['failedConditions'])),
        themeStatus=board.get('status', 'unavailable'), themeFirst=True,
        selectedThemeIds=(board.get('selection') or {}).get('selectedIds', []))
    return packet
