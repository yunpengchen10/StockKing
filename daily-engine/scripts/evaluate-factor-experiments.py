"""Evaluate an explicit JSON export of frozen factor experiments and outcomes.

Usage: python scripts/evaluate-factor-experiments.py rows.json --holdout-start YYYY-MM-DD --output report.json
Never connects to production or chooses a holdout boundary from observed results.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.yao_scout.factor_experiments import evaluate_frozen_experiments

parser = argparse.ArgumentParser()
parser.add_argument('input', type=Path)
parser.add_argument('--holdout-start', required=True)
parser.add_argument('--as-of', help='Timezone-aware outcome availability cutoff; defaults to the current time.')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
rows = json.loads(args.input.read_text(encoding='utf-8'))
result = evaluate_frozen_experiments(rows, holdout_start=args.holdout_start, as_of=args.as_of)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'status': result['status'], 'excludedDays': result['excludedDays']}, ensure_ascii=False))
