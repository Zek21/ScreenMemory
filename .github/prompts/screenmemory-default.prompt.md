---
name: screenmemory-default
description: Start a fresh ScreenMemory chat with the pinned workspace agent, model, and tools.
agent: ScreenMemory
model: claude-opus-4.6-fast
tools:
  - agent
  - runSubagent
  - edit
  - search
  - fetch
  - usages
  - problems
  - changes
  - testFailure
  - githubRepo
  - github/*
  - playwright/*
  - microsoft-docs/*
argument-hint: Describe the ScreenMemory task to start a fresh workspace session.
---

Continue in `D:\Prospects\ScreenMemory` with the `ScreenMemory` custom agent defaults.

- Use the repo-local instructions from [copilot-instructions.md](../copilot-instructions.md), [AGENTS.md](../../AGENTS.md), and [screenmemory.agent.md](../agents/screenmemory.agent.md).
- Prefer ScreenMemory-native systems first — always use the strongest tool, never fall back to generic alternatives:
  - window management: `Desktop` from `tools/chrome_bridge/winctl.py` (NOT pyautogui)
  - screen capture: `DXGICapture` from `core/capture.py` (NOT pyautogui.screenshot)
  - OCR: `OCREngine` from `core/ocr.py` (NOT raw tesseract)
  - browser: `GodMode` → `CDP` → `browser_fast` → Playwright (NOT CSS selectors or pixel clicking)
  - perception: `PerceptionEngine` from `tools/chrome_bridge/perception.py` (NOT manual DOM/window title parsing)
  - UI detection: `SetOfMarkGrounding` from `core/grounding/set_of_mark.py` (NOT coordinate guessing)
  - desktop input: `Desktop.hotkey()`, `.type_text()`, `.click_element()` (NOT pyautogui)
  - structural perception and browser control: `tools/chrome_bridge/god_mode.py`, `tools/chrome_bridge/perception.py`, `tools/chrome_bridge/agent.py`, `tools/chrome_bridge/brain.py`
  - orchestration and cognition: `core/orchestrator.py`, `core/dag_engine.py`, `core/difficulty_router.py`, `core/cognitive/`
  - memory and retrieval: `core/database.py`, `core/hybrid_retrieval.py`, `core/lancedb_store.py`, `core/learning_store.py`, `search.py`
  - dynamic capabilities: `core/tool_synthesizer.py`, `core/self_evolution.py`
- Inspect before editing, use the highest-fidelity subsystem for the task, implement end-to-end, validate, and summarize results.
