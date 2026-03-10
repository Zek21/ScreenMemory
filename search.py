"""
ScreenMemory Search — Natural language query interface.
Search your screen history using text queries, time ranges, or app filters.
"""
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.database import ScreenMemoryDB
from core.embedder import EmbeddingEngine

logger = logging.getLogger("screenmemory.search")


def parse_time_expression(expr: str) -> float:
    """Parse natural time expressions like '2h ago', 'today', 'yesterday'."""
    now = time.time()
    expr = expr.lower().strip()

    if expr == "today":
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return today.timestamp()
    elif expr == "yesterday":
        yesterday = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return yesterday.timestamp()
    elif expr.endswith("ago"):
        parts = expr.replace("ago", "").strip().split()
        if len(parts) == 2:
            num, unit = float(parts[0]), parts[1]
        elif len(parts) == 1:
            # e.g., "2h" -> "2 hours"
            s = parts[0]
            for i, c in enumerate(s):
                if not c.isdigit() and c != ".":
                    num = float(s[:i])
                    unit = s[i:]
                    break
            else:
                return now
        else:
            return now

        multipliers = {
            "s": 1, "sec": 1, "second": 1, "seconds": 1,
            "m": 60, "min": 60, "minute": 60, "minutes": 60,
            "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
            "d": 86400, "day": 86400, "days": 86400,
            "w": 604800, "week": 604800, "weeks": 604800,
        }
        mult = multipliers.get(unit, 3600)
        return now - (num * mult)

    # Try ISO format
    try:
        dt = datetime.fromisoformat(expr)
        return dt.timestamp()
    except ValueError:
        pass

    return now - 3600  # Default: 1 hour ago


def format_timestamp(ts: float) -> str:
    """Format timestamp for display."""
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_results(results: list, verbose: bool = False) -> str:
    """Format search results for terminal display."""
    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        ts = format_timestamp(r.get("timestamp", 0))
        process = r.get("active_process", "?")
        title = r.get("active_window_title", "")[:60]
        desc = r.get("analysis_text", "")[:120]
        score = r.get("score", "")

        header = f"[{i}] {ts}  {process}"
        if score:
            header += f"  (score: {score:.3f})"

        lines.append(header)
        if title:
            lines.append(f"    Title: {title}")
        if desc:
            lines.append(f"    {desc}")
        if verbose:
            ocr = r.get("ocr_text", "")[:200]
            if ocr:
                lines.append(f"    OCR: {ocr}")
        lines.append("")

    return "\n".join(lines)


def search_interactive(db: ScreenMemoryDB, embedder: EmbeddingEngine):
    """Interactive search REPL."""
    print("\n🔍 ScreenMemory Search")
    print("Commands: /recent, /app <name>, /time <range>, /stats, /quit")
    print("Or type a natural language query.\n")

    while True:
        try:
            query = input("search> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not query:
            continue

        if query == "/quit" or query == "/q":
            break

        if query == "/stats":
            stats = db.get_stats()
            print(json.dumps(stats, indent=2))
            continue

        if query == "/recent":
            results = db.get_recent(20)
            print(format_results(results))
            continue

        if query.startswith("/app "):
            app_name = query[5:].strip()
            results = db.get_by_process(app_name, 20)
            print(format_results(results))
            continue

        if query.startswith("/time "):
            time_expr = query[6:].strip()
            start_ts = parse_time_expression(time_expr)
            results = db.get_by_timerange(start_ts, time.time())
            print(format_results(results))
            continue

        # Natural language search
        results = []

        # Try hybrid search (semantic + text)
        if embedder.is_available:
            query_emb = embedder.embed_text(query)
            if query_emb is not None:
                emb_bytes = embedder.serialize(query_emb)
                results = db.search_hybrid(query, emb_bytes, limit=15)

        # Fallback to text-only search
        if not results:
            results = db.search_text(query, limit=15)

        print(format_results(results))


def main():
    parser = argparse.ArgumentParser(description="ScreenMemory Search")
    parser.add_argument("query", nargs="*", help="Search query (or interactive mode if empty)")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--recent", "-r", type=int, help="Show N most recent captures")
    parser.add_argument("--app", "-a", help="Filter by application name")
    parser.add_argument("--since", "-s", help="Show captures since time (e.g., '2h ago', 'today')")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    # Load config
    config = {}
    if os.path.exists(args.config):
        with open(args.config) as f:
            config = json.load(f)

    db_path = config.get("database", {}).get("path", "data/screen_memory.db")
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        print("Start the daemon first: python main.py")
        return

    db = ScreenMemoryDB(db_path)
    embedder = EmbeddingEngine(prefer_gpu=False)  # CPU for search queries

    try:
        if args.recent:
            results = db.get_recent(args.recent)
        elif args.app:
            results = db.get_by_process(args.app, args.limit)
        elif args.since:
            start_ts = parse_time_expression(args.since)
            results = db.get_by_timerange(start_ts, time.time())
        elif args.query:
            query = " ".join(args.query)
            if embedder.is_available:
                query_emb = embedder.embed_text(query)
                if query_emb is not None:
                    emb_bytes = embedder.serialize(query_emb)
                    results = db.search_hybrid(query, emb_bytes, limit=args.limit)
                else:
                    results = db.search_text(query, args.limit)
            else:
                results = db.search_text(query, args.limit)
        else:
            # Interactive mode
            search_interactive(db, embedder)
            return

        if args.json:
            print(json.dumps(results, indent=2, default=str))
        else:
            print(format_results(results, verbose=args.verbose))

    finally:
        db.close()


if __name__ == "__main__":
    main()
