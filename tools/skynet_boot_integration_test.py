"""
skynet_boot_integration_test.py — Boot Integration Verification (Rule #0.06)

Verifies that Orch-Start.ps1 and skynet_start.py correctly reference
the canonical boot script (skynet_worker_boot.py) and that all
supporting files exist.

Usage:
    python tools/skynet_boot_integration_test.py
"""
# signed: delta

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    status = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    if condition:
        PASS += 1
    else:
        FAIL += 1


def main():
    print("=" * 60)
    print("  BOOT INTEGRATION TEST (Rule #0.06)")
    print("=" * 60)
    print()

    # --- 1. Core files exist ---
    print("[1] Core file existence:")
    boot_script = ROOT / "tools" / "skynet_worker_boot.py"
    check("tools/skynet_worker_boot.py exists", boot_script.exists())

    boot_guard = ROOT / "tools" / "skynet_boot_guard.py"
    check("tools/skynet_boot_guard.py exists", boot_guard.exists())

    procedure_doc = ROOT / "docs" / "WORKER_BOOT_PROCEDURE.txt"
    check("docs/WORKER_BOOT_PROCEDURE.txt exists", procedure_doc.exists())

    integrity_file = ROOT / "data" / "boot_integrity.json"
    check("data/boot_integrity.json exists", integrity_file.exists())
    print()

    # --- 2. Importability ---
    print("[2] Module importability:")
    try:
        # skynet_worker_boot.py
        sys.path.insert(0, str(ROOT))
        import importlib
        mod = importlib.import_module("tools.skynet_worker_boot")
        check("skynet_worker_boot importable", True)
        has_boot_all = hasattr(mod, "boot_all_workers") or hasattr(mod, "boot_single_worker")
        check("skynet_worker_boot has boot functions", has_boot_all,
              "boot_all_workers or boot_single_worker")
    except Exception as e:
        check("skynet_worker_boot importable", False, str(e))
        check("skynet_worker_boot has boot functions", False, "import failed")

    try:
        mod2 = importlib.import_module("tools.skynet_boot_guard")
        check("skynet_boot_guard importable", True)
    except Exception as e:
        check("skynet_boot_guard importable", False, str(e))
    print()

    # --- 3. Orch-Start.ps1 references skynet_worker_boot.py ---
    print("[3] Orch-Start.ps1 integration:")
    orch_start = ROOT / "Orch-Start.ps1"
    if orch_start.exists():
        content = orch_start.read_text(encoding="utf-8", errors="replace")
        check("Orch-Start.ps1 exists", True)
        check("References skynet_worker_boot.py",
              "skynet_worker_boot.py" in content,
              "should call the canonical boot script")
        check("Has Rule #0.06 comment",
              "Rule #0.06" in content or "PROVEN BOOT" in content,
              "should mention the proven boot procedure")
        check("Has deprecated fallback warning",
              "DEPRECATED" in content and "skynet_start.py" in content,
              "skynet_start.py fallback should be marked DEPRECATED")
        # Verify orchestrator HWND is read correctly
        check("Reads orchestrator HWND",
              "orchestrator.json" in content and "orch-hwnd" in content,
              "should read HWND from orchestrator.json and pass via --orch-hwnd")
    else:
        check("Orch-Start.ps1 exists", False)
    print()

    # --- 4. skynet_start.py deprecation wrapper ---
    print("[4] skynet_start.py deprecation:")
    start_script = ROOT / "tools" / "skynet_start.py"
    if start_script.exists():
        content = start_script.read_text(encoding="utf-8", errors="replace")
        check("skynet_start.py exists", True)
        check("Has PROVEN BOOT PROCEDURE comment",
              "PROVEN BOOT PROCEDURE" in content,
              "should reference canonical boot method at top")
        check("phase_3_workers has deprecation warning",
              "DEPRECATED" in content and "phase_3_workers" in content,
              "legacy function should warn about deprecation")
        check("References skynet_worker_boot.py in docstring",
              "skynet_worker_boot.py" in content,
              "should point to the canonical script")
    else:
        check("skynet_start.py exists", False)
    print()

    # --- 5. boot_integrity.json validity ---
    print("[5] Boot integrity data:")
    if integrity_file.exists():
        try:
            data = json.loads(integrity_file.read_text())
            check("boot_integrity.json is valid JSON", True)
            check("Has boot_script_hash field",
                  "boot_script_hash" in data or "hash" in data or "script_hash" in data,
                  f"keys: {list(data.keys())[:5]}")
        except Exception as e:
            check("boot_integrity.json is valid JSON", False, str(e))
    else:
        check("boot_integrity.json exists", False, "can be created via boot_guard.py --update-hash")
    print()

    # --- 6. AGENTS.md consistency ---
    print("[6] AGENTS.md consistency:")
    agents_md = ROOT / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text(encoding="utf-8", errors="replace")
        check("AGENTS.md exists", True)
        check("Has Rule #0.06",
              "Rule #0.06" in content,
              "Proven Worker Boot Procedure rule")
        check("Has INCIDENT 016",
              "INCIDENT 016" in content,
              "Boot Method Resolution incident")
        check("Rule #0.06 references skynet_worker_boot.py",
              "skynet_worker_boot.py" in content,
              "canonical boot script in rule")
        check("Self-prompt disabled note",
              "PERMANENTLY DISABLED" in content and "self_prompt" in content.lower(),
              "self-prompt daemon status documented")
    else:
        check("AGENTS.md exists", False)
    print()

    # --- Summary ---
    total = PASS + FAIL
    print("=" * 60)
    print(f"  RESULTS: {PASS}/{total} passed, {FAIL}/{total} failed")
    if FAIL == 0:
        print("  STATUS: ALL CHECKS PASSED ✓")
    else:
        print(f"  STATUS: {FAIL} CHECK(S) FAILED ✗")
    print("=" * 60)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
