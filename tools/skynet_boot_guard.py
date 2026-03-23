"""
skynet_boot_guard.py — Boot Procedure Integrity Guard (Rule #0.06)

Protects the PROVEN worker boot procedure (docs/WORKER_BOOT_PROCEDURE.txt).
The boot procedure was tested and confirmed working on 2026-03-18.

Any changes to the boot script require:
  1. Proof that the new method is superior (tested and confirmed)
  2. Update to the known-good hash via --update-hash
  3. Documentation of the change in AGENTS.md

Changes without proof are treated as SECURITY INCIDENTS.

INCIDENT 016 (2026-03-18): Multiple boot methods caused repeated failures.
This guard ensures only the proven method is used going forward.
"""
# signed: orchestrator

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ─── Boot Method Registry ───────────────────────────────────────────────────

AUTHORIZED_BOOT_SCRIPT = "tools/skynet_worker_boot.py"
AUTHORIZED_PROCEDURE_DOC = "docs/WORKER_BOOT_PROCEDURE.txt"
DEPRECATED_METHODS = [
    "tools/new_chat.ps1",           # Old PowerShell method
    "tools/skynet_start.py",        # Old unified start (worker opening parts only)
    "tools/set_copilot_cli.py",     # Replaced by inline step in boot script
]

INTEGRITY_FILE = ROOT / "data" / "boot_integrity.json"
BOOT_LOG_FILE = ROOT / "data" / "boot_log.json"

# Directories and patterns to audit for deprecated usage
AUDIT_DIRS = [ROOT / "tools"]
AUDIT_ROOT_GLOBS = ["*.py", "*.ps1"]
AUDIT_EXTENSIONS = {".py", ".ps1"}

# Files to skip during audit (self + the procedure doc)
AUDIT_SKIP_NAMES = {"skynet_boot_guard.py", "WORKER_BOOT_PROCEDURE.txt"}

# Patterns that indicate a direct call/subprocess invocation (CRITICAL)
_CRITICAL_PATTERNS = [
    r"subprocess\.\w+\(.*(?:new_chat\.ps1|set_copilot_cli\.py)",
    r"Start-Process.*(?:new_chat\.ps1|set_copilot_cli\.py)",
    r"&\s+['\"]?.*(?:new_chat\.ps1|set_copilot_cli\.py)",
    r"powershell.*(?:new_chat\.ps1|set_copilot_cli\.py)",
    r"import\s+.*set_copilot_cli",
    r"from\s+.*set_copilot_cli\s+import",
    r"os\.system\(.*(?:new_chat\.ps1|set_copilot_cli\.py)",
]

# ─── Hash Helpers ────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str | None:
    """Return hex SHA-256 of a file, or None if it doesn't exist."""
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _load_json(path: Path, default=None):
    if not path.is_file():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ─── Hash Verification ──────────────────────────────────────────────────────

def verify_boot_integrity(alert_bus: bool = False) -> tuple[bool, dict]:
    """Compare current file hashes against stored known-good hashes.

    Returns (ok, details) where details contains per-file status and any
    mismatch information.
    """
    boot_script = ROOT / AUTHORIZED_BOOT_SCRIPT
    procedure_doc = ROOT / AUTHORIZED_PROCEDURE_DOC

    current_script_hash = _sha256(boot_script)
    current_doc_hash = _sha256(procedure_doc)

    details: dict = {
        "boot_script_exists": boot_script.is_file(),
        "procedure_doc_exists": procedure_doc.is_file(),
        "boot_script_hash": current_script_hash,
        "procedure_doc_hash": current_doc_hash,
        "integrity_file_exists": INTEGRITY_FILE.is_file(),
        "mismatches": [],
    }

    # First-run: no integrity file yet — initialize it
    if not INTEGRITY_FILE.is_file():
        record = {
            "boot_script_hash": current_script_hash or "",
            "procedure_doc_hash": current_doc_hash or "",
            "last_verified": _now_iso(),
            "last_updated_by": "auto-init",
            "update_reason": "Initial codification",
        }
        _save_json(INTEGRITY_FILE, record)
        details["initialized"] = True
        return True, details

    stored = _load_json(INTEGRITY_FILE, {})
    ok = True

    # Check boot script hash
    stored_script = stored.get("boot_script_hash", "")
    if current_script_hash is None:
        details["mismatches"].append({
            "file": AUTHORIZED_BOOT_SCRIPT,
            "issue": "FILE_MISSING",
            "stored_hash": stored_script,
        })
        ok = False
    elif stored_script and current_script_hash != stored_script:
        details["mismatches"].append({
            "file": AUTHORIZED_BOOT_SCRIPT,
            "issue": "HASH_MISMATCH",
            "stored_hash": stored_script,
            "current_hash": current_script_hash,
        })
        ok = False

    # Check procedure doc hash
    stored_doc = stored.get("procedure_doc_hash", "")
    if current_doc_hash is None:
        details["mismatches"].append({
            "file": AUTHORIZED_PROCEDURE_DOC,
            "issue": "FILE_MISSING",
            "stored_hash": stored_doc,
        })
        ok = False
    elif stored_doc and current_doc_hash != stored_doc:
        details["mismatches"].append({
            "file": AUTHORIZED_PROCEDURE_DOC,
            "issue": "HASH_MISMATCH",
            "stored_hash": stored_doc,
            "current_hash": current_doc_hash,
        })
        ok = False

    details["stored"] = stored

    if not ok and alert_bus:
        _post_bus_alert(details["mismatches"])

    return ok, details


def update_boot_hash(updater: str, reason: str) -> dict:
    """Recompute and store known-good hashes. Requires explicit invocation."""
    boot_script = ROOT / AUTHORIZED_BOOT_SCRIPT
    procedure_doc = ROOT / AUTHORIZED_PROCEDURE_DOC

    record = {
        "boot_script_hash": _sha256(boot_script) or "",
        "procedure_doc_hash": _sha256(procedure_doc) or "",
        "last_verified": _now_iso(),
        "last_updated_by": updater,
        "update_reason": reason,
    }
    _save_json(INTEGRITY_FILE, record)
    return record

# ─── Deprecation Guard ───────────────────────────────────────────────────────

def audit_deprecated_usage() -> list[tuple[str, int, str, str]]:
    """Scan project files for references to deprecated boot methods.

    Returns list of (file_rel_path, line_number, matched_text, severity).
    Severity is CRITICAL for direct calls/subprocesses, WARNING for
    comments and string references.
    """
    # Build simple match tokens from deprecated method basenames
    deprecated_basenames = [Path(d).name for d in DEPRECATED_METHODS]
    critical_res = [re.compile(p, re.IGNORECASE) for p in _CRITICAL_PATTERNS]

    results: list[tuple[str, int, str, str]] = []

    files_to_scan: list[Path] = []

    # Collect files from audit directories
    for d in AUDIT_DIRS:
        if d.is_dir():
            for fp in d.rglob("*"):
                if fp.is_file() and fp.suffix in AUDIT_EXTENSIONS:
                    files_to_scan.append(fp)

    # Collect root-level files
    for pattern in AUDIT_ROOT_GLOBS:
        for fp in ROOT.glob(pattern):
            if fp.is_file():
                files_to_scan.append(fp)

    # Also scan .ps1 files directly in root
    for fp in ROOT.glob("*.ps1"):
        if fp.is_file() and fp not in files_to_scan:
            files_to_scan.append(fp)

    for fp in files_to_scan:
        if fp.name in AUDIT_SKIP_NAMES:
            continue

        rel = str(fp.relative_to(ROOT)).replace("\\", "/")

        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for lineno, line in enumerate(lines, 1):
            for basename in deprecated_basenames:
                if basename in line:
                    # Determine severity
                    severity = "WARNING"
                    for crx in critical_res:
                        if crx.search(line):
                            severity = "CRITICAL"
                            break

                    # Heuristic: if the line is a comment, stay WARNING
                    stripped = line.lstrip()
                    if stripped.startswith("#") or stripped.startswith("//"):
                        severity = "WARNING"
                    if stripped.startswith("<!--"):
                        severity = "WARNING"

                    results.append((rel, lineno, line.strip(), severity))
                    break  # one match per line is sufficient

    return results

# ─── Semantic Content Validation (INCIDENT 020 defense) ──────────────────────

# Patterns that MUST exist in the boot script (pyautogui for Chromium overlays)
_REQUIRED_PATTERNS = {
    "pyautogui_import": {
        "pattern": r"^\s*import\s+pyautogui",
        "description": "pyautogui must be imported (INCIDENT 013: only hardware-level input reaches Chromium overlays)",
    },
    "pyperclip_import": {
        "pattern": r"^\s*import\s+pyperclip",
        "description": "pyperclip must be imported for clipboard save/restore",
    },
    "pyautogui_click": {
        "pattern": r"pyautogui\.click\s*\(",
        "description": "pyautogui.click() must be used for Chromium dropdown interactions",
    },
    "pyautogui_press": {
        "pattern": r"pyautogui\.press\s*\(",
        "description": "pyautogui.press() must be used for dropdown navigation",
    },
    "pyautogui_hotkey": {
        "pattern": r"pyautogui\.hotkey\s*\(",
        "description": "pyautogui.hotkey() must be used for clipboard paste (Ctrl+V)",
    },
}

# Patterns that MUST NOT exist (Win32 methods that cannot reach Chromium overlays)
_FORBIDDEN_PATTERNS = {
    "shadow_input_import": {
        "pattern": r"(?:from\s+tools\.shadow_input\s+import|import\s+.*shadow_input)",
        "description": "shadow_input uses PostMessage/keybd_event which CANNOT reach Chromium overlays (INCIDENT 013)",
    },
    "postmessage_click": {
        "pattern": r"PostMessage.*WM_LBUTTONDOWN",
        "description": "PostMessage WM_LBUTTONDOWN cannot click Chromium-rendered dropdowns (INCIDENT 013)",
    },
    "keybd_event_nav": {
        "pattern": r"keybd_event.*(?:VK_DOWN|VK_UP|VK_RETURN)",
        "description": "keybd_event cannot navigate Chromium quickpick overlays (INCIDENT 013)",
    },
    "sendinput_usage": {
        "pattern": r"SendInput\s*\(",
        "description": "SendInput cannot interact with Chromium-rendered VS Code overlays",
    },
}


def validate_boot_content(alert_bus: bool = False) -> tuple[bool, dict]:
    """Semantic validation of boot script content.

    Ensures the boot script uses pyautogui (hardware-level input) for
    Chromium overlay interactions, NOT Win32 PostMessage/keybd_event.

    This check exists because of INCIDENT 020: a worker replaced pyautogui
    with shadow_input (PostMessage/keybd_event), which CANNOT interact with
    Chromium-rendered VS Code dropdowns (proven by INCIDENT 013). The hash
    guard was useless because the worker updated the hash after rewriting.

    Returns (ok, details) with per-check results.
    """
    boot_script = ROOT / AUTHORIZED_BOOT_SCRIPT
    details: dict = {
        "boot_script_exists": boot_script.is_file(),
        "required_missing": [],
        "forbidden_found": [],
    }

    if not boot_script.is_file():
        details["required_missing"].append({
            "check": "file_exists",
            "description": "Boot script file is missing entirely",
        })
        if alert_bus:
            _post_semantic_alert(details)
        return False, details

    try:
        source = boot_script.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        details["required_missing"].append({
            "check": "file_readable",
            "description": f"Cannot read boot script: {e}",
        })
        return False, details

    ok = True

    # Check required patterns
    for name, spec in _REQUIRED_PATTERNS.items():
        if not re.search(spec["pattern"], source, re.MULTILINE):
            details["required_missing"].append({
                "check": name,
                "description": spec["description"],
            })
            ok = False

    # Check forbidden patterns (skip comments)
    source_lines = source.splitlines()
    for name, spec in _FORBIDDEN_PATTERNS.items():
        rx = re.compile(spec["pattern"], re.IGNORECASE)
        for lineno, line in enumerate(source_lines, 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # skip comments
            if rx.search(line):
                details["forbidden_found"].append({
                    "check": name,
                    "description": spec["description"],
                    "line": lineno,
                    "text": line.strip(),
                })
                ok = False

    if not ok and alert_bus:
        _post_semantic_alert(details)

    return ok, details


def _post_semantic_alert(details: dict):
    """POST a CRITICAL semantic violation alert to the Skynet bus."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        missing = [m["check"] for m in details.get("required_missing", [])]
        forbidden = [f["check"] for f in details.get("forbidden_found", [])]
        parts = []
        if missing:
            parts.append(f"MISSING: {','.join(missing)}")
        if forbidden:
            parts.append(f"FORBIDDEN: {','.join(forbidden)}")
        guarded_publish({
            "sender": "boot_guard",
            "topic": "orchestrator",
            "type": "alert",
            "content": f"BOOT_SEMANTIC_VIOLATION (INCIDENT 020): {'; '.join(parts)}",
        })
    except Exception:
        pass


# ─── Boot Log ────────────────────────────────────────────────────────────────

def log_boot_attempt(
    name: str,
    hwnd: int,
    success: bool,
    method: str,
    operator: str,
) -> dict:
    """Append a boot attempt to the persistent boot log."""
    entry = {
        "timestamp": _now_iso(),
        "worker_name": name,
        "hwnd": hwnd,
        "method_used": method,
        "success": success,
        "boot_script_hash": _sha256(ROOT / AUTHORIZED_BOOT_SCRIPT) or "N/A",
        "operator": operator,
    }

    log = _load_json(BOOT_LOG_FILE, [])
    if not isinstance(log, list):
        log = []
    log.append(entry)
    _save_json(BOOT_LOG_FILE, log)
    return entry


def get_boot_log(limit: int = 20) -> list[dict]:
    """Return the most recent boot log entries."""
    log = _load_json(BOOT_LOG_FILE, [])
    if not isinstance(log, list):
        return []
    return log[-limit:]

# ─── Bus Alert ───────────────────────────────────────────────────────────────

def _post_bus_alert(mismatches: list[dict]):
    """POST a CRITICAL integrity alert to the Skynet bus via SpamGuard."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        summary = "; ".join(
            f"{m['file']}: {m['issue']}" for m in mismatches
        )
        guarded_publish({
            "sender": "boot_guard",
            "topic": "orchestrator",
            "type": "alert",
            "content": f"BOOT_INTEGRITY_VIOLATION: {summary}",
        })
    except Exception:
        pass  # bus may be down — alert is best-effort

# ─── CLI ─────────────────────────────────────────────────────────────────────

def _cmd_verify(args):
    ok, details = verify_boot_integrity(alert_bus=args.alert)

    if details.get("initialized"):
        print("INITIALIZED -- first-time hash recorded.")
        print(f"  Boot script hash : {details['boot_script_hash'] or 'FILE NOT FOUND'}")
        print(f"  Procedure doc hash: {details['procedure_doc_hash'] or 'FILE NOT FOUND'}")
        print(f"  Stored in: {INTEGRITY_FILE}")
        return

    if ok:
        print("BOOT INTEGRITY: OK")
        print(f"  Boot script : {details['boot_script_hash']}")
        print(f"  Procedure doc: {details['procedure_doc_hash']}")
        stored = details.get("stored", {})
        print(f"  Last verified: {stored.get('last_verified', 'unknown')}")
        print(f"  Updated by   : {stored.get('last_updated_by', 'unknown')}")
    else:
        print("!! BOOT INTEGRITY: FAILED !!")
        for m in details["mismatches"]:
            print(f"  [{m['issue']}] {m['file']}")
            if m["issue"] == "HASH_MISMATCH":
                print(f"    Stored : {m['stored_hash']}")
                print(f"    Current: {m['current_hash']}")
            elif m["issue"] == "FILE_MISSING":
                print(f"    Expected hash: {m['stored_hash']}")
        print()
        print("  This may be a security incident. Run with --update-hash")
        print("  only if the change was intentional and tested.")
        sys.exit(1)


def _cmd_audit(args):
    findings = audit_deprecated_usage()
    if not findings:
        print("DEPRECATION AUDIT: CLEAN — no deprecated boot method usage found.")
        return

    crits = [f for f in findings if f[3] == "CRITICAL"]
    warns = [f for f in findings if f[3] == "WARNING"]

    print(f"DEPRECATION AUDIT: {len(findings)} finding(s)")
    print(f"  CRITICAL: {len(crits)}  |  WARNING: {len(warns)}")
    print()

    for filepath, lineno, text, severity in sorted(findings, key=lambda f: (f[3] != "CRITICAL", f[0], f[1])):
        tag = "CRIT" if severity == "CRITICAL" else "WARN"
        print(f"  [{tag}] {filepath}:{lineno}")
        print(f"         {text}")

    if crits:
        print()
        print("  CRITICAL findings indicate direct usage of deprecated boot methods.")
        print("  These MUST be migrated to the authorized procedure.")
        sys.exit(1)


def _cmd_log(args):
    entries = get_boot_log(limit=args.limit)
    if not entries:
        print("BOOT LOG: empty (no boot attempts recorded)")
        return

    print(f"BOOT LOG: last {len(entries)} attempt(s)")
    print()
    for e in entries:
        status = "OK" if e.get("success") else "FAIL"
        print(f"  [{status}] {e.get('timestamp', '?')}  "
              f"worker={e.get('worker_name', '?')}  "
              f"hwnd={e.get('hwnd', '?')}  "
              f"method={e.get('method_used', '?')}  "
              f"operator={e.get('operator', '?')}")


def _cmd_update_hash(args):
    if not args.updater or not args.reason:
        print("ERROR: --updater and --reason are required with --update-hash")
        sys.exit(1)

    record = update_boot_hash(args.updater, args.reason)
    print("BOOT HASH UPDATED")
    print(f"  Boot script hash : {record['boot_script_hash'] or 'FILE NOT FOUND'}")
    print(f"  Procedure doc hash: {record['procedure_doc_hash'] or 'FILE NOT FOUND'}")
    print(f"  Updated by       : {record['last_updated_by']}")
    print(f"  Reason           : {record['update_reason']}")
    print(f"  Timestamp        : {record['last_verified']}")


def _cmd_semantic(args):
    ok, details = validate_boot_content(alert_bus=args.alert)

    if ok:
        print("BOOT SEMANTIC CHECK: OK")
        print("  All required patterns present (pyautogui, pyperclip)")
        print("  No forbidden patterns found (shadow_input, PostMessage)")
    else:
        print("!! BOOT SEMANTIC CHECK: FAILED !!")
        print("  INCIDENT 013/020: Boot script MUST use pyautogui for Chromium overlays.")
        print()
        for m in details.get("required_missing", []):
            print(f"  [MISSING] {m['check']}: {m['description']}")
        for f in details.get("forbidden_found", []):
            print(f"  [FORBIDDEN] {f['check']} at line {f['line']}: {f['description']}")
            print(f"              {f['text']}")
        print()
        print("  Win32 PostMessage/keybd_event/SendInput CANNOT reach Chromium overlays.")
        print("  Only pyautogui (hardware-level input) works. See INCIDENT 013.")
        sys.exit(1)


def _cmd_status(args):
    print("=" * 60)
    print("  BOOT GUARD STATUS REPORT")
    print("=" * 60)

    # Semantic check (INCIDENT 020 — highest priority)
    print()
    print("--- Semantic Content Check (INCIDENT 020) ---")
    sem_ok, sem_details = validate_boot_content(alert_bus=False)
    if sem_ok:
        print("  Status: OK — pyautogui confirmed, no forbidden Win32 input methods")
    else:
        print("  Status: !! FAILED !!")
        for m in sem_details.get("required_missing", []):
            print(f"    [MISSING] {m['check']}: {m['description']}")
        for f in sem_details.get("forbidden_found", []):
            print(f"    [FORBIDDEN] {f['check']} L{f['line']}: {f['text']}")

    # Integrity
    print()
    print("--- Integrity Check ---")
    ok, details = verify_boot_integrity(alert_bus=False)
    if details.get("initialized"):
        print("  INITIALIZED -- first-time hash recorded this run.")
        # Re-verify now that hashes are stored
        ok, details = verify_boot_integrity(alert_bus=False)

    if ok:
        print("  Status: OK")
        stored = details.get("stored", {})
        print(f"  Last verified : {stored.get('last_verified', 'unknown')}")
        print(f"  Updated by    : {stored.get('last_updated_by', 'unknown')}")
        print(f"  Reason        : {stored.get('update_reason', 'unknown')}")
    else:
        print("  Status: !! FAILED !!")
        for m in details["mismatches"]:
            print(f"    [{m['issue']}] {m['file']}")

    # Audit
    print()
    print("--- Deprecation Audit ---")
    findings = audit_deprecated_usage()
    crits = [f for f in findings if f[3] == "CRITICAL"]
    warns = [f for f in findings if f[3] == "WARNING"]
    if not findings:
        print("  Status: CLEAN")
    else:
        print(f"  Findings: {len(crits)} CRITICAL, {len(warns)} WARNING")
        for filepath, lineno, text, severity in sorted(findings, key=lambda f: (f[3] != "CRITICAL", f[0], f[1])):
            tag = "CRIT" if severity == "CRITICAL" else "WARN"
            print(f"    [{tag}] {filepath}:{lineno}")

    # Boot log summary
    print()
    print("--- Boot Log Summary ---")
    entries = get_boot_log(limit=10)
    if not entries:
        print("  No boot attempts recorded.")
    else:
        successes = sum(1 for e in entries if e.get("success"))
        failures = len(entries) - successes
        print(f"  Total (last 10): {len(entries)}  |  OK: {successes}  |  FAIL: {failures}")
        if entries:
            last = entries[-1]
            status = "OK" if last.get("success") else "FAIL"
            print(f"  Last boot: [{status}] {last.get('timestamp', '?')} "
                  f"worker={last.get('worker_name', '?')} "
                  f"method={last.get('method_used', '?')}")

    # Authorized method reminder
    print()
    print("--- Authorized Boot Method ---")
    print(f"  Script : {AUTHORIZED_BOOT_SCRIPT}")
    print(f"  Doc    : {AUTHORIZED_PROCEDURE_DOC}")
    print(f"  Deprecated: {', '.join(Path(d).name for d in DEPRECATED_METHODS)}")
    print()
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Boot Procedure Integrity Guard (Rule #0.06)",
    )
    sub = parser.add_subparsers(dest="command")

    # verify
    p_verify = sub.add_parser("verify", help="Verify boot script integrity (hash check)")
    p_verify.add_argument("--alert", action="store_true", help="POST alert to bus on mismatch")

    # audit
    sub.add_parser("audit", help="Scan for deprecated boot method usage")

    # semantic
    p_sem = sub.add_parser("semantic", help="Validate boot script uses pyautogui (INCIDENT 020)")
    p_sem.add_argument("--alert", action="store_true", help="POST alert to bus on violation")

    # log
    p_log = sub.add_parser("log", help="Show recent boot log entries")
    p_log.add_argument("--limit", type=int, default=20, help="Number of entries to show")

    # status
    sub.add_parser("status", help="Full status (integrity + audit + log summary)")

    # --update-hash (top-level flag)
    parser.add_argument("--update-hash", action="store_true",
                        help="Update known-good hash (requires --updater and --reason)")
    parser.add_argument("--updater", type=str, default="",
                        help="Who is updating the hash")
    parser.add_argument("--reason", type=str, default="",
                        help="Why the hash is being updated")

    args = parser.parse_args()

    if args.update_hash:
        _cmd_update_hash(args)
        return

    if args.command == "verify":
        _cmd_verify(args)
    elif args.command == "audit":
        _cmd_audit(args)
    elif args.command == "semantic":
        _cmd_semantic(args)
    elif args.command == "log":
        _cmd_log(args)
    elif args.command == "status":
        _cmd_status(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
