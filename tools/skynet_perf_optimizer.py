#!/usr/bin/env python3
"""Skynet Performance Optimizer — automated performance analysis for tools/.

Profiles Python imports, detects unused imports via AST, finds duplicate code
patterns, and suggests lazy-loading alternatives for slow modules.

CLI:
    python tools/skynet_perf_optimizer.py --all        # Run all analyses
    python tools/skynet_perf_optimizer.py --imports     # Profile import times
    python tools/skynet_perf_optimizer.py --duplicates  # Find duplicate functions
    python tools/skynet_perf_optimizer.py --unused      # Detect unused imports
    python tools/skynet_perf_optimizer.py --json        # JSON output only
"""
# signed: delta

import ast
import collections
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
DATA_DIR = ROOT / "data"
REPORT_FILE = DATA_DIR / "perf_report.json"

SLOW_IMPORT_THRESHOLD_MS = 500


def _ts() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ── Import Profiling ─────────────────────────────────────────────

def profile_imports() -> list[dict]:
    """Measure import time for each Python module in tools/.

    Uses subprocess isolation to avoid caching effects.
    Returns list of {module, file, import_ms, slow, suggestion}.
    """
    results = []
    py_files = sorted(TOOLS_DIR.glob("*.py"))

    for f in py_files:
        if f.name.startswith("__"):
            continue

        module_name = f.stem
        # Use subprocess to measure cold import time
        code = textwrap.dedent(f"""\
            import time, sys
            sys.path.insert(0, r"{ROOT}")
            sys.path.insert(0, r"{TOOLS_DIR}")
            t0 = time.perf_counter()
            try:
                __import__("tools.{module_name}")
                elapsed = (time.perf_counter() - t0) * 1000
                print(f"OK {{elapsed:.1f}}")
            except Exception as e:
                elapsed = (time.perf_counter() - t0) * 1000
                print(f"ERR {{elapsed:.1f}} {{e}}")
        """)

        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=30,
                cwd=str(ROOT),
            )
            output = proc.stdout.strip()
            if output.startswith("OK "):
                ms = float(output.split()[1])
                status = "ok"
                error = None
            elif output.startswith("ERR "):
                parts = output.split(maxsplit=2)
                ms = float(parts[1])
                status = "error"
                error = parts[2] if len(parts) > 2 else "unknown"
            else:
                ms = 0
                status = "unknown"
                error = output or proc.stderr[:200]
        except subprocess.TimeoutExpired:
            ms = 30000
            status = "timeout"
            error = "Import timed out (>30s)"
        except Exception as e:
            ms = 0
            status = "error"
            error = str(e)

        slow = ms > SLOW_IMPORT_THRESHOLD_MS
        suggestion = None
        if slow and status == "ok":
            suggestion = f"Consider lazy-loading: move 'import {module_name}' inside functions that use it"

        results.append({
            "module": module_name,
            "file": f.name,
            "import_ms": round(ms, 1),
            "status": status,
            "slow": slow,
            "suggestion": suggestion,
            "error": error,
        })

    results.sort(key=lambda x: x["import_ms"], reverse=True)
    return results


# ── Unused Import Detection ─────────────────────────────────────

def _get_imports_from_ast(tree: ast.AST) -> list[dict]:
    """Extract all import statements from an AST."""
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imports.append({
                    "name": name,
                    "full_module": alias.name,
                    "line": node.lineno,
                    "type": "import",
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                imports.append({
                    "name": name,
                    "full_module": f"{module}.{alias.name}",
                    "line": node.lineno,
                    "type": "from_import",
                })
    return imports


def _get_used_names(tree: ast.AST, import_names: set) -> set:
    """Find which imported names are actually used in the code."""
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in import_names:
            # Skip the import statement itself
            if not isinstance(getattr(node, '_parent', None), (ast.Import, ast.ImportFrom)):
                used.add(node.id)
        elif isinstance(node, ast.Attribute):
            # Handle module.attr patterns
            if isinstance(node.value, ast.Name) and node.value.id in import_names:
                used.add(node.value.id)
    return used


def detect_unused_imports() -> list[dict]:
    """Detect unused imports across all tools/ Python files via AST analysis.

    Returns list of {file, import_name, module, line, unused}.
    """
    results = []
    py_files = sorted(TOOLS_DIR.glob("*.py"))

    for f in py_files:
        if f.name.startswith("__"):
            continue
        try:
            source = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(f))
        except SyntaxError:
            continue

        imports = _get_imports_from_ast(tree)
        if not imports:
            continue

        import_names = {imp["name"] for imp in imports}

        # Simple text-based usage check (more reliable than AST walking for edge cases)
        # Remove comments and strings for cleaner matching
        lines = source.splitlines()
        # Build usage map: check if each imported name appears elsewhere in the source
        for imp in imports:
            name = imp["name"]
            # Count occurrences of the name in all lines except the import line
            usage_count = 0
            for i, line in enumerate(lines, 1):
                if i == imp["line"]:
                    continue
                # Check for the name as a word boundary match
                if re.search(rf'\b{re.escape(name)}\b', line):
                    usage_count += 1

            if usage_count == 0:
                results.append({
                    "file": f.name,
                    "import_name": name,
                    "module": imp["full_module"],
                    "line": imp["line"],
                    "unused": True,
                })

    return results


# ── Duplicate Code Detection ────────────────────────────────────

def _extract_functions(filepath: Path) -> list[dict]:
    """Extract function definitions from a Python file."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Get function body as source lines
            try:
                body_lines = source.splitlines()[node.lineno - 1:node.end_lineno]
                body_text = "\n".join(body_lines)
            except (AttributeError, IndexError):
                body_text = ""

            # Normalize: strip whitespace, comments, docstrings for comparison
            normalized = _normalize_function_body(body_text)
            body_hash = hashlib.md5(normalized.encode()).hexdigest()

            functions.append({
                "name": node.name,
                "file": filepath.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "num_lines": getattr(node, "end_lineno", node.lineno) - node.lineno + 1,
                "args": [a.arg for a in node.args.args],
                "body_hash": body_hash,
                "normalized_length": len(normalized),
            })

    return functions


def _normalize_function_body(source: str) -> str:
    """Normalize function body for comparison (strip comments, whitespace, docstrings)."""
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Skip docstrings (rough detection)
        if stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def find_duplicates() -> list[dict]:
    """Find duplicate function patterns across tools/ files.

    Returns list of {group_hash, functions: [{name, file, line, num_lines}]}.
    """
    all_functions = []
    for f in sorted(TOOLS_DIR.glob("*.py")):
        if f.name.startswith("__"):
            continue
        all_functions.extend(_extract_functions(f))

    # Group by body_hash — exact duplicates
    by_hash = collections.defaultdict(list)
    for func in all_functions:
        if func["normalized_length"] > 50:  # Skip trivial functions
            by_hash[func["body_hash"]].append(func)

    duplicates = []
    for h, funcs in by_hash.items():
        if len(funcs) >= 2:
            # Only report if functions are in DIFFERENT files
            files = set(f["file"] for f in funcs)
            if len(files) >= 2:
                duplicates.append({
                    "group_hash": h,
                    "count": len(funcs),
                    "avg_lines": round(sum(f["num_lines"] for f in funcs) / len(funcs), 0),
                    "functions": [
                        {"name": f["name"], "file": f["file"],
                         "line": f["line"], "num_lines": f["num_lines"]}
                        for f in funcs
                    ],
                })

    # Also detect similar function names across files (potential duplicates)
    by_name = collections.defaultdict(list)
    for func in all_functions:
        by_name[func["name"]].append(func)

    name_dupes = []
    for name, funcs in by_name.items():
        files = set(f["file"] for f in funcs)
        if len(files) >= 3 and name not in ("main", "__init__", "log", "_ts"):
            name_dupes.append({
                "function_name": name,
                "appears_in": len(files),
                "files": sorted(files),
            })

    return {
        "exact_duplicates": duplicates,
        "name_collisions": sorted(name_dupes, key=lambda x: x["appears_in"], reverse=True)[:20],
    }


# ── Report Generation ───────────────────────────────────────────

def run_all() -> dict:
    """Run all analyses and return combined report."""
    report = {
        "generated_at": _ts(),
        "import_profile": None,
        "unused_imports": None,
        "duplicates": None,
        "summary": {},
    }

    print("[PERF] Profiling imports...", flush=True)
    imports = profile_imports()
    report["import_profile"] = imports
    slow_count = sum(1 for i in imports if i["slow"])
    print(f"  Found {len(imports)} modules, {slow_count} slow (>{SLOW_IMPORT_THRESHOLD_MS}ms)")

    print("[PERF] Detecting unused imports...", flush=True)
    unused = detect_unused_imports()
    report["unused_imports"] = unused
    print(f"  Found {len(unused)} unused imports")

    print("[PERF] Finding duplicate code...", flush=True)
    dupes = find_duplicates()
    report["duplicates"] = dupes
    exact = len(dupes.get("exact_duplicates", []))
    names = len(dupes.get("name_collisions", []))
    print(f"  Found {exact} exact duplicates, {names} name collisions")

    report["summary"] = {
        "total_modules": len(imports),
        "slow_imports": slow_count,
        "unused_imports": len(unused),
        "exact_duplicate_groups": exact,
        "name_collisions": names,
    }

    return report


def save_report(report: dict):
    """Save report to data/perf_report.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"[PERF] Report saved to {REPORT_FILE}")


def print_report(report: dict, as_json: bool = False):
    """Print human-readable or JSON report."""
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return

    s = report.get("summary", {})
    print("\n" + "=" * 60)
    print("  SKYNET PERFORMANCE REPORT")
    print("=" * 60)

    # Import profile
    imports = report.get("import_profile", [])
    if imports:
        print(f"\n  Import Profile ({s.get('total_modules', 0)} modules, "
              f"{s.get('slow_imports', 0)} slow):")
        # Show top 15 slowest
        for imp in imports[:15]:
            flag = " ** SLOW" if imp["slow"] else ""
            status = f" [{imp['status']}]" if imp["status"] != "ok" else ""
            print(f"    {imp['import_ms']:8.1f}ms  {imp['module']}{status}{flag}")
        if len(imports) > 15:
            print(f"    ... and {len(imports) - 15} more")

    # Unused imports
    unused = report.get("unused_imports", [])
    if unused:
        print(f"\n  Unused Imports ({len(unused)}):")
        by_file = collections.defaultdict(list)
        for u in unused:
            by_file[u["file"]].append(u)
        for fname, items in sorted(by_file.items()):
            names = ", ".join(f"{i['import_name']} (L{i['line']})" for i in items)
            print(f"    {fname}: {names}")

    # Duplicates
    dupes = report.get("duplicates", {})
    exact = dupes.get("exact_duplicates", [])
    if exact:
        print(f"\n  Exact Duplicate Functions ({len(exact)} groups):")
        for group in exact[:10]:
            funcs = group["functions"]
            locations = ", ".join(f"{f['file']}:{f['line']}" for f in funcs)
            print(f"    {funcs[0]['name']} ({group['avg_lines']:.0f} lines) -> {locations}")

    names = dupes.get("name_collisions", [])
    if names:
        print(f"\n  Function Name Collisions ({len(names)}):")
        for n in names[:10]:
            print(f"    {n['function_name']}() appears in {n['appears_in']} files")

    print("\n" + "=" * 60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Performance Optimizer")
    parser.add_argument("--imports", action="store_true", help="Profile import times")
    parser.add_argument("--duplicates", action="store_true", help="Find duplicate functions")
    parser.add_argument("--unused", action="store_true", help="Detect unused imports")
    parser.add_argument("--all", action="store_true", help="Run all analyses")
    parser.add_argument("--json", action="store_true", help="JSON output only")
    args = parser.parse_args()

    # Default to --all if no specific analysis requested
    if not any([args.imports, args.duplicates, args.unused, args.all]):
        args.all = True

    report = {"generated_at": _ts(), "summary": {}}

    if args.all:
        report = run_all()
    else:
        if args.imports:
            print("[PERF] Profiling imports...", flush=True)
            report["import_profile"] = profile_imports()
        if args.unused:
            print("[PERF] Detecting unused imports...", flush=True)
            report["unused_imports"] = detect_unused_imports()
        if args.duplicates:
            print("[PERF] Finding duplicate code...", flush=True)
            report["duplicates"] = find_duplicates()

    save_report(report)
    print_report(report, as_json=args.json)


if __name__ == "__main__":
    main()
