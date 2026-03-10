# ScreenMemory — Autonomous Digital Agent with Visual Grounding

A local-first autonomous web agent built on state-of-the-art research in visual grounding,
non-linear reasoning, hybrid memory architectures, and dynamic code generation.
Implements the full architecture from the 2024-2026 research frontier.

**Autonomy Level: L3-L4** — Independent execution with human-in-the-loop for critical decisions.

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

### Orchestration
| Module | File | Purpose |
|--------|------|---------|
| Agent | `agent.py` | Master orchestrator (GoT -> Plan -> MCTS -> Execute -> Reflect) |
| Daemon | `main.py` | Background capture + analysis pipeline |
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
Core Pipeline:           6/6  tests passing (capture, change, VLM, DB, search)
Cognitive Layer:        41/41 tests passing (logger, SoM, memory, planner, navigator)
Advanced Architecture:  66/66 tests passing (GoT, MCTS, Reflexion, DynaAct, CodeGen)
                       ─────
Total:                113/113 tests passing
```

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

## Tools

Integrated tooling for prospect discovery, browser automation, DNS management, and email delivery. All tools live under `tools/`.

### Directory Layout

```
tools/
├── prospecting/              # B2B lead generation pipeline
│   ├── finders/              # Google Maps scrapers (UAE, Austria, Europe)
│   ├── enrichers/            # Email research via Facebook, Bing, DuckDuckGo
│   ├── cleaners/             # Dedup, false-positive removal, filtering
│   ├── validators/           # Website existence checks, status validation
│   ├── exporters/            # CSV → Markdown report generators
│   ├── categories/           # Business category lists (UAE 417, Europe 50)
│   └── results/              # Output data by region (uae/, austria/, europe/)
├── chrome_bridge/            # Chrome automation over WebSocket (v4.0, 265+ commands)
│   ├── extension/            # Chrome extension (manifest v3)
│   ├── native/               # Native messaging hosts (Go + C#)
│   ├── dist/                 # Packaged CRX + proof artifacts
│   ├── god_mode.py           # Zero-pixel structural perception engine
│   ├── brain.py              # LLM connectors + error recovery + CAPTCHA
│   ├── agent.py              # Stealth launcher + autonomous execution
│   ├── cdp.py                # Chrome DevTools Protocol client
│   └── server.py             # WebSocket hub
├── dns/                      # Domain & DNS management
│   ├── cf_add_dns.py         # Cloudflare DNS record creation
│   ├── check_dns.py          # DNS resolution checker
│   ├── update_dns.py         # Bulk DNS updates
│   ├── fix_godaddy.py        # GoDaddy DNS fixes
│   ├── add_verification_txt.py  # TXT record for domain verification
│   └── assign_config_set.py  # DNS config set assignment
├── email/                    # Email delivery & verification
│   ├── check_ses.py          # AWS SES status checker
│   ├── check_ses_full.py     # Full SES account audit
│   ├── send_premium_email.py # Premium email sender
│   ├── send_test_email.py    # Test email sender
│   ├── test_smtp.py          # SMTP connectivity test
│   ├── verify_and_send.py    # Verify then send workflow
│   ├── verify_recipient.py   # Recipient validation
│   ├── find_ses_region.py    # Find active SES region
│   └── reset_ses_domain.py   # Reset SES domain identity
└── browser/                  # Browser control utilities
    ├── browser_control.py    # Playwright browser automation
    ├── browser_fast.py       # Lightweight fast browser ops
    ├── open_browser.py       # Browser launch helper
    └── test_cdp.py           # CDP connection test
```

### Prospecting Pipeline

Automated B2B lead generation: find businesses without websites, enrich with emails, validate, and export.

#### UAE Pipeline
```
finders/find_prospects_v3.py → enrichers/research_emails_fb.py → cleaners/clean_emails.py → validators/check_websites.py → exporters/generate_final_md.py
```
Cities: Dubai, Abu Dhabi, Sharjah, Ajman, Ras Al Khaimah, Fujairah, Umm Al Quwain

#### Austria Pipeline
```
finders/find_prospects_austria.py → enrichers/research_emails_austria.py → cleaners/clean_austria.py → validators/check_websites_austria.py → exporters/generate_austria_md.py
```
Cities: Vienna, Graz, Linz, Salzburg, Innsbruck, Klagenfurt, Villach, Wels, St. Pölten, Dornbirn

#### Europe Pipeline
```
finders/find_prospects_europe.py → enrichers/research_emails_europe.py → cleaners/clean_europe.py → exporters/generate_europe_md.py
```
Cities: London, Paris, Berlin, Madrid, Rome, Amsterdam, Brussels, Lisbon, Prague, Warsaw, Budapest, Bucharest, Dublin, Copenhagen, Stockholm, Zurich, Milan, Barcelona

### Chrome Bridge

Full Chrome automation via WebSocket hub + extension. Supports 265+ commands including stealth mode, CDP, GOD MODE structural perception, and autonomous agent execution. See `tools/chrome_bridge/README.md` for setup and API details.

```powershell
# Start the hub
python tools/chrome_bridge/server.py

# Run smoke test
python tools/chrome_bridge/demo.py
```

### DNS Tools

```powershell
python tools/dns/check_dns.py          # Check DNS resolution
python tools/dns/cf_add_dns.py         # Add Cloudflare records
python tools/dns/update_dns.py         # Bulk DNS updates
```

### Email Tools

```powershell
python tools/email/check_ses.py        # Check SES status
python tools/email/send_test_email.py   # Send test email
python tools/email/verify_recipient.py  # Verify recipient
```

### Documentation & Assets

```
docs/
├── screenshots/    # UI screenshots, dashboard captures, agent visuals
└── research/       # Gemini research notes and topic analyses
```
