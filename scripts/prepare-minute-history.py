"""Prepare verified historical minutes outside a live scan; no signal-ledger edits."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daily-engine'))
from src.services.yao_scout.history_preparation import prepare_history, read_preparation_status
from src.services.yao_scout.intraday_evidence import SHANGHAI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-dir', required=True)
    parser.add_argument('--codes', nargs='+', default=[])
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--max-codes', type=int, default=20)
    parser.add_argument('--max-pages-per-code', type=int, default=3)
    parser.add_argument('--target-session', help='Optional next exchange day; evidence cutoff remains the real current time')
    parser.add_argument('--allow-tainted-rebuild', action='store_true')
    parser.add_argument('--status', action='store_true', help='Read the cached progress only; no network or archive writes')
    args = parser.parse_args()
    now = datetime.now(SHANGHAI)
    if args.status:
        result = read_preparation_status(args.archive_dir, now)
    else:
        if not args.codes:
            parser.error('--codes is required to prepare history')
        result = prepare_history(args.codes, now, args.archive_dir, timeout=args.timeout,
            max_codes=args.max_codes, max_pages_per_code=args.max_pages_per_code,
            allow_tainted_rebuild=args.allow_tainted_rebuild, target_session=args.target_session)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
