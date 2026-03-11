#!/usr/bin/env python3
"""Wrapper entrypoint for worker state inspection."""

from __future__ import annotations

from _bootstrap import run_tool

TARGET_SCRIPT = "skynet_worker_check.py"


def main() -> int:
    return run_tool(TARGET_SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
