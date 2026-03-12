# Consultant HWND Boot And Test

Direct consultant HWND routing is only truthful when the consultant is running in its own top-level VS Code window. If `CC-Start` or `GC-Start` is launched inside the orchestrator window or any worker window, the HWND is rejected and the consultant stays on `bridge_queue`. <!-- signed: consultant -->

If you do not already have a dedicated consultant window, open a candidate surface first:

```powershell
python tools/skynet_consultant_hwnd.py open --consultant-id consultant
python tools/skynet_consultant_hwnd.py open --consultant-id gemini_consultant
```

That helper opens a detached Copilot CLI candidate window and records it in `data/consultant_window_candidates.json`. It does **not** bind the consultant automatically, because a new detached chat is only a candidate surface until consultant transcript markers exist and the direct-delivery gate passes.

## Boot Rules

1. Open a dedicated consultant VS Code chat window. Do not reuse the orchestrator window. Do not reuse a worker window.
2. Bring that consultant window to the foreground.
3. Run `.\CC-Start.ps1` for Codex or `.\GC-Start.ps1` for Gemini from that same window.
4. Verify truthful state:
   - `python tools/skynet_consultant_bridge.py --id consultant --status`
   - `python tools/skynet_consultant_bridge.py --id gemini_consultant --status`
   - `python tools/skynet_consultant_hwnd.py probe --consultant-id consultant`
   - `python tools/skynet_consultant_hwnd.py probe --consultant-id gemini_consultant`
5. A real HWND route must show:
   - non-zero `hwnd`
   - `requires_hwnd=true`
   - `prompt_transport=ghost_type`
   - probe `accepted=true`

Persistence markers required in the dedicated consultant transcript:

- Codex: `cc-start`, `Codex Consultant`, `sender: consultant`, `signed:consultant`
- Gemini: `gc-start`, `Gemini Consultant`, `sender: gemini_consultant`, `signed:gemini_consultant`

If the state shows `hwnd=0` and `prompt_transport=bridge_queue`, the consultant is still reachable through the bridge, but there is no truthful dedicated HWND route yet.

## Test-First Gate

Before any real consultant invoke, send a harmless test prompt first:

```powershell
python tools/skynet_consultant_prompt_gate.py `
  --consultant-id consultant `
  --test-prompt "HWND TEST ONLY -- reply ACK if this arrived by direct prompt."
```

For Gemini:

```powershell
python tools/skynet_consultant_prompt_gate.py `
  --consultant-id gemini_consultant `
  --test-prompt "HWND TEST ONLY -- reply ACK if this arrived by direct prompt."
```

Expected truth:

- `test_result.success=true` means direct consultant delivery succeeded.
- `test_result.success=false` means the system refused to claim direct delivery. Typical causes are `hwnd=0`, a reserved Skynet window, or bridge-only routing.

To send a real prompt only after the test succeeds:

```powershell
python tools/skynet_consultant_prompt_gate.py `
  --consultant-id consultant `
  --test-prompt "HWND TEST ONLY -- reply ACK if this arrived by direct prompt." `
  --real-prompt "REAL TASK HERE"
```

The gate aborts the real prompt unless the test prompt achieved real direct delivery. That keeps consultant invocation aligned with the Truth Principle and the pre-fire proof rule.
