"""Code knowledge graph for Skynet — AST-based dependency analysis.
# signed: delta

Parses every .py file in the repo with Python's ast module and builds
a graph of files, functions, classes, imports, calls, and inheritance.

The graph powers four queries:
  • impact_analysis  — who calls / depends on a function?
  • find_unused      — functions never referenced anywhere
  • dependency_chain — shortest path from caller to callee
  • build_graph      — full repo scan → data/code_graph.json

Usage:
    python tools/skynet_code_graph.py build [--root DIR]
    python tools/skynet_code_graph.py impact <file> <function>
    python tools/skynet_code_graph.py unused [--top N]
    python tools/skynet_code_graph.py chain <from_module.func> <to_module.func>
    python tools/skynet_code_graph.py stats

Python API:
    from tools.skynet_code_graph import build_graph, impact_analysis, find_unused, dependency_chain
"""
# signed: delta

import ast
import json
import os
import sys
import argparse
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = REPO_ROOT / "data" / "code_graph.json"

logger = logging.getLogger(__name__)

# Directories to skip during scanning
SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "env",
    "venv", ".tox", ".mypy_cache", ".pytest_cache", "Skynet",
}

# Files to skip
SKIP_FILES = {"__init__.py"}


def _rel_path(path: Path) -> str:
    """Return forward-slash relative path from repo root."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _module_key(rel_file: str) -> str:
    """Convert file path to dotted module key: core/ocr.py → core.ocr"""
    return rel_file.replace("/", ".").replace("\\", ".").removesuffix(".py")


# ── AST Visitor ──────────────────────────────────────────────────────
# signed: delta

class _CodeVisitor(ast.NodeVisitor):
    """Single-pass AST visitor that extracts nodes and edges from one file."""

    def __init__(self, module_key: str, rel_path: str):
        self.module_key = module_key
        self.rel_path = rel_path

        # Collected data
        self.functions: List[Dict] = []
        self.classes: List[Dict] = []
        self.imports: List[Dict] = []      # edges: this module → imported module
        self.calls: List[Dict] = []        # edges: caller → callee name
        self.inherits: List[Dict] = []     # edges: child class → parent class

        # State tracking
        self._scope_stack: List[str] = []  # current class/func scope

    @property
    def _current_scope(self) -> str:
        if self._scope_stack:
            return f"{self.module_key}.{'.'.join(self._scope_stack)}"
        return self.module_key

    # ── Functions ────────────────────────────────────────────────
    def visit_FunctionDef(self, node: ast.FunctionDef):
        fqn = f"{self.module_key}.{'.'.join(self._scope_stack + [node.name])}"
        self.functions.append({
            "name": node.name,
            "fqn": fqn,
            "file": self.rel_path,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "args": [a.arg for a in node.args.args],
            "is_method": len(self._scope_stack) > 0,
            "decorators": [_decorator_name(d) for d in node.decorator_list],
        })
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # same treatment

    # ── Classes ──────────────────────────────────────────────────
    def visit_ClassDef(self, node: ast.ClassDef):
        fqn = f"{self.module_key}.{'.'.join(self._scope_stack + [node.name])}"
        self.classes.append({
            "name": node.name,
            "fqn": fqn,
            "file": self.rel_path,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "bases": [_name_from_node(b) for b in node.bases if _name_from_node(b)],
        })

        # Inheritance edges
        for base in node.bases:
            base_name = _name_from_node(base)
            if base_name:
                self.inherits.append({
                    "child": fqn,
                    "parent": base_name,
                    "file": self.rel_path,
                    "line": node.lineno,
                })

        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    # ── Imports ──────────────────────────────────────────────────
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append({
                "from": self.module_key,
                "to": alias.name,
                "alias": alias.asname,
                "line": node.lineno,
                "kind": "import",
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        for alias in (node.names or []):
            self.imports.append({
                "from": self.module_key,
                "to": module,
                "name": alias.name,
                "alias": alias.asname,
                "line": node.lineno,
                "kind": "from_import",
            })
        self.generic_visit(node)

    # ── Calls ────────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call):
        callee = _name_from_node(node.func)
        if callee:
            self.calls.append({
                "caller": self._current_scope,
                "callee": callee,
                "file": self.rel_path,
                "line": node.lineno,
            })
        self.generic_visit(node)


def _name_from_node(node) -> Optional[str]:
    """Extract dotted name from an AST node (Name, Attribute, Subscript)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _name_from_node(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    if isinstance(node, ast.Subscript):
        return _name_from_node(node.value)
    return None


def _decorator_name(node) -> str:
    """Extract decorator name."""
    n = _name_from_node(node)
    if n:
        return n
    if isinstance(node, ast.Call):
        return _name_from_node(node.func) or "?"
    return "?"


# ── Graph Builder ────────────────────────────────────────────────────
# signed: delta

def _scan_file(py_path: Path) -> Optional[_CodeVisitor]:
    """Parse a single .py file and return its visitor, or None on error."""
    rel = _rel_path(py_path)
    mod = _module_key(rel)
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_path))
    except (SyntaxError, UnicodeDecodeError) as e:
        logger.warning("Parse error in %s: %s", rel, e)
        return None

    visitor = _CodeVisitor(mod, rel)
    visitor.visit(tree)
    return visitor


def build_graph(root_dir: Optional[str] = None) -> Dict[str, Any]:
    """Scan all .py files and build the code knowledge graph.

    Args:
        root_dir: Directory to scan (default: repo root).

    Returns:
        Graph dict with nodes, edges, and metadata.
    """
    root = Path(root_dir) if root_dir else REPO_ROOT
    graph: Dict[str, Any] = {
        "nodes": {
            "files": {},
            "functions": {},
            "classes": {},
        },
        "edges": {
            "imports": [],
            "calls": [],
            "defines": [],
            "inherits": [],
        },
        "metadata": {
            "root": str(root),
            "files_scanned": 0,
            "files_failed": 0,
            "total_functions": 0,
            "total_classes": 0,
            "total_imports": 0,
            "total_calls": 0,
        },
    }

    py_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py") and fn not in SKIP_FILES:
                py_files.append(Path(dirpath) / fn)

    for py_path in sorted(py_files):
        rel = _rel_path(py_path)
        visitor = _scan_file(py_path)
        if visitor is None:
            graph["metadata"]["files_failed"] += 1
            continue

        graph["metadata"]["files_scanned"] += 1

        # File node
        graph["nodes"]["files"][visitor.module_key] = {
            "path": rel,
            "functions": len(visitor.functions),
            "classes": len(visitor.classes),
            "imports": len(visitor.imports),
        }

        # Function nodes
        for func in visitor.functions:
            graph["nodes"]["functions"][func["fqn"]] = func
            # "defines" edge: file → function
            graph["edges"]["defines"].append({
                "file": visitor.module_key,
                "symbol": func["fqn"],
                "kind": "function",
                "line": func["line"],
            })
        graph["metadata"]["total_functions"] += len(visitor.functions)

        # Class nodes
        for cls in visitor.classes:
            graph["nodes"]["classes"][cls["fqn"]] = cls
            graph["edges"]["defines"].append({
                "file": visitor.module_key,
                "symbol": cls["fqn"],
                "kind": "class",
                "line": cls["line"],
            })
        graph["metadata"]["total_classes"] += len(visitor.classes)

        # Edges
        graph["edges"]["imports"].extend(visitor.imports)
        graph["metadata"]["total_imports"] += len(visitor.imports)

        graph["edges"]["calls"].extend(visitor.calls)
        graph["metadata"]["total_calls"] += len(visitor.calls)

        graph["edges"]["inherits"].extend(visitor.inherits)

    # Save to disk  # signed: delta
    _save_graph(graph)
    return graph


def _save_graph(graph: Dict) -> None:
    """Atomically save graph to data/code_graph.json."""
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(GRAPH_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, str(GRAPH_PATH))


def _load_graph() -> Dict[str, Any]:
    """Load graph from disk. Raises FileNotFoundError if not built yet."""
    if not GRAPH_PATH.exists():
        raise FileNotFoundError(
            f"Code graph not found at {GRAPH_PATH}. Run: "
            "python tools/skynet_code_graph.py build"
        )
    with open(GRAPH_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Queries ──────────────────────────────────────────────────────────
# signed: delta

def _resolve_fqn(graph: Dict, file_hint: str, func_name: str) -> Optional[str]:
    """Try to resolve a file+function hint to a fully-qualified node key."""
    # Exact match
    for fqn in graph["nodes"]["functions"]:
        if fqn.endswith(f".{func_name}"):
            mod = _module_key(file_hint) if file_hint else ""
            if not mod or fqn.startswith(mod):
                return fqn
    return None


def _callee_matches(callee: str, target_fqn: str) -> bool:
    """Check if an AST call name could refer to the target function.

    AST calls are often partial (e.g. 'self.foo', 'module.bar', just 'baz').
    We match if the callee is a suffix of the FQN or the bare function name.
    """
    target_name = target_fqn.rsplit(".", 1)[-1]
    # Exact bare name match
    if callee == target_name:
        return True
    # Dotted suffix match
    if target_fqn.endswith(f".{callee}"):
        return True
    # The callee itself ends with the target name (e.g. self.target_name)
    if callee.endswith(f".{target_name}"):
        return True
    return False


def impact_analysis(
    file_path: str,
    function_name: str,
    graph: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Find all callers and dependents of a function.

    Args:
        file_path:      File containing the function (relative or module-dotted).
        function_name:  Name of the function to analyze.
        graph:          Pre-loaded graph (loads from disk if None).

    Returns:
        {target_fqn, callers: [{caller, file, line}], dependents: [modules],
         import_dependents: [modules], call_count}
    """
    if graph is None:
        graph = _load_graph()

    target_fqn = _resolve_fqn(graph, file_path, function_name)
    if not target_fqn:
        return {"error": f"Function '{function_name}' not found in graph", "callers": [], "dependents": []}

    # Find callers: edges where callee matches target  # signed: delta
    callers = []
    caller_modules: Set[str] = set()
    for call in graph["edges"]["calls"]:
        if _callee_matches(call["callee"], target_fqn):
            callers.append({
                "caller": call["caller"],
                "file": call["file"],
                "line": call["line"],
            })
            # Extract module from caller FQN
            parts = call["caller"].split(".")
            if len(parts) >= 2:
                caller_modules.add(".".join(parts[:2]))
            else:
                caller_modules.add(parts[0])

    # Find import dependents: modules that import the target's module
    target_module = target_fqn.rsplit(".", 1)[0]
    import_deps: Set[str] = set()
    for imp in graph["edges"]["imports"]:
        if imp["to"] == target_module or (
            imp.get("name") == function_name and imp["to"] == target_module
        ):
            import_deps.add(imp["from"])

    return {
        "target_fqn": target_fqn,
        "callers": callers,
        "caller_count": len(callers),
        "dependent_modules": sorted(caller_modules),
        "import_dependents": sorted(import_deps),
    }


def find_unused(
    graph: Optional[Dict] = None,
    top_n: int = 50,
) -> List[Dict]:
    """Find functions that are never called anywhere in the codebase.

    Excludes: __main__ blocks, test functions, dunder methods, CLI entry
    points, decorated functions (likely framework hooks).

    Args:
        graph: Pre-loaded graph (loads from disk if None).
        top_n: Maximum results to return.

    Returns:
        List of [{fqn, file, line, name}] for unreferenced functions.
    """
    if graph is None:
        graph = _load_graph()

    # Build set of all callee names (bare + dotted forms)
    called_names: Set[str] = set()
    for call in graph["edges"]["calls"]:
        callee = call["callee"]
        called_names.add(callee)
        # Also add the bare name (after last dot)
        bare = callee.rsplit(".", 1)[-1]
        called_names.add(bare)

    # Exclusion patterns  # signed: delta
    EXCLUDE_PREFIXES = ("_", "test_", "Test")
    EXCLUDE_NAMES = {
        "main", "cli", "_cli", "setup", "teardown",
        "setUp", "tearDown", "setUpClass", "tearDownClass",
    }

    unused = []
    for fqn, func in graph["nodes"]["functions"].items():
        name = func["name"]

        # Skip private, test, and dunder methods
        if name.startswith("__") and name.endswith("__"):
            continue
        if name in EXCLUDE_NAMES:
            continue
        if any(name.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        # Skip decorated functions (likely hooks/endpoints)
        if func.get("decorators"):
            continue
        # Skip methods (they're called via self.method — hard to trace statically)
        if func.get("is_method"):
            continue

        # Check if this function is ever called
        if name not in called_names and fqn not in called_names:
            unused.append({
                "fqn": fqn,
                "name": name,
                "file": func["file"],
                "line": func["line"],
            })

    unused.sort(key=lambda x: x["file"])
    return unused[:top_n]


def dependency_chain(
    from_func: str,
    to_func: str,
    graph: Optional[Dict] = None,
) -> Optional[List[str]]:
    """Find shortest call-path from one function to another via BFS.

    Args:
        from_func: Starting function (bare name or dotted FQN).
        to_func:   Target function (bare name or dotted FQN).
        graph:     Pre-loaded graph (loads from disk if None).

    Returns:
        List of FQNs forming the path [from → ... → to], or None if no path.
    """
    if graph is None:
        graph = _load_graph()

    # Build adjacency: caller_fqn → set of callee_fqns  # signed: delta
    # We need to resolve callee names to FQNs where possible
    all_funcs = graph["nodes"]["functions"]
    name_to_fqns: Dict[str, List[str]] = defaultdict(list)
    for fqn in all_funcs:
        bare = fqn.rsplit(".", 1)[-1]
        name_to_fqns[bare].append(fqn)

    adj: Dict[str, Set[str]] = defaultdict(set)
    for call in graph["edges"]["calls"]:
        caller = call["caller"]
        callee_name = call["callee"]
        bare = callee_name.rsplit(".", 1)[-1]
        # Resolve callee to known FQNs
        for candidate in name_to_fqns.get(bare, []):
            if _callee_matches(callee_name, candidate):
                adj[caller].add(candidate)

    # Resolve start and end to FQNs
    def _resolve(hint: str) -> List[str]:
        if hint in all_funcs:
            return [hint]
        bare = hint.rsplit(".", 1)[-1]
        return name_to_fqns.get(bare, [])

    starts = _resolve(from_func)
    ends = set(_resolve(to_func))

    if not starts or not ends:
        return None

    # BFS from each start
    for start in starts:
        visited: Set[str] = {start}
        queue: deque = deque([(start, [start])])

        while queue:
            current, path = queue.popleft()
            for neighbor in adj.get(current, set()):
                if neighbor in ends:
                    return path + [neighbor]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

    return None


def graph_stats(graph: Optional[Dict] = None) -> Dict[str, Any]:
    """Return summary statistics about the code graph."""
    if graph is None:
        graph = _load_graph()
    m = graph["metadata"]
    return {
        "files_scanned": m["files_scanned"],
        "files_failed": m["files_failed"],
        "total_functions": m["total_functions"],
        "total_classes": m["total_classes"],
        "total_imports": m["total_imports"],
        "total_calls": m["total_calls"],
        "total_defines": len(graph["edges"]["defines"]),
        "total_inherits": len(graph["edges"]["inherits"]),
    }


# ── CLI ──────────────────────────────────────────────────────────────
# signed: delta

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet code knowledge graph"
    )
    sub = parser.add_subparsers(dest="command")

    # build
    bd = sub.add_parser("build", help="Build the code graph (scans all .py files)")
    bd.add_argument("--root", default=None, help="Root directory (default: repo root)")

    # impact
    im = sub.add_parser("impact", help="Impact analysis for a function")
    im.add_argument("file", help="File path or module key")
    im.add_argument("function", help="Function name")

    # unused
    un = sub.add_parser("unused", help="Find unused functions")
    un.add_argument("--top", type=int, default=30, help="Max results (default 30)")

    # chain
    ch = sub.add_parser("chain", help="Dependency chain between two functions")
    ch.add_argument("from_func", help="Source function (name or FQN)")
    ch.add_argument("to_func", help="Target function (name or FQN)")

    # stats
    sub.add_parser("stats", help="Show graph statistics")

    args = parser.parse_args()

    if args.command == "build":
        print("Building code graph...")
        g = build_graph(args.root)
        m = g["metadata"]
        print(
            f"Done: {m['files_scanned']} files, "
            f"{m['total_functions']} functions, "
            f"{m['total_classes']} classes, "
            f"{m['total_calls']} call edges, "
            f"{m['total_imports']} imports"
        )
        if m["files_failed"] > 0:
            print(f"  ({m['files_failed']} files failed to parse)")
        print(f"Saved to {GRAPH_PATH}")

    elif args.command == "impact":
        result = impact_analysis(args.file, args.function)
        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)
        print(f"Impact analysis for {result['target_fqn']}:")
        print(f"  Callers: {result['caller_count']}")
        for c in result["callers"][:20]:
            print(f"    ← {c['caller']} ({c['file']}:{c['line']})")
        if result["import_dependents"]:
            print(f"  Import dependents: {', '.join(result['import_dependents'][:15])}")
        if result["dependent_modules"]:
            print(f"  Dependent modules: {', '.join(result['dependent_modules'][:15])}")

    elif args.command == "unused":
        unused = find_unused(top_n=args.top)
        if not unused:
            print("No unused functions found (or graph not built).")
        else:
            print(f"Found {len(unused)} potentially unused functions:")
            for u in unused:
                print(f"  {u['fqn']}  ({u['file']}:{u['line']})")

    elif args.command == "chain":
        path = dependency_chain(args.from_func, args.to_func)
        if path is None:
            print(f"No call path found from '{args.from_func}' to '{args.to_func}'.")
        else:
            print(f"Call chain ({len(path)} hops):")
            for i, node in enumerate(path):
                prefix = "  → " if i > 0 else "  "
                print(f"{prefix}{node}")

    elif args.command == "stats":
        s = graph_stats()
        print("Code Graph Statistics:")
        for k, v in s.items():
            print(f"  {k}: {v}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: delta
