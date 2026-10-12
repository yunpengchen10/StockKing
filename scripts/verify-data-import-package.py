"""Verify the installed build candidate contains the tested data-ingestion code."""
import hashlib
import json
from pathlib import Path
import re
from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parents[1]
engine = root/'daily-engine/dist/stock_analysis/stock_analysis.exe'
desktop = root/'desktop/build/bin/Stock King.exe'
archive = CArchiveReader(str(engine))
pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.lower().endswith('.pyz')))
names = ['public_market_quotes', 'eastmoney_context', 'akshare_context',
         *['yao_scout.' + name for name in ('minute_history', 'signal_learning', 'precision_policy',
           'local_algorithm', 'local_opportunities', 'local_observation_review', 'sector_fund_evidence')]]
checks = {}
for suffix in names:
    name = 'src.services.' + suffix
    frozen = pyz.extract(name)
    path = root/'daily-engine'/Path(*name.split('.')).with_suffix('.py')
    checks[name] = frozen == compile(path.read_text(encoding='utf-8'), frozen.co_filename,
                                    'exec', dont_inherit=True, optimize=0)
assert all(checks.values()), checks
log = (root/'output/validation/data-import-regression.log').read_text(encoding='utf-8-sig')
assert not re.search(r'\d+ failed', log)
passed = int(re.search(r'(\d+) passed', log).group(1))
assert passed >= 279
assert max(p.stat().st_mtime for p in (root/'desktop/backend/marketminute').glob('*.go')) <= desktop.stat().st_mtime
report = {'frozenSourceMatches': checks, 'pythonRegressionPassed': passed,
          'engineSha256': hashlib.sha256(engine.read_bytes()).hexdigest(),
          'desktopSha256': hashlib.sha256(desktop.read_bytes()).hexdigest()}
(root/'output/validation/data-import-package-verification.json').write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
