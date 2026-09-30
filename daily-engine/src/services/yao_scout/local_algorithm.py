"""The live local selection and its validation share this executable contract."""
from hashlib import sha256
import json

from .precision_policy import V11_VERSION, v11_entry_ready
from .recommendation_evidence import VERSION as EVIDENCE_VERSION
from .v11_factors import EARLY_WEIGHTS, MAIN_WEIGHTS, RISK_WEIGHTS, SCORE_VERSION, RISK_PENALTY_COEFFICIENT


def algorithm_contract():
    payload = {
        'schemaVersion': 1,
        'scoreVersion': SCORE_VERSION,
        'entryPolicyVersion': V11_VERSION,
        'evidenceVersion': EVIDENCE_VERSION,
        'weights': {'early': dict(EARLY_WEIGHTS), 'main': dict(MAIN_WEIGHTS),
                    'distribution': dict(RISK_WEIGHTS)},
        'riskPenaltyCoefficient': RISK_PENALTY_COEFFICIENT,
        'formula': 'clamp(max(earlyScore, mainRiseScore) - riskPenaltyCoefficient * distributionRisk, 0, 100)',
        'missingFactorPolicy': '缺失因子不补值；剩余已声明权重归一化，覆盖不足降低置信',
        'entryConditions': ['当前评分版本', '有效报价与正常交易条件', '完整可计算分钟结构',
                            'evidenceEligible=true', 'status=conditional', 'finalScore为有限值',
                            '对应档位entryEligible=true', '完成时重新核验报价'],
        'profilePolicy': '当前 V1.1 三档共用有效报价、正常交易与分钟结构准入；旧版三档日线门槛不参与本地精选',
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
