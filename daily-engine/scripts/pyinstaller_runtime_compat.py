"""Compatibility settings that must run before PyInstaller's dependency hooks."""

import os
from pathlib import Path
import sys
import types


# NLTK 3.10's CWD import guard treats PyInstaller's bundled ``_internal``
# standard-library directory as application-controlled source when the frozen
# executable starts beside that directory. The frozen importer already fixes
# the module search roots, so disable only this incompatible NLTK guard inside
# packaged binaries. This custom hook runs before ``pyi_rth_nltk``.
os.environ.setdefault("NLTK_DISABLE_IMPORT_SECURITY", "1")


def _efinance_cache_directory() -> Path:
    """Keep efinance's search cache outside the signed application bundle."""
    screening = os.environ.get("SCREENING_DATA_DIR")
    if screening:
        return Path(screening).expanduser() / "efinance"
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "Stock King" / "screening" / "efinance"


# efinance 0.5.x config creates efinance/data beside its own code on import.
# A frozen engine puts that directory inside Stock King.app and any cache write
# invalidates the app's signature. Register its small config module before the
# package is imported, so shared/utils copy the user-cache path from the start.
_efinance_cache = _efinance_cache_directory()
_efinance_cache.mkdir(parents=True, exist_ok=True)
_efinance_config = types.ModuleType("efinance.config")
_efinance_config.__path__ = []
_efinance_config.DATA_DIR = _efinance_cache
_efinance_config.SEARCH_RESULT_CACHE_PATH = str(_efinance_cache / "search-cache-v2.json")
_efinance_config.MAX_CONNECTIONS = 50
_efinance_config.SHOW_TICKFLOW_PROMPT = True
sys.modules["efinance.config"] = _efinance_config
