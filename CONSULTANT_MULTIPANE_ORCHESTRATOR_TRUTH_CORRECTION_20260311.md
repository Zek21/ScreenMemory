# Consultant Multi-Pane Orchestrator Truth Correction — 2026-03-11

## Purpose

Correct a second-layer truth failure discovered after the initial consultant remediation: the orchestrator was not missing as a session. It was present as the left pane inside a shared top-level VS Code window, and I incorrectly reasoned at the whole-window level.

## Corrected Finding

The real orchestrator session is the left `ORCHESTRATOR-START` pane inside top-level VS Code window `HWND 67568`.

That same top-level window also contains:

- a middle Gemini `gc-start` consultant pane
- a right Codex pane

The top-level window title and whole-window model/agent scan were therefore not reliable session identity signals.

## What Went Wrong

I made a deeper mistaken assumption:

- I treated one top-level HWND as one session identity

That assumption is false in this layout.

The screenshot and pane-level UIA probe show that one top-level VS Code window can host multiple simultaneous session identities.

## Live Pane-Level Evidence

For `HWND 67568`, the pane-level probe found the left pane controls:

- `Pick Model, Claude Opus 4.6 (fast mode)`
- `Delegate Session - Copilot CLI`
- orchestrator-style transcript markers such as:
  - `Skynet Mass Dispatch Operation`
  - `all 4 workers`
  - `wait-all`
  - `dispatch`
  - `files changed`
  - `Apply`

At the same time, the same top-level window also exposed middle/right-pane consultant content, including `gc-start` and Gemini-local signals.

That is why whole-window scanning produced contradictory results.

## Root Cause

The routing and verification logic was still too coarse:

1. `resolve_orchestrator_hwnd()` reasoned mainly at the top-level window level
2. whole-window UIA model/agent scans could read the active middle pane
3. whole-window reject markers could overrule the actual left orchestrator pane

## Required Protocol Correction

When a top-level Code window is shared across multiple panes, consultants and routing code must:

1. identify pane-local session controls
2. evaluate pane-local model and agent
3. evaluate pane-local transcript markers
4. prefer pane-local identity over whole-window title or active-pane drift

## Structural Improvement Implemented

`tools/skynet_delivery.py` was updated so orchestrator resolution now includes left-pane fingerprinting for shared windows. Whole-window reject signals no longer automatically win when the left pane has strong orchestrator evidence.

## Runtime Truth After Fix

After the pane-aware resolver update:

- `resolve_orchestrator_hwnd()` resolves `67568`
- `data/orchestrator.json` was corrected to reflect the shared-window orchestrator truth
- consultant self-invocation protocols were updated to explicitly require pane-local inspection in multi-pane windows

## Final Truth

The user was right again.

The earlier statement that the left-side window did not validate as the orchestrator was not truthful enough because it reasoned at the wrong layer. The correct statement is:

- the orchestrator was present
- it was visible as the left `ORCHESTRATOR-START` pane
- my logic was wrong because I treated the whole top-level window as a single session identity

This artifact exists so that future consultant verification and self-prompt routing operate at pane granularity when the UI layout demands it.
