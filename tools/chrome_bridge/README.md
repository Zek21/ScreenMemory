# Chrome Bridge

Chrome Bridge is a local Chrome extension plus Python client for browser automation over a WebSocket hub.

---

## 🧠 GOD MODE — Structural Perception Engine

**GOD MODE** is a zero-pixel, zero-screenshot perception system that enables AI agents
to understand and navigate web pages using pure structural analysis.

### Quick Start
```python
from god_mode import GodMode
god = GodMode(cdp_port=9222)

# See what's on the page (~1400 tokens for LLM)
scene = god.scene()

# Find and click by concept, not selector
god.click("Submit")

# Fill forms by label text
god.fill_form({"Email": "me@site.com", "Password": "secret"})
```

### Autonomous Agent
```python
from agent import AutonomousAgent, stealth_open

# Pre-scripted automation (no LLM needed)
agent = AutonomousAgent()
agent.run_script([
    {"action": "navigate", "value": "https://google.com"},
    {"action": "type", "target": "Search", "value": "AI"},
    {"action": "press", "value": "Enter"},
    {"action": "done", "reason": "Searched"}
])

# Stealth: open site invisibly
launcher, god = stealth_open("https://facebook.com", profile="Profile 3")
god.click("Login")
launcher.stop()
```

### Documentation Index
| File | Purpose | When to Read |
|------|---------|-------------|
| **[DECISION_TREE.md](DECISION_TREE.md)** | Which function to call for any task | **READ FIRST** — AI routing guide |
| **[FUNCTION_MAP.md](FUNCTION_MAP.md)** | Every function with signatures & returns | When you need parameter details |
| **[GOD_MODE.md](GOD_MODE.md)** | Architecture deep-dive & theory | When you need to understand WHY |

### Architecture
```
brain.py   → THE BRAIN: LLM connectors + recovery + extraction + block detection
agent.py   → EXECUTION: stealth launcher + action protocol + executor + session
god_mode.py → PERCEPTION: 8 modules for zero-pixel structural analysis
cdp.py      → TRANSPORT: Chrome DevTools Protocol WebSocket client

Chrome (CDP:9222) → AccessibilityTree → SemanticGeometry → OcclusionResolver
                        ↓                     ↓                    ↓
                   ElementEmbedding → PageTopologyGraph → SpatialReasoner
                        ↓
                   ActionSpaceOptimizer → GodMode Controller → LLM (~1400 tokens)
                        ↓
                   ActionProtocol → ActionExecutor → AutonomousAgent → Loop
                        ↓
                   LLMConnector → ErrorRecovery → HumanBehavior → Brain
                        ↓
                   StealthLauncher (headless / hidden / offscreen)
```

### Key Files
| File | Lines | Purpose |
|------|-------|---------|
| `brain.py` | ~850 | **THE BRAIN**: LLM connectors, content extraction, error recovery, CAPTCHA detection, human behavior, multi-tab |
| `agent.py` | ~700 | Autonomous agent: StealthLauncher, ActionProtocol, Executor, SessionManager |
| `god_mode.py` | ~2300 | All 8 perception modules + GodMode controller + CLI |
| `cdp.py` | ~1200 | CDP WebSocket client (tab mgmt, input, DOM, network) |
| `perception.py` | ~1037 | Foundation: SpatialNode, Win32Scanner, UIA, PerceptionEngine |

---

## What changed in v4.0

- 265+ commands, up from 210+.
- **Full stealth suite** (`stealth.full`): canvas/WebGL/WebGL2/AudioContext fingerprint noise, timing noise, UA client hints spoofing, automation indicator cleanup — all injected pre-page via CDP.
- **Performance tracing**: `tracing.start` / `tracing.stop` for detailed Chrome timeline traces.
- **Vision simulation**: `emulate.colorBlind` (protanopia, deuteranopia, tritanopia, achromatopsia, blurredVision).
- **Advanced emulation**: `emulate.darkMode`, `emulate.reducedMotion`, `emulate.cpuThrottle`, `emulate.forcedColors`, `emulate.disableJS`, `emulate.printMedia`.
- **CDP overlays**: `overlay.showFPS`, `overlay.showPaintRects`, `overlay.showLayoutShifts`, `overlay.highlight`, `overlay.hide`.
- **Animation control**: `animation.disable`, `animation.enable`, `animation.setSpeed`.
- **Tab power ops**: `tabs.sort`, `tabs.deduplicate`, `tabs.groupByDomain`, `tabs.suspendInactive`, `tabs.closeByPattern`.
- **DOM CDP ops**: `dom.scrollIntoView`, `dom.focus`, `dom.search`, `dom.getOuterHTML`, `page.bringToFront`.
- **Runtime inspection**: `runtime.heapStats`, `runtime.collectGarbage`, `runtime.getProperties`, `runtime.queryObjects`.
- **Service worker & cache**: `serviceWorker.list`, `serviceWorker.unregister`, `cache.list`, `cache.clear`.
- **IndexedDB**: `indexedDB.list`, `indexedDB.clear`.
- **Network mocking**: `network.mock`, `network.unmock` for intercepting and replacing responses.
- **WebAuthn**: `webauthn.addVirtualAuth`, `webauthn.removeVirtualAuth` for passkey testing.
- **Content script commands**: `element.focus`, `element.blur`, `element.closest`, `element.style`, `element.offset`, `element.matches`, `element.type` (natural typing), `element.hover`, `forms.submit`, `dom.serialize`, `dom.ready`, `clipboard.copy`, `page.freeze`, `page.unfreeze`.
- **CDP scroll**: `input.scroll` for precise mouse wheel events.
- **Bridge export**: `bridge.export` for full state dump.
- **Popup upgrades**: God Stealth button, Dark Mode toggle, FPS counter, tab Sort/Dedupe/Group buttons.
- **Multi-profile installer**: `install_all_profiles.ps1` loads extension into all Chrome profiles.
- Updated user agent strings to Chrome 134 (2026 era).

## What changed in v3.1

- 210+ commands, up from 185+.
- **Critical fix**: duplicate `input.mouse`/`input.key`/`input.touch` case blocks merged (second set was dead code).
- Enhanced `input.mouse` with `dblclick`, `contextmenu`, `hover` types and modifier keys.
- Extended `input.key` keyMap with F1–F12 and a–z.
- `input.touch` now supports `swipe` with configurable distance, duration, direction.
- `smartFind` upgraded with fuzzy bigram scoring and shadow DOM traversal.
- `buildSelector` now prefers `data-testid`, `data-cy`, `data-test` for stable selectors.
- New background commands: `wait.function`, `wait.url`, `tabs.createAndWait`, `tabs.navigatePost`.
- New content commands: `element.waitGone`, `dom.waitStable`, `element.attributes`, `element.setAttribute`, `element.xpath`, `element.dispatchEvent`, `element.parent`, `element.children`, `element.siblings`, `scroll.position`, `intersection.observe`, `intersection.check`, `intersection.stop`, `canvas.readPixels`, `network.observeXHR`, `network.flushXHR`, `smart.select`, `element.highlight.multiple`.
- Full Python client methods for all new commands (sync `Chrome` and async `AsyncChrome`).

## What changed in v3.0

- Direct service-worker WebSocket transport on Chrome 116+.
- Volatile bridge state moved to `chrome.storage.session` instead of `storage.local`.
- 3-layer error normalization so content-script and inline execution failures raise cleanly.
- CRX packaging plus install-proof capture for real Chrome acceptance or rejection evidence.
- Configurable hub endpoint from the popup and Python client.
- Persisted bridge runtime state across service-worker restarts.
- HTTP health probe before WebSocket connect to avoid noisy refused-socket errors.

## Install

1. Open `chrome://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select the `extension` folder inside `tools/chrome_bridge/extension`.

## Run

Start the hub:

```powershell
cd d:\Prospects\ScreenMemory\tools\chrome_bridge
python server.py
```

Bind on a different host or port:

```powershell
cd d:\Prospects\ScreenMemory\tools\chrome_bridge
python server.py --host 0.0.0.0 --port 7777
```

Run the smoke test in another terminal:

```powershell
cd d:\Prospects\ScreenMemory\tools\chrome_bridge
python demo.py
```

The extension popup can now change the hub URL at runtime. For Python scripts, you can also point the client at another hub with `CHROME_BRIDGE_URL`:

```powershell
$env:CHROME_BRIDGE_URL = "ws://192.168.1.25:7777"
python demo.py
```

Run the protocol self-test without Chrome:

```powershell
cd d:\Prospects\ScreenMemory\tools\chrome_bridge
python test_bridge.py --synthetic
```

Prove the packaged CRX and capture Chrome install evidence:

```powershell
cd d:\Prospects\ScreenMemory\tools\chrome_bridge
python prove_crx_install.py
```

Artifact-only proof:

```powershell
cd d:\Prospects\ScreenMemory\tools\chrome_bridge
python prove_crx_install.py --artifact-only
```

Proof outputs are written to `chrome-bridge/dist/proof/`.
Package creation alone is not install proof; the install-proof script records either acceptance, policy rejection, or unknown.

## Python usage

```python
from bridge import Hub

with Hub() as hub:
    hub.wait_for_agent(timeout=15)
    chrome = hub.chrome()
    print(chrome.capabilities())
    print(chrome.status())
    tabs = chrome.tabs()
    active = next(tab for tab in tabs if tab["active"])
    print(chrome.page_info(active["id"]))
```
