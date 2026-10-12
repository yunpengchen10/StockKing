"""Inspect or rehearse a minute-data repair; --apply always makes a SQLite backup.

No network calls. A plan contains explicit quarantine or verified full-session
rebuild operations. Rebuilds need independent evidence and expected revisions.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daily-engine'))
from src.services.yao_scout.minute_history import MinuteArchive, read_archive_lineage, SHANGHAI


def copy_database(source, destination):
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as old:
        with closing(sqlite3.connect(destination)) as new:
            old.backup(new)


def execute_plan(directory, operations, cutoff):
    archive = MinuteArchive(directory)
    results = []
    for item in operations:
        code, day = str(item['code']), str(item['day'])
        if item['operation'] == 'quarantine':
            if not item.get('reason'):
                raise ValueError('quarantine requires a reason')
            results.append(archive.quarantine(code, day, item['reason'], evidence=item.get('evidence')))
        elif item['operation'] == 'rebuild':
            results.append(archive.rebuild(code, day, item['rows'], cutoff,
                verification=item['verification'], expected_revision=item['expected_revision']))
        else:
            raise ValueError('unsupported repair operation')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-dir', required=True)
    parser.add_argument('--code')
    parser.add_argument('--day', action='append')
    parser.add_argument('--plan')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir')
    args = parser.parse_args()
    directory = Path(args.archive_dir).resolve()
    source = directory / 'minute-bars-v2.sqlite3'
    if not source.is_file():
        raise ValueError('minute archive does not exist')
    before = read_archive_lineage(directory, args.code, args.day)
    result = {'mode':'read_only', 'before':before, 'applied':False}
    if args.apply and (not args.plan or not args.backup_dir):
        raise ValueError('--apply requires an explicit --plan and --backup-dir')
    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding='utf-8-sig'))
        operations = plan.get('operations', [])
        if not operations:
            raise ValueError('repair plan has no operations')
        cutoff = datetime.fromisoformat(plan.get('as_of') or datetime.now(SHANGHAI).isoformat())
        if cutoff.tzinfo is None or cutoff > datetime.now(SHANGHAI):
            raise ValueError('repair cutoff requires a non-future timezone-aware time')
        # Validate the complete plan against a consistent isolated snapshot.
        with tempfile.TemporaryDirectory(prefix='stock-king-minute-repair-') as work:
            copy_database(source, Path(work) / source.name)
            result['rehearsal'] = execute_plan(work, operations, cutoff)
        result['mode'] = 'dry_run'
        if args.apply:
            backup_dir = Path(args.backup_dir).resolve()
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / ('minute-bars-before-repair-' + datetime.now().strftime('%Y%m%d-%H%M%S')
                                   + '-' + uuid.uuid4().hex[:8] + '.sqlite3')
            copy_database(source, backup)
            result['backup'] = str(backup)
            # Rebuild operations recheck expected revisions against the live
            # database. Any partial operation remains auditable and recoverable.
            result['results'] = execute_plan(directory, operations, cutoff)
            result.update(mode='applied_with_backup', applied=True)
            result['after'] = read_archive_lineage(directory, args.code, args.day)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
