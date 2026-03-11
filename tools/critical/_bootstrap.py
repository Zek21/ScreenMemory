#!/usr/bin/env python3
"""Shared launcher for critical tool wrappers."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_tool(script_name: str) -> int:
    sys.path.insert(0, str(ROOT))
    runpy.run_path(str(ROOT / "tools" / script_name), run_name="__main__")
    return 0
