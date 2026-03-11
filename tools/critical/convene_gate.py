#!/usr/bin/env python3
"""Wrapper entrypoint for the convene gate monitor."""

from __future__ import annotations

from _bootstrap import run_tool

TARGET_SCRIPT = "convene_gate.py"


def main() -> int:
    return run_tool(TARGET_SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
