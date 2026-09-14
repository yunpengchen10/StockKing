"""Startup hook for the API-only Stock King sidecar.

The user interface is built and embedded by Wails. The Python process has no
second frontend bundle to install, but ``main`` keeps this hook so older
callers and scheduler tests retain a stable startup contract.
"""

from __future__ import annotations


def prepare_desktop_assets() -> bool:
    """Confirm that no additional sidecar UI assets are required."""
    return True
