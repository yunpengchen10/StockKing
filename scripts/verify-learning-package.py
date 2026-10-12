"""Verify that a release contains the tested learning source, without user data."""
import hashlib
import json
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parents[1]
engine = root / "daily-engine/dist/backend/stock_analysis/stock_analysis.exe"
desktop = root / "desktop/build/bin/Stock King.exe"
archive = CArchiveReader(str(engine))
pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.lower().endswith(".pyz")))
checks = {}
for suffix in (
    "local_algorithm", "local_opportunities", "local_observation_review", "signal_learning",
    "task_learning", "decision_policy", "research_policy", "minute_integrity", "minute_history",
    "next_day_watch", "recommendation_evidence", "sector_fund_evidence",
):
    name = "src.services.yao_scout." + suffix
    frozen = pyz.extract(name)
    source = root / "daily-engine" / Path(*name.split(".")).with_suffix(".py")
    checks[name] = frozen == compile(source.read_text(encoding="utf-8"), frozen.co_filename,
                                    "exec", dont_inherit=True, optimize=0)
assert all(checks.values()), checks
assert max(p.stat().st_mtime for p in (root / "desktop/frontend/src").rglob("*") if p.is_file()) <= desktop.stat().st_mtime
result = {
    "version": "2.7.0", "frozenSourceMatches": checks,
    "engineSha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
    "desktopSha256": hashlib.sha256(desktop.read_bytes()).hexdigest(),
    "meaning": "Package/source integrity only; not evidence of predictive performance.",
}
output = root / "output/validation/v270-package-integrity.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
