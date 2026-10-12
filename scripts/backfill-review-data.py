"""Import verified missing minutes and recompute reviews, preserving frozen signals.

Default: consistent database copies. --apply backs up both live databases first.
Public source packets may be supplied, or --network fetches the pending dates.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daily-engine'))
from src.services.yao_scout.minute_history import MinuteArchive, archive_universe, normalize_bars
from src.services.yao_scout.signal_learning import SignalLearningService


def connect(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)


def counts(path):
    with connect(path) as db:
        rows = db.execute("SELECT e.selected,o.status,count(*),sum(json_extract(o.details_json,'$.observedReturn') IS NOT NULL) "
                          "FROM signal_outcomes o JOIN signal_events e USING(signal_id) GROUP BY e.selected,o.status")
        return [{'selected': bool(s), 'status': status, 'records': n, 'priceObservations': m}
                for s, status, n, m in rows]


def frozen_digest(path):
    digest = hashlib.sha256()
    with connect(path) as db:
        for row in db.execute('SELECT signal_id,snapshot_json,features_json,training_eligible FROM signal_events ORDER BY signal_id'):
            digest.update(json.dumps(row, ensure_ascii=False).encode('utf-8'))
        models = db.execute('SELECT count(*) FROM model_versions').fetchone()[0]
    return digest.hexdigest(), models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--archive-dir', type=Path,
                        help='Import a verified staging minute archive, preserving existing observations')
    parser.add_argument('--history', action='store_true', help='Also import earlier dated minutes for the 20-session baseline')
    parser.add_argument('--codes-file', type=Path, help='Optional JSON list limiting a follow-up import to specific stocks')
    parser.add_argument('--network', action='store_true')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.evidence_dir and not args.archive_dir and not args.network:
        parser.error('Choose --evidence-dir, --archive-dir or --network')
    root = args.data_dir.resolve()
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    output = args.output_dir.resolve() / now.strftime('backfill-%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=False)
    for relative in ('signal-learning-v1.sqlite3', 'minute_history/minute-bars-v2.sqlite3'):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with connect(root / relative) as source, sqlite3.connect(target) as dest:
            source.backup(dest)
    target_root = root if args.apply else output
    ledger = target_root / 'signal-learning-v1.sqlite3'
    before, frozen = counts(ledger), frozen_digest(ledger)
    service = SignalLearningService(target_root, clock=lambda: now)
    requirements = service.review_requirements(now)
    archive = MinuteArchive(target_root / 'minute_history')
    imported = []
    allowed_codes = set(json.loads(args.codes_file.read_text(encoding='utf-8'))) if args.codes_file else None
    if args.archive_dir:
        source_path = args.archive_dir.resolve() / 'minute_history/minute-bars-v2.sqlite3'
        import zlib
        with connect(source_path) as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            for code in requirements:
                rows = []
                for day, payload in db.execute('SELECT day,payload FROM sessions WHERE code=? ORDER BY day', (code,)):
                    if day in requirements[code]:
                        rows.extend(json.loads(zlib.decompress(payload)))
                if rows:
                    merged = archive.save(code, rows, now)
                    imported.append({'code': code, 'sourceArchive': str(source_path), 'merge': merged,
                                     'addedMinutes': []})
    if args.evidence_dir:
        packets = [*args.evidence_dir.glob('sina-??????.json'), *args.evidence_dir.glob('mac-??????.json')]
        for packet_path in sorted(packets):
            packet = json.loads(packet_path.read_text(encoding='utf-8'))
            code = packet['code']
            if (code not in requirements and not args.history) or (allowed_codes is not None and code not in allowed_codes):
                continue
            fetched = datetime.fromisoformat(packet['fetchedAt'])
            if fetched.tzinfo is None or fetched > now:
                raise ValueError('Invalid source acquisition time')
            rows = normalize_bars(packet['normalized_bars'], now)
            old = normalize_bars(archive.read(code, now), now)
            eligible = [dict(row, fetched_at=packet['fetchedAt']) for stamp, row in rows.items()
                        if args.history or stamp.date().isoformat() in requirements[code]]
            added = [row for row in eligible if datetime.fromisoformat(row['end']) not in old]
            merged = archive.save(code, eligible, now)
            imported.append({'code': code, 'sourcePacket': str(packet_path.resolve()),
                             'packetSha256': hashlib.sha256(packet_path.read_bytes()).hexdigest(),
                             'source': packet['source'], 'fetchedAt': packet['fetchedAt'],
                             'merge': merged, 'addedMinutes': [row['end'] for row in added]})
    network = None
    if args.network:
        network = archive_universe(requirements, now, target_root / 'minute_history',
            include_registered=False, timeout=args.timeout, count=1970, required_days=requirements)
    review = service.review_due(now)
    assert frozen_digest(ledger) == frozen, 'Frozen signals, training eligibility or models changed'
    checks = {}
    for relative in ('signal-learning-v1.sqlite3', 'minute_history/minute-bars-v2.sqlite3'):
        with connect(target_root / relative) as db:
            checks[relative] = db.execute('PRAGMA quick_check').fetchone()[0]
    assert set(checks.values()) == {'ok'}
    selected = [row for row in service.reviews({'limit': 5000})['items']
                if row['selected'] and row['horizon'] == 1]
    receipt = {'mode': 'applied' if args.apply else 'dry_run_copy', 'asOf': now.isoformat(),
               'dataDirectory': str(root), 'databaseBackupDirectory': str(output),
               'before': before, 'after': counts(ledger), 'imports': imported,
               'networkImport': network, 'reviewUpdated': review['updated'],
               'selectedT1': selected, 'frozenSignalsUnchanged': True, 'integrity': checks}
    path = output / 'receipt.json'
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'mode': receipt['mode'], 'before': before, 'after': receipt['after'],
                      'addedMinutes': sum(len(item['addedMinutes']) for item in imported),
                      'reviewUpdated': review['updated'], 'receipt': str(path)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
