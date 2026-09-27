"""Frozen efinance must not create a cache inside the signed app bundle."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class EfinanceFrozenCacheTests(unittest.TestCase):
    def test_search_cache_stays_outside_package(self):
        spec = importlib.util.find_spec("efinance")
        if spec is None or not spec.submodule_search_locations:
            self.skipTest("efinance is not installed")
        source = Path(next(iter(spec.submodule_search_locations)))
        hook = Path(__file__).resolve().parents[1] / "scripts" / "pyinstaller_runtime_compat.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "efinance"
            shutil.copytree(source, package, ignore=shutil.ignore_patterns("data", "__pycache__"))
            cache = root / "user-cache"
            code = """
import runpy
import sys
from pathlib import Path

runpy.run_path(sys.argv[1])
import efinance
from efinance import config, shared, utils

cache = Path(sys.argv[2]) / 'efinance' / 'search-cache-v2.json'
assert config.SEARCH_RESULT_CACHE_PATH == str(cache)
assert shared.SEARCH_RESULT_CACHE_PATH == str(cache)
assert utils.SEARCH_RESULT_CACHE_PATH == str(cache)
utils.save_search_result('test', [])
assert cache.read_text(encoding='utf-8') == '{}'
assert not (Path(sys.argv[3]) / 'data').exists()
"""
            env = {**os.environ, "SCREENING_DATA_DIR": str(cache), "PYTHONPATH": str(root)}
            subprocess.run(
                [sys.executable, "-c", code, str(hook), str(cache), str(package)],
                cwd=root, env=env, check=True, capture_output=True, text=True, timeout=30,
            )


if __name__ == "__main__":
    unittest.main()
