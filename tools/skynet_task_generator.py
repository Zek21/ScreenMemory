#!/usr/bin/env python3
"""skynet_task_generator.py -- Autonomous codebase health scanner and task generator.

Scans the codebase using Python AST to find improvement opportunities and
generates dispatchable tasks for Skynet workers.

Usage:
    python tools/skynet_task_generator.py scan              # scan codebase health
    python tools/skynet_task_generator.py generate           # generate tasks from findings
    python tools/skynet_task_generator.py auto               # scan + generate + write TODOs
    python tools/skynet_task_generator.py scan --json        # JSON output
"""

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TODOS_FILE = DATA_DIR / "todos.json"

# Directories to scan (relative to ROOT)
SCAN_DIRS = ["core", "tools", "tests"]
# Directories/files to skip
SKIP_PATTERNS = {"__pycache__", ".git", "node_modules", ".venv", "env"}

MAX_FUNCTION_LINES = 50
TODO_PATTERNS = re.compile(r"#\s*(TODO|FIXME|HACK|XXX|BUG)\b", re.IGNORECASE)


@dataclass
class Finding:
    category: str       # "missing_test", "missing_docstring", "todo_comment", etc.
    file: str
    line: int = 0
    detail: str = ""
    severity: str = "low"   # low, medium, high

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthReport:
    files_scanned: int = 0
    findings: list = field(default_factory=list)

    def add(self, f: Finding):
        self.findings.append(f)

    def summary(self) -> dict:
        by_cat = {}
        for f in self.findings:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
        return {
            "files_scanned": self.files_scanned,
            "total_findings": len(self.findings),
            "by_category": by_cat,
        }

    def to_dict(self) -> dict:
        s = self.summary()
        s["findings"] = [f.to_dict() for f in self.findings]
        return s


def _iter_python_files():
    """Iterate all .py files in scan directories."""
    for dir_name in SCAN_DIRS:
        scan_dir = ROOT / dir_name
        if not scan_dir.is_dir():
            continue
        for py_file in scan_dir.rglob("*.py"):
            parts = set(py_file.parts)
            if parts & SKIP_PATTERNS:
                continue
            yield py_file

    # Also scan root-level .py files
    for py_file in ROOT.glob("*.py"):
        yield py_file


def _get_test_files() -> set:
    """Get set of module names that have corresponding test files."""
    test_dir = ROOT / "tests"
    if not test_dir.is_dir():
        return set()
    test_files = set()
    for tf in test_dir.glob("test_*.py"):
        module_name = tf.stem.replace("test_", "", 1)
        test_files.add(module_name)
    return test_files


def _scan_todo_comments(source: str, rel_path: str, report: HealthReport):
    """Scan source lines for TODO/FIXME/HACK comments."""
    for i, line in enumerate(source.splitlines(), 1):
        match = TODO_PATTERNS.search(line)
        if match:
            tag = match.group(1).upper()
            comment = line.strip()[:120]
            report.add(Finding(
                category="todo_comment",
                file=rel_path,
                line=i,
                detail=f"{tag}: {comment}",
                severity="low" if tag == "TODO" else "medium",
            ))


def _check_missing_tests(tree, py_file: Path, rel_path: str,
                         test_modules: set, report: HealthReport):
    """Check if a non-test module has a corresponding test file."""
    if py_file.name.startswith("test_") or "tests" in py_file.parts:
        return
    module_stem = py_file.stem
    if module_stem not in test_modules:
        has_public_fns = any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            for n in ast.walk(tree)
            if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name.startswith("_"))
        )
        if has_public_fns:
            report.add(Finding(
                category="missing_test",
                file=rel_path,
                detail=f"No test_{{name}}.py found for {module_stem}",
                severity="medium",
            ))


def _scan_ast_nodes(tree, rel_path: str, report: HealthReport):
    """Scan AST for missing docstrings and long functions/classes."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not ast.get_docstring(node) and not node.name.startswith("_"):
                report.add(Finding(
                    category="missing_docstring",
                    file=rel_path,
                    line=node.lineno,
                    detail=f"Function '{node.name}' has no docstring",
                    severity="low",
                ))
            if hasattr(node, "end_lineno") and node.end_lineno:
                length = node.end_lineno - node.lineno + 1
                if length > MAX_FUNCTION_LINES:
                    report.add(Finding(
                        category="long_function",
                        file=rel_path,
                        line=node.lineno,
                        detail=f"Function '{node.name}' is {length} lines "
                               f"(max recommended: {MAX_FUNCTION_LINES})",
                        severity="medium",
                    ))
        elif isinstance(node, ast.ClassDef):
            if not ast.get_docstring(node):
                report.add(Finding(
                    category="missing_docstring",
                    file=rel_path,
                    line=node.lineno,
                    detail=f"Class '{node.name}' has no docstring",
                    severity="low",
                ))


def scan_codebase_health() -> HealthReport:
    """Scan codebase for improvement opportunities using AST parsing."""
    report = HealthReport()
    test_modules = _get_test_files()

    for py_file in _iter_python_files():
        report.files_scanned += 1
        rel_path = str(py_file.relative_to(ROOT))

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        _scan_todo_comments(source, rel_path, report)

        # Parse AST
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            report.add(Finding(
                category="syntax_error",
                file=rel_path,
                detail="File has syntax errors",
                severity="high",
            ))
            continue

        _check_missing_tests(tree, py_file, rel_path, test_modules, report)

        _scan_ast_nodes(tree, rel_path, report)
        _scan_unused_imports(tree, source, rel_path, report)

    return report


def _scan_unused_imports(tree: ast.AST, source: str, rel_path: str, report: HealthReport):
    """Basic unused import detection."""
    imports = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imports.append((name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.names:
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    name = alias.asname or alias.name
                    imports.append((name, node.lineno))

    if not imports:
        return

    body_text = source
    for name, lineno in imports:
        # Count occurrences of the name in the full source
        count = len(re.findall(r'\b' + re.escape(name) + r'\b', body_text))
        # If only appears once (the import itself), likely unused
        if count <= 1 and name not in ("__all__",):
            report.add(Finding(
                category="unused_import",
                file=rel_path,
                line=lineno,
                detail=f"Import '{name}' appears only once (in import statement)",
                severity="low",
            ))


def generate_improvement_tasks(report: HealthReport) -> list:
    """Convert findings into dispatchable task descriptions."""
    tasks = []

    # Group findings by category
    by_cat = {}
    for f in report.findings:
        by_cat.setdefault(f.category, []).append(f)

    # Missing tests -> batch by file
    if "missing_test" in by_cat:
        files = [f.file for f in by_cat["missing_test"]]
        # Batch into groups of 3
        for i in range(0, len(files), 3):
            batch = files[i:i+3]
            tasks.append({
                "task": f"Write unit tests for: {', '.join(batch)}",
                "priority": "medium",
                "category": "testing",
                "files": batch,
            })

    # Long functions -> refactor tasks
    if "long_function" in by_cat:
        for f in by_cat["long_function"]:
            tasks.append({
                "task": f"Refactor long function in {f.file}:{f.line} -- {f.detail}",
                "priority": "medium",
                "category": "refactoring",
                "files": [f.file],
            })

    # TODO/FIXME/HACK -> group by severity
    for sev in ("high", "medium"):
        todos = [f for f in by_cat.get("todo_comment", []) if f.severity == sev]
        if todos:
            files = sorted(set(f.file for f in todos))
            tasks.append({
                "task": f"Address {len(todos)} {sev}-severity TODO/FIXME/HACK comments in: "
                        f"{', '.join(files[:5])}{'...' if len(files) > 5 else ''}",
                "priority": sev,
                "category": "maintenance",
                "files": files,
            })

    # Syntax errors -> urgent fix
    for f in by_cat.get("syntax_error", []):
        tasks.append({
            "task": f"Fix syntax error in {f.file}",
            "priority": "high",
            "category": "bugfix",
            "files": [f.file],
        })

    return tasks


def generate_todos(tasks: list, worker: str = "unassigned") -> list:
    """Write generated tasks to data/todos.json."""
    existing = []
    if TODOS_FILE.exists():
        try:
            data = json.loads(TODOS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
            elif isinstance(data, dict) and "todos" in data:
                existing = data["todos"]
        except Exception:
            pass

    new_todos = []
    existing_tasks = {t.get("task", "") for t in existing}

    for t in tasks:
        if t["task"] not in existing_tasks:
            new_todos.append({
                "task": t["task"],
                "priority": t.get("priority", "medium"),
                "category": t.get("category", "improvement"),
                "status": "pending",
                "worker": worker,
                "source": "task_generator",
            })

    all_todos = existing + new_todos
    DATA_DIR.mkdir(exist_ok=True)
    TODOS_FILE.write_text(json.dumps(all_todos, indent=2), encoding="utf-8")
    return new_todos


def main():
    parser = argparse.ArgumentParser(description="Skynet Autonomous Task Generator")
    parser.add_argument("mode", choices=["scan", "generate", "auto"],
                        help="scan=health scan, generate=create tasks, auto=scan+generate+todos")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--worker", type=str, default="unassigned",
                        help="Worker to assign generated TODOs to")
    args = parser.parse_args()

    if args.mode == "scan":
        report = scan_codebase_health()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            s = report.summary()
            print(f"\nCodebase Health Scan: {s['files_scanned']} files")
            print(f"Total findings: {s['total_findings']}")
            for cat, count in sorted(s["by_category"].items()):
                print(f"  {cat}: {count}")

    elif args.mode == "generate":
        report = scan_codebase_health()
        tasks = generate_improvement_tasks(report)
        if args.json:
            print(json.dumps(tasks, indent=2))
        else:
            print(f"\nGenerated {len(tasks)} improvement tasks:")
            for i, t in enumerate(tasks, 1):
                print(f"  {i}. [{t['priority']}] {t['task']}")

    elif args.mode == "auto":
        report = scan_codebase_health()
        tasks = generate_improvement_tasks(report)
        new_todos = generate_todos(tasks, worker=args.worker)
        if args.json:
            print(json.dumps({
                "scan": report.summary(),
                "tasks_generated": len(tasks),
                "new_todos_written": len(new_todos),
            }, indent=2))
        else:
            s = report.summary()
            print(f"\nAuto mode: scanned {s['files_scanned']} files, "
                  f"{s['total_findings']} findings, "
                  f"{len(tasks)} tasks, "
                  f"{len(new_todos)} new TODOs written to {TODOS_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
