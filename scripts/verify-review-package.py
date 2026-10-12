"""Check the frozen review modules and record reproducible build evidence."""
import hashlib
import json
from pathlib import Path
import re
from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parents[1]
out = root / 'output/validation'
engine = root / 'daily-engine/dist/backend/stock_analysis/stock_analysis.exe'
desktop = root / 'desktop/build/bin/Stock King.exe'
archive = CArchiveReader(str(engine))
pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.lower().endswith('.pyz')))
checks = {}
for suffix in ('local_opportunities', 'local_observation_review', 'signal_learning', 'minute_history', 'next_day_watch'):
    name = 'src.services.yao_scout.' + suffix
    frozen = pyz.extract(name)
    source = root / 'daily-engine' / Path(*name.split('.')).with_suffix('.py')
    checks[name] = frozen == compile(source.read_text(encoding='utf-8'), frozen.co_filename,
                                    'exec', dont_inherit=True, optimize=0)
assert all(checks.values()), checks
test_results = {}
for filename, expected in [('review-archive-tests.log', 39), ('review-pipeline-tests.log', 91)]:
    log = (out / filename).read_text(encoding='utf-8-sig')
    assert re.search(rf'\b{expected} passed\b', log) and not re.search(r'\d+ failed', log), filename
    test_results[filename] = expected
frontend = (out / 'review-frontend-tests.log').read_text(encoding='utf-8-sig')
assert re.search(r'\bfail\s+0\b', frontend)
test_results['frontend'] = int(re.search(r'\bpass\s+(\d+)', frontend).group(1))
assert max(path.stat().st_mtime for path in (root/'desktop/frontend/src').rglob('*') if path.is_file()) <= desktop.stat().st_mtime
result = {'frozenSourceMatches': checks, 'tests': test_results,
          'engineSha256': hashlib.sha256(engine.read_bytes()).hexdigest(),
          'desktopSha256': hashlib.sha256(desktop.read_bytes()).hexdigest(),
          'meaning': 'Software correctness checks only; no claim of improved stock prediction.'}
(out/'review-package-acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
