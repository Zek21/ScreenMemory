#!/usr/bin/env python3
"""
SKYNET Roster -- 'Who are we?' command.
Prints formatted roster of all workers with capabilities, status, mission history, and IQ.

Usage:
    python skynet_roster.py              # full roster
    python skynet_roster.py --brief      # one-line per worker
    python skynet_roster.py --worker beta # single worker detail
    python skynet_roster.py --json       # raw JSON output
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "data" / "agent_profiles.json"
WORKER_ORDER = ["orchestrator", "alpha", "beta", "gamma", "delta"]
WORKER_SYMBOLS = {"orchestrator": "[O]", "alpha": "[A]", "beta": "[B]", "gamma": "[G]", "delta": "[D]"}
WORKER_COLORS = {
    "orchestrator": "\033[92m",  # green
    "alpha": "\033[94m",         # blue
    "beta": "\033[95m",          # purple
    "gamma": "\033[35m",         # magenta
    "delta": "\033[91m",         # red
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def load_profiles():
    if not PROFILES.exists():
        print(f"ERROR: {PROFILES} not found", file=sys.stderr)
        sys.exit(1)
    return json.loads(PROFILES.read_text(encoding="utf-8"))


def get_live_status():
    """Fetch live status from Skynet backend."""
    try:
        from urllib.request import urlopen
        data = json.loads(urlopen("http://localhost:8420/status", timeout=3).read())
        return data.get("agents", {})
    except Exception:
        return {}


def get_pulse():
    """Fetch IQ from self-pulse."""
    try:
        from urllib.request import urlopen
        data = json.loads(urlopen("http://localhost:8421/skynet/self/pulse", timeout=5).read())
        return data
    except Exception:
        return {}


def print_divider(char="-", width=72):
    print(f"{DIM}{char * width}{RESET}")


def print_roster_full(profiles, live):
    pulse = get_pulse()
    iq = pulse.get("intelligence_score", "??")
    health = pulse.get("health", "UNKNOWN")
    engines = f"{pulse.get('engines_online', '?')}/{pulse.get('engines_total', '?')}"

    print()
    print_divider()
    print(f"  {BOLD}SKYNET v3.0 Level 3 -- Worker Roster{RESET}")
    print(f"  {DIM}IQ: {RESET}{BOLD}{iq}{RESET}  {DIM}Health: {RESET}{health}  {DIM}Engines: {RESET}{engines}")
    print(f"  {DIM}Profiles: {PROFILES}{RESET}")
    print_divider()
    print()

    for wid in WORKER_ORDER:
        p = profiles.get(wid)
        if not p:
            continue
        color = WORKER_COLORS.get(wid, "")
        sym = WORKER_SYMBOLS.get(wid, "?")
        live_status = live.get(wid, {}).get("status", p.get("current_status", "UNKNOWN"))
        missions = p.get("missions_completed", 0)

        # Header
        print(f"  {color}{BOLD}{sym} {p['name'].upper()}{RESET}  {DIM}-- {p.get('role', '')}{RESET}")
        print(f"    {DIM}Model:{RESET} {p.get('model', '?')}  {DIM}Status:{RESET} ", end="")
        if live_status == "WORKING":
            print(f"\033[93m{live_status}{RESET}", end="")
        elif live_status == "IDLE":
            print(f"\033[90m{live_status}{RESET}", end="")
        else:
            print(live_status, end="")
        print(f"  {DIM}Missions:{RESET} {missions}")

        # Capabilities
        caps = p.get("capabilities", [])
        if caps:
            print(f"    {DIM}Capabilities:{RESET}")
            for c in caps[:6]:
                print(f"      {DIM}-{RESET} {c}")
            if len(caps) > 6:
                print(f"      {DIM}  ...and {len(caps)-6} more{RESET}")

        # Strengths/Weaknesses
        strengths = p.get("strengths", [])
        weaknesses = p.get("weaknesses", [])
        if strengths:
            print(f"    {DIM}Strengths:{RESET} {', '.join(strengths)}")
        if weaknesses:
            print(f"    {DIM}Weaknesses:{RESET} {', '.join(weaknesses)}")

        # Last mission
        last = p.get("last_mission_summary", "")
        if last:
            print(f"    {DIM}Last mission:{RESET} {last[:100]}")

        print()

    # Summary
    total_missions = sum(profiles.get(w, {}).get("missions_completed", 0) for w in WORKER_ORDER)
    active = sum(1 for w in WORKER_ORDER if live.get(w, {}).get("status") in ("IDLE", "WORKING"))
    working = sum(1 for w in WORKER_ORDER if live.get(w, {}).get("status") == "WORKING")
    print_divider("─")
    print(f"  {DIM}Total:{RESET} {len(WORKER_ORDER)} agents | {active} connected | {working} working | {total_missions} missions completed")
    print_divider("─")
    print()


def print_roster_brief(profiles, live):
    print(f"\n  {BOLD}SKYNET Roster (brief){RESET}\n")
    for wid in WORKER_ORDER:
        p = profiles.get(wid)
        if not p:
            continue
        color = WORKER_COLORS.get(wid, "")
        sym = WORKER_SYMBOLS.get(wid, "?")
        status = live.get(wid, {}).get("status", p.get("current_status", "?"))
        missions = p.get("missions_completed", 0)
        specs = ", ".join(p.get("specializations", [])[:3])
        print(f"  {color}{sym} {p['name'].upper():13s}{RESET} {status:8s}  {missions:2d} missions  [{specs}]")
    print()


def print_worker_detail(wid, profiles, live):
    p = profiles.get(wid)
    if not p:
        print(f"Unknown worker: {wid}")
        sys.exit(1)
    color = WORKER_COLORS.get(wid, "")
    status = live.get(wid, {}).get("status", p.get("current_status", "?"))
    print(f"\n{color}{BOLD}{WORKER_SYMBOLS.get(wid, '?')} {p['name'].upper()} -- {p.get('role', '')}{RESET}")
    print(f"  Model: {p.get('model', '?')}  |  Status: {status}  |  Missions: {p.get('missions_completed', 0)}")
    print(f"\n  Capabilities:")
    for c in p.get("capabilities", []):
        print(f"    - {c}")
    print(f"\n  Specializations: {', '.join(p.get('specializations', []))}")
    print(f"  Strengths: {', '.join(p.get('strengths', []))}")
    print(f"  Weaknesses: {', '.join(p.get('weaknesses', []))}")
    print(f"\n  Mission History:")
    for m in p.get("mission_history", []):
        print(f"    > {m}")
    print(f"\n  Last: {p.get('last_mission_summary', 'N/A')}\n")


def main():
    parser = argparse.ArgumentParser(description="SKYNET Worker Roster")
    parser.add_argument("--brief", action="store_true", help="One-line summary per worker")
    parser.add_argument("--worker", type=str, help="Show detail for one worker")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    profiles = load_profiles()
    live = get_live_status()

    if args.json:
        # Merge live status
        for wid in WORKER_ORDER:
            if wid in profiles and wid in live:
                profiles[wid]["live_status"] = live[wid].get("status", "UNKNOWN")
        print(json.dumps(profiles, indent=2))
    elif args.worker:
        print_worker_detail(args.worker, profiles, live)
    elif args.brief:
        print_roster_brief(profiles, live)
    else:
        print_roster_full(profiles, live)


if __name__ == "__main__":
    main()
