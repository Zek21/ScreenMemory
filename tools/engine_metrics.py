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

def _probe(name, module_path, class_name, engine_type, extras_fn=None):
    """Probe a single engine: import, then attempt instantiation.

    Status levels (honest):
      - "online"    — class was instantiated successfully (verified working)
      - "available" — module imported and class found, but not instantiated
      - "offline"   — import failed entirely
    """
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

        # Import succeeded — try to instantiate to verify it actually works
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


def collect_engine_metrics() -> dict:
    """Collect metrics from all available engines. Cached for CACHE_TTL seconds."""
    global _cache, _cache_time
    now = time.time()
    if _cache and (now - _cache_time) < CACHE_TTL:
        return _cache

    t0 = time.time()

    # Define all probes
    probes = [
        ("router", "core.difficulty_router", "DAAORouter", "routing"),
        ("dag", "core.dag_engine", "DAGBuilder", "workflow"),
        ("guard", "core.input_guard", "InputGuard", "security"),
        ("retriever", "core.hybrid_retrieval", "HybridRetriever", "memory"),
        ("capture", "core.capture", "DXGICapture", "vision"),
        ("ocr", "core.ocr", "OCREngine", "vision"),
        ("orchestrator", "core.orchestrator", "Orchestrator", "cognition"),
        ("analyzer", "core.analyzer", "ScreenAnalyzer", "analysis"),
        ("embedder", "core.embedder", "EmbeddingEngine", "embedding"),
        ("change_detector", "core.change_detector", "ChangeDetector", "vision"),
        ("security", "core.security", "DPAPIKeyManager", "security"),
        ("evolution", "core.self_evolution", "SelfEvolutionSystem", "learning"),
        ("learning", "core.learning_store", "LearningStore", "learning"),
        ("tools", "core.tool_synthesizer", "ToolSynthesizer", "dynamic"),
        ("feedback", "core.feedback_loop", "FeedbackLoop", "feedback"),
        ("database", "core.database", "ScreenMemoryDB", "storage"),
        ("desktop", "winctl", "Desktop", "automation"),
        ("godmode", "god_mode", "GodMode", "browser"),
    ]

    # Parallel probing via ThreadPoolExecutor
    from concurrent.futures import ThreadPoolExecutor
    engines = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_probe, n, m, c, t): n for n, m, c, t in probes}
        for fut in futures:
            name = futures[fut]
            try:
                engines[name] = fut.result(timeout=10)
            except Exception as e:
                engines[name] = {"status": "offline", "name": name, "error": str(e)[:80]}

    online = sum(1 for e in engines.values() if e.get("status") == "online")
    available = sum(1 for e in engines.values() if e.get("status") == "available")
    total = len(engines)

    result = {
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

    _cache = result
    _cache_time = now
    return result


def collect_learner_health() -> dict:
    """Truthful learner daemon health telemetry.

    Reports: daemon alive/dead, episode count, last learning timestamp,
    verifier pass rate. All values from real data files -- never fabricated.
    """
    import subprocess
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

    # Check daemon alive via process list
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-Process python* -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty CommandLine -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=5
        )
        health["daemon_alive"] = "skynet_learner" in (result.stdout or "")
    except Exception:
        pass

    # Read learner state
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

    # Read learning episodes if present
    episodes_file = data_dir / "learning_episodes.json"
    if episodes_file.exists():
        try:
            episodes = json.loads(episodes_file.read_text())
            if isinstance(episodes, list):
                health["episode_count"] = max(health["episode_count"], len(episodes))
        except Exception:
            pass

    # Verifier pass rate from learning.db if available
    db_file = data_dir / "learning.db"
    if db_file.exists():
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

    return health


if __name__ == "__main__":
    import json
    m = collect_engine_metrics()
    m["learner_health"] = collect_learner_health()
    print(json.dumps(m, indent=2, default=str))
