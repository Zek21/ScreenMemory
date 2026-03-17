#!/usr/bin/env python3
"""
skynet_commit.py — Safe git commit wrapper for Skynet.

Ensures all whitelisted data files are staged before commit,
validates .gitignore patterns, and prevents silent data loss.

ROOT CAUSE (Commit 5278f02): .gitignore had `data/` which blocks the entire
directory. Git negation patterns (`!data/brain_config.json`) don't work when
the parent DIRECTORY is excluded. Fix: `data/` → `data/*`.

Usage:
  python tools/skynet_commit.py -m "commit message"
  python tools/skynet_commit.py -m "commit message" --verify-only
  python tools/skynet_commit.py --check-gitignore
"""
# signed: orchestrator

import argparse
import io
import json
import os
import subprocess
import sys

# Fix cp1252 encoding on Windows — enable UTF-8 output
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Critical data files that MUST be tracked in git
WHITELISTED_DATA_FILES = [
    'data/brain_config.json',
    'data/agent_profiles.json',
    'data/boot_protocol.json',
    'data/critical_processes.json',
    'data/incidents.json',
    'data/version_history.json',
    'data/level4_architecture.md',
    'data/skynet_bootstrap.md',
]

# Runtime state files that must NEVER be committed
RUNTIME_STATE_FILES = [
    'data/realtime.json',
    'data/workers.json',
    'data/orchestrator.json',
    'data/worker_scores.json',
    'data/spam_log.json',
    'data/dispatch_log.json',
    'data/convene_gate.json',
    'data/convene_sessions.json',
]


def run_git(*args, check=True):
    """Run a git command and return output."""
    result = subprocess.run(
        ['git', '--no-pager'] + list(args),
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if check and result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout.strip(), None


def check_gitignore_pattern():
    """
    Verify .gitignore uses `data/*` (not `data/`) so negation patterns work.
    Returns (ok: bool, issues: list[str]).
    """
    issues = []
    gitignore_path = '.gitignore'

    if not os.path.exists(gitignore_path):
        issues.append(".gitignore file not found")
        return False, issues

    with open(gitignore_path, 'r') as f:
        lines = f.readlines()

    has_data_star = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Check for the dangerous `data/` pattern (blocks entire directory)
        if stripped == 'data/' or stripped == 'data':
            issues.append(
                f"Line {i}: '{stripped}' blocks entire directory — "
                f"negation patterns won't work. Change to 'data/*'"
            )
        if stripped == 'data/*':
            has_data_star = True

    if not has_data_star and not issues:
        issues.append("Missing 'data/*' pattern — data directory not properly configured")

    return len(issues) == 0, issues


def check_whitelisted_files_tracked():
    """
    Verify whitelisted data files are NOT ignored by git.
    Returns (ok: bool, issues: list[str]).
    """
    issues = []

    for fpath in WHITELISTED_DATA_FILES:
        if not os.path.exists(fpath):
            continue  # File doesn't exist yet, skip

        output, err = run_git('check-ignore', '-v', fpath, check=False)
        if output:  # If git reports the file is ignored
            issues.append(f"BLOCKED: {fpath} is ignored by git ({output})")

    return len(issues) == 0, issues


def check_runtime_files_ignored():
    """
    Verify runtime state files ARE ignored by git.
    Returns (ok: bool, issues: list[str]).
    """
    issues = []

    for fpath in RUNTIME_STATE_FILES:
        if not os.path.exists(fpath):
            continue

        output, err = run_git('check-ignore', '-v', fpath, check=False)
        if not output:  # File is NOT ignored — danger!
            issues.append(f"EXPOSED: {fpath} is NOT ignored — runtime state would be committed")

    return len(issues) == 0, issues


def auto_stage_whitelisted():
    """Stage all whitelisted data files that exist and have changes."""
    staged = []
    for fpath in WHITELISTED_DATA_FILES:
        if not os.path.exists(fpath):
            continue

        # Check if file has changes (modified or untracked)
        status_out, _ = run_git('status', '--porcelain', fpath, check=False)
        if status_out:  # Has changes
            run_git('add', '-f', fpath, check=False)
            staged.append(fpath)

    return staged


def safe_commit(message, verify_only=False):
    """
    Perform a safe commit with all pre-flight checks.

    Steps:
      1. Validate .gitignore pattern (data/* not data/)
      2. Verify whitelisted files aren't ignored
      3. Auto-stage whitelisted data files
      4. Verify runtime state files ARE ignored
      5. Commit with message
      6. Post-commit verification

    Returns (success: bool, report: str).
    """
    report_lines = []
    all_ok = True

    # Check 1: .gitignore pattern
    ok, issues = check_gitignore_pattern()
    if ok:
        report_lines.append("✅ .gitignore pattern correct (data/*)")
    else:
        all_ok = False
        for issue in issues:
            report_lines.append(f"❌ {issue}")

    # Check 2: Whitelisted files not ignored
    ok, issues = check_whitelisted_files_tracked()
    if ok:
        report_lines.append("✅ All whitelisted data files trackable")
    else:
        all_ok = False
        for issue in issues:
            report_lines.append(f"❌ {issue}")

    # Check 3: Runtime state files are ignored
    ok, issues = check_runtime_files_ignored()
    if ok:
        report_lines.append("✅ Runtime state files properly ignored")
    else:
        for issue in issues:
            report_lines.append(f"⚠ {issue}")

    if verify_only:
        return all_ok, '\n'.join(report_lines)

    if not all_ok:
        report_lines.append("\n❌ Pre-flight checks failed — commit aborted")
        return False, '\n'.join(report_lines)

    # Auto-stage whitelisted data files
    staged = auto_stage_whitelisted()
    if staged:
        report_lines.append(f"📦 Auto-staged {len(staged)} data files: {', '.join(os.path.basename(f) for f in staged)}")

    # Commit
    out, err = run_git('commit', '-m', message, check=False)
    if err and 'nothing to commit' in err:
        report_lines.append("ℹ Nothing to commit (working tree clean)")
        return True, '\n'.join(report_lines)
    elif err:
        report_lines.append(f"❌ Commit failed: {err}")
        return False, '\n'.join(report_lines)

    # Post-commit verification: check committed files include expected data
    last_commit, _ = run_git('log', '--oneline', '-1')
    files_in_commit, _ = run_git('diff', '--name-only', 'HEAD~1..HEAD', check=False)
    report_lines.append(f"✅ Committed: {last_commit}")

    if files_in_commit:
        committed_data = [f for f in files_in_commit.split('\n') if f.startswith('data/')]
        if committed_data:
            report_lines.append(f"📊 Data files in commit: {', '.join(committed_data)}")

    return True, '\n'.join(report_lines)


def main():
    parser = argparse.ArgumentParser(description='Safe git commit wrapper for Skynet')
    parser.add_argument('-m', '--message', help='Commit message')
    parser.add_argument('--verify-only', action='store_true',
                        help='Only run checks, do not commit')
    parser.add_argument('--check-gitignore', action='store_true',
                        help='Only check .gitignore pattern')
    parser.add_argument('--stage', action='store_true',
                        help='Auto-stage whitelisted files without committing')
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if args.check_gitignore:
        ok, issues = check_gitignore_pattern()
        for issue in issues:
            print(f"  {'✅' if ok else '❌'} {issue}")
        if ok:
            print("✅ .gitignore pattern is correct")
        sys.exit(0 if ok else 1)

    if args.stage:
        staged = auto_stage_whitelisted()
        if staged:
            print(f"✅ Staged {len(staged)} files: {', '.join(staged)}")
        else:
            print("ℹ No whitelisted files need staging")
        sys.exit(0)

    if args.verify_only:
        ok, report = safe_commit("", verify_only=True)
        print(report)
        sys.exit(0 if ok else 1)

    if not args.message:
        print("❌ Commit message required (-m 'message')")
        sys.exit(1)

    ok, report = safe_commit(args.message)
    print(report)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
