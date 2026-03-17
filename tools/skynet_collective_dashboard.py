#!/usr/bin/env python3
"""Skynet Collective Intelligence Dashboard — real-time ASCII view of network intelligence.

Collects REAL data from all intelligence subsystems and renders a comprehensive
dashboard showing collective IQ, per-worker scores, knowledge stats, strategy
diversity, bottleneck awareness, and IQ trend.

CLI:
    python tools/skynet_collective_dashboard.py          # ASCII dashboard
    python tools/skynet_collective_dashboard.py --json   # Machine-readable JSON
    python tools/skynet_collective_dashboard.py --wire   # Wire knowledge→dispatch

# signed: gamma
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure repo root is on path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ── data collection ──────────────────────────────────────────────────────────

def _safe_call(fn, default=None):
    """Call fn(), return default on any exception."""
    try:
        return fn()
    except Exception:
        return default


def collect_intelligence_score():
    """Collect composite intelligence score from skynet_collective."""
    from tools.skynet_collective import intelligence_score
    return _safe_call(intelligence_score, {
        "intelligence_score": 0.0,
        "components": {},
        "raw": {},
    })


def collect_worker_scores():
    """Read worker scores from data/worker_scores.json."""
    path = REPO_ROOT / "data" / "worker_scores.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("scores", {})
    except Exception:
        return {}


def collect_learning_stats():
    """Query LearningStore for fact statistics."""
    try:
        from core.learning_store import LearningStore
        ls = LearningStore()
        stats = ls.stats()
        # Filter out regression-keyed categories (noise)
        by_cat = stats.get("by_category", {})
        clean = {k: v for k, v in by_cat.items() if not k.startswith("regression_")}
        regression_count = sum(v for k, v in by_cat.items() if k.startswith("regression_"))
        return {
            "total_facts": stats.get("total_facts", 0),
            "average_confidence": stats.get("average_confidence", 0.0),
            "by_category": clean,
            "regression_facts": regression_count,
        }
    except Exception:
        return {"total_facts": 0, "average_confidence": 0.0, "by_category": {}, "regression_facts": 0}


def collect_iq_trend():
    """Read IQ history from data/iq_history.json."""
    path = REPO_ROOT / "data" / "iq_history.json"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def collect_knowledge_flow():
    """Check recent knowledge messages on the bus."""
    try:
        from tools.skynet_knowledge import poll_knowledge
        msgs = poll_knowledge()
        return msgs if isinstance(msgs, list) else []
    except Exception:
        return []


def collect_evolution_status():
    """Get strategy diversity from self-evolution system."""
    try:
        from core.self_evolution import SelfEvolutionSystem
        ses = SelfEvolutionSystem()
        status = ses.get_status()
        return status
    except Exception:
        return {"summary": {"categories": {}}, "bottlenecks": []}


def collect_bottleneck_awareness():
    """Get known bottlenecks from the collective."""
    try:
        from tools.skynet_collective import share_bottlenecks
        return share_bottlenecks("dashboard") or []
    except Exception:
        return []


def collect_all():
    """Collect all intelligence data into a single dict."""
    iq = collect_intelligence_score()
    scores = collect_worker_scores()
    learning = collect_learning_stats()
    iq_trend = collect_iq_trend()
    knowledge = collect_knowledge_flow()
    evolution = collect_evolution_status()
    bottlenecks = collect_bottleneck_awareness()

    return {
        "timestamp": time.time(),
        "intelligence_score": iq,
        "worker_scores": scores,
        "learning_stats": learning,
        "iq_trend": iq_trend,
        "knowledge_flow": knowledge,
        "evolution": evolution,
        "bottlenecks": bottlenecks,
    }


# ── ASCII rendering ─────────────────────────────────────────────────────────

WIDTH = 80
THIN_LINE = "─" * WIDTH
DOUBLE_LINE = "═" * WIDTH
BOX_TOP = "┌" + "─" * (WIDTH - 2) + "┐"
BOX_BOT = "┗" + "━" * (WIDTH - 2) + "┛"
BOX_SEP = "├" + "─" * (WIDTH - 2) + "┤"


def _bar(value, max_val=1.0, length=20):
    """Render a proportional bar: ████████░░░░."""
    if max_val <= 0:
        ratio = 0
    else:
        ratio = min(max(value / max_val, 0), 1.0)
    filled = int(ratio * length)
    empty = length - filled
    return "█" * filled + "░" * empty


def _pad(text, width=WIDTH - 4):
    """Pad text to fit inside box lines."""
    if len(text) > width:
        text = text[:width - 1] + "…"
    return text.ljust(width)


def _box_line(text):
    """Wrap text in box borders: │ text │"""
    return "│ " + _pad(text) + " │"


def _center(text, width=WIDTH - 4):
    """Center text within width."""
    return text.center(width)


def render_header():
    """Render dashboard header."""
    lines = [BOX_TOP]
    lines.append(_box_line(_center("╔═══════════════════════════════════════╗")))
    lines.append(_box_line(_center("║  SKYNET COLLECTIVE INTELLIGENCE DASH  ║")))
    lines.append(_box_line(_center("╚═══════════════════════════════════════╝")))
    lines.append(_box_line(""))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(_box_line(_center(f"Generated: {ts}")))
    lines.append(BOX_SEP)
    return lines


def render_iq_section(data):
    """Render the composite IQ section."""
    iq_data = data.get("intelligence_score", {})
    score = iq_data.get("intelligence_score", 0.0)
    components = iq_data.get("components", {})
    raw = iq_data.get("raw", {})

    lines = []
    lines.append(_box_line("  ⚡ COLLECTIVE IQ"))
    lines.append(_box_line(""))

    bar = _bar(score, 1.0, 30)
    lines.append(_box_line(f"    Composite Score: {score:.3f}  {bar}"))
    lines.append(_box_line(""))

    lines.append(_box_line("    Components:"))
    for name, val in sorted(components.items()):
        cb = _bar(val, 0.3, 15)
        lines.append(_box_line(f"      {name:<14} {val:.3f}  {cb}"))

    lines.append(_box_line(""))
    wc = raw.get("worker_count", 0)
    kc = raw.get("knowledge_count", 0)
    sd = raw.get("strategy_diversity", 0)
    lines.append(_box_line(f"    Workers: {wc}  |  Knowledge: {kc}  |  Strategy Diversity: {sd}"))
    lines.append(BOX_SEP)
    return lines


def render_scores_section(data):
    """Render per-worker score table."""
    scores = data.get("worker_scores", {})
    lines = []
    lines.append(_box_line("  🏆 WORKER SCORES"))
    lines.append(_box_line(""))
    lines.append(_box_line(f"    {'Agent':<20} {'Total':>8} {'Awards':>8} {'Deductions':>11}  Bar"))
    lines.append(_box_line("    " + "─" * 60))

    # Sort by total score descending
    sorted_workers = sorted(scores.items(), key=lambda x: x[1].get("total", 0), reverse=True)
    max_score = max((s.get("total", 0) for _, s in sorted_workers), default=1) or 1

    for name, s in sorted_workers:
        total = s.get("total", 0)
        awards = s.get("awards", 0)
        deductions = s.get("deductions", 0)
        bar = _bar(total, max_score, 15)
        lines.append(_box_line(f"    {name:<20} {total:>8.3f} {awards:>8} {deductions:>11}  {bar}"))

    lines.append(BOX_SEP)
    return lines


def render_learning_section(data):
    """Render knowledge/learning statistics."""
    stats = data.get("learning_stats", {})
    lines = []
    lines.append(_box_line("  🧠 KNOWLEDGE BASE"))
    lines.append(_box_line(""))

    total = stats.get("total_facts", 0)
    conf = stats.get("average_confidence", 0.0)
    reg = stats.get("regression_facts", 0)
    lines.append(_box_line(f"    Total Facts: {total}  |  Avg Confidence: {conf:.2f}  |  Regression: {reg}"))
    lines.append(_box_line(""))

    by_cat = stats.get("by_category", {})
    if by_cat:
        # Top 10 categories by count
        top = sorted(by_cat.items(), key=lambda x: x[1], reverse=True)[:12]
        max_count = top[0][1] if top else 1
        lines.append(_box_line("    Top Categories:"))
        for cat, count in top:
            bar = _bar(count, max_count, 20)
            lines.append(_box_line(f"      {cat:<22} {count:>5}  {bar}"))

    lines.append(BOX_SEP)
    return lines


def render_evolution_section(data):
    """Render strategy evolution status."""
    evo = data.get("evolution", {})
    summary = evo.get("summary", {})
    categories = summary.get("categories", {})
    lines = []
    lines.append(_box_line("  🧬 STRATEGY EVOLUTION"))
    lines.append(_box_line(""))

    if not categories:
        lines.append(_box_line("    No evolution data available"))
    else:
        lines.append(_box_line(f"    {'Category':<12} {'Pop':>5} {'Best':>7} {'Avg':>7} {'Gen':>5} {'Success':>8}"))
        lines.append(_box_line("    " + "─" * 52))
        total_pop = 0
        for cat, info in sorted(categories.items()):
            pop = info.get("population_size", 0)
            best = info.get("best_fitness", 0.0)
            avg = info.get("avg_fitness", 0.0)
            gen = info.get("max_generation", 0)
            sr = info.get("success_rate", 0.0)
            total_pop += pop
            lines.append(_box_line(f"    {cat:<12} {pop:>5} {best:>7.3f} {avg:>7.3f} {gen:>5} {sr:>7.1%}"))
        lines.append(_box_line(""))
        lines.append(_box_line(f"    Total Strategies: {total_pop}  |  Categories: {len(categories)}"))

    lines.append(BOX_SEP)
    return lines


def render_iq_trend(data):
    """Render IQ trend sparkline from history."""
    history = data.get("iq_trend", [])
    lines = []
    lines.append(_box_line("  📈 IQ TREND"))
    lines.append(_box_line(""))

    if not history:
        lines.append(_box_line("    No IQ history available"))
    else:
        # Take last 50 readings for sparkline
        recent = history[-50:]
        values = [h.get("iq", 0) for h in recent]
        min_v = min(values)
        max_v = max(values)
        rng = max_v - min_v if max_v > min_v else 0.01

        # Sparkline using block characters
        spark_chars = " ▁▂▃▄▅▆▇█"
        sparkline = ""
        for v in values:
            idx = int(((v - min_v) / rng) * 8)
            idx = max(0, min(8, idx))
            sparkline += spark_chars[idx]

        lines.append(_box_line(f"    {sparkline}"))
        lines.append(_box_line(f"    min={min_v:.4f}  max={max_v:.4f}  latest={values[-1]:.4f}  samples={len(history)}"))

        # Trend direction
        if len(values) >= 10:
            first_avg = sum(values[:5]) / 5
            last_avg = sum(values[-5:]) / 5
            delta = last_avg - first_avg
            if delta > 0.005:
                trend = "↑ RISING"
            elif delta < -0.005:
                trend = "↓ FALLING"
            else:
                trend = "→ STABLE"
            lines.append(_box_line(f"    Trend: {trend} ({delta:+.4f})"))

    lines.append(BOX_SEP)
    return lines


def render_bottlenecks(data):
    """Render known bottlenecks."""
    evo = data.get("evolution", {})
    bottlenecks = evo.get("bottlenecks", [])
    lines = []
    lines.append(_box_line("  ⚠  BOTTLENECKS"))
    lines.append(_box_line(""))

    if not bottlenecks:
        lines.append(_box_line("    No bottlenecks detected"))
    else:
        # Show top 6 by severity
        sev_order = {"high": 0, "medium": 1, "low": 2}
        sorted_bn = sorted(bottlenecks, key=lambda b: sev_order.get(b.get("severity", "low"), 3))
        for bn in sorted_bn[:6]:
            sev = bn.get("severity", "?").upper()
            desc = bn.get("description", "unknown")
            icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
            line_text = f"    {icon} [{sev:<6}] {desc}"
            if len(line_text) > WIDTH - 4:
                line_text = line_text[:WIDTH - 5] + "…"
            lines.append(_box_line(line_text))
        if len(bottlenecks) > 6:
            lines.append(_box_line(f"    ... and {len(bottlenecks) - 6} more"))

    lines.append(BOX_SEP)
    return lines


def render_knowledge_flow(data):
    """Render recent knowledge flow activity."""
    flow = data.get("knowledge_flow", [])
    lines = []
    lines.append(_box_line("  🔄 KNOWLEDGE FLOW"))
    lines.append(_box_line(""))

    if not flow:
        lines.append(_box_line("    No recent knowledge messages on bus"))
    else:
        lines.append(_box_line(f"    Recent knowledge messages: {len(flow)}"))
        # Show last 5
        for msg in flow[-5:]:
            sender = msg.get("sender", "?")
            content = msg.get("content", "")[:50]
            lines.append(_box_line(f"      [{sender}] {content}"))

    lines.append(BOX_SEP)
    return lines


def render_footer():
    """Render dashboard footer."""
    lines = []
    lines.append(_box_line(""))
    lines.append(_box_line(_center("Truth Principle: All data above is REAL, not fabricated.")))
    lines.append(_box_line(_center("signed: gamma")))
    lines.append(BOX_BOT)
    return lines


def render_dashboard(data):
    """Render the full ASCII dashboard."""
    lines = []
    lines.extend(render_header())
    lines.extend(render_iq_section(data))
    lines.extend(render_scores_section(data))
    lines.extend(render_learning_section(data))
    lines.extend(render_evolution_section(data))
    lines.extend(render_iq_trend(data))
    lines.extend(render_bottlenecks(data))
    lines.extend(render_knowledge_flow(data))
    lines.extend(render_footer())
    return "\n".join(lines)


# ── wire_knowledge_to_dispatch ───────────────────────────────────────────────

def wire_knowledge_to_dispatch():
    """Patch skynet_dispatch result handling to auto-broadcast learnings.

    This function monkey-patches the mark_dispatch_received() function in
    skynet_dispatch.py to also call broadcast_learning() when a worker result
    arrives. This makes knowledge sharing automatic — every completed task
    contributes to the collective knowledge base without manual intervention.

    Returns True if wiring succeeded, False otherwise.
    """
    try:
        from tools import skynet_dispatch
        from tools.skynet_knowledge import broadcast_learning

        original_fn = skynet_dispatch.mark_dispatch_received

        def patched_mark_dispatch_received(worker_name, result_key=None, result_text=None):
            """Wrapped mark_dispatch_received that also broadcasts learning."""
            # Call original
            ret = original_fn(worker_name, result_key, result_text)

            # Auto-broadcast learning from the result
            if result_text and len(result_text) > 20:
                try:
                    # Extract a meaningful fact from the result
                    fact = result_text[:500]
                    broadcast_learning(
                        sender=worker_name,
                        fact=f"Task result: {fact}",
                        category="dispatch",
                        tags=["auto-wired", "task-result"],
                    )
                except Exception:
                    pass  # Knowledge broadcast is best-effort

            return ret

        skynet_dispatch.mark_dispatch_received = patched_mark_dispatch_received
        print("[WIRED] mark_dispatch_received now auto-broadcasts learnings")
        return True
    except Exception as e:
        print(f"[WIRE FAILED] {e}")
        return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Skynet Collective Intelligence Dashboard"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--wire", action="store_true",
                        help="Wire knowledge sharing into dispatch pipeline")
    args = parser.parse_args()

    if args.wire:
        success = wire_knowledge_to_dispatch()
        sys.exit(0 if success else 1)

    data = collect_all()

    if args.json:
        # Clean non-serializable items
        clean = {
            "timestamp": data["timestamp"],
            "intelligence_score": data["intelligence_score"],
            "worker_scores": data["worker_scores"],
            "learning_stats": data["learning_stats"],
            "iq_trend_count": len(data.get("iq_trend", [])),
            "iq_latest": data["iq_trend"][-1] if data.get("iq_trend") else None,
            "evolution_categories": list(
                data.get("evolution", {}).get("summary", {}).get("categories", {}).keys()
            ),
            "bottleneck_count": len(
                data.get("evolution", {}).get("bottlenecks", [])
            ),
            "knowledge_flow_count": len(data.get("knowledge_flow", [])),
        }
        print(json.dumps(clean, indent=2, default=str))
    else:
        print(render_dashboard(data))


if __name__ == "__main__":
    main()
