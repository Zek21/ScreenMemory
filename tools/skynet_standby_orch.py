"""Skynet Standby Orchestrator — P3.05

Hot-standby orchestrator that monitors the primary orchestrator via heartbeats.
If the primary misses 3 consecutive heartbeats (configurable), the standby
promotes itself via bus consensus and takes over orchestration duties.

States:
    PRIMARY   — this instance is the active orchestrator
    STANDBY   — this instance is monitoring the primary
    PROMOTING — this instance is executing leader election

Heartbeat protocol:
    - Primary writes data/orch_heartbeat.json every HEARTBEAT_INTERVAL_S
    - Standby reads the file and checks timestamp freshness
    - 3 consecutive stale reads → initiate promotion via bus consensus

Usage:
    python tools/skynet_standby_orch.py monitor     # run as standby
    python tools/skynet_standby_orch.py promote     # force promotion
    python tools/skynet_standby_orch.py status      # show current state
    python tools/skynet_standby_orch.py heartbeat   # send one heartbeat (primary)
    python tools/skynet_standby_orch.py primary     # run as primary heartbeat emitter

Python API:
    from tools.skynet_standby_orch import StandbyOrchestrator
    standby = StandbyOrchestrator()
    standby.run_standby()  # blocking monitor loop
"""
# signed: alpha

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

DATA_DIR = _REPO / "data"
HEARTBEAT_FILE = DATA_DIR / "orch_heartbeat.json"
STANDBY_STATE_FILE = DATA_DIR / "standby_orch_state.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"

logger = logging.getLogger("skynet_standby_orch")

# ── Constants ──────────────────────────────────────────────────────
# signed: alpha

HEARTBEAT_INTERVAL_S = 10        # primary emits heartbeat every 10s
MISS_THRESHOLD = 3               # consecutive misses before promotion
STALE_TIMEOUT_S = HEARTBEAT_INTERVAL_S * MISS_THRESHOLD  # 30s total
ELECTION_TIMEOUT_S = 15          # wait for consensus votes
ELECTION_QUORUM = 2              # minimum YES votes to win election
MONITOR_POLL_S = 5               # standby checks heartbeat every 5s
BUS_URL = "http://127.0.0.1:8420"


class OrchestratorState(str, Enum):
    PRIMARY = "PRIMARY"
    STANDBY = "STANDBY"
    PROMOTING = "PROMOTING"


# ── Heartbeat Protocol ─────────────────────────────────────────────
# signed: alpha

@dataclass
class Heartbeat:
    """Heartbeat record written by the primary orchestrator."""
    timestamp: float = 0.0
    iso_time: str = ""
    hwnd: int = 0
    pid: int = 0
    uptime_s: float = 0.0
    state: str = "PRIMARY"
    version: str = "1.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Heartbeat:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def age_s(self) -> float:
        """Seconds since this heartbeat was emitted."""
        return time.time() - self.timestamp

    def is_stale(self, timeout_s: float = STALE_TIMEOUT_S) -> bool:
        """True if heartbeat is older than timeout_s."""
        return self.age_s() > timeout_s


def write_heartbeat(hwnd: int = 0, uptime_s: float = 0.0) -> Heartbeat:
    """Write a fresh heartbeat to the heartbeat file."""
    hb = Heartbeat(
        timestamp=time.time(),
        iso_time=time.strftime("%Y-%m-%dT%H:%M:%S"),
        hwnd=hwnd,
        pid=os.getpid(),
        uptime_s=uptime_s,
        state=OrchestratorState.PRIMARY.value,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hb.to_dict(), f, indent=2)
    os.replace(str(tmp), str(HEARTBEAT_FILE))
    return hb


def read_heartbeat() -> Optional[Heartbeat]:
    """Read the current heartbeat file. Returns None if missing/corrupt."""
    if not HEARTBEAT_FILE.exists():
        return None
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Heartbeat.from_dict(data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


# ── Bus Helpers ────────────────────────────────────────────────────
# signed: alpha

def _bus_post(msg: Dict[str, Any]) -> bool:
    """Post a message to the Skynet bus (fail-safe)."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(msg)
        return result.get("published", False) if isinstance(result, dict) else False
    except Exception:
        # Fallback to raw HTTP if spam guard unavailable
        try:
            import urllib.request
            data = json.dumps(msg).encode("utf-8")
            req = urllib.request.Request(
                BUS_URL + "/bus/publish",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status < 300
        except Exception:
            return False


def _bus_read(limit: int = 20) -> List[Dict[str, Any]]:
    """Read recent bus messages (fail-safe)."""
    try:
        import urllib.request
        url = "%s/bus/messages?limit=%d" % (BUS_URL, limit)
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── Leader Election ────────────────────────────────────────────────
# signed: alpha

@dataclass
class ElectionState:
    """Tracks a leader election round."""
    election_id: str = ""
    initiated_at: float = 0.0
    initiated_by_pid: int = 0
    votes_yes: int = 0
    votes_no: int = 0
    voters: List[str] = field(default_factory=list)
    result: str = ""  # "won", "lost", "timeout"
    completed_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LeaderElection:
    """Bus-based leader election using simple majority voting.

    Protocol:
        1. Standby posts election_start to bus
        2. Workers/consultants vote YES or NO within timeout
        3. If quorum reached (ELECTION_QUORUM YES votes) → promotion wins
        4. If timeout or too many NO votes → election fails
        5. If no votes at all within timeout → standby promotes anyway
           (workers may be dead, that's WHY we're promoting)
    """

    def __init__(self, timeout_s: float = ELECTION_TIMEOUT_S,
                 quorum: int = ELECTION_QUORUM):
        self.timeout_s = timeout_s
        self.quorum = quorum
        self.state: Optional[ElectionState] = None

    def initiate(self) -> ElectionState:
        """Start a new election round."""
        election_id = "election_%d_%d" % (int(time.time()), os.getpid())
        self.state = ElectionState(
            election_id=election_id,
            initiated_at=time.time(),
            initiated_by_pid=os.getpid(),
        )

        _bus_post({
            "sender": "standby_orch",
            "topic": "orchestrator",
            "type": "election_start",
            "content": "LEADER_ELECTION: Primary heartbeat stale for %ds. "
                       "Standby (PID %d) initiating promotion. "
                       "Vote YES/NO on bus topic=orchestrator type=election_vote "
                       "election_id=%s" % (
                           int(STALE_TIMEOUT_S), os.getpid(), election_id),
        })

        logger.info("Election initiated: %s", election_id)
        return self.state

    def collect_votes(self) -> ElectionState:
        """Poll bus for votes until timeout or quorum."""
        if not self.state:
            raise RuntimeError("No election initiated")

        deadline = self.state.initiated_at + self.timeout_s

        while time.time() < deadline:
            messages = _bus_read(limit=30)
            for msg in messages:
                if (msg.get("type") == "election_vote"
                        and self.state.election_id in msg.get("content", "")):
                    voter = msg.get("sender", "unknown")
                    if voter in self.state.voters:
                        continue  # already voted
                    self.state.voters.append(voter)
                    content = msg.get("content", "").upper()
                    if "YES" in content:
                        self.state.votes_yes += 1
                    elif "NO" in content:
                        self.state.votes_no += 1

            # Check for quorum
            if self.state.votes_yes >= self.quorum:
                self.state.result = "won"
                self.state.completed_at = time.time()
                logger.info("Election won: %d YES votes", self.state.votes_yes)
                return self.state

            if self.state.votes_no >= self.quorum:
                self.state.result = "lost"
                self.state.completed_at = time.time()
                logger.info("Election lost: %d NO votes", self.state.votes_no)
                return self.state

            time.sleep(1.0)

        # Timeout — promote anyway if no opposition (workers may be dead)
        if self.state.votes_no == 0:
            self.state.result = "won"
            logger.info("Election won by timeout (no opposition)")
        else:
            self.state.result = "timeout"
            logger.info("Election timed out with opposition")

        self.state.completed_at = time.time()
        return self.state


# ── Standby Orchestrator ───────────────────────────────────────────
# signed: alpha

class StandbyOrchestrator:
    """Hot-standby orchestrator that monitors and can replace the primary.

    Lifecycle:
        1. Starts in STANDBY state
        2. Polls heartbeat file every MONITOR_POLL_S seconds
        3. Counts consecutive stale heartbeats
        4. After MISS_THRESHOLD consecutive misses → enter PROMOTING state
        5. Run leader election via bus consensus
        6. If election won → become PRIMARY
        7. If election lost → return to STANDBY

    Args:
        miss_threshold:  Consecutive misses before promoting (default 3)
        poll_interval_s: Seconds between heartbeat checks (default 5)
        auto_promote:    If True, promote immediately without election
    """

    def __init__(
        self,
        miss_threshold: int = MISS_THRESHOLD,
        poll_interval_s: float = MONITOR_POLL_S,
        auto_promote: bool = False,
    ):
        self.miss_threshold = miss_threshold
        self.poll_interval_s = poll_interval_s
        self.auto_promote = auto_promote
        self.state = OrchestratorState.STANDBY
        self.consecutive_misses: int = 0
        self.start_time: float = time.time()
        self.last_heartbeat_seen: Optional[Heartbeat] = None
        self.election: Optional[LeaderElection] = None
        self.promotion_history: List[Dict[str, Any]] = []
        self._running = True
        self._lock = threading.Lock()

    def check_heartbeat(self) -> Dict[str, Any]:
        """Check the primary's heartbeat once.

        Returns:
            Dict with check result: alive (bool), age_s, consecutive_misses.
        """
        hb = read_heartbeat()
        result = {
            "alive": False,
            "age_s": -1.0,
            "consecutive_misses": self.consecutive_misses,
            "state": self.state.value,
        }

        if hb is None:
            self.consecutive_misses += 1
            result["reason"] = "no_heartbeat_file"
            logger.warning("No heartbeat file (miss %d/%d)",
                           self.consecutive_misses, self.miss_threshold)
            return result

        age = hb.age_s()
        result["age_s"] = round(age, 1)

        if hb.is_stale(STALE_TIMEOUT_S):
            self.consecutive_misses += 1
            result["reason"] = "stale_heartbeat"
            logger.warning("Stale heartbeat (%.1fs old, miss %d/%d)",
                           age, self.consecutive_misses, self.miss_threshold)
        else:
            self.consecutive_misses = 0
            result["alive"] = True
            self.last_heartbeat_seen = hb
            result["primary_pid"] = hb.pid
            result["primary_hwnd"] = hb.hwnd

        result["consecutive_misses"] = self.consecutive_misses
        return result

    def should_promote(self) -> bool:
        """True if enough consecutive heartbeat misses detected."""
        return self.consecutive_misses >= self.miss_threshold

    def promote(self) -> Dict[str, Any]:
        """Execute promotion: run election, then take over if won.

        Returns:
            Promotion result dict.
        """
        with self._lock:
            self.state = OrchestratorState.PROMOTING

        result = {
            "action": "promote",
            "pid": os.getpid(),
            "previous_state": "STANDBY",
            "consecutive_misses": self.consecutive_misses,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        if self.auto_promote:
            # Skip election — immediate takeover
            logger.info("Auto-promoting without election")
            result["election"] = "skipped"
            result["success"] = True
        else:
            # Run bus-based leader election
            self.election = LeaderElection()
            election_state = self.election.initiate()
            election_state = self.election.collect_votes()
            result["election"] = election_state.to_dict()
            result["success"] = election_state.result == "won"

        if result["success"]:
            self._become_primary()
            result["new_state"] = "PRIMARY"
            logger.info("PROMOTED to PRIMARY")
        else:
            self.state = OrchestratorState.STANDBY
            self.consecutive_misses = 0
            result["new_state"] = "STANDBY"
            logger.info("Promotion failed, returning to STANDBY")

        self.promotion_history.append(result)
        self._save_state()
        return result

    def _become_primary(self) -> None:
        """Take over as the primary orchestrator."""
        self.state = OrchestratorState.PRIMARY
        self.consecutive_misses = 0

        # Write initial heartbeat
        write_heartbeat(uptime_s=time.time() - self.start_time)

        # Update orchestrator.json with our PID
        try:
            orch_data = {}
            if ORCH_FILE.exists():
                with open(ORCH_FILE, "r", encoding="utf-8") as f:
                    orch_data = json.load(f)
            orch_data["pid"] = os.getpid()
            orch_data["promoted_from_standby"] = True
            orch_data["promoted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            tmp = ORCH_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(orch_data, f, indent=2)
            os.replace(str(tmp), str(ORCH_FILE))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not update orchestrator.json")

        # Announce on bus
        _bus_post({
            "sender": "standby_orch",
            "topic": "orchestrator",
            "type": "identity_ack",
            "content": "STANDBY PROMOTED TO PRIMARY. PID=%d. "
                       "Previous primary was unresponsive for %ds." % (
                           os.getpid(), int(STALE_TIMEOUT_S)),
        })

    def run_primary_heartbeat(self) -> None:
        """Run as primary — emit heartbeats continuously."""
        self.state = OrchestratorState.PRIMARY
        logger.info("Running as PRIMARY heartbeat emitter (PID %d)", os.getpid())

        while self._running:
            hb = write_heartbeat(uptime_s=time.time() - self.start_time)
            logger.debug("Heartbeat emitted: %.1fs uptime", hb.uptime_s)
            time.sleep(HEARTBEAT_INTERVAL_S)

    def run_standby(self) -> None:
        """Run as standby — monitor primary and promote if needed.

        This is a blocking loop. Use Ctrl+C to stop.
        """
        self.state = OrchestratorState.STANDBY
        logger.info("Running as STANDBY monitor (PID %d, poll every %ds, "
                     "miss threshold %d)",
                     os.getpid(), int(self.poll_interval_s), self.miss_threshold)

        _bus_post({
            "sender": "standby_orch",
            "topic": "orchestrator",
            "type": "standby_online",
            "content": "Standby orchestrator online. PID=%d. "
                       "Monitoring primary heartbeat every %ds. "
                       "Will promote after %d consecutive misses." % (
                           os.getpid(), int(self.poll_interval_s),
                           self.miss_threshold),
        })

        self._save_state()

        while self._running:
            if self.state == OrchestratorState.PRIMARY:
                # We've been promoted — emit heartbeats
                write_heartbeat(uptime_s=time.time() - self.start_time)
                time.sleep(HEARTBEAT_INTERVAL_S)
                continue

            check = self.check_heartbeat()

            if self.should_promote():
                logger.warning("Primary unresponsive (%d consecutive misses). "
                               "Initiating promotion.",
                               self.consecutive_misses)
                result = self.promote()
                if result["success"]:
                    logger.info("Now operating as PRIMARY")
                else:
                    logger.info("Promotion failed, continuing standby")

            time.sleep(self.poll_interval_s)

    def stop(self) -> None:
        """Stop the standby/primary loop."""
        self._running = False

    def get_status(self) -> Dict[str, Any]:
        """Get current standby orchestrator status."""
        hb = read_heartbeat()
        return {
            "state": self.state.value,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.start_time, 1),
            "consecutive_misses": self.consecutive_misses,
            "miss_threshold": self.miss_threshold,
            "poll_interval_s": self.poll_interval_s,
            "auto_promote": self.auto_promote,
            "last_heartbeat": {
                "age_s": round(hb.age_s(), 1) if hb else None,
                "primary_pid": hb.pid if hb else None,
                "primary_hwnd": hb.hwnd if hb else None,
                "stale": hb.is_stale() if hb else True,
            },
            "promotion_history": self.promotion_history,
        }

    def _save_state(self) -> None:
        """Persist standby state for diagnostics."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "state": self.state.value,
            "pid": os.getpid(),
            "consecutive_misses": self.consecutive_misses,
            "start_time": self.start_time,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "promotion_count": len(self.promotion_history),
        }
        try:
            tmp = STANDBY_STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(str(tmp), str(STANDBY_STATE_FILE))
        except OSError:
            pass


# ── Module-Level Utilities ─────────────────────────────────────────
# signed: alpha

def get_primary_status() -> Dict[str, Any]:
    """Check if the primary orchestrator is alive.

    Returns:
        Dict with alive (bool), age_s, pid, hwnd.
    """
    hb = read_heartbeat()
    if hb is None:
        return {"alive": False, "reason": "no_heartbeat_file"}
    return {
        "alive": not hb.is_stale(),
        "age_s": round(hb.age_s(), 1),
        "pid": hb.pid,
        "hwnd": hb.hwnd,
        "iso_time": hb.iso_time,
        "stale_threshold_s": STALE_TIMEOUT_S,
    }


def force_promote() -> Dict[str, Any]:
    """Force-promote this instance to primary (no election)."""
    standby = StandbyOrchestrator(auto_promote=True)
    standby.consecutive_misses = MISS_THRESHOLD  # bypass check
    return standby.promote()


# ── CLI ────────────────────────────────────────────────────────────
# signed: alpha

def _cli() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Skynet Standby Orchestrator — P3.05",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/skynet_standby_orch.py monitor              # run as standby
  python tools/skynet_standby_orch.py monitor --auto       # auto-promote without election
  python tools/skynet_standby_orch.py primary              # run as primary heartbeat emitter
  python tools/skynet_standby_orch.py promote              # force promotion
  python tools/skynet_standby_orch.py status               # show current state
  python tools/skynet_standby_orch.py heartbeat            # emit one heartbeat
""",
    )
    sub = parser.add_subparsers(dest="command")

    # monitor
    mon_p = sub.add_parser("monitor", help="Run as standby monitor")
    mon_p.add_argument("--auto", action="store_true",
                       help="Auto-promote without election")
    mon_p.add_argument("--poll", type=float, default=MONITOR_POLL_S,
                       help="Poll interval in seconds (default %d)" % MONITOR_POLL_S)
    mon_p.add_argument("--threshold", type=int, default=MISS_THRESHOLD,
                       help="Miss threshold for promotion (default %d)" % MISS_THRESHOLD)

    # primary
    sub.add_parser("primary", help="Run as primary heartbeat emitter")

    # promote
    sub.add_parser("promote", help="Force-promote to primary")

    # status
    sub.add_parser("status", help="Show primary/standby status")

    # heartbeat
    sub.add_parser("heartbeat", help="Emit a single heartbeat")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "monitor":
        standby = StandbyOrchestrator(
            miss_threshold=args.threshold,
            poll_interval_s=args.poll,
            auto_promote=args.auto,
        )

        def _sighandler(sig, frame):
            standby.stop()
            print("\nStandby monitor stopped.")
            sys.exit(0)

        signal.signal(signal.SIGINT, _sighandler)
        signal.signal(signal.SIGTERM, _sighandler)

        print("Standby Orchestrator")
        print("=" * 50)
        print("  PID:            %d" % os.getpid())
        print("  Poll interval:  %ds" % int(args.poll))
        print("  Miss threshold: %d" % args.threshold)
        print("  Auto-promote:   %s" % args.auto)
        print("  Stale timeout:  %ds" % int(STALE_TIMEOUT_S))
        print()
        print("Monitoring primary orchestrator...")
        print("Press Ctrl+C to stop.")
        print()

        standby.run_standby()

    elif args.command == "primary":
        standby = StandbyOrchestrator()

        def _sighandler(sig, frame):
            standby.stop()
            print("\nPrimary heartbeat stopped.")
            sys.exit(0)

        signal.signal(signal.SIGINT, _sighandler)
        signal.signal(signal.SIGTERM, _sighandler)

        print("Primary Heartbeat Emitter")
        print("=" * 50)
        print("  PID:      %d" % os.getpid())
        print("  Interval: %ds" % HEARTBEAT_INTERVAL_S)
        print()

        standby.run_primary_heartbeat()

    elif args.command == "promote":
        print("Force Promotion")
        print("=" * 50)
        result = force_promote()
        print("  Success: %s" % result["success"])
        print("  New state: %s" % result.get("new_state", "unknown"))
        if "election" in result and isinstance(result["election"], dict):
            e = result["election"]
            print("  Votes YES: %d" % e.get("votes_yes", 0))
            print("  Votes NO: %d" % e.get("votes_no", 0))
        elif result.get("election") == "skipped":
            print("  Election: skipped (auto-promote)")

    elif args.command == "status":
        ps = get_primary_status()
        print("Orchestrator Status")
        print("=" * 50)

        if ps.get("alive"):
            print("  Primary:    ALIVE")
            print("  PID:        %s" % ps.get("pid", "?"))
            print("  HWND:       %s" % ps.get("hwnd", "?"))
            print("  Heartbeat:  %ss ago" % ps.get("age_s", "?"))
            print("  Last seen:  %s" % ps.get("iso_time", "?"))
        else:
            reason = ps.get("reason", "stale")
            print("  Primary:    DOWN (%s)" % reason)
            if ps.get("age_s") is not None:
                print("  Last beat:  %ss ago (threshold %ds)" % (
                    ps["age_s"], STALE_TIMEOUT_S))

        # Check standby state file
        if STANDBY_STATE_FILE.exists():
            try:
                with open(STANDBY_STATE_FILE, "r", encoding="utf-8") as f:
                    sstate = json.load(f)
                print()
                print("  Standby:    %s" % sstate.get("state", "?"))
                print("  Standby PID: %s" % sstate.get("pid", "?"))
                print("  Misses:     %s" % sstate.get("consecutive_misses", "?"))
                print("  Promotions: %s" % sstate.get("promotion_count", 0))
            except (json.JSONDecodeError, OSError):
                pass

    elif args.command == "heartbeat":
        hb = write_heartbeat()
        print("Heartbeat emitted:")
        print("  Timestamp: %s" % hb.iso_time)
        print("  PID:       %d" % hb.pid)

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: alpha
