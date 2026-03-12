"""
engine_metrics.py -- Collect status/health from all ScreenMemory engines.
Called by GOD Console /engines endpoint.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "chrome_bridge"))

_cache = {}
_cache_time = 0
CACHE_TTL = 30

# All engine probes: (name, module_path, class_name, engine_type, import_only)
# import_only=True skips instantiation for engines with expensive constructors
# (network calls, ML model loading, hardware enumeration). Reports "available"
# truthfully -- import proves the module exists; instantiation is deferred.
# signed: delta
_PROBES = [
    ("router", "core.difficulty_router", "DAAORouter", "routing", False),
    ("dag", "core.dag_engine", "DAGBuilder", "workflow", False),
    ("guard", "core.input_guard", "InputGuard", "security", False),
    ("retriever", "core.hybrid_retrieval", "HybridRetriever", "memory", False),
    ("capture", "core.capture", "DXGICapture", "vision", True),        # 289ms: monitor enum + mss init
    ("ocr", "core.ocr", "OCREngine", "vision", True),                  # 635ms: ONNX model load
    ("orchestrator", "core.orchestrator", "Orchestrator", "cognition", False),
    ("analyzer", "core.analyzer", "ScreenAnalyzer", "analysis", True),  # 2115ms: Ollama HTTP check
    ("embedder", "core.embedder", "EmbeddingEngine", "embedding", True),# 2053ms: ML model load
    ("change_detector", "core.change_detector", "ChangeDetector", "vision", False),
    ("security", "core.security", "DPAPIKeyManager", "security", False),
    ("evolution", "core.self_evolution", "SelfEvolutionSystem", "learning", False),
    ("learning", "core.learning_store", "LearningStore", "learning", False),
    ("tools", "core.tool_synthesizer", "ToolSynthesizer", "dynamic", False),
    ("feedback", "core.feedback_loop", "FeedbackLoop", "feedback", False),
    ("database", "core.database", "ScreenMemoryDB", "storage", False),
    ("desktop", "winctl", "Desktop", "automation", False),
    ("godmode", "god_mode", "GodMode", "browser", False),
    ("reflexion", "core.cognitive.reflexion", "ReflexionEngine", "cognition", False),        # signed: gamma
    ("graph_of_thoughts", "core.cognitive.graph_of_thoughts", "GraphOfThoughts", "cognition", False),  # signed: gamma
    ("planner", "core.cognitive.planner", "HierarchicalPlanner", "cognition", False),        # signed: gamma
]

def _probe(name, module_path, class_name, engine_type, import_only=False, extras_fn=None):
    """Probe a single engine: import, then optionally attempt instantiation.

    Status levels (honest):
      - "online"    -- class was instantiated successfully (verified working)
      - "available" -- module imported and class found, but not instantiated
      - "offline"   -- import failed entirely

    When import_only=True, instantiation is skipped and status is "available"
    if the import succeeds. This avoids expensive constructors (ML model loads,
    network calls, hardware init) that dominate probe latency.
    """
    # signed: delta
    import warnings, io, contextlib
    t0 = time.time()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            mod = __import__(module_path, fromlist=[class_name])
            cls = getattr(mod, class_name)
        except Exception as e:
            return {"status": "offline", "name": class_name, "type": engine_type,
                    "error": str(e)[:120], "probe_ms": round((time.time() - t0) * 1000, 1)}

        # Import succeeded -- skip instantiation if flagged as expensive
        if import_only:
            return {"status": "available", "name": class_name, "type": engine_type,
                    "probe_ms": round((time.time() - t0) * 1000, 1),
                    "note": "import-only probe (expensive constructor skipped)"}

        # Try to instantiate to verify it actually works
        status = "available"
        error = None
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                _instance = cls()
            status = "online"
            del _instance
        except Exception as e:
            error = str(e)[:120]

    info = {"status": status, "name": class_name, "type": engine_type,
            "probe_ms": round((time.time() - t0) * 1000, 1)}
    if error:
        info["error"] = error
    if extras_fn:
        try:
            info.update(extras_fn(cls))
        except Exception:
            pass
    return info


def _run_probes(probes: list) -> dict:
    """Run all engine probes in parallel and return name->result dict.

    Uses max_workers=len(probes) for full parallelism and 5s per-probe timeout.
    """
    # signed: delta
    from concurrent.futures import ThreadPoolExecutor
    engines = {}
    n_workers = min(len(probes), 18)  # cap at 18 to avoid thread explosion
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_probe, n, m, c, t, io): n
                   for n, m, c, t, io in probes}
        for fut in futures:
            name = futures[fut]
            try:
                engines[name] = fut.result(timeout=5)
            except Exception as e:
                engines[name] = {"status": "offline", "name": name, "error": str(e)[:80]}
    return engines


def _build_metrics_result(engines: dict, now: float, t0: float) -> dict:
    """Build the final metrics dict with summary stats."""
    online = sum(1 for e in engines.values() if e.get("status") == "online")
    available = sum(1 for e in engines.values() if e.get("status") == "available")
    total = len(engines)
    return {
        "engines": engines,
        "summary": {
            "online": online,
            "available": available,
            "offline": total - online - available,
            "total": total,
            "health_pct": round(online / total * 100) if total else 0,
        },
        "timestamp": now,
        "collection_ms": round((time.time() - t0) * 1000, 1),
    }


def collect_engine_metrics() -> dict:
    """Collect metrics from all available engines. Cached for CACHE_TTL seconds."""
    global _cache, _cache_time
    now = time.time()
    if _cache and (now - _cache_time) < CACHE_TTL:
        return _cache

    t0 = time.time()
    engines = _run_probes(_PROBES)
    result = _build_metrics_result(engines, now, t0)

    _cache = result
    _cache_time = now
    return result


def _check_learner_daemon_alive() -> bool:
    """Check if skynet_learner daemon is running via process list."""
    import subprocess
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-Process python* -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty CommandLine -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=5
        )
        return "skynet_learner" in (result.stdout or "")
    except Exception:
        return False


def _read_learner_state_files(data_dir: Path, health: dict) -> None:
    """Populate health dict from learner_state.json and learning_episodes.json."""
    state_file = data_dir / "learner_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            health["episode_count"] = state.get("total_processed", 0)
            health["total_learnings"] = state.get("total_learnings", 0)
            health["total_broadcasts"] = state.get("total_broadcasts", 0)
            health["last_learning_ts"] = state.get("last_run")
        except Exception:
            pass

    episodes_file = data_dir / "learning_episodes.json"
    if episodes_file.exists():
        try:
            episodes = json.loads(episodes_file.read_text())
            if isinstance(episodes, list):
                health["episode_count"] = max(health["episode_count"], len(episodes))
        except Exception:
            pass


def _read_verifier_stats(data_dir: Path, health: dict) -> None:
    """Read verifier pass rate from learning.db if available."""
    db_file = data_dir / "learning.db"
    if not db_file.exists():
        return
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_file), timeout=2)
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "verifications" in tables:
            total = cur.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
            passed = cur.execute(
                "SELECT COUNT(*) FROM verifications WHERE result='pass'").fetchone()[0]
            health["verifier_pass_rate"] = round(passed / total, 3) if total else None
            health["verifier_total"] = total
            health["verifier_passed"] = passed
        conn.close()
    except Exception:
        pass


def collect_learner_health() -> dict:
    """Truthful learner daemon health telemetry.

    Reports: daemon alive/dead, episode count, last learning timestamp,
    verifier pass rate. All values from real data files -- never fabricated.
    """
    data_dir = ROOT / "data"
    health: dict = {
        "daemon_alive": False,
        "episode_count": 0,
        "total_learnings": 0,
        "total_broadcasts": 0,
        "last_learning_ts": None,
        "verifier_pass_rate": None,
        "learner_state_file": str(data_dir / "learner_state.json"),
    }

    health["daemon_alive"] = _check_learner_daemon_alive()
    _read_learner_state_files(data_dir, health)
    _read_verifier_stats(data_dir, health)
    return health


if __name__ == "__main__":
    import json
    m = collect_engine_metrics()
    m["learner_health"] = collect_learner_health()
    print(json.dumps(m, indent=2, default=str))
