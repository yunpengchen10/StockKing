"""Per-stock readiness is an I/O routing hint, never formal admission."""
from __future__ import annotations

VERSION = 'per-stock-minute-routing-v2'


def partition_queue(queued, preparation, objective_at, *, force_intraday=False):
    details = preparation.get('details') or {}
    session = objective_at.date().isoformat()
    clock = objective_at.replace(second=0, microsecond=0).time().replace(tzinfo=None).isoformat()
    minute, daily, records = [], [], []
    for row in queued:
        state = details.get(row['code']) or {}
        reason = None
        if not force_intraday:
            if preparation.get('targetSession') != session:
                reason = 'minute_history_session_not_prepared'
            elif state.get('fullSessionReady') is True or clock in (state.get('readyClocks') or []):
                pass
            elif state.get('ready') is True and 'readyClocks' not in state:
                # Legacy preparation recorded only one successful clock. Let the
                # actual minute reader recheck this stock locally; a routing
                # hint never grants history coverage or formal admission.
                pass
            else:
                reason = ('minute_history_clock_not_prepared' if state.get('readyClocks')
                          else 'minute_history_not_ready')
        (daily if reason else minute).append(row)
        records.append({'code': row['code'], 'route': 'daily_observation' if reason else 'minute_research',
                        'reason': reason, 'formalEligibilityGranted': False})
    return minute, daily, {'version': VERSION, 'targetSession': session, 'baselineClock': clock,
        'requested': len(queued), 'minuteResearchCount': len(minute), 'dailyObservationCount': len(daily),
        'forcedIntradayResearch': force_intraday, 'stocks': records,
        'meaning': '逐股分流；历史就绪仅允许采集分钟证据，仍须全部市场、板块、资金和风险门槛通过'}
