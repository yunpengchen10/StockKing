"""Append-only evidence of received cumulative funds, grouped by measurement.

No backfill, inferred history or rewritten provider values. Conflicting revisions
are retained separately and quarantine their timestamp after first observation.
"""
from __future__ import annotations

from contextlib import closing
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from time import monotonic

from .fund_flow_evidence import TZ, series_rows, timestamp


class FundFlowArchive:
    def __init__(self, directory, *, timeout=10, deadline=None):
        self.timeout, self.deadline = max(0., float(timeout)), deadline
        self.path = Path(directory) / 'fund-flow-v1.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS fund_observations (
                    code TEXT NOT NULL, provider TEXT NOT NULL, measurement_version TEXT NOT NULL,
                    source TEXT NOT NULL, day TEXT NOT NULL, bar_end TEXT NOT NULL,
                    first_observed_at TEXT NOT NULL, value_json TEXT NOT NULL,
                    PRIMARY KEY(code,provider,measurement_version,source,bar_end));
                CREATE TABLE IF NOT EXISTS fund_conflicts (
                    code TEXT NOT NULL, provider TEXT NOT NULL, measurement_version TEXT NOT NULL,
                    source TEXT NOT NULL, bar_end TEXT NOT NULL, first_observed_at TEXT NOT NULL,
                    PRIMARY KEY(code,provider,measurement_version,source,bar_end));
                CREATE TABLE IF NOT EXISTS fund_revisions (
                    code TEXT NOT NULL, provider TEXT NOT NULL, measurement_version TEXT NOT NULL,
                    source TEXT NOT NULL, bar_end TEXT NOT NULL, observed_at TEXT NOT NULL,
                    value_hash TEXT NOT NULL, value_json TEXT NOT NULL,
                    PRIMARY KEY(code,provider,measurement_version,source,bar_end,value_hash));
                CREATE INDEX IF NOT EXISTS fund_observations_day ON fund_observations(day,code);
            ''')

    def _connect(self):
        remaining = self.timeout if self.deadline is None else self.deadline-monotonic()
        if remaining <= 0:
            raise TimeoutError('fund archive deadline exhausted')
        connection = sqlite3.connect(self.path, timeout=min(self.timeout, remaining))
        connection.row_factory = sqlite3.Row
        if self.deadline is not None:
            connection.set_progress_handler(lambda: int(monotonic() >= self.deadline), 1000)
        return connection

    def save(self, packet, cutoff, *, observed_at=None):
        cutoff = timestamp(cutoff)
        observed = timestamp(observed_at) if observed_at is not None else datetime.now(TZ)
        if cutoff is None or observed is None:
            raise ValueError('cutoff and observation time require explicit timezones')
        # Later received data cannot be made historically available by its bar time.
        rows, reasons = series_rows(packet, min(cutoff, observed))
        identity = tuple(packet.get(key) for key in ('code', 'provider', 'measurementVersion', 'source'))
        if 'fund_identity_or_time_basis_missing' in reasons:
            return {'status': 'unavailable', 'saved': 0, 'conflicts': 0, 'reasons': reasons}
        saved = conflicts = 0
        stamp = observed.isoformat()
        with closing(self._connect()) as db, db:
            for end, value in rows.items():
                key = (*identity, end.isoformat())
                serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                old = db.execute('SELECT value_json FROM fund_observations WHERE code=? AND provider=? '
                                 'AND measurement_version=? AND source=? AND bar_end=?', key).fetchone()
                if old is None:
                    db.execute('INSERT INTO fund_observations VALUES (?,?,?,?,?,?,?,?)',
                               (*identity, end.date().isoformat(), end.isoformat(), stamp, serialized))
                    saved += 1
                elif old['value_json'] != serialized:
                    db.execute('INSERT OR IGNORE INTO fund_conflicts VALUES (?,?,?,?,?,?)', (*key, stamp))
                    digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
                    db.execute('INSERT OR IGNORE INTO fund_revisions VALUES (?,?,?,?,?,?,?,?)',
                               (*key, stamp, digest, serialized))
                    conflicts += 1
            # The provider parser may already have removed conflicting duplicate
            # rows. Preserve a tombstone even if no canonical observation existed.
            conflict_times = set(packet.get('conflictingTimes') or [])
            if 'conflicting_fund_observations' in reasons:
                # A direct caller can supply duplicate cumulative rows instead
                # of going through the provider parser. Quarantine dropped times.
                conflict_times.update(item.get('end') for item in packet.get('cumulativeSeries') or []
                                      if isinstance(item, dict) and timestamp(item.get('end')) not in rows)
            for value in conflict_times:
                end = timestamp(value)
                if end is not None and end.date() == cutoff.date() and end <= min(cutoff, observed):
                    db.execute('INSERT OR IGNORE INTO fund_conflicts VALUES (?,?,?,?,?,?)',
                               (*identity, end.isoformat(), stamp))
                    conflicts += 1
            conflict_values = list(packet.get('conflictingObservations') or [])
            conflict_values.extend(item for item in packet.get('cumulativeSeries') or []
                                   if isinstance(item, dict) and item.get('end') in conflict_times)
            for value in conflict_values:
                end = timestamp(value.get('end')) if isinstance(value, dict) else None
                if end is None or end.date() != cutoff.date() or end > min(cutoff, observed):
                    continue
                serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
                db.execute('INSERT OR IGNORE INTO fund_revisions VALUES (?,?,?,?,?,?,?,?)',
                           (*identity, end.isoformat(), stamp, digest, serialized))
        return {'status': 'saved', 'saved': saved, 'conflicts': conflicts, 'observedAt': stamp,
                'scope': 'received completed cumulative observations only; no historical coverage implied'}

    def read(self, code, day, provider, measurement_version, *, as_of=None, source=None):
        now = timestamp(as_of) if as_of is not None else datetime.now(TZ)
        if now is None:
            raise ValueError('as_of requires an explicit timezone')
        args = [str(code), str(day), provider, measurement_version, now.isoformat(), now.isoformat()]
        query = ('SELECT * FROM fund_observations WHERE code=? AND day=? AND provider=? '
                 'AND measurement_version=? AND first_observed_at<=? AND bar_end<=?')
        if source is not None:
            query += ' AND source=?'
            args.append(source)
        with closing(self._connect()) as db, db:
            rows = db.execute(query+' ORDER BY bar_end', args).fetchall()
            sources = {row['source'] for row in rows}
            packet = {'code': str(code), 'provider': provider, 'measurementVersion': measurement_version,
                      'source': source or (next(iter(sources)) if len(sources) == 1 else None),
                      'amountUnit': 'CNY', 'normalizedTimeSemantics': 'bar_end', 'flowKind': 'vendor_estimate',
                      'asOf': None, 'cumulativeSeries': [], 'conflictingTimes': [],
                      'archiveReadAt': now.isoformat(), 'status': 'unavailable', 'reasons': []}
            if len(sources) > 1:
                packet['reasons'].append('multiple_sources_require_explicit_source')
                return packet
            conflicts = db.execute('SELECT bar_end FROM fund_conflicts WHERE code=? AND provider=? '
                'AND measurement_version=? AND source=? AND first_observed_at<=?',
                (str(code), provider, measurement_version, packet['source'], now.isoformat())).fetchall()
            bad = {row['bar_end'] for row in conflicts if row['bar_end'][:10] == str(day)}
            packet['conflictingTimes'] = sorted(bad)
            packet['cumulativeSeries'] = [json.loads(row['value_json']) for row in rows if row['bar_end'] not in bad]
            if packet['cumulativeSeries']:
                packet.update(status='observed', asOf=packet['cumulativeSeries'][-1]['end'])
            else:
                packet['reasons'].append('no_observed_unconflicted_history_for_requested_source')
            return packet


def archive_fund_packets(packets, cutoff, directory, *, clock, deadline, timeout=1):
    """Verify packets independently; a late failure cannot erase earlier checks.

    A packet is usable only after its own archive read has applied all known
    revision tombstones. Failed or unattempted packets stay unavailable.
    """
    started = monotonic()
    malformed = {code for code, packet in packets.items() if not isinstance(packet, dict)}
    output = {code: dict(packet) if isinstance(packet, dict) else {'metrics': {}}
              for code, packet in packets.items()}
    eligible = [code for code, packet in output.items() if packet.get('cumulativeSeries') or code in malformed]
    summary = {'status': 'not_required', 'packetCount': len(eligible),
        'savedPacketCount': 0, 'verifiedPacketCount': 0, 'failedPacketCount': 0,
        'timeoutPacketCount': 0, 'remainingPacketCount': 0, 'unverifiedPacketCount': len(eligible),
        'attemptedPacketCount': 0, 'noSeriesPacketCount': len(output)-len(eligible),
        'savedRows': 0, 'conflicts': 0, 'reasonCounts': {},
        'meaning': '逐包保存并读取已知冲突隔离；已核验部分保留，未核验部分不能参与资金窗口'}
    reasons = Counter()

    def reject(code, error, *, attempted):
        timed_out = isinstance(error, TimeoutError) or monotonic() >= deadline
        reason = 'fund_archive_deadline_exhausted' if timed_out else 'fund_archive_packet_unverified'
        packet = output[code]
        output[code] = {**packet, 'cumulativeSeries': [], 'archiveStatus': 'unavailable',
            'reasonCodes': list(dict.fromkeys([*(packet.get('reasonCodes') or []), reason]))}
        summary['failedPacketCount'] += 1
        summary['timeoutPacketCount'] += int(timed_out)
        summary['remainingPacketCount'] += int(not attempted and timed_out)
        reasons[type(error).__name__] += 1

    archive = None
    if eligible:
        try:
            archive = FundFlowArchive(directory, timeout=timeout, deadline=deadline)
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            for code in eligible:
                reject(code, error, attempted=False)
    if archive is not None:
        for code in eligible:
            if code in malformed:
                reject(code, TypeError('fund packet must be a dictionary'), attempted=False)
                continue
            if monotonic() >= deadline:
                reject(code, TimeoutError('fund archive deadline exhausted'), attempted=False)
                continue
            summary['attemptedPacketCount'] += 1
            packet = dict(output[code])
            try:
                if str(packet.get('code') or '') != str(code):
                    raise ValueError('fund packet identity does not match requested security')
                observed_at = clock()
                saved = archive.save(packet, cutoff, observed_at=observed_at)
                if saved.get('status') != 'saved':
                    raise ValueError('fund archive identity or time basis unverified')
                summary['savedPacketCount'] += 1
                summary['savedRows'] += saved.get('saved', 0)
                summary['conflicts'] += saved.get('conflicts', 0)
                frozen = archive.read(code, timestamp(cutoff).date().isoformat(), packet.get('provider'),
                    packet.get('measurementVersion'), as_of=observed_at, source=packet.get('source'))
                output[code] = {**packet, 'cumulativeSeries': frozen['cumulativeSeries'],
                    'conflictingTimes': frozen['conflictingTimes'], 'archiveStatus': 'verified'}
                summary['verifiedPacketCount'] += 1
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
                reject(code, error, attempted=True)
    summary['unverifiedPacketCount'] = len(eligible)-summary['verifiedPacketCount']
    if eligible:
        summary['status'] = ('saved' if not summary['failedPacketCount'] else
                             'partial' if summary['verifiedPacketCount'] else 'unavailable')
    summary['reasonCounts'] = dict(reasons)
    if reasons:
        summary['reason'] = next(iter(reasons))
    summary['elapsedMs'] = round((monotonic()-started)*1000)
    return output, summary
