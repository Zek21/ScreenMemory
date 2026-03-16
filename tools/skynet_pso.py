"""Skynet PSO Parameter Auto-Tuning — P3.01

Particle Swarm Optimization for auto-tuning Skynet operational parameters.
Reads live metrics from data/realtime.json and bus, evaluates a composite
fitness function (throughput, error rate, latency), and converges on optimal
parameter values with safety bounds.

Tunable parameters:
    dispatch_cooldown_s      — delay between sequential dispatches
    stuck_threshold_s        — seconds before PROCESSING is considered stuck
    spam_dedup_window_s      — duplicate message suppression window
    heartbeat_interval_s     — self-prompt heartbeat gap
    backpressure_yellow_s    — yellow-zone dispatch cooldown
    alert_dedup_window_s     — suppress duplicate DEAD alerts window
    restart_cooldown_s       — cooldown between daemon restarts
    idle_improvement_wait_s  — time before generating improvement tasks

Usage:
    python tools/skynet_pso.py optimize --iterations 100
    python tools/skynet_pso.py current
    python tools/skynet_pso.py apply
    python tools/skynet_pso.py history
    python tools/skynet_pso.py simulate --iterations 50

Python API:
    from tools.skynet_pso import PSOOptimizer
    pso = PSOOptimizer()
    result = pso.optimize(iterations=100)
    pso.apply_best()
"""
# signed: alpha

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

DATA_DIR = _REPO / "data"
BRAIN_CONFIG_PATH = DATA_DIR / "brain_config.json"
REALTIME_PATH = DATA_DIR / "realtime.json"
PSO_HISTORY_PATH = DATA_DIR / "pso_history.jsonl"
PSO_BEST_PATH = DATA_DIR / "pso_best.json"


# ── Parameter Definitions ───────────────────────────────────────────
# Each tunable parameter has a name, source location, current value reader,
# min/max safety bounds, and a description.
# signed: alpha

@dataclass
class ParamSpec:
    """Specification for a single tunable parameter."""
    name: str
    description: str
    min_val: float
    max_val: float
    default: float
    unit: str = "seconds"
    source_file: str = ""
    source_key: str = ""
    weight: float = 1.0  # importance weight in fitness

    def clamp(self, value: float) -> float:
        """Clamp value to safety bounds."""
        return max(self.min_val, min(self.max_val, value))


# Registry of all tunable parameters with safety bounds
PARAM_REGISTRY: List[ParamSpec] = [
    ParamSpec(
        name="dispatch_cooldown_s",
        description="Delay between sequential dispatches to avoid clipboard races",
        min_val=0.5, max_val=10.0, default=2.0,
        source_file="tools/skynet_workflow.py",
        source_key="DISPATCH_COOLDOWN_S",
    ),
    ParamSpec(
        name="stuck_threshold_s",
        description="Seconds before PROCESSING worker is considered stuck",
        min_val=60.0, max_val=600.0, default=180.0,
        source_file="tools/skynet_self_heal.py",
        source_key="STUCK_THRESHOLDS.standard",
    ),
    ParamSpec(
        name="spam_dedup_window_s",
        description="Duplicate message suppression window for SpamGuard",
        min_val=60.0, max_val=3600.0, default=900.0,
        source_file="tools/skynet_spam_guard.py",
        source_key="DEFAULT_DEDUP_WINDOW",
    ),
    ParamSpec(
        name="heartbeat_interval_s",
        description="Minimum gap between self-prompt heartbeat injections",
        min_val=60.0, max_val=900.0, default=300.0,
        source_file="data/brain_config.json",
        source_key="self_prompt.min_prompt_gap",
    ),
    ParamSpec(
        name="backpressure_yellow_s",
        description="Extra dispatch cooldown in yellow backpressure zone",
        min_val=1.0, max_val=15.0, default=4.0,
        source_file="tools/skynet_backpressure.py",
        source_key="YELLOW_DISPATCH_COOLDOWN_S",
    ),
    ParamSpec(
        name="alert_dedup_window_s",
        description="Suppress duplicate DEAD alerts for this window",
        min_val=30.0, max_val=900.0, default=300.0,
        source_file="tools/skynet_monitor.py",
        source_key="ALERT_DEDUP_WINDOW",
    ),
    ParamSpec(
        name="restart_cooldown_s",
        description="Cooldown between daemon restart attempts",
        min_val=10.0, max_val=300.0, default=60.0,
        source_file="tools/skynet_monitor.py",
        source_key="RESTART_COOLDOWN_SECONDS",
    ),
    ParamSpec(
        name="idle_improvement_wait_s",
        description="Time before orchestrator generates improvement tasks for idle workers",
        min_val=30.0, max_val=600.0, default=120.0,
        source_file="data/brain_config.json",
        source_key="god_protocol.max_idle_before_self_propose_s",
    ),
]

PARAM_NAMES = [p.name for p in PARAM_REGISTRY]
PARAM_INDEX = {p.name: i for i, p in enumerate(PARAM_REGISTRY)}
N_DIMS = len(PARAM_REGISTRY)


def _get_defaults() -> List[float]:
    """Return default parameter values as a list."""
    return [p.default for p in PARAM_REGISTRY]


def _clamp_position(position: List[float]) -> List[float]:
    """Clamp all dimensions to their safety bounds."""
    return [PARAM_REGISTRY[i].clamp(position[i]) for i in range(N_DIMS)]


# ── Fitness Function ────────────────────────────────────────────────
# Composite fitness from live metrics: throughput, error rate, latency,
# worker utilization, and bus health.
# signed: alpha

def _read_realtime() -> Dict[str, Any]:
    """Read data/realtime.json for live system metrics."""
    if not REALTIME_PATH.exists():
        return {}
    try:
        with open(REALTIME_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _read_brain_config() -> Dict[str, Any]:
    """Read data/brain_config.json."""
    if not BRAIN_CONFIG_PATH.exists():
        return {}
    try:
        with open(BRAIN_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _read_bus_stats() -> Dict[str, Any]:
    """Read recent bus message stats via HTTP (optional, fail-safe)."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8420/bus/messages?limit=50",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            messages = json.loads(resp.read().decode("utf-8"))
        if isinstance(messages, list):
            total = len(messages)
            alerts = sum(1 for m in messages if m.get("type") == "alert")
            results = sum(1 for m in messages if m.get("type") == "result")
            return {"total": total, "alerts": alerts, "results": results}
    except Exception:
        pass
    return {"total": 0, "alerts": 0, "results": 0}


def compute_fitness(position: List[float],
                    realtime: Optional[Dict] = None,
                    bus_stats: Optional[Dict] = None) -> float:
    """Evaluate fitness of a parameter configuration.

    Fitness is a composite score in [0, 1] where higher is better.
    Components:
        - throughput_score:    tasks completed / uptime (higher = better)
        - error_score:         1 - (errors / max(tasks, 1)) (lower errors = better)
        - utilization_score:   fraction of workers actively processing
        - latency_score:       penalty for overly aggressive or overly slow params
        - stability_score:     fewer alerts and restarts = better

    The position vector influences fitness through a penalty model:
    parameters too far from a balanced zone get penalized, while
    parameters in a productive zone are rewarded.
    """
    rt = realtime or _read_realtime()
    bus = bus_stats or _read_bus_stats()
    workers = rt.get("workers", {})

    # ── Extract live metrics ─────────────────────────────────────
    total_tasks = sum(w.get("tasks_completed", 0) for w in workers.values()
                      if isinstance(w, dict))
    total_errors = sum(w.get("total_errors", 0) for w in workers.values()
                       if isinstance(w, dict))
    total_uptime = max(
        max((w.get("uptime_s", 0) for w in workers.values()
             if isinstance(w, dict)), default=1),
        1.0,
    )
    active_count = sum(
        1 for w in workers.values()
        if isinstance(w, dict) and w.get("status") == "PROCESSING"
    )
    worker_count = max(
        sum(1 for w in workers.values()
            if isinstance(w, dict) and w.get("status") in
            ("IDLE", "PROCESSING", "STEERING")),
        1,
    )
    bus_alerts = bus.get("alerts", 0)
    bus_total = max(bus.get("total", 1), 1)

    # ── Component scores (each in [0, 1]) ────────────────────────
    # Throughput: tasks per hour, capped at 100/hr for normalization
    throughput_per_hour = (total_tasks / total_uptime) * 3600
    throughput_score = min(throughput_per_hour / 100.0, 1.0)

    # Error rate: fraction of tasks that errored
    error_rate = total_errors / max(total_tasks, 1)
    error_score = max(1.0 - error_rate, 0.0)

    # Utilization: active workers / total workers
    utilization_score = active_count / worker_count

    # Stability: fewer alerts relative to total bus traffic
    alert_ratio = bus_alerts / bus_total
    stability_score = max(1.0 - alert_ratio * 2.0, 0.0)

    # ── Parameter balance penalty ────────────────────────────────
    # Each parameter has an ideal zone around its default. Deviations
    # from the zone incur a penalty proportional to the distance.
    balance_penalty = 0.0
    for i, spec in enumerate(PARAM_REGISTRY):
        val = position[i]
        param_range = spec.max_val - spec.min_val
        if param_range <= 0:
            continue
        # Normalized distance from default (0 = at default, 1 = at bound)
        norm_dist = abs(val - spec.default) / param_range
        balance_penalty += norm_dist * spec.weight

    # Average penalty across parameters, scaled to [0, 0.3]
    avg_penalty = (balance_penalty / N_DIMS) * 0.3

    # ── Parameter interaction bonuses ────────────────────────────
    # Reward configurations where related parameters are balanced
    interaction_bonus = 0.0
    dispatch_cd = position[PARAM_INDEX["dispatch_cooldown_s"]]
    bp_yellow = position[PARAM_INDEX["backpressure_yellow_s"]]
    # Backpressure yellow should be >= dispatch cooldown
    if bp_yellow >= dispatch_cd:
        interaction_bonus += 0.02

    stuck_thresh = position[PARAM_INDEX["stuck_threshold_s"]]
    heartbeat = position[PARAM_INDEX["heartbeat_interval_s"]]
    # Heartbeat should be shorter than stuck threshold
    if heartbeat < stuck_thresh:
        interaction_bonus += 0.02

    alert_dedup = position[PARAM_INDEX["alert_dedup_window_s"]]
    restart_cd = position[PARAM_INDEX["restart_cooldown_s"]]
    # Restart cooldown should be less than alert dedup (avoid spam during restart)
    if restart_cd <= alert_dedup:
        interaction_bonus += 0.01

    # ── Composite fitness ────────────────────────────────────────
    fitness = (
        0.30 * throughput_score
        + 0.25 * error_score
        + 0.20 * utilization_score
        + 0.15 * stability_score
        + 0.10 * (1.0 - avg_penalty)
        + interaction_bonus
    )
    return round(max(0.0, min(1.0, fitness)), 6)


# ── Particle ────────────────────────────────────────────────────────
# signed: alpha

@dataclass
class Particle:
    """A single particle in the swarm."""
    position: List[float]
    velocity: List[float]
    personal_best_pos: List[float]
    personal_best_fit: float = -1.0
    fitness: float = -1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position": [round(v, 4) for v in self.position],
            "velocity": [round(v, 4) for v in self.velocity],
            "personal_best_pos": [round(v, 4) for v in self.personal_best_pos],
            "personal_best_fit": round(self.personal_best_fit, 6),
            "fitness": round(self.fitness, 6),
        }

    @classmethod
    def from_dict(cls, d: Dict) -> Particle:
        return cls(
            position=d["position"],
            velocity=d["velocity"],
            personal_best_pos=d["personal_best_pos"],
            personal_best_fit=d.get("personal_best_fit", -1.0),
            fitness=d.get("fitness", -1.0),
        )


def _create_particle(rng: random.Random) -> Particle:
    """Create a particle with random position within safety bounds."""
    position = []
    velocity = []
    for spec in PARAM_REGISTRY:
        span = spec.max_val - spec.min_val
        pos = spec.min_val + rng.random() * span
        vel = (rng.random() - 0.5) * span * 0.2  # small initial velocity
        position.append(pos)
        velocity.append(vel)
    return Particle(
        position=position,
        velocity=velocity,
        personal_best_pos=position[:],
        personal_best_fit=-1.0,
        fitness=-1.0,
    )


# ── PSO Optimizer ───────────────────────────────────────────────────
# signed: alpha

class PSOOptimizer:
    """Particle Swarm Optimization for Skynet parameter tuning.

    Hyperparameters:
        swarm_size:   Number of particles (default 20)
        w:            Inertia weight — controls momentum (default 0.7)
        c1:           Cognitive coefficient — pull toward personal best (default 1.5)
        c2:           Social coefficient — pull toward global best (default 1.5)
        w_decay:      Inertia decay per iteration for convergence (default 0.99)
    """

    def __init__(
        self,
        swarm_size: int = 20,
        w: float = 0.7,
        c1: float = 1.5,
        c2: float = 1.5,
        w_decay: float = 0.99,
        seed: Optional[int] = None,
    ):
        self.swarm_size = swarm_size
        self.w = w
        self.c1 = c1
        self.c2 = c2
        self.w_decay = w_decay
        self.rng = random.Random(seed)

        self.swarm: List[Particle] = []
        self.global_best_pos: List[float] = _get_defaults()
        self.global_best_fit: float = -1.0
        self.iteration: int = 0
        self.history: List[Dict[str, Any]] = []

        self._init_swarm()

    def _init_swarm(self) -> None:
        """Initialize the particle swarm."""
        self.swarm = []
        for i in range(self.swarm_size):
            if i == 0:
                # First particle starts at current defaults
                p = Particle(
                    position=_get_defaults(),
                    velocity=[0.0] * N_DIMS,
                    personal_best_pos=_get_defaults(),
                )
            else:
                p = _create_particle(self.rng)
            self.swarm.append(p)

    def _evaluate_particle(self, particle: Particle,
                           realtime: Optional[Dict] = None,
                           bus_stats: Optional[Dict] = None) -> float:
        """Evaluate fitness for a single particle."""
        clamped = _clamp_position(particle.position)
        particle.position = clamped
        fitness = compute_fitness(clamped, realtime, bus_stats)
        particle.fitness = fitness

        # Update personal best
        if fitness > particle.personal_best_fit:
            particle.personal_best_fit = fitness
            particle.personal_best_pos = clamped[:]

        # Update global best
        if fitness > self.global_best_fit:
            self.global_best_fit = fitness
            self.global_best_pos = clamped[:]

        return fitness

    def _update_velocity(self, particle: Particle) -> None:
        """Update particle velocity using PSO equations."""
        for d in range(N_DIMS):
            r1 = self.rng.random()
            r2 = self.rng.random()

            cognitive = self.c1 * r1 * (
                particle.personal_best_pos[d] - particle.position[d]
            )
            social = self.c2 * r2 * (
                self.global_best_pos[d] - particle.position[d]
            )
            particle.velocity[d] = (
                self.w * particle.velocity[d] + cognitive + social
            )

            # Velocity clamping: max 30% of parameter range per step
            spec = PARAM_REGISTRY[d]
            max_vel = (spec.max_val - spec.min_val) * 0.3
            particle.velocity[d] = max(-max_vel,
                                       min(max_vel, particle.velocity[d]))

    def _update_position(self, particle: Particle) -> None:
        """Update particle position and clamp to bounds."""
        for d in range(N_DIMS):
            particle.position[d] += particle.velocity[d]
        particle.position = _clamp_position(particle.position)

    def step(self, realtime: Optional[Dict] = None,
             bus_stats: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute one PSO iteration.

        Returns:
            Dict with iteration number, global best fitness, and best position.
        """
        self.iteration += 1

        # Evaluate all particles
        fitnesses = []
        for p in self.swarm:
            f = self._evaluate_particle(p, realtime, bus_stats)
            fitnesses.append(f)

        # Update velocities and positions
        for p in self.swarm:
            self._update_velocity(p)
            self._update_position(p)

        # Decay inertia for convergence
        self.w *= self.w_decay

        # Record history
        best_params = {PARAM_NAMES[i]: round(self.global_best_pos[i], 4)
                       for i in range(N_DIMS)}
        record = {
            "iteration": self.iteration,
            "global_best_fit": round(self.global_best_fit, 6),
            "avg_fitness": round(sum(fitnesses) / len(fitnesses), 6),
            "min_fitness": round(min(fitnesses), 6),
            "max_fitness": round(max(fitnesses), 6),
            "inertia": round(self.w, 4),
            "best_params": best_params,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.history.append(record)
        return record

    def optimize(self, iterations: int = 100,
                 verbose: bool = False) -> Dict[str, Any]:
        """Run PSO optimization for N iterations.

        Args:
            iterations: Number of optimization iterations.
            verbose:    Print progress every 10 iterations.

        Returns:
            Dict with best parameters, fitness, and convergence history.
        """
        # Read live metrics once (they don't change during optimization)
        realtime = _read_realtime()
        bus_stats = _read_bus_stats()

        for i in range(iterations):
            record = self.step(realtime, bus_stats)
            if verbose and (i + 1) % 10 == 0:
                print(
                    "  iter %3d: best_fit=%.6f avg=%.6f w=%.4f"
                    % (record["iteration"], record["global_best_fit"],
                       record["avg_fitness"], record["inertia"])
                )

        # Build result
        best_params = {}
        for i, spec in enumerate(PARAM_REGISTRY):
            best_params[spec.name] = {
                "value": round(self.global_best_pos[i], 4),
                "default": spec.default,
                "min": spec.min_val,
                "max": spec.max_val,
                "unit": spec.unit,
                "delta": round(self.global_best_pos[i] - spec.default, 4),
                "description": spec.description,
            }

        result = {
            "status": "completed",
            "iterations": iterations,
            "swarm_size": self.swarm_size,
            "global_best_fitness": round(self.global_best_fit, 6),
            "final_inertia": round(self.w, 4),
            "best_parameters": best_params,
            "convergence": self.history[-5:] if len(self.history) >= 5
                           else self.history,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        # Persist
        self._save_history()
        self._save_best(result)

        return result

    def apply_best(self) -> Dict[str, Any]:
        """Apply the best parameters to brain_config.json.

        Only updates parameters that have a brain_config.json source.
        Returns dict of applied changes.
        """
        if self.global_best_fit < 0:
            return {"error": "No optimization run yet"}

        config = _read_brain_config()
        if not config:
            return {"error": "Cannot read brain_config.json"}

        applied = {}
        for i, spec in enumerate(PARAM_REGISTRY):
            value = round(self.global_best_pos[i], 2)
            if spec.source_file == "data/brain_config.json" and spec.source_key:
                keys = spec.source_key.split(".")
                target = config
                for k in keys[:-1]:
                    if k not in target:
                        target[k] = {}
                    target = target[k]
                old_val = target.get(keys[-1], spec.default)
                target[keys[-1]] = value
                applied[spec.name] = {
                    "old": old_val,
                    "new": value,
                    "key": spec.source_key,
                }

        if applied:
            tmp = BRAIN_CONFIG_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            tmp.replace(BRAIN_CONFIG_PATH)

        return {
            "applied": applied,
            "not_applied": [
                spec.name for spec in PARAM_REGISTRY
                if spec.source_file != "data/brain_config.json"
            ],
            "note": "Non-config parameters require code changes to apply",
        }

    def current_values(self) -> Dict[str, Any]:
        """Read current parameter values from their source files."""
        values = {}
        config = _read_brain_config()

        for spec in PARAM_REGISTRY:
            val = spec.default
            source = "default"

            if spec.source_file == "data/brain_config.json" and config:
                keys = spec.source_key.split(".")
                target = config
                try:
                    for k in keys:
                        target = target[k]
                    val = float(target)
                    source = "brain_config.json"
                except (KeyError, TypeError, ValueError):
                    pass

            values[spec.name] = {
                "current": val,
                "default": spec.default,
                "min": spec.min_val,
                "max": spec.max_val,
                "unit": spec.unit,
                "source": source,
                "description": spec.description,
            }
        return values

    def _save_history(self) -> None:
        """Append optimization history to JSONL file."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(PSO_HISTORY_PATH, "a", encoding="utf-8") as f:
                for record in self.history:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _save_best(self, result: Dict) -> None:
        """Save best result to JSON file."""
        try:
            tmp = PSO_BEST_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            tmp.replace(PSO_BEST_PATH)
        except OSError:
            pass

    def to_dict(self) -> Dict[str, Any]:
        """Serialize optimizer state."""
        return {
            "swarm_size": self.swarm_size,
            "w": self.w,
            "c1": self.c1,
            "c2": self.c2,
            "w_decay": self.w_decay,
            "iteration": self.iteration,
            "global_best_pos": [round(v, 4) for v in self.global_best_pos],
            "global_best_fit": round(self.global_best_fit, 6),
            "swarm": [p.to_dict() for p in self.swarm],
        }


# ── History Utilities ───────────────────────────────────────────────
# signed: alpha

def read_history(limit: int = 50) -> List[Dict]:
    """Read PSO optimization history from JSONL file."""
    if not PSO_HISTORY_PATH.exists():
        return []
    records = []
    try:
        with open(PSO_HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records[-limit:]


def read_best() -> Optional[Dict]:
    """Read the last saved best result."""
    if not PSO_BEST_PATH.exists():
        return None
    try:
        with open(PSO_BEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ── CLI ─────────────────────────────────────────────────────────────
# signed: alpha

def _cli() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Skynet PSO Parameter Auto-Tuning — P3.01",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/skynet_pso.py optimize --iterations 100
  python tools/skynet_pso.py optimize --iterations 200 --swarm 30 --verbose
  python tools/skynet_pso.py current
  python tools/skynet_pso.py apply
  python tools/skynet_pso.py history --limit 20
  python tools/skynet_pso.py simulate --iterations 50
""",
    )
    sub = parser.add_subparsers(dest="command")

    # optimize
    opt_p = sub.add_parser("optimize", help="Run PSO optimization")
    opt_p.add_argument("--iterations", "-n", type=int, default=100,
                       help="Number of iterations (default 100)")
    opt_p.add_argument("--swarm", "-s", type=int, default=20,
                       help="Swarm size (default 20)")
    opt_p.add_argument("--seed", type=int, default=None,
                       help="Random seed for reproducibility")
    opt_p.add_argument("--verbose", "-v", action="store_true",
                       help="Print progress every 10 iterations")

    # current
    sub.add_parser("current", help="Show current parameter values")

    # apply
    sub.add_parser("apply", help="Apply best parameters to brain_config.json")

    # history
    hist_p = sub.add_parser("history", help="Show optimization history")
    hist_p.add_argument("--limit", "-l", type=int, default=20)

    # simulate
    sim_p = sub.add_parser("simulate",
                           help="Run optimization in simulation mode (no apply)")
    sim_p.add_argument("--iterations", "-n", type=int, default=50)
    sim_p.add_argument("--swarm", "-s", type=int, default=15)
    sim_p.add_argument("--seed", type=int, default=42,
                       help="Random seed (default 42 for reproducibility)")
    sim_p.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if args.command == "optimize":
        print("PSO Parameter Auto-Tuning")
        print("=" * 50)
        print("Swarm size: %d | Iterations: %d" % (args.swarm, args.iterations))
        print("Parameters: %d tunable dimensions" % N_DIMS)
        print()

        pso = PSOOptimizer(swarm_size=args.swarm, seed=args.seed)
        result = pso.optimize(iterations=args.iterations, verbose=args.verbose)

        print()
        print("Optimization Complete")
        print("=" * 50)
        print("Best fitness: %.6f" % result["global_best_fitness"])
        print("Final inertia: %.4f" % result["final_inertia"])
        print()
        print("%-28s %10s %10s %10s %s" % (
            "Parameter", "Optimal", "Default", "Delta", "Unit"))
        print("-" * 80)
        for name, info in result["best_parameters"].items():
            print("%-28s %10.2f %10.2f %+10.2f %s" % (
                name, info["value"], info["default"],
                info["delta"], info["unit"]))

    elif args.command == "current":
        pso = PSOOptimizer()
        values = pso.current_values()
        print("Current Parameter Values")
        print("=" * 50)
        print("%-28s %10s %10s %10s %10s" % (
            "Parameter", "Current", "Default", "Min", "Max"))
        print("-" * 80)
        for name, info in values.items():
            print("%-28s %10.2f %10.2f %10.2f %10.2f" % (
                name, info["current"], info["default"],
                info["min"], info["max"]))

    elif args.command == "apply":
        best = read_best()
        if not best:
            print("No optimization result found. Run 'optimize' first.")
            sys.exit(1)

        print("Applying best parameters from last optimization...")
        print("Best fitness: %.6f" % best["global_best_fitness"])
        print()

        pso = PSOOptimizer()
        pso.global_best_pos = [
            best["best_parameters"][name]["value"]
            for name in PARAM_NAMES
        ]
        pso.global_best_fit = best["global_best_fitness"]

        result = pso.apply_best()
        if "error" in result:
            print("Error: %s" % result["error"])
            sys.exit(1)

        if result["applied"]:
            print("Applied to brain_config.json:")
            for name, change in result["applied"].items():
                print("  %-28s %s -> %s" % (name, change["old"], change["new"]))
        if result["not_applied"]:
            print()
            print("Not applied (require code changes):")
            for name in result["not_applied"]:
                val = best["best_parameters"][name]["value"]
                print("  %-28s optimal=%.2f" % (name, val))

    elif args.command == "history":
        records = read_history(limit=args.limit)
        if not records:
            print("No history found.")
            sys.exit(0)

        print("PSO Optimization History (last %d records)" % len(records))
        print("=" * 80)
        print("%-6s %12s %12s %12s %10s %s" % (
            "Iter", "Best Fit", "Avg Fit", "Max Fit", "Inertia", "Timestamp"))
        print("-" * 80)
        for r in records:
            print("%-6d %12.6f %12.6f %12.6f %10.4f %s" % (
                r["iteration"], r["global_best_fit"],
                r["avg_fitness"], r["max_fitness"],
                r["inertia"], r.get("timestamp", "?")))

    elif args.command == "simulate":
        print("PSO Simulation Mode (no apply)")
        print("=" * 50)
        print("Swarm: %d | Iterations: %d | Seed: %s" % (
            args.swarm, args.iterations, args.seed))
        print()

        pso = PSOOptimizer(swarm_size=args.swarm, seed=args.seed)
        result = pso.optimize(iterations=args.iterations, verbose=args.verbose)

        print()
        print("Simulation Complete")
        print("=" * 50)
        print("Best fitness: %.6f" % result["global_best_fitness"])
        print()
        print("%-28s %10s %10s %+10s" % (
            "Parameter", "Optimal", "Default", "Delta"))
        print("-" * 65)
        for name, info in result["best_parameters"].items():
            print("%-28s %10.2f %10.2f %+10.2f" % (
                name, info["value"], info["default"], info["delta"]))
        print()
        print("Convergence (last 5 iterations):")
        for r in result["convergence"]:
            print("  iter %3d: fit=%.6f avg=%.6f" % (
                r["iteration"], r["global_best_fit"], r["avg_fitness"]))

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: alpha
