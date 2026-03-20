#!/usr/bin/env python3
"""
Skynet Upgrade Scanner — Autonomous codebase improvement detector.

Scans all Python files in tools/ and core/ to identify and prioritize
improvement opportunities. Replaces manual TODO generation with automated,
repeatable analysis.

Checks performed:
  1. Missing test files (UNTESTED modules)
  2. TODO/FIXME/HACK/XXX inline comments
  3. Bare except clauses (security/reliability risk)
  4. Hardcoded credentials, paths, ports
  5. Large files (>500 lines) without module docstrings
  6. Syntax errors (py_compile)
  7. Dead code (defined but never referenced functions)

Usage:
    python tools/skynet_upgrade_scanner.py                  # full scan, human summary
    python tools/skynet_upgrade_scanner.py --json            # JSON to stdout
    python tools/skynet_upgrade_scanner.py --severity HIGH   # filter by severity
    python tools/skynet_upgrade_scanner.py --path core/      # scan specific dir
    python tools/skynet_upgrade_scanner.py --bus             # post summary to bus

# signed: alpha
"""

import argparse
import ast
import json
import os
import py_compile
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORT_FILE = DATA_DIR / "upgrade_scan_results.json"

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

# Patterns for hardcoded credential/secret detection  # signed: alpha
_CREDENTIAL_PATTERNS = [
    (re.compile(r"""(?:password|passwd|secret|api_key|apikey|token|auth)\s*=\s*['"][^'"]{4,}['"]""", re.I),
     "Possible hardcoded credential"),
    (re.compile(r"""['"](?:sk-|pk_|rk_|ghp_|gho_|github_pat_)[A-Za-z0-9]{10,}['"]"""),
     "Possible API key/token literal"),
]

# Patterns for hardcoded paths (Windows-specific absolute paths)  # signed: alpha
_PATH_PATTERNS = [
    (re.compile(r"""['"][A-Z]:\\(?:Users|Windows|Program)[^'"]{5,}['"]"""),
     "Hardcoded Windows absolute path"),
]

# Patterns for hardcoded ports (common suspicious port assignments)
_PORT_PATTERNS = [
    (re.compile(r"""\bport\s*=\s*(\d{4,5})\b""", re.I), "Hardcoded port assignment"),
]

# Known port constants that are intentional (Skynet architecture)
_KNOWN_PORTS = {8420, 8421, 8422, 8423, 8424, 8425, 9222}

# Critical modules that MUST have tests  # signed: alpha
_CRITICAL_MODULES = {
    "skynet_dispatch", "skynet_monitor", "skynet_spam_guard", "skynet_self",
    "skynet_scoring", "skynet_bus_relay", "skynet_worker_boot", "skynet_realtime",
    "god_console", "security", "input_guard", "capture", "ocr",
}

# Bare except regex for line-level detection (backup for AST failures)
_BARE_EXCEPT_RE = re.compile(r"^\s*except\s*:", re.MULTILINE)

# TODO/FIXME marker regex  # noqa: signed: alpha
_TODO_RE = re.compile(r"#\s*(TODO|FIXME|HACK|XXX|WORKAROUND)\b[:\s]*(.*)", re.I)


class Finding:
    """A single scan finding."""

    __slots__ = ("file", "line", "category", "severity", "description", "suggested_fix")

    def __init__(self, file, line, category, severity, description, suggested_fix=""):
        self.file = str(file)
        self.line = line
        self.category = category
        self.severity = severity
        self.description = description
        self.suggested_fix = suggested_fix

    def to_dict(self):
        return {
            "file": self.file,
            "line": self.line,
            "category": self.category,
            "severity": self.severity,
            "description": self.description,
            "suggested_fix": self.suggested_fix,
        }


class UpgradeScanner:
    """Scans Python files for improvement opportunities."""

    def __init__(self, scan_paths=None):
        self.scan_paths = scan_paths or [ROOT / "tools", ROOT / "core"]
        self.findings: list[Finding] = []
        self.files_scanned = 0
        self._all_py_files: list[Path] = []
        self._function_defs: dict[str, list[str]] = defaultdict(list)  # name -> [files]
        self._all_source_text = ""  # concatenated source for reference grep

    def scan(self):
        """Run all scan passes."""
        t0 = time.perf_counter()
        self._collect_files()
        self._build_function_index()
        for py_file in self._all_py_files:
            self._scan_file(py_file)
        self._check_dead_code()
        elapsed = time.perf_counter() - t0
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": round(elapsed, 2),
            "total_files_scanned": self.files_scanned,
            "findings_by_severity": self._severity_counts(),
            "findings": [f.to_dict() for f in self.findings],
        }

    # ── File Collection ──────────────────────────────────────────

    def _collect_files(self):
        """Gather all .py files from scan paths."""
        for scan_dir in self.scan_paths:
            if not scan_dir.is_dir():
                continue
            for py in sorted(scan_dir.rglob("*.py")):
                if "__pycache__" in str(py):
                    continue
                self._all_py_files.append(py)

    # ── Per-File Scanning ────────────────────────────────────────

    def _scan_file(self, py_file: Path):
        self.files_scanned += 1
        rel = self._rel(py_file)

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self.findings.append(Finding(rel, 0, "io_error", "CRITICAL",
                                        f"Cannot read file: {rel}"))
            return

        lines = source.splitlines()

        # 1. Syntax check (py_compile)
        self._check_syntax(py_file, rel)

        # 2. Missing tests
        self._check_missing_tests(py_file, rel)

        # 3. TODO/FIXME markers
        self._check_todo_markers(lines, rel)

        # 4. Bare except (AST-based, with line-level fallback)
        self._check_bare_except(source, lines, rel)

        # 5. Hardcoded credentials/paths/ports
        self._check_hardcoded(lines, rel)

        # 6. Large file without module docstring
        self._check_large_undocumented(source, lines, rel)

    # ── Check Implementations ────────────────────────────────────

    def _check_syntax(self, py_file: Path, rel: str):
        """py_compile each file for syntax errors."""
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            msg = str(e).split("\n")[0][:200]
            self.findings.append(Finding(rel, 0, "syntax_error", "CRITICAL",
                                        f"Syntax error: {msg}",
                                        "Fix the syntax error before deployment"))

    def _check_missing_tests(self, py_file: Path, rel: str):
        """Check if the module has a corresponding test file."""
        stem = py_file.stem
        if stem.startswith("_") or stem == "__init__":
            return

        tests_dir = ROOT / "tests"
        test_candidates = [
            tests_dir / f"test_{stem}.py",
            tests_dir / f"{stem}_test.py",
            ROOT / f"test_{stem}.py",
        ]
        has_test = any(t.exists() for t in test_candidates)

        if not has_test:
            severity = "HIGH" if stem in _CRITICAL_MODULES else "MEDIUM"
            self.findings.append(Finding(rel, 0, "missing_tests", severity,
                                        f"No test file found for {stem}",
                                        f"Create tests/test_{stem}.py"))

    def _check_todo_markers(self, lines: list[str], rel: str):
        """Extract TODO/FIXME/HACK/XXX comments."""
        for i, line in enumerate(lines, 1):
            m = _TODO_RE.search(line)
            if m:
                marker = m.group(1).upper()
                text = m.group(2).strip()[:120]
                severity = "MEDIUM" if marker in ("FIXME", "HACK", "XXX") else "LOW"
                self.findings.append(Finding(rel, i, "todo_marker", severity,
                                            f"{marker}: {text}",
                                            "Address or remove the inline marker"))

    def _check_bare_except(self, source: str, lines: list[str], rel: str):
        """Detect bare except clauses via AST (falls back to regex)."""
        try:
            tree = ast.parse(source, filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    self.findings.append(Finding(
                        rel, node.lineno, "bare_except", "MEDIUM",
                        "Bare except clause — catches all exceptions including SystemExit/KeyboardInterrupt",
                        "Specify exception type: except Exception: or except (TypeError, ValueError):"))
        except SyntaxError:
            # Fallback to regex for files with syntax errors
            for i, line in enumerate(lines, 1):
                if _BARE_EXCEPT_RE.match(line):
                    self.findings.append(Finding(
                        rel, i, "bare_except", "MEDIUM",
                        "Bare except clause (regex detection — AST parse failed)",
                        "Specify exception type"))

    def _check_hardcoded(self, lines: list[str], rel: str):
        """Check for hardcoded credentials, paths, and ports."""
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # skip comments

            for pattern, desc in _CREDENTIAL_PATTERNS:
                if pattern.search(line):
                    # Skip known false positives (empty strings, placeholders)
                    if re.search(r"""['"](?:YOUR_|CHANGE_ME|xxx|placeholder|example)""", line, re.I):
                        continue
                    self.findings.append(Finding(rel, i, "hardcoded_credential", "CRITICAL",
                                                desc, "Move to environment variable or config file"))

            for pattern, desc in _PATH_PATTERNS:
                if pattern.search(line):
                    self.findings.append(Finding(rel, i, "hardcoded_path", "LOW",
                                                desc, "Use pathlib relative paths or config"))

            for pattern, desc in _PORT_PATTERNS:
                m = pattern.search(line)
                if m:
                    port = int(m.group(1))
                    if port not in _KNOWN_PORTS:
                        self.findings.append(Finding(rel, i, "hardcoded_port", "LOW",
                                                    f"{desc} (port={port})",
                                                    "Consider making port configurable"))

    def _check_large_undocumented(self, source: str, lines: list[str], rel: str):
        """Flag files >500 lines with no module-level docstring."""
        if len(lines) <= 500:
            return
        try:
            tree = ast.parse(source)
            docstring = ast.get_docstring(tree)
            if not docstring:
                self.findings.append(Finding(
                    rel, 1, "large_undocumented", "LOW",
                    f"File has {len(lines)} lines but no module docstring",
                    "Add a module-level docstring explaining purpose and usage"))
        except SyntaxError:
            pass  # syntax errors caught elsewhere

    # ── Dead Code Detection ──────────────────────────────────────

    def _build_function_index(self):
        """Parse all files to build a function definition index."""
        # Also build concatenated source for grep-style reference checking
        source_parts = []
        for py_file in self._all_py_files:
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                source_parts.append(source)
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # Skip private/dunder/test functions
                        name = node.name
                        if name.startswith("__") and name.endswith("__"):
                            continue
                        if name.startswith("test_"):
                            continue
                        self._function_defs[name].append(str(self._rel(py_file)))
            except (SyntaxError, OSError):
                continue

        # Also scan tests/ and root .py files for references
        for extra_dir in [ROOT / "tests", ROOT]:
            if extra_dir == ROOT:
                py_files = list(extra_dir.glob("*.py"))
            else:
                py_files = list(extra_dir.rglob("*.py")) if extra_dir.is_dir() else []
            for pf in py_files:
                if "__pycache__" in str(pf):
                    continue
                try:
                    source_parts.append(pf.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue

        self._all_source_text = "\n".join(source_parts)

    def _check_dead_code(self):
        """Find functions defined but never referenced elsewhere.

        Uses fast string counting (str.count) instead of per-function regex
        to keep runtime under 10s for 300+ file codebases.
        """
        # Split all source into words once for fast lookup  # signed: alpha
        # Use simple substring count — much faster than regex per function
        all_text = self._all_source_text
        for func_name, def_files in self._function_defs.items():
            if len(def_files) > 1:
                continue  # defined in multiple files = likely used
            if func_name.startswith("_") and not func_name.startswith("__"):
                continue  # private functions — skip
            if len(func_name) < 4:
                continue  # too short, high false positive rate

            # Fast count: occurrences of the function name as a substring
            ref_count = all_text.count(func_name)
            # Heuristic: at least 1 is the `def func_name(` itself,
            # so if count <= 1 it's likely unused
            if ref_count <= 1:
                self.findings.append(Finding(
                    def_files[0], 0, "dead_code", "LOW",
                    f"Function '{func_name}' appears to be unused (only {ref_count} reference found)",
                    "Remove if truly unused, or add underscore prefix if internal"))

    # ── Helpers ──────────────────────────────────────────────────

    def _rel(self, path: Path) -> str:
        """Get path relative to repo root."""
        try:
            return str(path.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _severity_counts(self) -> dict:
        counts = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts


# ── Report Output ────────────────────────────────────────────────

def save_report(report: dict):
    """Save JSON report to data/upgrade_scan_results.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def print_summary(report: dict, severity_filter: str | None = None):
    """Print human-readable summary to stdout."""
    counts = report["findings_by_severity"]
    total = sum(counts.values())
    findings = report["findings"]

    if severity_filter:
        sf = severity_filter.upper()
        findings = [f for f in findings if f["severity"] == sf]

    print(f"\n{'='*60}")
    print(f"  SKYNET UPGRADE SCANNER — Results")
    print(f"{'='*60}")
    print(f"  Files scanned:  {report['total_files_scanned']}")
    print(f"  Scan time:      {report['elapsed_s']}s")
    print(f"  Total findings: {total}")
    print(f"    CRITICAL: {counts.get('CRITICAL', 0)}")
    print(f"    HIGH:     {counts.get('HIGH', 0)}")
    print(f"    MEDIUM:   {counts.get('MEDIUM', 0)}")
    print(f"    LOW:      {counts.get('LOW', 0)}")
    if severity_filter:
        print(f"  (filtered to {severity_filter.upper()} only)")
    print(f"{'='*60}")

    # Group by category
    by_cat: dict[str, list] = defaultdict(list)
    for f in findings:
        by_cat[f["category"]].append(f)

    for cat, items in sorted(by_cat.items()):
        print(f"\n  [{cat.upper()}] ({len(items)} findings)")
        for item in items[:15]:  # cap display per category
            loc = f"  L{item['line']}" if item["line"] else ""
            print(f"    [{item['severity']}] {item['file']}{loc}: {item['description'][:100]}")
        if len(items) > 15:
            print(f"    ... and {len(items) - 15} more")

    print(f"\n  Report saved to: {REPORT_FILE}")
    print(f"{'='*60}\n")


def build_bus_summary(report: dict) -> str:
    """Build a concise bus summary string."""
    counts = report["findings_by_severity"]
    total = sum(counts.values())
    return (
        f"UPGRADE_SCAN complete: {report['total_files_scanned']} files, "
        f"{total} findings "
        f"(CRITICAL={counts.get('CRITICAL', 0)} "
        f"HIGH={counts.get('HIGH', 0)} "
        f"MEDIUM={counts.get('MEDIUM', 0)} "
        f"LOW={counts.get('LOW', 0)}) "
        f"in {report['elapsed_s']}s. "
        f"Report: data/upgrade_scan_results.json"
    )


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Skynet Upgrade Scanner — autonomous codebase improvement detector")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    parser.add_argument("--severity", choices=["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                        help="Filter results by severity")
    parser.add_argument("--path", type=str, help="Scan specific directory (relative to repo root)")
    parser.add_argument("--bus", action="store_true", help="Post summary to Skynet bus")
    parser.add_argument("--no-save", action="store_true", help="Skip saving JSON report to disk")
    args = parser.parse_args()

    # Determine scan paths
    if args.path:
        scan_dir = ROOT / args.path
        if not scan_dir.is_dir():
            print(f"Error: {args.path} is not a directory", file=sys.stderr)
            sys.exit(1)
        scan_paths = [scan_dir]
    else:
        scan_paths = [ROOT / "tools", ROOT / "core"]

    scanner = UpgradeScanner(scan_paths=scan_paths)
    report = scanner.scan()

    # Save report
    if not args.no_save:
        save_report(report)

    # Output
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_summary(report, severity_filter=args.severity)

    # Bus notification
    if args.bus:
        try:
            sys.path.insert(0, str(ROOT))
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish({
                "sender": "upgrade_scanner",
                "topic": "orchestrator",
                "type": "scan_report",
                "content": build_bus_summary(report),
            })
            print("  → Summary posted to bus")
        except Exception as e:
            print(f"  → Bus post failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
# signed: alpha
