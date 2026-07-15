from __future__ import annotations

import sys
from pathlib import Path


def resource_path(relative: str) -> Path:
    """Resolve a bundled PyInstaller data file or a source-tree file."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative
