#!/usr/bin/env python3
"""
skynet_changelog.py -- Automated changelog generator from git history.

Reads git log, categorizes commits (feat, fix, refactor, test, docs, chore),
and generates a formatted changelog in Markdown or JSON.

Usage:
    python tools/skynet_changelog.py                        # Last 50 commits, markdown
    python tools/skynet_changelog.py --since 2026-03-15     # Since date
    python tools/skynet_changelog.py --last 20              # Last N commits
    python tools/skynet_changelog.py --format json          # JSON output
    python tools/skynet_changelog.py --output CHANGELOG.md  # Write to file
    python tools/skynet_changelog.py --hook                 # Post-commit hook mode (last 1)

# signed: delta
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Commit prefix → category mapping
CATEGORY_PATTERNS = [
    (re.compile(r"^feat[\(:]", re.IGNORECASE), "Features"),
    (re.compile(r"^fix[\(:]", re.IGNORECASE), "Bug Fixes"),
    (re.compile(r"^refactor[\(:]", re.IGNORECASE), "Refactoring"),
    (re.compile(r"^test[\(:]", re.IGNORECASE), "Tests"),
    (re.compile(r"^docs?[\(:]", re.IGNORECASE), "Documentation"),
    (re.compile(r"^chore[\(:]", re.IGNORECASE), "Chores"),
    (re.compile(r"^perf[\(:]", re.IGNORECASE), "Performance"),
    (re.compile(r"^ci[\(:]", re.IGNORECASE), "CI/CD"),
    (re.compile(r"^style[\(:]", re.IGNORECASE), "Style"),
    (re.compile(r"^build[\(:]", re.IGNORECASE), "Build"),
    # Keyword-based fallback detection
    (re.compile(r"\bfix\b", re.IGNORECASE), "Bug Fixes"),
    (re.compile(r"\bfeat\b|\badd\b|\bcreate\b|\bnew\b", re.IGNORECASE), "Features"),
    (re.compile(r"\brefactor\b|\bclean\b|\brewrite\b", re.IGNORECASE), "Refactoring"),
    (re.compile(r"\btest\b", re.IGNORECASE), "Tests"),
    (re.compile(r"\bdoc\b|\breadme\b", re.IGNORECASE), "Documentation"),
]


def _categorize(message: str) -> str:
    """Categorize a commit message."""
    msg = message.strip()
    for pattern, category in CATEGORY_PATTERNS:
        if pattern.search(msg):
            return category
    return "Other"


def _run_git(*args) -> str:
    """Run a git command and return stdout."""
    cmd = ["git", "--no-pager", "-C", str(ROOT)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def get_commits(since=None, last=50):
    """Get commit list from git log."""
    fmt = "--format=%H|%aI|%an|%s"
    args = ["log", fmt]

    if since:
        args.append(f"--since={since}")
    if last and not since:
        args.append(f"-{last}")

    output = _run_git(*args)
    if not output:
        return []

    commits = []
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        sha, date_str, author, message = parts
        category = _categorize(message)
        commits.append({
            "sha": sha[:8],
            "date": date_str[:10],
            "author": author,
            "message": message,
            "category": category,
        })
    return commits


def format_markdown(commits, title="Changelog"):
    """Format commits as Markdown changelog."""
    if not commits:
        return f"# {title}\n\nNo commits found.\n"

    lines = [f"# {title}\n"]
    date_range = f"{commits[-1]['date']} to {commits[0]['date']}" if len(commits) > 1 else commits[0]["date"]
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | {len(commits)} commits | {date_range}*\n")

    # Group by category
    by_category = defaultdict(list)
    for c in commits:
        by_category[c["category"]].append(c)

    # Sort categories: Features first, then fixes, then alphabetical
    priority = ["Features", "Bug Fixes", "Refactoring", "Tests", "Performance", "Documentation"]
    sorted_cats = []
    for cat in priority:
        if cat in by_category:
            sorted_cats.append(cat)
    for cat in sorted(by_category.keys()):
        if cat not in sorted_cats:
            sorted_cats.append(cat)

    for category in sorted_cats:
        cat_commits = by_category[category]
        lines.append(f"\n## {category} ({len(cat_commits)})\n")
        for c in cat_commits:
            lines.append(f"- {c['message']} (`{c['sha']}` {c['date']})")

    lines.append(f"\n---\n*{len(commits)} total commits across {len(by_category)} categories*\n")
    return "\n".join(lines)


def format_json(commits):
    """Format commits as JSON."""
    by_category = defaultdict(list)
    for c in commits:
        by_category[c["category"]].append(c)

    return {
        "generated": datetime.now().isoformat(),
        "total_commits": len(commits),
        "categories": {cat: items for cat, items in by_category.items()},
        "commits": commits,
    }


def main():
    since = None
    last = 50
    fmt = "md"
    output_file = None
    hook_mode = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--since" and i + 1 < len(args):
            since = args[i + 1]
            i += 2
        elif args[i] == "--last" and i + 1 < len(args):
            last = int(args[i + 1])
            i += 2
        elif args[i] == "--format" and i + 1 < len(args):
            fmt = args[i + 1]
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_file = args[i + 1]
            i += 2
        elif args[i] == "--hook":
            hook_mode = True
            last = 1
            i += 1
        else:
            i += 1

    commits = get_commits(since=since, last=last)

    if fmt == "json":
        result = json.dumps(format_json(commits), indent=2, default=str)
    else:
        result = format_markdown(commits)

    if output_file:
        Path(output_file).write_text(result, encoding="utf-8")
        print(f"Changelog written to {output_file} ({len(commits)} commits)")
    elif hook_mode:
        # In hook mode, append to CHANGELOG.md
        changelog_path = ROOT / "CHANGELOG.md"
        if changelog_path.exists():
            existing = changelog_path.read_text(encoding="utf-8")
        else:
            existing = ""
        # Prepend new entry
        entry = format_markdown(commits, title="Latest Changes")
        changelog_path.write_text(entry + "\n\n" + existing, encoding="utf-8")
        print(f"Appended {len(commits)} commit(s) to CHANGELOG.md")
    else:
        print(result)


if __name__ == "__main__":
    main()
