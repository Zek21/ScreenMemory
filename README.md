# Skynet AI — Autonomous Digital Agent with Visual Grounding

A local-first autonomous web agent built on state-of-the-art research in visual grounding,
non-linear reasoning, hybrid memory architectures, and dynamic code generation.
Implements the full architecture from the 2024-2026 research frontier.

**Autonomy Level: L3-L4** — Independent execution with human-in-the-loop for critical decisions.

## What is in this repository

This is a **research repository**. It documents the architecture, design decisions and
measured results of a working private system; it is **not** a distribution of that
system's source.

**Public here:** this README (architecture + module map), `SECURITY.md`, `LICENSE`,
`requirements.txt`, `docs/SEARCHABILITY.md`, and `data/paper_final_checklist.md`.

**Not public:** the implementation — the capture pipeline, cognitive layer, Chrome
bridge, orchestration/fleet tooling and test suite. Those live in a private working tree.
Paths referenced in the sections below describe that internal layout so the architecture
can be read precisely; **they are not files you will find in this clone.**

There is therefore no build or run step to follow here, and nothing to install. If you are
looking for something runnable, see the Contact section — please don't file issues asking
why a path is missing.

## System Architecture

```
                        ┌─────────────────────┐
                        │   agent.py           │
                        │   (Orchestrator)     │
                        └─────────┬───────────┘
                                  │
            ┌─────────────────────┼──────────────────────┐
            │                     │                      │
    ┌───────▼───────┐    ┌───────▼───────┐    ┌─────────▼──────────┐
    │  GoT Reasoner │    │  Hierarchical │    │  Dynamic Code Gen  │
    │  (non-linear  │    │  Planner      │    │  + Sandboxed Exec  │
    │   graph)      │    │  (subtasks)   │    │  (bypass GUI)      │
    └───────┬───────┘    └───────┬───────┘    └────────────────────┘
            │                    │
    ┌───────▼───────┐    ┌───────▼───────┐
    │  R-MCTS       │    │  DynaAct      │
    │  (tree search │    │  (action      │
    │   + reflect)  │    │   filtering)  │
    └───────┬───────┘    └───────┬───────┘
            │                    │
    ┌───────▼────────────────────▼───────┐
    │        PERCEPTION + ACTION          │
    │   SoM Grounding  │  Web Navigator  │
    │   (visual marks) │  (click/type)   │
    └───────────────────┬────────────────┘
                        │
    ┌───────────────────▼────────────────┐
    │        REFLECTIVE FEEDBACK          │
    │   Reflexion      │  Verify         │
    │   (self-critique)│  (screenshot    │
    │                  │   compare)      │
    └───────────────────┬────────────────┘
                        │
    ┌───────────────────▼────────────────┐
    │        MEMORY SYSTEM                │
    │   Working │ Episodic │ Semantic     │
    │   (7 items)│(vector) │(knowledge)  │
    │            │         │  graph)     │
    │   + Knowledge Distillation         │
    └───────────────────┬────────────────┘
                        │
    ┌───────────────────▼────────────────┐
    │        SCREEN CAPTURE PIPELINE      │
    │   DXGI Capture → Change Detect →   │
    │   VLM Analysis → Embed → Store     │
    └────────────────────────────────────┘
```

## Module Map

### Core Capture Pipeline
| Module | File | Purpose |
|--------|------|---------|
| Screen Capture | `core/capture.py` | DXGI-backed capture (~33ms), dual-monitor |
| Change Detector | `core/change_detector.py` | dHash perceptual hashing, grid-based regions |
| VLM Analyzer | `core/analyzer.py` | Moondream via Ollama (vision-language) |
| Embedder | `core/embedder.py` | SigLIP 2 / Ollama text embeddings |
| Database | `core/database.py` | sqlite-vec + FTS5 hybrid search |
| Activity Logger | `core/activity_log.py` | Structured JSONL + console logging |

### Cognitive Agent Layer
| Module | File | Purpose | Reference |
|--------|------|---------|-----------|
| Graph of Thoughts | `core/cognitive/graph_of_thoughts.py` | Non-linear reasoning graph | Besta et al. 2024 |
| R-MCTS | `core/cognitive/mcts.py` | Tree search + contrastive reflection | WebPilot / R-MCTS |
| Reflexion | `core/cognitive/reflexion.py` | Verbal self-critique on failures | Shinn et al. 2023 |
| DynaAct | `core/cognitive/reflexion.py` | Dynamic action space filtering | DynaAct framework |
| Episodic Memory | `core/cognitive/memory.py` | Tripartite: working/episodic/semantic | Cognitive science |
| Knowledge Distill | `core/cognitive/knowledge_distill.py` | Decay + LLM summarization | Memory consolidation |
| Planner | `core/cognitive/planner.py` | Hierarchical goal decomposition | Agent-E architecture |
| Code Generation | `core/cognitive/code_gen.py` | Write + sandbox Python scripts | AutoGen / AutoCodeSherpa |

### Visual Grounding
| Module | File | Purpose | Reference |
|--------|------|---------|-----------|
| Set-of-Mark | `core/grounding/set_of_mark.py` | Overlay numbered markers on UI | Yang et al. 2023 |

### Navigation
| Module | File | Purpose |
|--------|------|---------|
| Web Navigator | `core/navigator/web_navigator.py` | Pixel-level autonomous navigation |

### System Entry Points
| Module | File | Purpose |
|--------|------|---------|
| Agent | `agent.py` | Main agent (GoT → Plan → MCTS → Execute → Reflect) |
| Pipeline | `main.py` | Background capture + analysis pipeline |
| Search CLI | `search.py` | Interactive semantic search over history |

## Key Innovations

### Pure Visual Grounding (bypasses DOM)
Instead of parsing HTML/Accessibility Trees (95% of sites have accessibility failures),
the agent perceives the screen as raw pixels and overlays Set-of-Mark numbered markers
for spatial interaction. This is immune to prompt injection attacks via hidden DOM elements.

### Graph of Thoughts Reasoning
Replaces linear chain-of-thought with a graph topology where information units are
vertices and logical dependencies are edges. Supports parallel exploration, aggregation,
refinement, and pruning of reasoning paths.

### Reflective Monte Carlo Tree Search
Adapts MCTS for web navigation with UCB1 exploration/exploitation balance and
contrastive reflection. When a path fails, the agent analyzes WHY by comparing
failed states against successful states, preventing repeated mistakes.

### Tripartite Memory with Knowledge Distillation
- **Working Memory**: 7 items (Miller's Law), flushed per subtask
- **Episodic Memory**: Vector-backed, time-indexed, utility-scored events
- **Semantic Memory**: Permanent knowledge, populated via distillation
- **Intelligent Decay**: Low-utility episodic entries get LLM-summarized into semantic

### Dynamic Code Generation
When GUI interaction is inefficient (pagination, bulk data), the agent writes
custom Python scripts, validates them against a security whitelist, and executes
in a sandboxed subprocess. Failed scripts trigger Reflexion for iterative debugging.

## Test Results

```
Test files:              423 pytest modules in the private tree (tests/)
Status:                  Suite is not published; see "What is in this repository"
```
<!-- signed: alpha -->

## Hardware

- **GPU**: AMD RX 6600 (4GB VRAM) — Moondream VLM fits at 1.7GB
- **RAM**: 48GB — ample for model loading + graph structures
- **CPU**: i5-9400F — handles embedding, graph ops, subprocess management
- **Displays**: Dual 1920x1080

## Privacy

- All processing is local — zero cloud calls
- Database encrypted with AES-256 via SQLCipher
- Encryption keys derived from user passphrase + machine-bound salt
- Generated code sandboxed with import whitelist + dangerous pattern blocking
- No telemetry, no analytics, no cloud sync

---

## Chrome Bridge

Full Chrome automation via a WebSocket hub plus a browser extension, exposing 265+
commands including stealth mode, CDP access, structural page perception and autonomous
agent execution. The hub is a Python server; the agent drives it over the socket rather
than through synthetic input, so actions survive page re-renders.

Implementation and setup docs are part of the private tree and are not published here.

## Documentation in this repository

| file | contents |
|---|---|
| `README.md` | architecture, module map, key innovations, measured results |
| `docs/SEARCHABILITY.md` | discovery / indexing notes |
| `data/paper_final_checklist.md` | research write-up checklist |
| `SECURITY.md` | security policy and disclosure contact |

## Contact

Questions about the architecture, or interest in the implementation, are welcome via the
repository's issue tracker or the contact in `SECURITY.md`.
