#!/usr/bin/env python3
"""
Git pre-commit guard: ensures critical data files are never silently
excluded from commits by .gitignore misconfiguration.

Three checks:
  1. .gitignore uses 'data/*' (glob), NOT 'data/' (directory block)
  2. Whitelisted files are not ignored by git check-ignore
  3. Modified whitelisted files that are unstaged get a warning

Exit code 0 = all checks pass, 1 = commit blocked.

Usage:
  python tools/skynet_git_guard.py          # run all checks
  python tools/skynet_git_guard.py --check  # same, explicit flag

Called automatically by .git/hooks/pre-commit.
"""
# signed: alpha

import subprocess
import sys
import os
import re

WHITELIST = [
    "data/brain_config.json",
    "data/agent_profiles.json",
    "data/boot_protocol.json",
    "data/critical_processes.json",
    "data/incidents.json",
    "data/version_history.json",
    "data/level4_architecture.md",
    "data/skynet_bootstrap.md",
]

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=10
    )


def _repo_root() -> str:
    """Find the repo root from cwd."""
    r = _run(["git", "rev-parse", "--show-toplevel"])
    if r.returncode != 0:
        print(f"{RED}FATAL: not inside a git repository{RESET}")
        sys.exit(1)
    return r.stdout.strip()


def check_gitignore_pattern(root: str) -> bool:
    """Ensure .gitignore uses 'data/*' not 'data/'."""
    gitignore = os.path.join(root, ".gitignore")
    if not os.path.isfile(gitignore):
        print(f"{YELLOW}WARN: no .gitignore found{RESET}")
        return True  # no gitignore = no blocking pattern

    with open(gitignore, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Match exactly 'data/' but NOT 'data/*' or negation patterns
        if stripped == "data/":
            print(f"{RED}BLOCKED: .gitignore line {i} has 'data/' which blocks ALL negation patterns.{RESET}")
            print(f"  Fix: change 'data/' to 'data/*' so !data/file.json patterns work.")
            return False

    # Verify 'data/*' exists
    has_glob = any(line.strip() == "data/*" for line in lines)
    if not has_glob:
        print(f"{YELLOW}WARN: .gitignore has no 'data/*' pattern — data files may be unprotected{RESET}")

    return True


def check_files_not_ignored(root: str) -> bool:
    """Verify whitelisted files are NOT ignored by git."""
    existing = [f for f in WHITELIST if os.path.isfile(os.path.join(root, f))]
    if not existing:
        return True  # nothing to check

    # Without --verbose: exit 0 = some files ARE ignored, exit 1 = none ignored
    r = _run(["git", "check-ignore"] + existing, cwd=root)

    if r.returncode == 0 and r.stdout.strip():
        ignored = [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]
        if ignored:
            print(f"{RED}BLOCKED: These whitelisted files are being IGNORED by git:{RESET}")
            for f in ignored:
                print(f"  {RED}✗{RESET} {f}")
            print(f"  Fix: ensure .gitignore uses 'data/*' and has '!{f}' negation.")
            return False

    return True


def check_unstaged_whitelist(root: str) -> bool:
    """Warn if whitelisted files are modified but not staged."""
    r = _run(["git", "diff", "--name-only"], cwd=root)
    if r.returncode != 0:
        return True

    modified_unstaged = set(r.stdout.strip().splitlines())
    warnings = []
    for f in WHITELIST:
        if f in modified_unstaged:
            warnings.append(f)

    if warnings:
        print(f"{YELLOW}WARNING: These critical files are modified but NOT staged:{RESET}")
        for f in warnings:
            print(f"  {YELLOW}!{RESET} {f}")
        print(f"  Consider: git add {' '.join(warnings)}")
        # Warning only — don't block commit for unstaged files
    return True


def main() -> int:
    root = _repo_root()
    failed = False

    print(f"{BOLD}=== Skynet Git Guard ==={RESET}")

    # Check 1: .gitignore pattern
    if not check_gitignore_pattern(root):
        failed = True

    # Check 2: files not ignored
    if not check_files_not_ignored(root):
        failed = True

    # Check 3: unstaged warnings (never blocks)
    check_unstaged_whitelist(root)

    if failed:
        print(f"\n{RED}PRE-COMMIT BLOCKED. Fix the issues above and retry.{RESET}")
        return 1

    print(f"{GREEN}All checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
