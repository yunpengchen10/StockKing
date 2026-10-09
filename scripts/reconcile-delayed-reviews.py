"""Reconcile mature reviews from archived evidence; never fetch or train.

The default is a database-copy dry run. --apply first creates a consistent
SQLite backup, then updates only the review ledger with the actual current time.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daily-engine'))
from src.services.yao_scout.signal_learning import SignalLearningService


def snapshot(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        states = dict(db.execute('SELECT status,count(*) FROM signal_outcomes GROUP BY status'))
        observed = db.execute("SELECT count(*) FROM signal_outcomes WHERE json_extract(details_json,'$.observedReturn') IS NOT NULL").fetchone()[0]
        models = db.execute('SELECT count(*) FROM model_versions').fetchone()[0]
        training = db.execute('SELECT count(*) FROM signal_events WHERE training_eligible=1').fetchone()[0]
        next_day = db.execute("SELECT count(*) FROM sqlite_master WHERE name='next_day_outcomes'").fetchone()[0]
        return {'outcomeStates': states, 'priceObservations': observed, 'models': models,
                'trainingEligibleSignals': training,
                'nextDayOutcomes': db.execute('SELECT count(*) FROM next_day_outcomes').fetchone()[0] if next_day else 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root, output = args.data_dir.resolve(), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = root / 'signal-learning-v1.sqlite3'
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    tag = now.strftime('%Y%m%d-%H%M%S')
    backup = output / f'review-ledger-{tag}.sqlite3'
    # SQLite backup includes committed WAL content while readers remain online.
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(backup) as dest:
        src.backup(dest)
    target = source if args.apply else backup
    before = snapshot(target)
    service = SignalLearningService(root, db_path=target, clock=lambda: now)
    result = service.review_due(now)
    after = snapshot(target)
    assert after['models'] == before['models'], 'Reconciliation must not train or publish models'
    assert after['trainingEligibleSignals'] == before['trainingEligibleSignals'], 'Historical eligibility must remain unchanged'
    visible = service.reviews({'limit': 5000})
    selected = [row for row in visible['items'] if row['selected'] and row['horizon'] == 1]
    receipt = {'mode': 'applied' if args.apply else 'dry_run_copy', 'asOf': now.isoformat(),
               'source': str(source), 'backup': str(backup), 'before': before, 'after': after,
               'updated': result['updated'], 'selectedT1': selected,
               'nextDay': {'count': visible['nextDayReview']['count'],
                           'statusCounts': visible['nextDayReview']['statusCounts']},
               'meaning': 'Existing archived prices only; no new historical signals, trades or model learning.'}
    path = output / f'review-reconciliation-{tag}.json'
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({**{key: receipt[key] for key in ('mode', 'asOf', 'before', 'after', 'updated')},
                      'receipt': str(path)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
