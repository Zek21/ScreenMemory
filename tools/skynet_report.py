#!/usr/bin/env python3
"""
skynet_report.py -- Comprehensive worker report protocol.

Workers call post_report() after completing tasks to produce:
1. A structured markdown report in data/reports/
2. A bus message linking to the report with a brief summary
3. Learnings auto-stored to LearningStore for future context injection

Usage:
    from tools.skynet_report import post_report

    post_report(
        worker="alpha",
        topic="dispatch_fix",
        task="Fix dispatch race condition",
        approach="Added HWND validation with 3 retries",
        changes={"tools/skynet_dispatch.py": "Added dispatch_active.lock, HWND validation"},
        issues=["Daemon collision was root cause"],
        learnings=["File-based locks needed for cross-process coordination"],
        recommendations=["Test all 4 workers after dispatch changes"],
        verification="All 4 workers confirmed receipt via bus",
    )

CLI:
    python tools/skynet_report.py --list          # list recent reports
    python tools/skynet_report.py --read FILE     # print a report
    python tools/skynet_report.py --summary       # summary of all reports
"""

import json
import os
import sys
import re
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WORKER_OUTPUT_DIR = DATA_DIR / "worker_output"
REPORTS_DIR = WORKER_OUTPUT_DIR / "reports"
BUS_URL = "http://localhost:8420"

# Type-to-subfolder mapping
DOC_TYPE_DIRS = {
    "report": "reports",
    "diagnosis": "diagnoses",
    "roadmap": "roadmaps",
    "proposal": "proposals",
    "audit": "audits",
}

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
for _subdir in DOC_TYPE_DIRS.values():
    (WORKER_OUTPUT_DIR / _subdir).mkdir(parents=True, exist_ok=True)


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = re.sub(r'[^a-z0-9]+', '_', text.lower().strip())
    slug = slug.strip('_')
    return slug[:max_len]


def _post_bus(sender, topic, msg_type, content):
    """Post a message to the Skynet bus."""
    try:
        payload = json.dumps({
            "sender": sender,
            "topic": topic,
            "type": msg_type,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{BUS_URL}/bus/publish", payload,
            {"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _store_learnings(worker: str, learnings: list, topic: str):
    """Store learnings in LearningStore for future context injection."""
    try:
        sys.path.insert(0, str(ROOT))
        from core.learning_store import LearningStore
        store = LearningStore()
        for learning in learnings:
            store.learn(
                content=learning,
                category="worker_learning",
                source=f"{worker}/{topic}",
                confidence=0.7,
                tags=[worker, topic],
            )
    except Exception:
        pass


def validate_output_path(filepath) -> bool:
    """Return True only if filepath is under data/worker_output/."""
    try:
        target = Path(filepath).resolve()
        allowed = WORKER_OUTPUT_DIR.resolve()
        return str(target).startswith(str(allowed))
    except Exception:
        return False


def save_artifact(
    worker: str,
    doc_type: str,
    slug: str,
    content: str,
    summary: str = "",
    task_id: str = "",
    status: str = "final",
) -> str:
    """Save a typed artifact with YAML frontmatter to the correct subfolder.

    Args:
        worker: Worker name (alpha, beta, gamma, delta)
        doc_type: One of report, diagnosis, roadmap, proposal, audit
        slug: Short descriptor for filename
        content: Markdown body (frontmatter is prepended automatically)
        summary: One-line summary (max 120 chars)
        task_id: Optional task tracker ID
        status: draft or final

    Returns:
        Relative path to saved file.
    """
    subdir = DOC_TYPE_DIRS.get(doc_type, "reports")
    out_dir = WORKER_OUTPUT_DIR / subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_slug = _slugify(slug)
    filename = f"{ts}_{worker}_{safe_slug}.md"
    filepath = out_dir / filename
    rel_path = f"data/worker_output/{subdir}/{filename}"

    if not validate_output_path(filepath):
        raise ValueError(f"Path {filepath} is outside data/worker_output/")

    frontmatter = (
        f"---\n"
        f"worker: {worker}\n"
        f"date: {datetime.now().isoformat()}\n"
        f"task_id: {task_id}\n"
        f"type: {doc_type}\n"
        f"status: {status}\n"
        f"summary: {summary[:120]}\n"
        f"---\n\n"
    )
    filepath.write_text(frontmatter + content, encoding="utf-8")

    _post_bus(worker, "orchestrator", "artifact", rel_path)
    return rel_path


def _build_report_markdown(worker: str, topic: str, task: str, approach: str,
                           changes: dict, issues: list, learnings: list,
                           recommendations: list, verification: str,
                           auto_summary: str) -> str:
    """Build the markdown report content with YAML frontmatter."""
    lines = [
        "---",
        f"worker: {worker}",
        f"date: {datetime.now().isoformat()}",
        f"task_id: ",
        f"type: report",
        f"status: final",
        f"summary: {auto_summary[:120]}",
        "---",
        "",
        f"# Report: {topic}",
        f"**Worker:** {worker}  ",
        f"**Timestamp:** {datetime.now().isoformat()}  ",
        "",
        "## Task",
        task,
        "",
    ]

    _section_pairs = [
        (approach, "## Approach", False),
        (changes, "## Changes Made", True),
        (issues, "## Issues Found", False),
        (learnings, "## Learnings", False),
        (recommendations, "## Recommendations", False),
        (verification, "## Verification", False),
    ]
    for data, heading, is_dict in _section_pairs:
        if not data:
            continue
        lines.append(heading)
        if is_dict:
            for filepath_key, desc in data.items():
                lines.append(f"- **{filepath_key}**: {desc}")
        elif isinstance(data, list):
            for item in data:
                lines.append(f"- {item}")
        else:
            lines.append(data)
        lines.append("")

    return "\n".join(lines)


def post_report(
    worker: str,
    topic: str,
    task: str,
    approach: str = "",
    changes: dict = None,
    issues: list = None,
    learnings: list = None,
    recommendations: list = None,
    verification: str = "",
    summary: str = "",
) -> str:
    """Write a comprehensive report and post to bus. Returns relative path."""
    changes = changes or {}
    issues = issues or []
    learnings = learnings or []
    recommendations = recommendations or []

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(topic)
    filename = f"{ts}_{worker}_{slug}.md"
    filepath = REPORTS_DIR / filename
    rel_path = f"data/worker_output/reports/{filename}"

    auto_summary = summary or topic.upper().replace("_", " ")
    if not summary and approach:
        auto_summary += f": {approach[:80]}"

    content = _build_report_markdown(
        worker, topic, task, approach, changes, issues,
        learnings, recommendations, verification, auto_summary)
    filepath.write_text(content, encoding="utf-8")

    _post_bus(worker, "orchestrator", "result",
              f"REPORT:{rel_path}|SUMMARY:{auto_summary}")

    if learnings:
        _store_learnings(worker, learnings, topic)

    return rel_path


def list_reports(limit: int = 20) -> list:
    """List recent report files."""
    reports = sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [r.name for r in reports[:limit]]


def read_report(filename: str) -> str:
    """Read a report file."""
    path = REPORTS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"Report not found: {filename}"


def summary() -> dict:
    """Summary stats of all reports."""
    reports = list(REPORTS_DIR.glob("*.md"))
    by_worker = {}
    for r in reports:
        parts = r.stem.split("_", 3)
        worker = parts[2] if len(parts) > 2 else "unknown"
        by_worker[worker] = by_worker.get(worker, 0) + 1
    return {
        "total_reports": len(reports),
        "by_worker": by_worker,
        "latest": reports[-1].name if reports else None,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Report Protocol")
    parser.add_argument("--list", action="store_true", help="List recent reports")
    parser.add_argument("--read", type=str, help="Read a specific report")
    parser.add_argument("--summary", action="store_true", help="Show report summary")
    args = parser.parse_args()

    if args.list:
        for name in list_reports():
            print(f"  {name}")
        return

    if args.read:
        print(read_report(args.read))
        return

    if args.summary:
        print(json.dumps(summary(), indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
