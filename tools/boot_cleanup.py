#!/usr/bin/env python3
"""Clean stale remnants before Skynet boot.

Removes dead PID files, marks dead worker HWNDs, clears stale dispatch logs.

Usage:
    python tools/boot_cleanup.py           # Dry-run (report only)
    python tools/boot_cleanup.py --clean   # Actually clean stale items
    python tools/boot_cleanup.py --json    # JSON output

# signed: orchestrator
"""

import json
import os
import sys
import time
import ctypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DISPATCH_MAX_AGE_HOURS = 2


def _pid_alive(pid):
    """Check if a PID is alive."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _hwnd_alive(hwnd):
    """Check if a window HWND is alive."""
    return bool(ctypes.windll.user32.IsWindow(hwnd))


def scan_pid_files():
    """Find stale PID files in data/."""
    results = []
    for f in DATA.glob("*.pid"):
        try:
            pid = int(f.read_text().strip())
            alive = _pid_alive(pid)
            results.append({
                "file": str(f.name),
                "pid": pid,
                "alive": alive,
                "stale": not alive,
            })
        except (ValueError, IOError):
            results.append({
                "file": str(f.name),
                "pid": None,
                "alive": False,
                "stale": True,
                "error": "Cannot read PID",
            })
    return results


def scan_worker_hwnds():
    """Check worker HWNDs in workers.json."""
    wf = DATA / "workers.json"
    if not wf.exists():
        return []

    raw = json.load(open(wf))
    workers = raw.get("workers", raw) if isinstance(raw, dict) else raw
    results = []
    for w in workers:
        hwnd = w.get("hwnd", 0)
        alive = _hwnd_alive(hwnd) if hwnd else False
        results.append({
            "name": w.get("name", "unknown"),
            "hwnd": hwnd,
            "alive": alive,
            "stale": bool(hwnd and not alive),  # hwnd=0 is unassigned, not stale
        })
    return results


def scan_orchestrator():
    """Check orchestrator HWND."""
    of = DATA / "orchestrator.json"
    if not of.exists():
        return {"exists": False, "stale": True}

    try:
        data = json.load(open(of))
        hwnd = data.get("hwnd", 0)
        alive = _hwnd_alive(hwnd)
        return {"hwnd": hwnd, "alive": alive, "stale": not alive, "exists": True}
    except Exception as e:
        return {"exists": True, "error": str(e), "stale": True}


def scan_consultant_state():
    """Check consultant state files."""
    results = []
    for sf in ["consultant_state.json", "gemini_consultant_state.json"]:
        path = DATA / sf
        if not path.exists():
            continue
        try:
            data = json.load(open(path))
            hwnd = data.get("hwnd", 0)
            alive = _hwnd_alive(hwnd) if hwnd else False
            results.append({
                "file": sf,
                "hwnd": hwnd,
                "alive": alive,
                "stale": hwnd and not alive,
            })
        except Exception as e:
            results.append({"file": sf, "error": str(e), "stale": True})
    return results


def scan_dispatch_log(max_age_hours=DISPATCH_MAX_AGE_HOURS):
    """Find stale dispatch log entries."""
    max_age_seconds = max_age_hours * 3600
    dl = DATA / "dispatch_log.json"
    if not dl.exists():
        return []

    try:
        entries = json.load(open(dl))
        if not isinstance(entries, list):
            return []

        cutoff = time.time() - max_age_seconds
        stale = []
        for e in entries:
            ts = e.get("timestamp", 0)
            if isinstance(ts, str):
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(ts).timestamp()
                except Exception:
                    ts = 0
            if ts < cutoff and not e.get("result_received", False):
                stale.append({
                    "worker": e.get("worker", "?"),
                    "task": str(e.get("task", ""))[:80],
                    "timestamp": e.get("timestamp", "?"),
                    "age_hours": round((time.time() - ts) / 3600, 1) if ts else "?",
                })
        return stale
    except Exception:
        return []


def full_scan():
    """Run all scans and return summary."""
    return {
        "pid_files": scan_pid_files(),
        "workers": scan_worker_hwnds(),
        "orchestrator": scan_orchestrator(),
        "consultants": scan_consultant_state(),
        "stale_dispatches": scan_dispatch_log(),
    }


def clean(scan_results):
    """Clean stale items. Returns count of items cleaned."""
    cleaned = 0

    # Clean stale PID files
    for p in scan_results["pid_files"]:
        if p["stale"]:
            pid_path = DATA / p["file"]
            if pid_path.exists():
                pid_path.unlink()
                print(f"  🗑️  Removed stale PID: {p['file']} (PID {p.get('pid', '?')})")
                cleaned += 1

    # Clean stale dispatch log entries
    stale_dispatches = scan_results.get("stale_dispatches", [])
    if stale_dispatches:
        dl = DATA / "dispatch_log.json"
        if dl.exists():
            try:
                from tools.skynet_atomic import atomic_write_json
                entries = json.load(open(dl))
                if isinstance(entries, list):
                    cutoff = time.time() - (DISPATCH_MAX_AGE_HOURS * 3600)
                    kept = []
                    removed = 0
                    for e in entries:
                        ts = e.get("timestamp", 0)
                        if isinstance(ts, str):
                            try:
                                from datetime import datetime
                                ts = datetime.fromisoformat(ts).timestamp()
                            except Exception:
                                ts = 0
                        if ts >= cutoff or e.get("result_received", False):
                            kept.append(e)
                        else:
                            removed += 1
                    atomic_write_json(dl, kept)
                    if removed:
                        print(f"  🗑️  Cleaned {removed} stale dispatch log entries (kept {len(kept)})")
                        cleaned += removed
            except Exception as e:
                print(f"  ⚠️  Failed to clean dispatch log: {e}")

    return cleaned


def print_report(results, do_clean=False):
    """Print scan report."""
    print("\n" + "=" * 60)
    print("  SKYNET BOOT CLEANUP SCAN")
    print("=" * 60)

    stale_count = 0

    # PID files
    pids = results["pid_files"]
    if pids:
        print(f"\n  PID Files ({len(pids)}):")
        for p in pids:
            icon = "✅" if not p["stale"] else "❌"
            print(f"    {icon} {p['file']} — PID {p.get('pid', '?')} "
                  f"{'alive' if p.get('alive') else 'DEAD'}")
            if p["stale"]:
                stale_count += 1

    # Workers
    workers = results["workers"]
    if workers:
        print(f"\n  Worker HWNDs ({len(workers)}):")
        for w in workers:
            icon = "✅" if not w["stale"] else "❌"
            print(f"    {icon} {w['name']:8s} HWND={w['hwnd']:8d} "
                  f"{'alive' if w.get('alive') else 'DEAD'}")
            if w["stale"]:
                stale_count += 1

    # Orchestrator
    orch = results["orchestrator"]
    if orch.get("exists"):
        icon = "✅" if not orch.get("stale") else "❌"
        print(f"\n  Orchestrator: {icon} HWND={orch.get('hwnd', '?')} "
              f"{'alive' if orch.get('alive') else 'DEAD'}")
        if orch.get("stale"):
            stale_count += 1

    # Consultants
    consultants = results["consultants"]
    if consultants:
        print(f"\n  Consultant State ({len(consultants)}):")
        for c in consultants:
            icon = "✅" if not c.get("stale") else "❌"
            print(f"    {icon} {c['file']} HWND={c.get('hwnd', '?')} "
                  f"{'alive' if c.get('alive') else 'stale' if c.get('stale') else 'no HWND'}")
            if c.get("stale"):
                stale_count += 1

    # Stale dispatches
    stale_d = results["stale_dispatches"]
    if stale_d:
        print(f"\n  Stale Dispatches ({len(stale_d)}):")
        for d in stale_d:
            print(f"    ❌ {d['worker']} — {d['age_hours']}h ago — {d['task']}")
            stale_count += 1

    print("\n" + "=" * 60)
    if stale_count == 0:
        print("  ✅ NO STALE REMNANTS FOUND")
    else:
        print(f"  ⚠️  {stale_count} STALE ITEMS FOUND")
        if do_clean:
            cleaned = clean(results)
            print(f"  🗑️  Cleaned {cleaned} items")
    print("=" * 60 + "\n")

    return stale_count


if __name__ == "__main__":
    do_clean = "--clean" in sys.argv
    as_json = "--json" in sys.argv

    results = full_scan()

    if as_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        stale = print_report(results, do_clean=do_clean)
        sys.exit(0 if stale == 0 else 1)
