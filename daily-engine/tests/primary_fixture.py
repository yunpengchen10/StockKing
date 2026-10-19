"""Explicit complete evidence for isolated admission/learning unit tests."""
from datetime import datetime


def make_primary_evidence(candidate):
    now = datetime.fromisoformat(candidate.get('decision_at') or '2026-09-24T10:30:20+08:00')
    minute = now.replace(second=0, microsecond=0).isoformat()
    return {**candidate, 'decision_at': now.isoformat(), 'minuteSourceTime': minute,
            'factorCutoffAt': minute, 'dataLineage': {'status': 'verified'},
            'factorAvailability': {'S': {'available': True}, 'F': {'available': True}},
            'sectorEvidence': {'asOf': minute}, 'fundEvidence': {'asOf': minute},
            'evidenceChecks': {'minute_structure': True, 'relative_volume': True,
                               'sector_resonance': True, 'fund_direction': True,
                               **candidate.get('evidenceChecks', {})},
            'quote': {**candidate.get('quote', {}), 'active_flow_source': 'test_classification',
                      'active_flow_as_of': now.isoformat(), 'active_buy_share_pct': 60},
            'researchFactors': {'E': {'source': 'test_full_universe', 'asOf': minute,
                'status': 'insufficient', 'regime': 'unknown', 'score': None,
                'features': {'universe_count': 3000, 'source_coverage': 1, 'advancing_pct': 65}}}}
