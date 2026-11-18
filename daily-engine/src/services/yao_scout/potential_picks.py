"""Rank the scan's frozen, untapped main-board stocks using trend and risk-adjusted room."""
from datetime import date, time
import re

from src.services.public_market_quotes import market_phase
from .daily_primary import _events, _intraday_direction
from .limit_entry import assess_limit_entry
from .premarket import previous_session
from .research_policy import DEFAULT_PARAMETERS
from .theme_rotation import _at, _invalid_quote_evidence, _number

VERSION = 'potential-picks-v1'
REFERENCE_ASSESSMENT_VERSION = 'potential-reference-assessment-v1'
_MAIN_BOARD = re.compile(r'(?:000|001|002|003|600|601|603|605)\d{3}')


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _quote_stamp(quote):
    return _at(quote.get('provider_timestamp') or quote.get('source_time'))


def _valid_reference_metadata(quote, published):
    """Separate an honest session reference from a rejected realtime quote.

    The public adapter's is_stale flag also means "not continuously trading".
    Only its explicit session-reference assessment can relax that compatibility flag;
    price conflicts, identity failures and unavailable evidence still veto it.
    The original quote and its execution flags are never changed.
    """
    stamp = _quote_stamp(quote)
    meta = quote.get('public_quote_metadata')
    if not isinstance(meta, dict) or not stamp or stamp.date() != published.date() or stamp > published:
        return False
    phase = market_phase(published)
    if phase not in {'post_close', 'lunch_break', 'closing_auction', 'opening_pause'} or market_phase(stamp) != phase:
        return False
    checked = _at(meta.get('checked_at'))
    if (meta.get('schema_version') != 1 or meta.get('status') != 'reference_only'
            or meta.get('same_market_date') is not True or meta.get('phase') != phase
            or meta.get('code') != quote.get('code') or not checked or not stamp <= checked <= published):
        return False
    allowed = {'request_phase_' + phase, 'source_phase_' + phase}
    if phase == 'post_close':
        allowed.add('post_close_cannot_reconstruct_1455')
    # A completed session reference keeps its original clock; its age cannot
    # make it executable, but the realtime 30-second limit does not erase it.
    if (phase in {'post_close', 'lunch_break'} and meta.get('max_age_seconds') == 30
            and (checked-stamp).total_seconds() > 30):
        allowed.add('stale_quote')
    issues = meta.get('issues')
    if (not isinstance(issues, list) or not issues or any(issue not in allowed for issue in issues)
            or any(key not in issues for key in ('request_phase_' + phase, 'source_phase_' + phase))):
        return False
    clean_meta = {key: value for key, value in meta.items()
                  if key not in {'quote_usable_for_current_price_check', 'issues'}}
    checked_quote = {key: value for key, value in quote.items()
                     if key not in {'is_stale', 'quote_usable_for_current_price_check', 'public_quote_metadata'}}
    checked_quote['public_quote_metadata'] = clean_meta
    return not _invalid_quote_evidence(checked_quote)


def _candidate_rows(run):
    # Never use a T+1 queue, its score, or a reconstruction made after the scan.
    for key in ('dailyCandidates', 'dailyObservationResearch', 'premarketCandidates',
                'candidates', 'controls', 'precisionResearch', 'evidenceInsufficient',
                'dailyAttackWatchlist', 'precisionWatchlist', 'invalidatedCandidates'):
        yield from (row for row in (run.get(key) or []) if isinstance(row, dict))
    for key in ('dailyProfileCandidates', 'profileCandidates'):
        for group in (run.get(key) or {}).values():
            yield from (row for row in (group or []) if isinstance(row, dict))
    channels = run.get('opportunityChannels') or {}
    for key in ('recommendations', 'researchCandidates', 'observations'):
        yield from (row for row in (channels.get(key) or []) if isinstance(row, dict))


def _already_touched(code, quote, published):
    stamp = _quote_stamp(quote)
    if not (quote.get('code') == code and stamp and stamp.date() == published.date() and stamp <= published):
        return False
    if any(quote.get(key) is not None and not (stamp <= (_at(quote[key]) or published) <= published)
           for key in ('checked_at', 'fetched_at')):
        return False
    upper = _positive(quote.get('limit_up'))
    high = max(_positive(quote.get('high')) or 0, _positive(quote.get('price')) or 0)
    return bool(quote.get('locked_limit_up') or (upper and high >= upper - 1e-7))


def _blocked_codes(run, rows, published):
    blocked = set()
    maximum_unlock = DEFAULT_PARAMETERS['profiles']['regular']['max_unlock_pct']
    for row in rows:
        code = row.get('code')
        if not isinstance(code, str) or code in blocked:
            continue
        if (_already_touched(code, row.get('quote') or {}, published)
                or row.get('currentSignalState') in ('invalidated', 'withdrawn')
                or row.get('dailyCurrentSignalState') in ('invalidated', 'withdrawn')):
            blocked.add(code)
            continue
        signal = row.get('nextDaySignal') or {}
        signal_time = _at(signal.get('sourceTime'))
        if (signal.get('touchedLimitToday') is True and signal_time and signal_time <= published
                and signal_time.date() == published.date()):
            blocked.add(code)
        for packet in (row, signal, row.get('opportunity') or {}, row.get('primaryAdmission') or {}):
            marker = packet.get('limitEntry') or {}
            source_at = _at(marker.get('sourceTime'))
            if (marker.get('code') == code and marker.get('status') in ('touched_limit', 'at_limit')
                    and source_at and source_at.date() == published.date() and source_at <= published):
                blocked.add(code)
        event = row.get('eventAssessment') or {}
        context = (row.get('precisionDecision') or {}).get('sourceContext') or {}
        if event.get('riskReasons') or (_number(event.get('unlock14dPct')) or 0) >= maximum_unlock:
            blocked.add(code)
        if context:
            event = _events(context, published)
            if event['riskReasons'] or event['unlock14dPct'] >= maximum_unlock:
                blocked.add(code)
    for row in run.get('dailyRejectionLedger') or []:
        if isinstance(row, dict) and any(str(reason).startswith(('known_loss', 'known_negative_earnings', 'known_large_unlock'))
                                        for reason in row.get('reasons') or []):
            blocked.add(row.get('code'))
    for code, quote in (run.get('dailyObservationQuotes') or {}).items():
        if isinstance(quote, dict) and _already_touched(code, quote, published):
            blocked.add(code)
    return blocked


def _market_reference(row, entries, published, baseline):
    """Keep the timestamp and nature of the actual price in every market phase."""
    code = row['code']
    quote = row.get('quote') or {}
    clock = published.time()
    if clock < time(9, 25) and row.get('referencePriceKind') == 'previous_close':
        price = _positive(row.get('dailyLastClose'))
        if (row.get('status') == 'premarket' and row.get('baselineAsOf') == baseline
                and row.get('sourceTime') == baseline and row.get('baselineSource') and price == _positive(row.get('price'))
                and price and not quote):
            return price, baseline, row['baselineSource'], '前收盘价', None
        return None
    stamp = _quote_stamp(quote)
    if not (stamp and stamp.date() == published.date() and stamp <= published and quote.get('code') == code
            and quote.get('source') and (not _invalid_quote_evidence(quote)
                                        or _valid_reference_metadata(quote, published))):
        return None
    if any(quote.get(k) for k in ('suspended', 'locked_limit_up', 'trading_restricted', 'corporate_action', 'no_price_limit')):
        return None
    price, high, upper, previous = [_positive(quote.get(k)) for k in ('price', 'high', 'limit_up', 'pre_close')]
    if not all((price, high, upper, previous)) or not price <= high < upper or upper <= previous:
        return None
    last_close = _positive(row.get('dailyLastClose') or (row.get('dailyEvidence') or {}).get('metrics', {}).get('last_close'))
    if last_close is None or abs(last_close - previous) > .011:
        return None
    for key in ('checked_at', 'fetched_at'):
        if quote.get(key) is not None:
            known = _at(quote[key])
            if not known or not stamp <= known <= published:
                return None
    basis = '扫描价'
    if time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(14, 57):
        entry = entries.get(code) if entries is not None else assess_limit_entry(code, quote, published)
        if (not entry or entry.get('eligible') is not True or entry.get('status') != 'pre_limit'
                or entry.get('code') != code or _positive(entry.get('price')) != price
                or entry.get('source') != quote['source'] or _at(entry.get('sourceTime')) != stamp
                or (published-stamp).total_seconds() > 30):
            return None
    else:
        if clock >= time(15):
            if stamp.time() < time(15):
                return None
            basis = '收盘价'
        elif time(11, 30) <= clock < time(13):
            if not time(11, 30) <= stamp.time() < time(13):
                return None
            basis = '午间收盘价'
        elif stamp.time() < time(9, 25) or (published-stamp).total_seconds() > 30:
            return None
    direction = _intraday_direction(quote)
    vwap = _positive(direction['metrics']['sessionVwap'])
    if direction['status'] != 'observed' or not vwap or price < vwap or price <= previous:
        return None
    return price, stamp.isoformat(), quote['source'], basis, vwap


def _daily_levels(row, baseline):
    packet = row.get('dailyEvidence') or {}
    levels = {}
    if packet.get('asOf') == baseline and packet.get('source'):
        levels.update(packet.get('metrics') or {})
    for item in row.get('indicatorEvidence') or []:
        if (isinstance(item, dict) and item.get('asOf') == baseline and item.get('source')
                and item.get('status') == 'observed'):
            levels.setdefault(item.get('key'), item.get('value'))
    return {k: _positive(levels.get(k)) for k in ('ma5', 'ma10', 'ma20', 'atr14', 'low20', 'high20', 'high60')}


def _evaluate(row, reference, baseline):
    price, stamp, source, label, vwap = reference
    levels = _daily_levels(row, baseline)
    fast, mid, slow, atr = (levels[k] for k in ('ma5', 'ma10', 'ma20', 'atr14'))
    if not all((fast, mid, slow, atr)) or not (fast > mid > slow and price > slow):
        return None
    support = slow
    overhead = [v for v in (levels['high20'], levels['high60'], _positive(row.get('resistance'))) if v and v > price]
    resistance = min(overhead, default=None)
    risk_distance = max(price-support, atr)
    room = 100 * (resistance/price-1) if resistance else None
    ratio = (resistance-price)/risk_distance if resistance else None
    breakout = bool(levels['high20'] and price > levels['high20'] and vwap and price > vwap)
    if (ratio is None and not breakout) or (ratio is not None and ratio < 1):
        return None
    reasons = [f'截至{baseline}的5日、10日、20日均线依次为{fast:.2f}、{mid:.2f}、{slow:.2f}元，均线多头排列。']
    if vwap:
        reasons.append(f'{label}{price:.2f}元，当日成交均价{vwap:.2f}元。')
    if resistance:
        reasons.append(f'距已记录压力位{resistance:.2f}元有{room:.2f}%空间，约为支撑及波动风险距离的{ratio:.2f}倍。')
    elif breakout:
        reasons.append(f'价格突破此前20日高点{levels["high20"]:.2f}元，上方可量化空间待确认。')
    risk = f'回落至20日均线{support:.2f}元下方时，趋势依据减弱。14日平均真实波幅为{atr:.2f}元。'
    result = dict(code=row['code'], name=row.get('name') or row['code'], price=price, sourceTime=stamp,
                  source=source, basisLabel=label, baselineDate=baseline, reasons=reasons, risk=risk,
                  supportPrice=round(support, 4), resistancePrice=resistance, upsideToResistancePct=round(room, 4) if room is not None else None,
                  rewardRisk=round(ratio, 4) if ratio is not None else None,
                  selectionBasis='breakout' if breakout else 'trend_with_room')
    # Do not invent a price target for a breakout to make its ratio comparable.
    tier = 1 if ratio is not None else 0
    return (tier, ratio or 0., room or 0.), result


def select_potential_picks(run, screen):
    """Return zero to five unique, deterministic, JSON-ready rows for this snapshot."""
    published = _at(run.get('generatedAt') or run.get('as_of'))
    if not published:
        return []
    baseline = ((run.get('dailyAssessment') or {}).get('baselineAsOf')
                or (run.get('premarketAssessment') or {}).get('baselineSession')
                or previous_session(published).isoformat())
    try:
        if date.fromisoformat(str(baseline)) >= published.date():
            return []
    except ValueError:
        return []
    entries = ({row['code']: row.get('limitEntry') for row in screen.get('rows') or []
                if isinstance(row, dict) and row.get('code')} if isinstance(screen, dict) else None)
    rows = list(_candidate_rows(run))
    # All current-scan references can veto a touch, including other queues which
    # supply no candidates, ranks or target prices to this selector.
    exclusions = [*rows, *(run.get('windvanes') or []), *((run.get('marketBrief') or {}).get('focusStocks') or [])]
    if not (run.get('nextDayResearch') or {}).get('retrospectiveProjection'):
        exclusions.extend(run.get('nextDayWatchlist') or [])
        exclusions.extend(run.get('nextDayContinuationWatchlist') or [])
    blocked = _blocked_codes(run, [r for r in exclusions if isinstance(r, dict)], published)
    selected = {}
    for row in rows:
        code = row.get('code')
        if not isinstance(code, str) or code in selected or code in blocked or not _MAIN_BOARD.fullmatch(code) or 'ST' in str(row.get('name')).upper():
            continue
        reference = _market_reference(row, entries, published, baseline)
        if not reference:
            continue
        ranked = _evaluate(row, reference, baseline)
        if not ranked:
            continue
        events = row.get('eventAssessment') or {}
        context = (row.get('precisionDecision') or {}).get('sourceContext')
        if context:
            events = _events(context, published)
        if events.get('missingReasons') or not events:
            ranked[1]['risk'] += '公告、业绩或解禁风险覆盖待补。'
        selected[code] = ranked
    ordered = sorted(selected.values(), key=lambda item: (*(-value for value in item[0]), item[1]['code']))
    return [dict(result, rank=rank) for rank, (_, result) in enumerate(ordered[:5], 1)]


def prepare_reference_assessment(run):
    """Create a new manual publication assessment outside continuous trading.

    Called only by the new-scan persistence path. Historical reads never call
    this function, and previously published rows retain their frozen document.
    """
    at = _at(run.get('generatedAt') or run.get('as_of'))
    if (run.get('scanSlot') != 'live' or run.get('official') or run.get('publishedPicks')
            or run.get('status') not in {'expired', 'completed_observations', 'no_candidates_with_coverage_limits'}
            or not at or market_phase(at) == 'continuous'
            or (run.get('dataQuality') or {}).get('snapshot_count', 0) <= 0):
        return
    items = select_potential_picks(run, None)
    if not items:
        return
    run['potentialAssessment'] = {'version': REFERENCE_ASSESSMENT_VERSION,
        'asOf': at.isoformat(), 'phase': market_phase(at), 'referenceOnly': True,
        'selectedCount': len(items), 'items': items}
    run['legacySelectionStatus'] = run['status']
    run['status'] = 'completed_observations'
    run['message'] = f'本轮依据已核验参考价、趋势与风险空间选出{len(items)}只潜力股票。'


def reference_assessment_valid(run, *, historical=False):
    packet = run.get('potentialAssessment')
    at = _at(run.get('generatedAt') or run.get('as_of'))
    if (not isinstance(packet, dict) or packet.get('version') != REFERENCE_ASSESSMENT_VERSION
            or run.get('scanSlot') != 'live' or run.get('official') or not at
            or packet.get('asOf') != at.isoformat() or packet.get('phase') != market_phase(at)
            or market_phase(at) == 'continuous' or packet.get('referenceOnly') is not True
            or not isinstance(packet.get('items'), list) or not 1 <= len(packet['items']) <= 5
            or packet.get('selectedCount') != len(packet['items'])
            or (run.get('dataQuality') or {}).get('snapshot_count', 0) <= 0):
        return False
    if historical:
        publication = run.get('publishedPicks') or {}
        return (publication.get('status') == 'published' and publication.get('runId') == run.get('run_id')
                and publication.get('asOf') == (run.get('generatedAt') or run.get('as_of'))
                and publication.get('items') == packet['items'])
    return select_potential_picks(run, None) == packet['items']
