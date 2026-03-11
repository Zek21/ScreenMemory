#!/usr/bin/env python3
"""Wrapper entrypoint for the stuck worker detector."""

from __future__ import annotations

from _bootstrap import run_tool

TARGET_SCRIPT = "skynet_stuck_detector.py"


def main() -> int:
    return run_tool(TARGET_SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
