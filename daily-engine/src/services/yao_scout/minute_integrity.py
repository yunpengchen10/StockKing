"""Read-only lineage checks and security-session price integrity.

Large moves are evidence requiring verification, not a presumed 10% exchange
limit. Quarantine is reversible; raw evidence and previous revisions survive.
"""
from __future__ import annotations

from datetime import datetime
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib

DATA_QUALITY_VERSION = 'minute-integrity-v1'


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


def session_revision(code, day, rows):
    keys = ('end', 'open', 'high', 'low', 'close', 'amount_cny', 'volume_shares',
            'source', 'identity_evidence', 'integrity_revision', 'corporate_action', 'no_price_limit', 'suspended')
    return canonical_hash([code, day, [{key: row.get(key) for key in keys}
                                      for row in sorted(rows, key=lambda row: row['end'])]])


def _source_family(source):
    source = str(source).lower()
    for family in ('tdx', 'sina', 'tencent', 'eastmoney', 'tushare'):
        if family in source:
            return family
    return source


def identity_matches(row, code):
    evidence = row.get('identity_evidence') or {}
    market = 1 if str(code).startswith('6') else 0
    return (evidence.get('status') == 'provider_reply_checked'
            and str(evidence.get('requested_code', '')).strip() == code
            and str(evidence.get('response_code', '')).strip() == code
            and evidence.get('requested_market') == market
            and evidence.get('response_market') == market
            and str(evidence.get('period')) == '1'
            and evidence.get('page_cursor_checked') is True)


def inspect_session(code, day, rows, verification=None):
    """Return reasons, independent verification and identity status, without I/O."""
    reasons = []
    ordered = sorted(rows, key=lambda row: row['end'])
    for row in ordered:
        if str(row.get('end', ''))[:10] != day:
            reasons.append('session_date_mismatch')
        if row.get('data_quality_status') == 'tainted' or row.get('conflicting_observations'):
            reasons.append('conflicting_source_observations')
        identity = row.get('identity_evidence') or {}
        if identity and ((identity.get('requested_code') and identity['requested_code'] != code)
                         or (identity.get('response_code') and identity['response_code'] != code)):
            reasons.append('security_identity_mismatch')
        if identity.get('status') == 'provider_reply_checked' and not identity_matches(row, code):
            reasons.append('security_identity_contract_mismatch')
    identity_ok = bool(ordered) and all(identity_matches(row, code) for row in ordered)
    # Within-session discontinuities only: an ex-right overnight gap is not a
    # same-day splice. These thresholds quarantine pending evidence, not reject
    # a legal high-volatility session under an invented exchange price limit.
    scale_suspect = any(max(a['close'], b['open']) / min(a['close'], b['open']) > 1.30
                        for a, b in zip(ordered, ordered[1:]))
    if ordered and max(row['high'] for row in ordered) / min(row['low'] for row in ordered) > 1.40:
        scale_suspect = True
    verified = False
    verified_through = None
    evidence = verification or {}
    if evidence:
        try:
            at = datetime.fromisoformat(str(evidence.get('observed_at', '')))
            sources = {_source_family(source) for row in ordered
                       for source in [row.get('source'), *(row.get('sources') or [])]}
            family = _source_family(evidence.get('source'))
            independent = family in {'tdx','sina','tencent','eastmoney','tushare'} and family not in sources
            kind = evidence.get('kind')
            valid = (kind in {'daily_ohlc', 'quote_prefix'}
                     and evidence.get('code') == code and evidence.get('day') == day
                     and evidence.get('price_basis') == 'unadjusted' and at.tzinfo is not None
                     and at.date().isoformat() >= day and independent)
            scope = datetime.fromisoformat(str(evidence.get('source_time'))) if kind == 'quote_prefix' else None
            if kind == 'quote_prefix':
                valid = valid and scope.tzinfo is not None and scope.date().isoformat() == day and scope <= at
            else:
                valid = valid and evidence.get('complete') is True
            op, hi, lo, cl = [float(evidence[key]) for key in ('open', 'high', 'low', 'close')]
            valid = valid and 0 < lo <= min(op, cl) <= max(op, cl) <= hi
            covered = [row for row in ordered if scope is None or datetime.fromisoformat(row['end']) <= scope]
            if valid and covered:
                tolerance = max(.011, hi * 1e-6)
                if any(row['low'] < lo-tolerance or row['high'] > hi+tolerance for row in covered):
                    reasons.append('independent_daily_range_conflict')
                elif (covered[-1]['end'][11:16] == '15:00'
                      and abs(covered[-1]['close']-cl) > tolerance):
                    reasons.append('independent_daily_close_conflict')
                elif (len(covered) == 240 and (abs(covered[0]['open']-op) > tolerance
                      or abs(max(row['high'] for row in covered)-hi) > tolerance
                      or abs(min(row['low'] for row in covered)-lo) > tolerance)):
                    reasons.append('independent_daily_ohlc_conflict')
                else:
                    covered_identity = all(identity_matches(row, code) for row in covered)
                    verified_through = covered[-1]['end'] if covered_identity else None
                    verified = identity_ok and len(covered) == len(ordered)
            else:
                reasons.append('verification_evidence_invalid')
        except (KeyError, TypeError, ValueError, OverflowError):
            reasons.append('verification_evidence_invalid')
    if scale_suspect and not verified:
        reasons.append('intraday_price_scale_discontinuity_requires_independent_verification')
    if not identity_ok and not reasons:
        reasons.append('provider_security_identity_unverified')
    if not verified and not reasons:
        reasons.append('independent_price_evidence_missing')
    severe = any(reason not in {'provider_security_identity_unverified', 'independent_price_evidence_missing',
                               'verification_evidence_invalid'} for reason in reasons)
    return {'status': 'tainted' if severe else 'verified' if verified else 'unknown',
            'reasonCodes': sorted(set(reasons)), 'identityVerified': identity_ok,
            'priceVerified': verified, 'verifiedThrough': verified_through,
            'verificationLevel': 'security_identity_and_independent_ohlc_bounds' if verified_through else None,
            'data_quality_version': DATA_QUALITY_VERSION}


def _lineage(sessions, missing=False):
    sessions = sorted(sessions, key=lambda row: (row['code'], row['day'], row.get('frozenThrough') or ''))
    status = ('tainted' if any(row['status'] == 'tainted' for row in sessions) else
              'verified' if sessions and not missing and all(row['status'] == 'verified' for row in sessions) else 'unknown')
    ids = [f"{row['code']}/{row['day']}/{row.get('frozenThrough')}/{row['revision']}" for row in sessions]
    fingerprint = canonical_hash([DATA_QUALITY_VERSION, ids])
    reasons = {reason for row in sessions for reason in row.get('reasonCodes', [])}
    if missing:
        reasons.add('session_quality_metadata_missing')
    return {'status': status, 'fingerprint': fingerprint, 'raw_data_revision': fingerprint,
            'data_quality_version': DATA_QUALITY_VERSION, 'sourceIds': ids,
            'reasonCodes': sorted(reasons), 'sessions': sessions}


def read_archive_lineage(directory, code=None, days=None, *, cutoff=None):
    """Never creates/changes the archive; legacy days remain explicitly unknown."""
    path = Path(directory) / 'minute-bars-v2.sqlite3'
    if not path.exists():
        return _lineage([], missing=True)
    conditions, args = [], []
    if code is not None:
        conditions.append('s.code=?')
        args.append(str(code))
    if days is not None:
        days = sorted({str(day)[:10] for day in days})
        if not days:
            return _lineage([], missing=True)
        conditions.append('s.day IN (' + ','.join('?' for _ in days) + ')')
        args.extend(days)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_quality'").fetchone()
        query = ('SELECT s.code,s.day,q.revision,q.status,q.reasons_json,s.payload,q.verification_json FROM sessions s '
                 'LEFT JOIN session_quality q ON q.code=s.code AND q.day=s.day' if exists else
                 'SELECT s.code,s.day,NULL,NULL,NULL,s.payload,NULL FROM sessions s')
        rows = db.execute(query + where, args).fetchall()
        anchors = {}
        evidence_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_evidence'").fetchone()
        if evidence_exists:
            anchor_where = " WHERE s.kind='independent_price_anchor'" + (' AND ' + ' AND '.join(conditions) if conditions else '')
            for anchor in db.execute('SELECT s.code,s.day,s.metadata_json FROM session_evidence s' + anchor_where, args):
                anchors.setdefault((anchor[0],anchor[1]), []).append(json.loads(anchor[2]))
    sessions = []
    for row in rows:
        stored = json.loads(zlib.decompress(row[5]))
        limit = datetime.fromisoformat(str(cutoff)) if cutoff is not None else None
        prefix = [item for item in stored if limit is None or datetime.fromisoformat(item['end']) <= limit]
        if not prefix:
            continue
        # Dependencies refer to exactly the price prefix known at decision time.
        # Normal later bars may extend a session without altering old evidence.
        through = max(item['end'] for item in prefix)
        evidence = json.loads(row[6]) if row[6] else {}
        scoped = inspect_session(row[0], row[1], prefix, evidence)
        # Retain the earliest successful independently observed check for this
        # exact repair generation and prefix. A later quote cannot rewrite when
        # this evidence first became knowable to a historical training fold.
        generation = prefix[0].get('integrity_revision')
        for anchor in anchors.get((row[0],row[1]), []):
            candidate = anchor.get('verification') or {}
            if anchor.get('integrityRevision') != generation or not candidate.get('known_at'):
                continue
            candidate_quality = inspect_session(row[0], row[1], prefix, candidate)
            if candidate_quality['status'] == 'verified' and (scoped['status'] != 'verified'
                    or not evidence.get('known_at') or candidate['known_at'] < evidence['known_at']):
                evidence, scoped = candidate, candidate_quality
        status = 'tainted' if row[3] == 'tainted' else scoped['status']
        reasons = json.loads(row[4]) if row[3] == 'tainted' else scoped['reasonCodes']
        if row[3] is None:
            legacy = inspect_session(row[0], row[1], stored)
            if legacy['status'] == 'tainted':
                status, reasons = 'tainted', legacy['reasonCodes']
            reasons += ['session_quality_metadata_missing']
        sessions.append({'code': row[0], 'day': row[1], 'revision': session_revision(row[0], row[1], prefix),
                         'archiveRevision':row[2], 'frozenThrough':through,
                         'verifiedThrough':scoped['verifiedThrough'], 'qualityKnownAt':evidence.get('known_at'),
                         'verificationLevel':scoped['verificationLevel'], 'status':status, 'reasonCodes':reasons})
    missing = not sessions or any(row[3] is None for row in rows)
    if code is not None and days is not None and {row['day'] for row in sessions} != set(days):
        missing = True
    return _lineage(sessions, missing)


def validate_archive_lineage(directory, lineage):
    """Check recorded dependencies, not the unrelated latest day in the archive."""
    if not isinstance(lineage, dict) or lineage.get('data_quality_version') != DATA_QUALITY_VERSION or not lineage.get('sessions'):
        return {**_lineage([], True), 'valid': False}
    current, invalid = [], []
    for old in lineage['sessions']:
        state = read_archive_lineage(directory, old.get('code'), [old.get('day')], cutoff=old.get('frozenThrough'))
        current.extend(state['sessions'])
        if len(state['sessions']) != 1 or state['sessions'][0]['revision'] != old.get('revision'):
            invalid.append('referenced_data_revision_changed_or_missing')
        if state['status'] != 'verified':
            invalid.extend(state['reasonCodes'] or ['referenced_data_not_verified'])
    result = _lineage(current, bool(invalid))
    result['valid'] = not invalid and result['status'] == 'verified'
    if invalid:
        result['status'] = 'tainted'
        result['reasonCodes'] = sorted(set(result['reasonCodes'] + invalid))
    return result
