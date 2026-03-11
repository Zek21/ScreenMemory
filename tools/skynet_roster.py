#!/usr/bin/env python3
"""
SKYNET Roster -- 'Who are we?' command.
Prints formatted roster of all registered agents with capabilities, status, mission history, and IQ.

Usage:
    python skynet_roster.py              # full roster
    python skynet_roster.py --brief      # one-line per agent
    python skynet_roster.py --worker beta # single agent detail
    python skynet_roster.py --json       # raw JSON output
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "data" / "agent_profiles.json"
AGENT_ORDER = ["orchestrator", "consultant", "alpha", "beta", "gamma", "delta"]
AGENT_SYMBOLS = {
    "orchestrator": "[O]",
    "consultant": "[C]",
    "alpha": "[A]",
    "beta": "[B]",
    "gamma": "[G]",
    "delta": "[D]",
}
AGENT_COLORS = {
    "orchestrator": "\033[92m",  # green
    "consultant": "\033[96m",    # cyan
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


def is_agent_profile(profile):
    return isinstance(profile, dict) and ("role" in profile or "model" in profile)


def ordered_agents(profiles):
    known_profiles = [name for name, profile in profiles.items() if is_agent_profile(profile)]
    extras = sorted(name for name in known_profiles if name not in AGENT_ORDER)
    return [name for name in AGENT_ORDER if name in known_profiles] + extras


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


def _print_roster_header(pulse: dict):
    """Print the Skynet roster header with IQ, health, and engine counts."""
    iq = pulse.get("intelligence_score", "??")
    health = pulse.get("health", "UNKNOWN")
    engines = f"{pulse.get('engines_online', '?')}/{pulse.get('engines_total', '?')}"
    print()
    print_divider()
    print(f"  {BOLD}SKYNET v3.0 Level 3 -- Agent Roster{RESET}")
    print(f"  {DIM}IQ: {RESET}{BOLD}{iq}{RESET}  {DIM}Health: {RESET}{health}  {DIM}Engines: {RESET}{engines}")
    print(f"  {DIM}Profiles: {PROFILES}{RESET}")
    print_divider()
    print()


def _print_agent_detail(wid: str, p: dict, live_status: str):
    """Print details for a single agent."""
    color = AGENT_COLORS.get(wid, "")
    sym = AGENT_SYMBOLS.get(wid, f"[{wid[:1].upper()}]")
    missions = p.get("missions_completed", 0)

    print(f"  {color}{BOLD}{sym} {p['name'].upper()}{RESET}  {DIM}-- {p.get('role', '')}{RESET}")
    print(f"    {DIM}Model:{RESET} {p.get('model', '?')}  {DIM}Status:{RESET} ", end="")
    status_colors = {"WORKING": "\033[93m", "IDLE": "\033[90m"}
    sc = status_colors.get(live_status, "")
    print(f"{sc}{live_status}{RESET}" if sc else live_status, end="")
    print(f"  {DIM}Missions:{RESET} {missions}")

    caps = p.get("capabilities", [])
    if caps:
        print(f"    {DIM}Capabilities:{RESET}")
        for c in caps[:6]:
            print(f"      {DIM}-{RESET} {c}")
        if len(caps) > 6:
            print(f"      {DIM}  ...and {len(caps)-6} more{RESET}")

    strengths = p.get("strengths", [])
    weaknesses = p.get("weaknesses", [])
    if strengths:
        print(f"    {DIM}Strengths:{RESET} {', '.join(strengths)}")
    if weaknesses:
        print(f"    {DIM}Weaknesses:{RESET} {', '.join(weaknesses)}")

    last = p.get("last_mission_summary", "")
    if last:
        print(f"    {DIM}Last mission:{RESET} {last[:100]}")
    print()


def print_roster_full(profiles, live):
    agent_ids = ordered_agents(profiles)
    _print_roster_header(get_pulse())

    for wid in agent_ids:
        p = profiles.get(wid)
        if not p:
            continue
        live_status = live.get(wid, {}).get("status", p.get("current_status", "UNKNOWN"))
        _print_agent_detail(wid, p, live_status)

    total_missions = sum(profiles.get(w, {}).get("missions_completed", 0) for w in agent_ids)
    active = sum(1 for w in agent_ids if live.get(w, {}).get("status") in ("IDLE", "WORKING"))
    working = sum(1 for w in agent_ids if live.get(w, {}).get("status") == "WORKING")
    print_divider("─")
    print(f"  {DIM}Total:{RESET} {len(agent_ids)} profiles | {active} live | {working} working | {total_missions} missions completed")
    print_divider("─")
    print()


def print_roster_brief(profiles, live):
    agent_ids = ordered_agents(profiles)
    print(f"\n  {BOLD}SKYNET Roster (brief){RESET}\n")
    for wid in agent_ids:
        p = profiles.get(wid)
        if not p:
            continue
        color = AGENT_COLORS.get(wid, "")
        sym = AGENT_SYMBOLS.get(wid, f"[{wid[:1].upper()}]")
        status = live.get(wid, {}).get("status", p.get("current_status", "?"))
        missions = p.get("missions_completed", 0)
        specs = ", ".join(p.get("specializations", [])[:3])
        print(f"  {color}{sym} {p['name'].upper():13s}{RESET} {status:8s}  {missions:2d} missions  [{specs}]")
    print()


def print_agent_detail(wid, profiles, live):
    p = profiles.get(wid)
    if not p:
        print(f"Unknown agent: {wid}")
        sys.exit(1)
    color = AGENT_COLORS.get(wid, "")
    status = live.get(wid, {}).get("status", p.get("current_status", "?"))
    print(f"\n{color}{BOLD}{AGENT_SYMBOLS.get(wid, f'[{wid[:1].upper()}]')} {p['name'].upper()} -- {p.get('role', '')}{RESET}")
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
    parser = argparse.ArgumentParser(description="SKYNET Agent Roster")
    parser.add_argument("--brief", action="store_true", help="One-line summary per agent")
    parser.add_argument("--worker", type=str, help="Show detail for one agent")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    profiles = load_profiles()
    live = get_live_status()

    if args.json:
        # Merge live status
        for wid in ordered_agents(profiles):
            if wid in profiles and wid in live:
                profiles[wid]["live_status"] = live[wid].get("status", "UNKNOWN")
        print(json.dumps(profiles, indent=2))
    elif args.worker:
        print_agent_detail(args.worker, profiles, live)
    elif args.brief:
        print_roster_brief(profiles, live)
    else:
        print_roster_full(profiles, live)


if __name__ == "__main__":
    main()
