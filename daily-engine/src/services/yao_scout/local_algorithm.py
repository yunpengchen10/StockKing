"""The live local selection and its validation share this executable contract."""
from hashlib import sha256
import json

from .precision_policy import V11_VERSION, v11_entry_ready
from .recommendation_evidence import VERSION as EVIDENCE_VERSION
from .v11_factors import EARLY_WEIGHTS, MAIN_WEIGHTS, RISK_WEIGHTS, SCORE_VERSION, RISK_PENALTY_COEFFICIENT
from .research_policy import parameter_contract


def algorithm_contract():
    payload = {
        'schemaVersion': 2,
        'scoreVersion': SCORE_VERSION,
        'entryPolicyVersion': V11_VERSION,
        'evidenceVersion': EVIDENCE_VERSION,
        'weights': {'early': dict(EARLY_WEIGHTS), 'main': dict(MAIN_WEIGHTS),
                    'distribution': dict(RISK_WEIGHTS)},
        'riskPenaltyCoefficient': RISK_PENALTY_COEFFICIENT,
        'parameters': parameter_contract(),
        'formula': 'clamp(stageSelectedScore - riskPenaltyCoefficient * distributionRisk, 0, 100) * observedRegimeMultiplier',
        'stagePolicy': {'breakout': 'earlyScore', 'repair': 'earlyScore', 'continuation': 'mainRiseScore', 'unconfirmed': None},
        'missingFactorPolicy': '分量及总分均固定分母；缺失保持null且贡献为零，不向已知因子转移权重',
        'missingRiskPolicy': '缺失风险保持null但使用该项上界贡献惩罚；不可因风险缺失抬高排序',
        'researchPolicy': 'C/G为时点合法的可解释规则分；E仅向下市场状态调节；H只保留shadow特征',
        'entryConditions': ['当前评分版本', '有效报价与正常交易条件', '完整可计算分钟结构',
                            'evidenceEligible=true', 'status=conditional', 'finalScore为有限值',
                            '量与价同刻历史均至少20日', 'M/V/W/B关键因子有效', 'dataEligibility.status=formal',
                            '对应档位entryEligible=true', '供应商前收盘及涨跌停价完整且一致', '完成时重新核验报价'],
        'profilePolicy': '三档执行乖离、ATR、结构盈亏比、解禁和财务门槛；激进档独立核验分钟突破时允许上方压力及RR未知并披露，不编目标；已知RR仍须达标；未知事件覆盖只观察，已知利空独立否决',
        'samplePolicy': '同一算法契约与事前准入的正式信号及对照；手动刷新仅观察；缺证据旧记录不补造训练资格',
        'rankingPolicy': '同一 SignalLearningService 排序；只启用契约匹配且通过发布门槛的模型，否则使用规则分',
        'rankingTieBreakPolicy': '模型同分保留本轮规则分与证据顺序；账户基准使用冻结推荐名次',
    }
    fingerprint = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()
    return {**payload, 'contractId': 'local-picks-' + fingerprint[:20]}


def is_entry_eligible(candidate, profile='regular'):
    """Identical final entry predicate for live selection and validation samples."""
    return bool(v11_entry_ready(candidate)
                and (candidate.get('precisionDecision') or {}).get('version') == V11_VERSION
                and ((candidate.get('precisionDecision') or {}).get('profiles') or {})
                .get(profile, {}).get('entryEligible') is True)
