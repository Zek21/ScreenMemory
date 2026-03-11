#!/usr/bin/env python3
"""Wrapper entrypoint for hanging draft inspection."""

from __future__ import annotations

from _bootstrap import run_tool

TARGET_SCRIPT = "skynet_draft_inspector.py"


def main() -> int:
    return run_tool(TARGET_SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
