# GOD MODE — AI Decision Tree

> **PURPOSE**: This file is the FIRST file any AI agent should read.
> It tells you exactly which function to call for any task, with zero ambiguity.
> Read this → know what to do → invoke → done.

## Architecture Stack
```
brain.py  → THE BRAIN: LLM connectors + recovery + extraction + block detection
agent.py  → EXECUTION: stealth launcher + action protocol + executor + session
god_mode.py → PERCEPTION: 8 modules for zero-pixel structural analysis
cdp.py    → TRANSPORT: Chrome DevTools Protocol WebSocket client
```

## Quick Start — Simple (3 lines)
```python
from god_mode import GodMode
god = GodMode(cdp_port=9222)
result = god.see()  # Start here — see what's on screen
```

## Quick Start — Brain (the ultimate, 4 lines)
```python
from brain import Brain
brain = Brain(llm_provider="openai", llm_api_key="sk-...")
result = brain.execute_mission("Search for AI on Wikipedia", start_url="https://en.wikipedia.org")
brain.shutdown()
```

## Quick Start — Autonomous Agent (4 lines)
```python
from agent import AutonomousAgent
agent = AutonomousAgent(cdp_port=9222)
agent.run_script([
    {"action": "navigate", "value": "https://google.com"},
    {"action": "type", "target": "Search", "value": "AI"},
    {"action": "press", "value": "Enter"},
    {"action": "done", "reason": "Searched for AI"}
])
```

## Quick Start — Stealth (invisible Chrome)
```python
from agent import stealth_open
launcher, god = stealth_open("https://facebook.com", profile="Profile 3", mode="headless")
scene = god.scene()  # Page content, no window visible
launcher.stop()
```

---

## MASTER DECISION TREE

```
WHAT DO YOU NEED?
│
├─── "What's on the page?" ──────────── god.see(depth='standard')
│    ├── Just text list ──────────────── god.see(depth='minimal')
│    ├── Full scene for LLM ─────────── god.scene()
│    ├── Compact action space JSON ───── god.action_space()
│    └── Everything + graph + VoT ────── god.see(depth='god')
│
├─── "Find an element" ──────────────── god.find("concept text")
│    ├── By concept ("login") ────────── god.find("login")
│    ├── By coordinates ──────────────── god.what_is_at(x, y)
│    └── Find and describe ──────────── god.describe()
│
├─── "Click something" ──────────────── god.click(target)
│    ├── By concept ──────────────────── god.click("Submit")
│    ├── By coordinates ──────────────── god.click((400, 300))
│    └── By element dict ────────────── god.click(element_dict)
│
├─── "Fill a form" ──────────────────── god.fill_form({"Email": "x@y.com"})
│    ├── Single field ───────────────── god.find_and_fill("Email", "x@y.com")
│    └── Multiple fields ────────────── god.fill_form({"Name": "A", "Email": "B"})
│
├─── "Type text" ────────────────────── god.type_text("hello")
│    └── Press a key ────────────────── god.press("Enter")
│
├─── "Navigate" ─────────────────────── god.navigate("https://...")
│    ├── Scroll ─────────────────────── god.scroll('down', 300)
│    └── Wait for page ──────────────── god.wait_for(text="Welcome")
│
├─── "Handle popups/modals" ─────────── god.dismiss_overlays()
│
├─── "Manage tabs" ──────────────────── god.tabs()
│    ├── New tab ─────────────────────── god.new_tab("https://...")
│    ├── Close tab ───────────────────── god.close_tab(tab_id)
│    └── Switch tab ──────────────────── god.activate_tab(tab_id)
│
├─── "Run JavaScript" ───────────────── god.eval("document.title")
│
├─── "Take screenshot" ──────────────── god.screenshot("out.png")
│
├─── "System status" ────────────────── god.status()
│    ├── Action history ─────────────── god.history()
│    ├── Windows list ───────────────── god.windows()
│    ├── Monitors ───────────────────── god.monitors()
│    └── Full world scan ────────────── god.scan_world()
│
├─── "Autonomous LLM loop" ─────────── agent.run(task, decide_fn)
│    ├── With LLM function ──────────── agent.run("Log in", my_llm)
│    ├── Pre-scripted (no LLM) ──────── agent.run_script(actions)
│    ├── Interactive REPL ───────────── agent.interactive()
│    └── One-liner script ───────────── quick_script(url, actions)
│
├─── "Stealth/invisible Chrome" ─────── StealthLauncher
│    ├── Headless (no window) ───────── launcher.launch(mode=HEADLESS)
│    ├── Hidden (Win32 SW_HIDE) ─────── launcher.launch(mode=HIDDEN)
│    ├── Offscreen (-32000,-32000) ──── launcher.launch(mode=OFFSCREEN)
│    ├── List profiles ──────────────── StealthLauncher.list_profiles()
│    └── Quick open ─────────────────── stealth_open(url, profile)
│
└─── "Direct module access" ─────────── (see Module Map below)
```

---

## MODULE MAP (When You Need Precision)

| Need | Module | Method | Returns |
|------|--------|--------|---------|
| Raw accessibility tree | `god.a11y` | `.parse(tab_id)` | `List[Dict]` — all semantic nodes |
| Compact a11y text | `god.a11y` | `.parse_compact(tab_id)` | `str` — YAML-like tree |
| Only actionable elements | `god.a11y` | `.find_actionable(tab_id)` | `List[Dict]` — clickable/typeable only |
| Bounding boxes + coords | `god.geometry` | `.extract(tab_id)` | `Dict` with viewport + elements |
| Primary CTA button | `god.geometry` | `.find_primary_cta(tab_id)` | `Dict` or None |
| Spatial element clusters | `god.geometry` | `.spatial_clusters(tab_id)` | `List[List[Dict]]` |
| Visibility check | `god.occlusion` | `.resolve(tab_id)` | `Dict` with visible/occluded counts |
| Only truly clickable | `god.occlusion` | `.get_truly_interactable(tab_id)` | `List[Dict]` |
| Detect popups/modals | `god.occlusion` | `.detect_overlays(tab_id)` | `List[Dict]` |
| Vector similarity search | `god.embeddings` | `.find_similar(text, els)` | `List[Tuple[float, Dict]]` |
| Identify UI concept | `god.embeddings` | `.identify_concept(el)` | `str` — "login", "search", etc. |
| Page type classification | `god.embeddings` | `.classify_page_type(els)` | `str` — "login", "search_results", etc. |
| Build element graph | `god.graph` | `.build(elements)` | `PageTopologyGraph` |
| Find element groups | `god.graph` | `.find_groups()` | `List[List[int]]` |
| Find form groups | `god.graph` | `.find_form_groups()` | `List[Dict]` |
| Find nav bars | `god.graph` | `.find_navigation_bars()` | `List[List[int]]` |
| Detect grid layouts | `god.graph` | `.find_grid_patterns()` | `List[List[int]]` |
| Nearby elements | `god.spatial` | `.what_is_near(target, els)` | `List[Dict]` |
| Layout regions | `god.spatial` | `.detect_layout_regions(els, vp)` | `Dict` |
| Row/column detection | `god.spatial` | `.detect_rows_and_columns(els)` | `Dict` |
| Label → input mapping | `god.spatial` | `.find_related_input(label, els)` | `Dict` or None |
| VoT description | `god.spatial` | `.spatial_description(el, els, vp)` | `str` |

---

## COMMON WORKFLOWS

### 1. Navigate to URL and fill a form
```python
god = GodMode()
god.navigate("https://example.com/login")
god.wait_for(text="Email")
god.fill_form({"Email": "me@example.com", "Password": "secret123"})
god.click("Sign In")
```

### 2. Find and interact with element by concept
```python
results = god.find("shopping cart")
if results:
    god.click(results[0])  # Click the best match
```

### 3. Handle modal/popup then continue
```python
dismissed = god.dismiss_overlays()
# Now proceed with actual task
god.click("Submit")
```

### 4. Understand page layout for LLM
```python
scene = god.scene()  # ~1400 tokens, perfect for LLM context
# OR
action_space = god.action_space()  # Even more compact
```

### 5. Multi-tab workflow
```python
tab_id = god.new_tab("https://second-site.com")
god.activate_tab(tab_id)
god.wait_for(text="Ready")
result = god.see(tab_id=tab_id)
```

---

## DEPTH LEVELS

| Depth | Tokens | Speed | Contents |
|-------|--------|-------|----------|
| `minimal` | ~200 | <0.5s | A11y tree text only |
| `standard` | ~1400 | <1s | A11y + geometry + page type |
| `deep` | ~3000 | <2s | Standard + occlusion + groups |
| `god` | ~5000 | <3s | Everything: graph, VoT, embeddings |

**Default: `standard`** — use this unless you need more.

---

## ERROR HANDLING

```python
if not god.connected:
    # Chrome not running or CDP port wrong
    # Start Chrome with: --remote-debugging-port=9222
    pass

result = god.click("Submit")  # Returns False if not found
if not result:
    # Element not found — try alternate concept
    result = god.click("Send")
```

---

## CLI COMMANDS

```bash
# god_mode.py — Perception
python god_mode.py status          # System check
python god_mode.py see             # See current page
python god_mode.py scene           # LLM-optimized scene
python god_mode.py find "login"    # Find elements by concept
python god_mode.py click "Submit"  # Click by concept
python god_mode.py describe        # Full spatial description
python god_mode.py overlays        # Detect popups/modals
python god_mode.py graph           # Topology graph
python god_mode.py tabs            # List tabs
python god_mode.py a11y            # Raw accessibility tree
python god_mode.py page-type       # Classify page type

# agent.py — Execution
python agent.py interactive                              # Interactive REPL
python agent.py profiles                                 # List Chrome profiles
python agent.py perceive                                 # Run perception
python agent.py stealth --url https://site.com --profile "Profile 3"
python agent.py script --script actions.json --url https://site.com

# brain.py — Cognitive (THE BRAIN)
python brain.py mission "Search for AI" --url https://wikipedia.org --provider openai
python brain.py extract https://example.com --what text
python brain.py extract https://example.com --what tables
python brain.py extract https://example.com --what forms
python brain.py check                                    # CAPTCHA/block check
python brain.py profiles                                 # Chrome profiles
```

---

## CRITICAL RULES

1. **NEVER** use physical mouse or keyboard — all interactions go through CDP
2. **NEVER** kill Chrome processes — Antigravity manages its own Chrome
3. Chrome must run with `--remote-debugging-port=9222`
4. Always call `god._ensure_modules()` before using `god.a11y`, `god.geometry`, `god.occlusion`
   (or just use high-level methods which do this automatically)
5. Use `god.see()` as your starting point for ANY task

---

## AUTONOMOUS AGENT (agent.py)

### Action Protocol — What the LLM outputs
```json
{"action": "click", "target": "Submit"}
{"action": "type", "target": "Email", "value": "me@site.com"}
{"action": "press", "value": "Enter"}
{"action": "scroll", "direction": "down", "amount": 500}
{"action": "navigate", "value": "https://url.com"}
{"action": "wait", "target": "text to wait for"}
{"action": "dismiss"}
{"action": "done", "reason": "Task completed"}
{"action": "fail", "reason": "Cannot proceed"}
```

### Autonomous Loop with LLM
```python
from agent import AutonomousAgent

agent = AutonomousAgent()

def my_llm(system_prompt, user_prompt):
    # Call any LLM API — OpenAI, Claude, Gemini, local, etc.
    response = openai.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    return response.choices[0].message.content

result = agent.run("Search for AI on Wikipedia", decide_fn=my_llm)
print(result)  # Session summary with all steps
```

### Pre-scripted (no LLM)
```python
agent.run_script([
    {"action": "navigate", "value": "https://google.com"},
    {"action": "type", "target": "Search", "value": "machine learning"},
    {"action": "press", "value": "Enter"},
    {"action": "wait", "target": "results"},
    {"action": "done", "reason": "Searched successfully"}
])
```

### Stealth Modes
| Mode | Window | Bot Detection | Speed | Use When |
|------|--------|---------------|-------|----------|
| `headless` | None | Detectable | Fastest | Internal tools, scraping |
| `hidden` | Created then SW_HIDE | Undetectable | Fast | Social media, banking |
| `offscreen` | At -32000,-32000 | Undetectable | Normal | Full rendering needed |

```python
from agent import StealthLauncher, stealth_open

# Quick: open URL invisibly
launcher, god = stealth_open("https://site.com", profile="Profile 3", mode="hidden")
god.click("Login")
launcher.stop()

# Manual: full control
launcher = StealthLauncher(port=9333)
cdp = launcher.launch(mode=StealthLauncher.Mode.HIDDEN, profile="Profile 3")
# ... use cdp directly ...
launcher.stop()
```

### Session Safety
- **Max steps**: Default 30, configurable
- **Loop detection**: Same action 3x on same URL → auto-stop
- **Failure detection**: 5 consecutive failures → auto-stop
- **History**: Full session saved as JSON with every step recorded

---

## THE BRAIN (brain.py) — Cognitive Layer

### LLM Providers
```python
from brain import Brain

# OpenAI
brain = Brain(llm_provider="openai", llm_api_key="sk-...")
# Or set env: OPENAI_API_KEY

# Claude
brain = Brain(llm_provider="claude", llm_api_key="sk-ant-...")

# Gemini
brain = Brain(llm_provider="gemini", llm_api_key="AIza...")

# Local Ollama (no API key needed)
brain = Brain(llm_provider="ollama", llm_model="llama3.1")

# Mock (for testing)
brain = Brain(llm_provider="mock")

# Custom function
brain = Brain(llm_provider="custom", custom_fn=my_fn)
```

### Full Mission (LLM decides everything)
```python
with Brain(llm_provider="openai") as brain:
    result = brain.execute_mission(
        "Search for machine learning on Wikipedia",
        start_url="https://en.wikipedia.org",
        max_steps=20,
    )
    print(f"Status: {result['status']}, Steps: {result['total_steps']}")
```

### Extract Data from Pages
```python
from brain import Brain
brain = Brain()
data = brain.extract("https://example.com")
print(data['metadata']['title'])    # Page title
print(data['text'][:500])           # Visible text
print(data['links'][:5])            # Links
print(data['tables'])               # Table data
print(data['forms'])                # Form fields
```

### CAPTCHA/Block Detection
```python
check = brain.check_blocks()
if check['captcha']:
    print(f"CAPTCHA: {check['signals']}")
    print(check['recommendation'])
```

### Multi-Tab Orchestration
```python
# Open in new tab, extract, return to original
data = brain.tabs.extract_and_return("https://other-site.com")

# Parallel extraction from multiple URLs
results = brain.tabs.parallel_extract([
    "https://site1.com", "https://site2.com", "https://site3.com"
])
```

### Error Recovery (automatic)
When an action fails, the Brain automatically:
1. Scrolls down to find the element
2. Dismisses overlays that might be blocking
3. Scrolls up if we scrolled past it
4. Tries alternative target text (Submit→Send→OK)
5. Waits and retries for timeout errors

### Human-Like Behavior (automatic)
- Gaussian-distributed delays (not fixed intervals)
- Variable typing speed with occasional pauses
- Scroll chunking (large scrolls broken into small human-like pieces)
- Familiarity effect (actions get slightly faster over time)
- 10% chance of reading pauses between actions
