#!/usr/bin/env python3
"""
skynet_intelligence_bootstrap.py — Single entry point that activates ALL intelligence systems.

Runs on every boot (from Orch-Start.ps1 or skynet_start.py) and ensures all
intelligence subsystems are live and healthy.

Usage:
  python tools/skynet_intelligence_bootstrap.py            # Full activation
  python tools/skynet_intelligence_bootstrap.py --check    # Quick health check
  python tools/skynet_intelligence_bootstrap.py --json     # Machine-readable output

As library:
  from tools.skynet_intelligence_bootstrap import activate_intelligence_stack, ensure_all_live

# signed: beta
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
BRAIN_CONFIG = DATA_DIR / "brain_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("intel_bootstrap")


# ── Subsystem Definitions ───────────────────────────────────────

# Each subsystem: (key, display_name, check_function)
# Status values: LIVE, DEGRADED, DEAD, MISSING

SUBSYSTEM_ORDER = [
    "brain_config",
    "reflexion",
    "graph_of_thoughts",
    "planner",
    "mcts",
    "self_awareness",
    "collective_iq",
    "knowledge_store",
    "backup",
    "edit_guard",
    "dispatch_resilience",
    "post_task",
]


def _check_brain_config() -> Dict[str, Any]:
    """Verify brain_config.json exists and has intelligence_stack section."""  # signed: beta
    result = {"name": "Brain Config", "status": "DEAD", "detail": ""}
    try:
        if not BRAIN_CONFIG.exists():
            result["detail"] = "brain_config.json not found"
            return result

        with open(BRAIN_CONFIG, "r", encoding="utf-8") as f:
            config = json.load(f)

        top_keys = list(config.keys())
        has_intel = "intelligence_stack" in config

        if not has_intel:
            # Create the intelligence_stack section
            config["intelligence_stack"] = {
                "enabled": True,
                "subsystems": SUBSYSTEM_ORDER,
                "auto_repair": True,
                "boot_timeout_s": 30,
                "created_by": "intel_bootstrap",
                "created_at": time.time(),
            }
            with open(BRAIN_CONFIG, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            result["status"] = "LIVE"
            result["detail"] = f"created intelligence_stack section ({len(top_keys)+1} keys)"
        else:
            result["status"] = "LIVE"
            result["detail"] = f"intelligence_stack present ({len(top_keys)} keys)"

    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {e}"
    return result


def _check_cognitive_engine(module_path: str, class_name: str,
                            display_name: str) -> Dict[str, Any]:
    """Test import and basic instantiation of a cognitive engine."""  # signed: beta
    result = {"name": display_name, "status": "DEAD", "detail": ""}
    try:
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name)
        # Try instantiation with no args (all engines support this)
        instance = cls()
        result["status"] = "LIVE"
        result["detail"] = f"{class_name} instantiated OK"
        del instance
    except ImportError as e:
        result["status"] = "DEAD"
        result["detail"] = f"import failed: {e}"
    except Exception as e:
        # Import worked but instantiation failed — engine is available but not fully live
        result["status"] = "DEGRADED"
        result["detail"] = f"import OK, init failed: {type(e).__name__}: {e}"
    return result


def _check_self_awareness() -> Dict[str, Any]:
    """Verify skynet_self.py pulse completes within 5s."""  # signed: beta
    result = {"name": "Self-Awareness (pulse)", "status": "DEAD", "detail": ""}
    try:
        from tools.skynet_self import SkynetSelf

        t0 = time.perf_counter()
        skynet = SkynetSelf()
        pulse = skynet.quick_pulse()
        elapsed = time.perf_counter() - t0

        if elapsed > 5.0:
            result["status"] = "DEGRADED"
            result["detail"] = f"pulse took {elapsed:.1f}s (>5s threshold)"
        else:
            result["status"] = "LIVE"
            overall = pulse.get("health", "UNKNOWN")
            iq = pulse.get("iq", 0)
            agents_alive = pulse.get("alive", 0)
            agents_total = pulse.get("total", 0)
            result["detail"] = (
                f"health={overall}, iq={iq:.2f}, "
                f"agents={agents_alive}/{agents_total}, {elapsed:.2f}s"
            )
        result["pulse_data"] = pulse

    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {e}"
    return result


def _check_collective_iq() -> Dict[str, Any]:
    """Check collective intelligence score."""  # signed: beta
    result = {"name": "Collective IQ", "status": "DEAD", "detail": ""}
    try:
        from tools.skynet_collective import intelligence_score

        score_data = intelligence_score()
        iq = score_data.get("intelligence_score", 0)
        components = score_data.get("components", {})

        result["status"] = "LIVE"
        result["detail"] = f"IQ={iq:.3f}"
        if components:
            parts = [f"{k}={v:.2f}" for k, v in components.items()]
            result["detail"] += f" ({', '.join(parts)})"
        result["iq_data"] = score_data

    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {e}"
    return result


def _check_knowledge_store() -> Dict[str, Any]:
    """Verify LearningStore has entries and is accessible."""  # signed: beta
    result = {"name": "Knowledge Store", "status": "DEAD", "detail": ""}
    try:
        from core.learning_store import LearningStore

        store = LearningStore()
        # Recall broad query to check if store has any entries
        facts = store.recall("skynet", top_k=5)
        fact_count = len(facts)

        result["status"] = "LIVE"
        if fact_count > 0:
            result["detail"] = f"{fact_count} facts found (store operational)"
        else:
            result["detail"] = "store accessible, 0 facts (empty but functional)"

    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {e}"
    return result


def _check_optional_module(module_path: str, func_name: str,
                           display_name: str) -> Dict[str, Any]:
    """Check an optional module that may not exist."""  # signed: beta
    result = {"name": display_name, "status": "MISSING", "detail": ""}
    try:
        mod = __import__(module_path, fromlist=[func_name])
        fn = getattr(mod, func_name, None)
        if fn and callable(fn):
            result["status"] = "LIVE"
            result["detail"] = f"{func_name}() available"
        else:
            result["status"] = "DEGRADED"
            result["detail"] = f"{func_name} not found in module"
    except ImportError:
        result["status"] = "MISSING"
        result["detail"] = f"module {module_path} not installed"
    except Exception as e:
        result["status"] = "DEAD"
        result["detail"] = f"{type(e).__name__}: {e}"
    return result


def _check_importable(module_path: str, check_names: List[str],
                      display_name: str) -> Dict[str, Any]:
    """Verify a module is importable and has expected names."""  # signed: beta
    result = {"name": display_name, "status": "DEAD", "detail": ""}
    try:
        mod = __import__(module_path, fromlist=check_names)
        found = []
        missing = []
        for name in check_names:
            if hasattr(mod, name):
                found.append(name)
            else:
                missing.append(name)

        if missing:
            result["status"] = "DEGRADED"
            result["detail"] = f"found: {found}, missing: {missing}"
        else:
            result["status"] = "LIVE"
            result["detail"] = f"all {len(found)} exports available"

    except ImportError as e:
        result["detail"] = f"import failed: {e}"
    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {e}"
    return result


# ── Main Functions ───────────────────────────────────────────────

def activate_intelligence_stack() -> Dict[str, Any]:
    """Activate and test all intelligence subsystems.

    Returns a health dict with all subsystem statuses:
    {
        "timestamp": float,
        "elapsed_ms": float,
        "subsystems": {
            "brain_config": {"name": ..., "status": LIVE|DEGRADED|DEAD|MISSING, "detail": ...},
            ...
        },
        "summary": {"live": N, "degraded": N, "dead": N, "missing": N, "total": N},
        "overall": "HEALTHY" | "DEGRADED" | "CRITICAL"
    }
    """  # signed: beta
    t0 = time.perf_counter()
    subsystems = {}

    log.info("Activating intelligence stack...")

    # 1. Brain config
    subsystems["brain_config"] = _check_brain_config()
    log.info("  brain_config: %s", subsystems["brain_config"]["status"])

    # 2. Cognitive engines
    engines = [
        ("reflexion", "core.cognitive.reflexion", "ReflexionEngine", "ReflexionEngine"),
        ("graph_of_thoughts", "core.cognitive.graph_of_thoughts", "GraphOfThoughts", "Graph of Thoughts"),
        ("planner", "core.cognitive.planner", "HierarchicalPlanner", "Hierarchical Planner"),
        ("mcts", "core.cognitive.mcts", "ReflectiveMCTS", "R-MCTS"),
    ]
    for key, mod_path, cls_name, display in engines:
        subsystems[key] = _check_cognitive_engine(mod_path, cls_name, display)
        log.info("  %s: %s", key, subsystems[key]["status"])

    # 3. Self-awareness pulse
    subsystems["self_awareness"] = _check_self_awareness()
    log.info("  self_awareness: %s", subsystems["self_awareness"]["status"])

    # 4. Collective IQ
    subsystems["collective_iq"] = _check_collective_iq()
    log.info("  collective_iq: %s", subsystems["collective_iq"]["status"])

    # 5. Knowledge store
    subsystems["knowledge_store"] = _check_knowledge_store()
    log.info("  knowledge_store: %s", subsystems["knowledge_store"]["status"])

    # 6. Backup system (optional — may not exist)
    subsystems["backup"] = _check_optional_module(
        "tools.skynet_backup", "status", "Backup System"
    )
    log.info("  backup: %s", subsystems["backup"]["status"])

    # 7. Edit guard (optional — may not exist)
    subsystems["edit_guard"] = _check_optional_module(
        "tools.skynet_edit_guard", "get_protected_files", "Edit Guard"
    )
    log.info("  edit_guard: %s", subsystems["edit_guard"]["status"])

    # 8. Dispatch resilience
    subsystems["dispatch_resilience"] = _check_importable(
        "tools.skynet_dispatch_resilience",
        ["DispatchResilience"],
        "Dispatch Resilience",
    )
    log.info("  dispatch_resilience: %s", subsystems["dispatch_resilience"]["status"])

    # 9. Post-task lifecycle
    subsystems["post_task"] = _check_importable(
        "tools.skynet_post_task",
        ["execute_post_task_lifecycle"],
        "Post-Task Lifecycle",
    )
    log.info("  post_task: %s", subsystems["post_task"]["status"])

    # Summarize
    counts = {"live": 0, "degraded": 0, "dead": 0, "missing": 0}
    for s in subsystems.values():
        status = s["status"].lower()
        if status in counts:
            counts[status] += 1

    counts["total"] = sum(counts.values())

    if counts["dead"] > 0:
        overall = "CRITICAL"
    elif counts["degraded"] > 0 or counts["missing"] > 1:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    elapsed = (time.perf_counter() - t0) * 1000

    result = {
        "timestamp": time.time(),
        "elapsed_ms": round(elapsed, 1),
        "subsystems": subsystems,
        "summary": counts,
        "overall": overall,
    }

    log.info("Intelligence stack: %s (%d live, %d degraded, %d dead, %d missing) in %.0fms",
             overall, counts["live"], counts["degraded"], counts["dead"],
             counts["missing"], elapsed)

    return result


def ensure_all_live() -> bool:
    """Activate intelligence stack, attempt auto-repair on DEAD subsystems, post to bus.

    Returns True only if ALL subsystems are LIVE (MISSING is tolerated for optional modules).
    """  # signed: beta
    health = activate_intelligence_stack()
    subsystems = health["subsystems"]
    repaired = []

    # Auto-repair pass: retry DEAD subsystems once
    for key, info in subsystems.items():
        if info["status"] != "DEAD":
            continue

        log.warning("Attempting auto-repair for DEAD subsystem: %s", key)
        try:
            if key == "brain_config":
                subsystems[key] = _check_brain_config()
            elif key in ("reflexion", "graph_of_thoughts", "planner", "mcts"):
                # Force reimport by removing from sys.modules
                engine_map = {
                    "reflexion": ("core.cognitive.reflexion", "ReflexionEngine", "ReflexionEngine"),
                    "graph_of_thoughts": ("core.cognitive.graph_of_thoughts", "GraphOfThoughts", "Graph of Thoughts"),
                    "planner": ("core.cognitive.planner", "HierarchicalPlanner", "Hierarchical Planner"),
                    "mcts": ("core.cognitive.mcts", "ReflectiveMCTS", "R-MCTS"),
                }
                mod_path, cls_name, display = engine_map[key]
                # Clear cached module to force fresh import
                if mod_path in sys.modules:
                    del sys.modules[mod_path]
                subsystems[key] = _check_cognitive_engine(mod_path, cls_name, display)
            elif key == "self_awareness":
                if "tools.skynet_self" in sys.modules:
                    del sys.modules["tools.skynet_self"]
                subsystems[key] = _check_self_awareness()
            elif key == "collective_iq":
                if "tools.skynet_collective" in sys.modules:
                    del sys.modules["tools.skynet_collective"]
                subsystems[key] = _check_collective_iq()
            elif key == "knowledge_store":
                if "core.learning_store" in sys.modules:
                    del sys.modules["core.learning_store"]
                subsystems[key] = _check_knowledge_store()
            elif key == "dispatch_resilience":
                if "tools.skynet_dispatch_resilience" in sys.modules:
                    del sys.modules["tools.skynet_dispatch_resilience"]
                subsystems[key] = _check_importable(
                    "tools.skynet_dispatch_resilience",
                    ["DispatchResilience"],
                    "Dispatch Resilience",
                )
            elif key == "post_task":
                if "tools.skynet_post_task" in sys.modules:
                    del sys.modules["tools.skynet_post_task"]
                subsystems[key] = _check_importable(
                    "tools.skynet_post_task",
                    ["execute_post_task_lifecycle"],
                    "Post-Task Lifecycle",
                )

            if subsystems[key]["status"] == "LIVE":
                repaired.append(key)
                log.info("  Auto-repair SUCCESS: %s → LIVE", key)
            else:
                log.warning("  Auto-repair FAILED: %s still %s", key, subsystems[key]["status"])

        except Exception as e:
            log.error("  Auto-repair ERROR for %s: %s", key, e)

    # Recompute summary after repairs
    counts = {"live": 0, "degraded": 0, "dead": 0, "missing": 0}
    for s in subsystems.values():
        status = s["status"].lower()
        if status in counts:
            counts[status] += 1
    counts["total"] = sum(counts.values())

    if counts["dead"] > 0:
        overall = "CRITICAL"
    elif counts["degraded"] > 0 or counts["missing"] > 1:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    health["summary"] = counts
    health["overall"] = overall
    health["repaired"] = repaired

    # Post intelligence status to bus
    try:
        from tools.skynet_spam_guard import guarded_publish

        status_parts = []
        for key, info in subsystems.items():
            status_parts.append(f"{key}={info['status']}")
        status_str = ", ".join(status_parts)
        repair_str = f", repaired: {repaired}" if repaired else ""

        guarded_publish({
            "sender": "system",
            "topic": "system",
            "type": "intelligence_boot",
            "content": (
                f"Intelligence stack {overall}: "
                f"{counts['live']} live, {counts['degraded']} degraded, "
                f"{counts['dead']} dead, {counts['missing']} missing"
                f"{repair_str}. [{status_str}]"
            ),
        })
    except Exception as e:
        log.warning("Failed to post intelligence status to bus: %s", e)

    # All live = success. MISSING is tolerated for optional modules (backup, edit_guard)
    all_operational = counts["dead"] == 0 and counts["degraded"] == 0
    return all_operational


# ── Display Helpers ──────────────────────────────────────────────

def _format_table(health: Dict[str, Any]) -> str:
    """Format health dict as a human-readable table."""  # signed: beta
    lines = []
    lines.append("=" * 70)
    lines.append("SKYNET INTELLIGENCE STACK STATUS")
    lines.append("=" * 70)

    subsystems = health.get("subsystems", {})
    for key in SUBSYSTEM_ORDER:
        if key not in subsystems:
            continue
        info = subsystems[key]
        status = info["status"]
        name = info.get("name", key)
        detail = info.get("detail", "")

        # Status icons
        icon = {"LIVE": "+", "DEGRADED": "~", "DEAD": "X", "MISSING": "?"}
        marker = icon.get(status, " ")
        pad_name = name.ljust(25)
        pad_status = status.ljust(9)

        lines.append(f"  [{marker}] {pad_name} {pad_status} {detail}")

    lines.append("-" * 70)
    summary = health.get("summary", {})
    overall = health.get("overall", "UNKNOWN")
    elapsed = health.get("elapsed_ms", 0)
    repaired = health.get("repaired", [])

    lines.append(
        f"  Overall: {overall}  |  "
        f"Live: {summary.get('live', 0)}  "
        f"Degraded: {summary.get('degraded', 0)}  "
        f"Dead: {summary.get('dead', 0)}  "
        f"Missing: {summary.get('missing', 0)}  |  "
        f"{elapsed:.0f}ms"
    )
    if repaired:
        lines.append(f"  Auto-repaired: {', '.join(repaired)}")
    lines.append("=" * 70)
    return "\n".join(lines)


# ── Self-Tests ───────────────────────────────────────────────────

def _run_self_test() -> bool:
    """Internal validation of the bootstrap module."""  # signed: beta
    print("=" * 60)
    print("skynet_intelligence_bootstrap.py — Self-Test Suite")
    print("=" * 60)

    passed = 0
    failed = 0
    tests = []

    # Test 1: activate_intelligence_stack returns valid structure
    def test_activate_structure():
        health = activate_intelligence_stack()
        assert "timestamp" in health
        assert "elapsed_ms" in health
        assert "subsystems" in health
        assert "summary" in health
        assert "overall" in health
        assert health["overall"] in ("HEALTHY", "DEGRADED", "CRITICAL")
        # Must have all defined subsystems
        for key in SUBSYSTEM_ORDER:
            assert key in health["subsystems"], f"missing subsystem: {key}"
    tests.append(("activate_intelligence_stack structure", test_activate_structure))

    # Test 2: Each subsystem has required fields
    def test_subsystem_fields():
        health = activate_intelligence_stack()
        for key, info in health["subsystems"].items():
            assert "name" in info, f"{key} missing 'name'"
            assert "status" in info, f"{key} missing 'status'"
            assert info["status"] in ("LIVE", "DEGRADED", "DEAD", "MISSING"), \
                f"{key} invalid status: {info['status']}"
            assert "detail" in info, f"{key} missing 'detail'"
    tests.append(("subsystem field validation", test_subsystem_fields))

    # Test 3: Summary counts match
    def test_summary_counts():
        health = activate_intelligence_stack()
        summary = health["summary"]
        total = summary["live"] + summary["degraded"] + summary["dead"] + summary["missing"]
        assert total == summary["total"], f"count mismatch: {total} != {summary['total']}"
        assert total == len(health["subsystems"]), \
            f"total {total} != subsystem count {len(health['subsystems'])}"
    tests.append(("summary count consistency", test_summary_counts))

    # Test 4: ensure_all_live returns bool
    def test_ensure_returns_bool():
        result = ensure_all_live()
        assert isinstance(result, bool)
    tests.append(("ensure_all_live returns bool", test_ensure_returns_bool))

    # Test 5: Table formatting doesn't crash
    def test_format_table():
        health = activate_intelligence_stack()
        table = _format_table(health)
        assert "SKYNET INTELLIGENCE STACK STATUS" in table
        assert "Overall:" in table
    tests.append(("format table", test_format_table))

    # Test 6: Brain config check creates intelligence_stack
    def test_brain_config():
        result = _check_brain_config()
        assert result["status"] in ("LIVE", "DEAD")
        assert result["name"] == "Brain Config"
    tests.append(("brain_config check", test_brain_config))

    # Test 7: Optional module check returns MISSING for nonexistent
    def test_optional_missing():
        result = _check_optional_module(
            "tools.nonexistent_module_xyz", "some_func", "Test Missing"
        )
        assert result["status"] == "MISSING"
    tests.append(("optional module MISSING detection", test_optional_missing))

    # Test 8: Importable check works for known module
    def test_importable_known():
        result = _check_importable(
            "tools.skynet_post_task",
            ["execute_post_task_lifecycle"],
            "Post-Task Test",
        )
        assert result["status"] == "LIVE"
    tests.append(("importable check (known module)", test_importable_known))

    # Run all tests
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL: {name} — {type(e).__name__}: {e}")

    print("-" * 60)
    print(f"Results: {passed}/{passed + failed} passed, {failed} failed")
    print("=" * 60)
    return failed == 0


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Skynet Intelligence Stack Bootstrap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/skynet_intelligence_bootstrap.py            # Full activation + auto-repair
  python tools/skynet_intelligence_bootstrap.py --check    # Quick health check (no repair)
  python tools/skynet_intelligence_bootstrap.py --json     # Machine-readable output
  python tools/skynet_intelligence_bootstrap.py --test     # Self-tests
        """,
    )
    parser.add_argument("--check", action="store_true", help="Quick health check only (no auto-repair)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--test", action="store_true", help="Run self-tests")

    args = parser.parse_args()

    if args.test:
        ok = _run_self_test()
        sys.exit(0 if ok else 1)

    if args.check:
        health = activate_intelligence_stack()
        if args.json:
            # Strip non-serializable data for clean JSON
            clean = {
                "timestamp": health["timestamp"],
                "elapsed_ms": health["elapsed_ms"],
                "overall": health["overall"],
                "summary": health["summary"],
                "subsystems": {
                    k: {"name": v["name"], "status": v["status"], "detail": v["detail"]}
                    for k, v in health["subsystems"].items()
                },
            }
            print(json.dumps(clean, indent=2))
        else:
            print(_format_table(health))
        sys.exit(0 if health["overall"] != "CRITICAL" else 1)
    else:
        # Full activation with auto-repair
        all_live = ensure_all_live()
        health = activate_intelligence_stack()
        if args.json:
            clean = {
                "timestamp": health["timestamp"],
                "elapsed_ms": health["elapsed_ms"],
                "overall": health["overall"],
                "summary": health["summary"],
                "all_live": all_live,
                "subsystems": {
                    k: {"name": v["name"], "status": v["status"], "detail": v["detail"]}
                    for k, v in health["subsystems"].items()
                },
            }
            print(json.dumps(clean, indent=2))
        else:
            print(_format_table(health))
            if all_live:
                print("\n  All intelligence subsystems operational.")
            else:
                print("\n  WARNING: Some subsystems are not fully operational.")
        sys.exit(0 if all_live else 1)


if __name__ == "__main__":
    main()
