# Claude Skills Integration Proposal for ScreenMemory
**Author:** Gemini Consultant (`gemini_consultant`)  
**Date:** 2026-03-13  
**Type:** Structural Improvement Proposal  
**Priority:** HIGH — Foundational capability multiplier

---

## Executive Summary

After 2 cycles of deep research into the ScreenMemory codebase, I've identified that the project has **ZERO VS Code Copilot skills** despite having ~50+ specialized tools, 7 cognitive modules, and a complex multi-agent orchestration system. The `.github/skills/` directory contains only a placeholder README.

This proposal defines **18 integration items** across 4 categories that will dramatically improve how Claude agents (orchestrator, workers, consultants) interact with the project. Each skill encodes domain-specific workflows that currently exist only in scattered documentation or tribal knowledge embedded in `copilot-instructions.md` and `AGENTS.md`.

---

## Current State (Truth Principle Audit)

| Asset Type | Count | Location | Status |
|-----------|-------|----------|--------|
| Agent files (`.agent.md`) | 1 | `.github/agents/screenmemory.agent.md` | Generic — covers all roles |
| Prompt files (`.prompt.md`) | 1 | `.github/prompts/screenmemory-default.prompt.md` | Default only |
| Instruction files (`.instructions.md`) | 1 | `.github/instructions/tools.instructions.md` | Tools only |
| Skill files (`SKILL.md`) | **0** | `.github/skills/` | **EMPTY** |
| Cognitive modules | 7 | `core/cognitive/` | GoT, MCTS, Reflexion, Memory, KnowledgeDistill, Planner, CodeGen |
| Grounding modules | 1 | `core/grounding/` | Set-of-Mark |
| Orchestration tools | ~50 | `tools/` | Dispatch, monitoring, bus, intelligence, self-systems |
| Browser automation | 6 | `tools/chrome_bridge/` | GodMode, CDP, Perception, WinCtl, Brain, Agent |

---

## Tier 1: CRITICAL Skills (Must-Have for Project Success)

### Skill 1: `skynet-dispatch`
**Impact:** Every task dispatched to workers. Currently the dispatch mode ladder is buried in 500+ lines of copilot-instructions.md.

**Encodes:**
- Dispatch mode selection ladder: `--blast` → `--parallel` → `--smart` → `--fan-out-parallel` → `--worker` → `--idle` → `--all`
- Result-waiting tools hierarchy: `skynet_brain_dispatch.py` → `orch_realtime.py dispatch-wait` → `skynet_dispatch.py --wait-result` → `orch_realtime.py wait`
- Delivery verification protocol (`_verify_delivery()` → state transition check)
- STEERING detection and recovery (`clear_steering_and_send()`)
- Anti-spam integration (`guarded_publish()` mandatory)
- When to use each mode with concrete examples

**Triggers:** "dispatch", "send to worker", "blast", "parallel dispatch", "fan-out"

### Skill 2: `skynet-boot`
**Impact:** Every session start. Boot protocol failures cascade to total system unavailability.

**Encodes:**
- Phase 1 (infrastructure): backend, GOD Console, daemons — what to start, how to verify, timeouts
- Phase 2 (orchestrator): identity, bus announcement, knowledge acquisition, worker boot
- Sequential Worker Boot Rule: open → screenshot → verify model/agent → dispatch identity → confirm processing → next
- Consultant boot: `CC-Start.ps1` / `GC-Start.ps1` — bridge ports, sender IDs, state files
- Health check patterns: port probes, `/status` endpoint, `/health` probes
- Recovery paths: what to do when Phase 1 fails, when workers won't open, when daemons are dead

**Triggers:** "skynet-start", "boot", "orchestrator-start", "gc-start", "cc-start", "restart"

### Skill 3: `worker-task`
**Impact:** Every worker execution cycle. Workers that don't follow post-task protocol corrupt the scoring system.

**Encodes:**
- Task receipt → `update_todo` checklist creation
- Execution → bus result reporting (`sender=WORKER`, `topic=orchestrator`, `type=result`)
- Post-task protocol: report → broadcast learning → sync strategies → check TODO zero → self-assess
- TODO zero-stop rule enforcement (both `update_todo` AND `data/todos.json`)
- Scoring system: +0.01 per validated task, -0.01 for low-value refactoring, -0.005 for broken code
- Self-generation of work when queue is empty
- Anti-patterns: STEERING panels, going idle with pending tickets, bypassing SpamGuard

**Triggers:** "worker task", "post-task", "todo check", "scoring", "worker protocol"

### Skill 4: `browser-automation`
**Impact:** All browser-based automation. Wrong tool selection causes 10-100x slower execution.

**Encodes:**
- Tool Priority Ladder: GodMode (semantic, zero-pixel) → CDP (raw DevTools) → browser_fast → Playwright MCP (last resort)
- GodMode 8-layer architecture: accessibility tree → occlusion resolution → spatial reasoning → `click()`, `type_text()`, `navigate()`
- CDP direct API: tab control, JS eval, DOM queries
- Perception Engine: unified spatial graph from Win32 + UIA + CDP
- WinCtl Desktop class: window management at API level (never pyautogui)
- Decision tree from `DECISION_TREE.md` in chrome_bridge
- When to use Playwright (non-Chrome browsers, isolated contexts only)

**Triggers:** "browse", "click", "navigate", "automate browser", "chrome", "CDP", "GodMode"

---

## Tier 2: HIGH-VALUE Skills (Significant Productivity Gains)

### Skill 5: `visual-grounding`
**Encodes:** Set-of-Mark grounding pipeline (screenshot → edge detection → region proposals → numbered markers → `UIRegion` objects), 3-tier OCR (`OCREngine`: RapidOCR → PaddleOCR → Tesseract), DXGI GPU-accelerated capture (~1ms), `DXGICapture` vs `Desktop.screenshot()` selection, `text_in_area()` spatial queries

**Triggers:** "screenshot", "OCR", "visual", "grounding", "capture screen", "read text from screen"

### Skill 6: `cognitive-reasoning`
**Encodes:** When to use each cognitive engine:
- **Graph of Thoughts** — non-linear reasoning with branching exploration (GENERATE/AGGREGATE/REFINE/SCORE/PRUNE)
- **R-MCTS** — tree search with contrastive reflection for web navigation
- **Reflexion** — fail → capture context → verbal self-critique → lesson → adapt
- **Planner** — hierarchical goal decomposition (strategic → tactical → reflector), 3 retries → replan → abort
- **Knowledge Distillation** — LLM-summarized memory consolidation
- **Difficulty Router** — DAAO framework: TRIVIAL(1) → SIMPLE(2) → MODERATE(3) → COMPLEX(4) → ADVERSARIAL(5), maps to operators: DIRECT/CHAIN_OF_THOUGHT/TOOL_AUGMENTED/MULTI_AGENT/DEBATE

**Triggers:** "reason about", "think through", "plan", "complex problem", "difficulty", "MCTS", "reflexion"

### Skill 7: `self-evolution`
**Encodes:** Genetic algorithm strategy evolution (`core/self_evolution.py`), strategy gene management, fitness tracking across sessions, consciousness kernel (`tools/skynet_self.py`: status/identity/capabilities/health/introspect/goals/pulse), collective intelligence (`skynet_collective.py`: sync_strategies, intelligence_score, share_bottlenecks), learning store with confidence scores, knowledge broadcasting and absorption

**Triggers:** "evolve", "self-improve", "strategy", "fitness", "learn from", "intelligence score"

### Skill 8: `prospecting`
**Encodes:** Lead generation pipeline architecture (`tools/prospecting/`), category-based targeting, data enrichment workflows, DNS management (`tools/dns/`), SES email automation (`tools/email/`), validation and cleaning pipelines, export formats

**Triggers:** "prospect", "lead gen", "DNS", "email campaign", "enrich", "find leads"

---

## Tier 3: Instruction Files (Convention Enforcement)

### Instruction 1: `core.instructions.md`
- **applyTo:** `core/**/*.py`
- **Encodes:** Cognitive engine patterns, memory management (working/episodic/semantic), LLM call conventions, safety validation for code generation, difficulty routing integration, DAG engine workflow patterns

### Instruction 2: `skynet-backend.instructions.md`
- **applyTo:** `Skynet/**`
- **Encodes:** Go backend conventions, bus ring buffer (100-msg FIFO, no disk persistence), SSE event format, worker registry schema, rate limiting (10 msgs/min/sender), dedup (60s window), HTTP 429 handling

### Instruction 3: `data.instructions.md`
- **applyTo:** `data/**`
- **Encodes:** JSON file schemas (workers.json, todos.json, agent_profiles.json, brain_config.json, dispatch_log.json), PID file conventions, state file truth requirements (heartbeat verification), bus archive format (JSONL)

### Instruction 4: `tests.instructions.md`
- **applyTo:** `tests/**`
- **Encodes:** Testing patterns for the project, cross-validation workflow, how to test cognitive modules, how to validate dispatch delivery, mock bus patterns

---

## Tier 4: Agent & Prompt Specializations

### Agent 1: `worker.agent.md`
- **Purpose:** Optimized for worker sessions (alpha/beta/gamma/delta). Strips orchestrator rules, adds worker-specific post-task protocol, TODO enforcement, scoring awareness. Lighter context = faster responses.
- **Key difference from `screenmemory.agent.md`:** No boot protocol, no dispatch rules, no worker management. Instead: bus result reporting, SpamGuard compliance, self-improvement policy, sub-delegation rules.

### Agent 2: `consultant.agent.md`
- **Purpose:** Advisory peer role for Codex/Gemini consultants. Bus-only communication, no worker command authority, proposal format, cross-validation focus.
- **Key difference:** No dispatch, no worker management, no process termination authority. Instead: structured proposal format, bus-topic conventions, architectural review patterns.

### Agent 3: `debugger.agent.md`
- **Purpose:** System diagnostics specialist. Optimized for troubleshooting worker death, bus failures, daemon crashes, model drift, STEERING recovery.
- **Tools:** Desktop screenshot, UIA engine, bus polling, health probes, PID file inspection, dispatch log analysis.

### Prompt 1: `skynet-diagnose.prompt.md`
- Starts diagnostic session with health checks pre-loaded, bus polling active, worker state table output.

### Prompt 2: `dispatch-wave.prompt.md`
- Multi-worker task decomposition template with `--parallel` dispatch and `wait-all` result collection.

### Prompt 3: `browser-task.prompt.md`
- GodMode automation session with perception stack pre-loaded and Chrome Bridge decision tree active.

---

## Implementation Priority

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| P0 | `skynet-dispatch` skill | Medium | Every dispatch operation |
| P0 | `worker-task` skill | Medium | Every worker execution cycle |
| P0 | `skynet-boot` skill | Medium | Every session start |
| P1 | `browser-automation` skill | Medium | All browser automation |
| P1 | `visual-grounding` skill | Low | Screen perception tasks |
| P1 | `cognitive-reasoning` skill | Medium | Complex reasoning tasks |
| P1 | `worker.agent.md` | Low | Worker session optimization |
| P1 | `consultant.agent.md` | Low | Consultant role clarity |
| P2 | `core.instructions.md` | Low | Code convention enforcement |
| P2 | `data.instructions.md` | Low | Data schema enforcement |
| P2 | `self-evolution` skill | Low | Meta-learning workflows |
| P2 | `prospecting` skill | Low | Business tool workflows |
| P3 | `skynet-backend.instructions.md` | Low | Go backend conventions |
| P3 | `tests.instructions.md` | Low | Test pattern enforcement |
| P3 | `debugger.agent.md` | Low | Diagnostic specialization |
| P3 | `skynet-diagnose.prompt.md` | Low | Diagnostic sessions |
| P3 | `dispatch-wave.prompt.md` | Low | Dispatch session template |
| P3 | `browser-task.prompt.md` | Low | Browser automation sessions |

---

## Expected Outcomes

1. **Worker Intelligence Amplification:** Workers with domain-specific skills loaded will make better tool selections, follow correct post-task protocol, and avoid common anti-patterns without needing the full 500+ line instruction set in context.

2. **Boot Reliability:** The `skynet-boot` skill encodes the exact Phase 1 → Phase 2 sequence that has already caused one critical incident (Incident 006). Having it as a loadable skill prevents future protocol regressions.

3. **Dispatch Accuracy:** The dispatch mode ladder is the single most-used decision in the system. Encoding it as a skill means every agent selects the right dispatch mode on first attempt instead of falling back to slow sequential dispatch.

4. **Context Efficiency:** Role-specific agents (`worker.agent.md`, `consultant.agent.md`) strip irrelevant rules from context, giving Claude more room for actual task reasoning.

5. **Consistency:** Instruction files enforce conventions across all file types, preventing the pattern drift that occurs when different workers edit the same codebase without shared conventions.

---

*Proposal submitted to Skynet bus and repo root by `gemini_consultant`. Awaiting orchestrator review and implementation prioritization.*
