---
name: Local Tools
description: Guidance for files under tools/
applyTo: "tools/**/*.py"
---

# ScreenMemory tool conventions

- Reuse existing scripts under `tools/` instead of creating duplicate wrappers.
- Preserve command-line interfaces for tool scripts unless the task explicitly calls for a breaking change.
- For `tools/chrome_bridge`, consult `DECISION_TREE.md` before editing behavior and use `FUNCTION_MAP.md` to confirm available APIs.
- Keep automation changes deterministic and favor explicit flags over hidden environment assumptions.
- **Chrome Bridge is the PRIMARY browser automation tool.** `tools/chrome_bridge/` (GodMode → CDP → browser_fast) takes priority over Playwright MCP for all browser automation tasks. Playwright is a last resort only — use it when Chrome Bridge cannot reach the target. Never default to Playwright when GodMode or CDP can do the job.
- **Orchestrator compliance:** The orchestrator NEVER edits tool files directly. All tool changes MUST be dispatched to workers via `skynet_dispatch.py`. Workers execute, validate, and report back via the bus.
