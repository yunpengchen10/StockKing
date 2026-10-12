"""Check the compiled repair against source and recorded acceptance results."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parents[1]
out = root / 'output/validation'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prefix', default='navigation-nextday')
parser.add_argument('--test-results', default='next-day-test-results.json')
parser.add_argument('--frontend-log', default='navigation-frontend-tests.log')
parser.add_argument('--ui-results', default='navigation-retention/results.json')
parser.add_argument('--go-log', default='navigation-go-tests.log')
args = parser.parse_args()
engine = root / 'daily-engine/dist/backend/stock_analysis/stock_analysis.exe'
desktop = root / 'desktop/build/bin/Stock King.exe'
archive = CArchiveReader(str(engine))
pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.lower().endswith('.pyz')))
modules = ['stock_king_display', 'akshare_context'] + ['yao_scout.' + name for name in (
    'labels', 'local_algorithm', 'local_opportunities', 'minute_history', 'model',
    'precision_policy', 'recommendation_evidence', 'research_factors',
    'research_policy', 'signal_learning', 'theme_evidence', 'v11_factors', 'next_day_watch')]
checks = {}
for suffix in modules:
    name = 'src.services.' + suffix
    frozen = pyz.extract(name)
    source = root / 'daily-engine' / Path(*name.split('.')).with_suffix('.py')
    checks[name] = frozen == compile(source.read_text(encoding='utf-8'), frozen.co_filename,
                                    'exec', dont_inherit=True, optimize=0)
assert all(checks.values()), checks
tests = json.loads((out / args.test_results).read_text(encoding='utf-8'))
assert tests['exitCode'] == 0 and tests['failed'] == 0
frontend_log = (out / args.frontend_log).read_text(encoding='utf-8-sig')
counts = {key: int(re.search(r'\b' + key + r'\s+(\d+)', frontend_log).group(1))
          for key in ('tests', 'pass', 'fail')}
assert counts['fail'] == 0 and counts['tests'] == counts['pass']
ui = json.loads((out / args.ui_results).read_text(encoding='utf-8'))
assert not ui['errors'] and len(ui['checks']) >= 6
go = (out / args.go_log).read_text(encoding='utf-8-sig')
assert len(re.findall(r'^ok\s', go, re.M)) == 2 and 'FAIL' not in go
frontend = root / 'desktop/frontend'
assert max(path.stat().st_mtime for path in (frontend / 'src').rglob('*') if path.is_file()) <= desktop.stat().st_mtime
assets = sorted((frontend / 'dist').rglob('*'))
asset_hashes = {str(path.relative_to(frontend / 'dist')): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in assets if path.is_file()}
result = {
    'frozenSourceMatches': checks, 'pythonTests': tests['tests'], 'frontendTests': counts,
    'browserChecks': ui['checks'], 'goPackagesPassed': 2, 'frontendAssets': asset_hashes,
    'backendExeSha256': hashlib.sha256(engine.read_bytes()).hexdigest(),
    'desktopExeSha256': hashlib.sha256(desktop.read_bytes()).hexdigest(),
    'replay': tests.get('postFreezeReadOnlyReplay'),
    'performanceClaim': 'unvalidated_rules; replay checks coverage, not future prediction accuracy',
}
(out / (args.prefix + '-acceptance.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({key: result[key] for key in ('pythonTests', 'frontendTests', 'goPackagesPassed',
                                             'backendExeSha256', 'desktopExeSha256')}, ensure_ascii=False))
