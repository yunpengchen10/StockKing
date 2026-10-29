"""Verify that a release contains the tested learning source, without user data."""
import argparse
import hashlib
import json
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path)
parser.add_argument('--engine', type=Path,
                    default=root / 'daily-engine/dist/backend/stock_analysis/stock_analysis.exe',
                    help='Frozen engine executable to compare with this checkout.')
parser.add_argument('--desktop', type=Path,
                    default=root / 'desktop/build/bin/Stock King.exe',
                    help='Desktop executable paired with the frozen engine.')
args = parser.parse_args()
engine = args.engine.resolve(strict=True)
desktop = args.desktop.resolve(strict=True)
archive = CArchiveReader(str(engine))
pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.lower().endswith(".pyz")))
checks = {}
for suffix in (
    "local_algorithm", "local_opportunities", "local_observation_review", "signal_learning",
    "opportunity_channels", "opportunity_discovery", "discovery_snapshot",
    "task_learning", "decision_policy", "research_policy", "minute_integrity", "minute_history",
    "next_day_watch", "recommendation_evidence", "sector_fund_evidence", "morning_task", "primary_policy",
    "precision_policy", "research_factors", "premarket", "daily_primary", "admission_diagnostics",
    "history_preparation", "history_maintenance", "v11_factors", "trend_evidence",
    "scan_routing", "daily_observation_outcomes", "daily_observation_queue",
    "learning_contracts", "entry_execution_policy", "factor_experiments", "primary_readiness",
):
    name = "src.services.yao_scout." + suffix
    frozen = pyz.extract(name)
    source = root / "daily-engine" / Path(*name.split(".")).with_suffix(".py")
    checks[name] = frozen == compile(source.read_text(encoding="utf-8"), frozen.co_filename,
                                    "exec", dont_inherit=True, optimize=0)
for name in ('src.services.software_market', 'src.services.stock_king_display', 'src.services.akshare_context', 'src.storage',
             'api.v1.endpoints.stock_king'):
    frozen = pyz.extract(name)
    source = root / 'daily-engine' / Path(*name.split('.')).with_suffix('.py')
    checks[name] = frozen == compile(source.read_text(encoding='utf-8'), frozen.co_filename,
                                    'exec', dont_inherit=True, optimize=0)
assert all(checks.values()), checks
assert max(p.stat().st_mtime for p in (root / "desktop/frontend/src").rglob("*") if p.is_file()) <= desktop.stat().st_mtime
result = {
    "version": json.loads((root / "desktop/wails.json").read_text(encoding="utf-8"))["info"]["productVersion"], "frozenSourceMatches": checks,
    "engineSha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
    "desktopSha256": hashlib.sha256(desktop.read_bytes()).hexdigest(),
    "meaning": "Package/source integrity only; not evidence of predictive performance.",
}
output = args.output or root / "output/validation/package-integrity.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
