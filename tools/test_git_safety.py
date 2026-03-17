#!/usr/bin/env python3
"""
test_git_safety.py — Tests for Skynet git safety system.

Validates:
  - .gitignore uses data/* (not data/) so negation patterns work
  - Whitelisted data files are not ignored by git
  - Runtime state files ARE properly ignored
  - skynet_git_guard.py pre-commit hook is importable and functional
  - skynet_commit.py safe commit wrapper works
  - skynet_apply_handler.py is importable

Usage:
  python tools/test_git_safety.py
  python -m pytest tools/test_git_safety.py -v
"""
# signed: orchestrator

import json
import os
import subprocess
import sys
import importlib
import importlib.util

# Ensure repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)


def run_git(*args):
    """Run git command, return (stdout, returncode)."""
    r = subprocess.run(
        ['git', '--no-pager'] + list(args),
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    return r.stdout.strip(), r.returncode


def test_gitignore_uses_data_star():
    """Verify .gitignore uses data/* not data/ to allow negation patterns."""
    with open('.gitignore', 'r') as f:
        content = f.read()

    lines = [l.strip() for l in content.split('\n')]
    assert 'data/*' in lines, ".gitignore must contain 'data/*'"
    assert 'data/' not in lines, ".gitignore must NOT contain 'data/' (blocks negation patterns)"
    assert 'data' not in lines, ".gitignore must NOT contain bare 'data'"


def test_negation_patterns_present():
    """Verify negation patterns exist for critical data files."""
    with open('.gitignore', 'r') as f:
        content = f.read()

    critical_files = [
        '!data/brain_config.json',
        '!data/agent_profiles.json',
        '!data/boot_protocol.json',
        '!data/incidents.json',
    ]
    for pattern in critical_files:
        assert pattern in content, f"Missing negation pattern: {pattern}"


def test_brain_config_tracked():
    """Verify brain_config.json is tracked in git (not ignored)."""
    if not os.path.exists('data/brain_config.json'):
        return  # Skip if file doesn't exist

    out, rc = run_git('check-ignore', '-v', 'data/brain_config.json')
    assert rc != 0 or out == '', \
        f"brain_config.json is IGNORED by git: {out}"


def test_whitelisted_files_not_ignored():
    """Verify ALL whitelisted data files are not ignored by git."""
    whitelisted = [
        'data/brain_config.json',
        'data/agent_profiles.json',
        'data/boot_protocol.json',
        'data/critical_processes.json',
        'data/incidents.json',
        'data/version_history.json',
    ]
    blocked = []
    for fpath in whitelisted:
        if not os.path.exists(fpath):
            continue
        out, rc = run_git('check-ignore', '-v', fpath)
        if rc == 0 and out:
            blocked.append(f"{fpath}: {out}")

    assert not blocked, f"Whitelisted files blocked by git: {blocked}"


def test_runtime_state_files_ignored():
    """Verify runtime state files ARE properly ignored."""
    runtime_files = [
        'data/realtime.json',
        'data/workers.json',
        'data/orchestrator.json',
        'data/worker_scores.json',
        'data/spam_log.json',
        'data/dispatch_log.json',
    ]
    exposed = []
    for fpath in runtime_files:
        if not os.path.exists(fpath):
            continue
        out, rc = run_git('check-ignore', '-v', fpath)
        if rc != 0 or not out:
            exposed.append(fpath)

    assert not exposed, f"Runtime files NOT ignored (would leak to git): {exposed}"


def test_skynet_git_guard_importable():
    """Verify skynet_git_guard.py is importable."""
    assert os.path.exists('tools/skynet_git_guard.py'), "skynet_git_guard.py not found"
    result = subprocess.run(
        [sys.executable, '-c', 'import py_compile; py_compile.compile("tools/skynet_git_guard.py", doraise=True)'],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"skynet_git_guard.py has syntax errors: {result.stderr}"


def test_skynet_commit_importable():
    """Verify skynet_commit.py is importable and has required functions."""
    assert os.path.exists('tools/skynet_commit.py'), "skynet_commit.py not found"
    result = subprocess.run(
        [sys.executable, '-c', 'import py_compile; py_compile.compile("tools/skynet_commit.py", doraise=True)'],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"skynet_commit.py has syntax errors: {result.stderr}"

    # Check required functions exist
    spec = importlib.util.spec_from_file_location("skynet_commit", "tools/skynet_commit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, 'safe_commit'), "Missing safe_commit function"
    assert hasattr(mod, 'check_gitignore_pattern'), "Missing check_gitignore_pattern function"
    assert hasattr(mod, 'auto_stage_whitelisted'), "Missing auto_stage_whitelisted function"


def test_skynet_apply_handler_importable():
    """Verify skynet_apply_handler.py is importable."""
    assert os.path.exists('tools/skynet_apply_handler.py'), "skynet_apply_handler.py not found"
    result = subprocess.run(
        [sys.executable, '-c', 'import py_compile; py_compile.compile("tools/skynet_apply_handler.py", doraise=True)'],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"skynet_apply_handler.py has syntax errors: {result.stderr}"


def test_skynet_commit_verify():
    """Run skynet_commit.py --verify-only and check output."""
    result = subprocess.run(
        [sys.executable, 'tools/skynet_commit.py', '--verify-only'],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    assert '✅' in result.stdout, f"Verify should pass with checkmarks: {result.stdout}"
    assert result.returncode == 0, f"Verify failed: {result.stdout}\n{result.stderr}"


def test_skynet_commit_check_gitignore():
    """Run skynet_commit.py --check-gitignore."""
    result = subprocess.run(
        [sys.executable, 'tools/skynet_commit.py', '--check-gitignore'],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    assert result.returncode == 0, f"Gitignore check failed: {result.stdout}\n{result.stderr}"


def test_brain_config_has_required_sections():
    """Verify brain_config.json has the permanent intelligence sections."""
    if not os.path.exists('data/brain_config.json'):
        return
    with open('data/brain_config.json') as f:
        config = json.load(f)

    required = ['intelligence_stack', 'backup_protection', 'resilient_dispatch', 'post_task_lifecycle']
    missing = [s for s in required if s not in config]
    assert not missing, f"brain_config.json missing permanent sections: {missing}"


def run_all_tests():
    """Run all tests and report results."""
    tests = [
        test_gitignore_uses_data_star,
        test_negation_patterns_present,
        test_brain_config_tracked,
        test_whitelisted_files_not_ignored,
        test_runtime_state_files_ignored,
        test_skynet_git_guard_importable,
        test_skynet_commit_importable,
        test_skynet_apply_handler_importable,
        test_skynet_commit_verify,
        test_skynet_commit_check_gitignore,
        test_brain_config_has_required_sections,
    ]

    passed = 0
    failed = 0
    errors = []

    for test_fn in tests:
        name = test_fn.__name__
        try:
            test_fn()
            print(f"  ✅ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {name}: {e}")
            failed += 1
            errors.append((name, str(e)))
        except Exception as e:
            print(f"  💥 {name}: {type(e).__name__}: {e}")
            failed += 1
            errors.append((name, f"{type(e).__name__}: {e}"))

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{passed+failed} passed")
    if errors:
        print(f"Failures:")
        for name, err in errors:
            print(f"  - {name}: {err}")

    return failed == 0


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
