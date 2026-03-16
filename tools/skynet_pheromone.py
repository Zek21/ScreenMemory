"""Ant Colony Optimization pheromone scoring for Skynet task routing.
# signed: beta

Bio-inspired routing layer that complements skynet_specialization.py.
Workers deposit pheromone on task-category trails proportional to task quality.
Pheromone evaporates over time so stale expertise fades and fresh performance
dominates routing decisions.

Pheromone Model:
    trail[category][worker] = strength   (float >= 0.0)

    deposit:    strength += quality * deposit_rate
    evaporate:  strength *= (1 - decay_rate)   per cycle

    get_best_worker returns the worker with the strongest pheromone for a
    category, optionally blended with specialization composite scores.

Storage: data/pheromone_trails.json

Usage:
    python tools/skynet_pheromone.py deposit <worker> <category> <quality>
    python tools/skynet_pheromone.py evaporate [--decay <float>]
    python tools/skynet_pheromone.py best <category>
    python tools/skynet_pheromone.py status
    python tools/skynet_pheromone.py reset
"""
# signed: beta

import json
import os
import sys
import time
import argparse
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAILS_PATH = REPO_ROOT / "data" / "pheromone_trails.json"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# Default deposit multiplier — quality * DEPOSIT_RATE is added to trail
DEPOSIT_RATE = 1.0  # signed: beta

# Default evaporation decay rate per cycle (5% reduction)
DEFAULT_DECAY_RATE = 0.05  # signed: beta

# Minimum pheromone — prevents a trail from decaying to zero (exploration floor)
MIN_PHEROMONE = 0.01  # signed: beta

# Maximum pheromone cap — prevents runaway accumulation
MAX_PHEROMONE = 10.0  # signed: beta

# Weight for pheromone vs specialization blend in get_best_worker
PHEROMONE_WEIGHT = 0.5  # signed: beta
SPECIALIZATION_WEIGHT = 0.5  # signed: beta

_lock = threading.Lock()


class PheromoneTrail:
    """Manages task_category -> worker -> pheromone_strength mappings.

    Thread-safe via internal lock.  Persists to data/pheromone_trails.json.
    # signed: beta
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = path or TRAILS_PATH
        self._trails: Dict[str, Dict[str, float]] = {}
        self._metadata: Dict[str, Any] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        """Load trails from disk.  Creates empty state if missing."""
        if self._path.exists() and self._path.stat().st_size > 0:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._trails = raw.get("trails", {})
                self._metadata = raw.get("metadata", {})
            except (json.JSONDecodeError, KeyError):
                self._trails = {}
                self._metadata = {}
        # signed: beta

    def _save(self) -> None:
        """Atomically persist current state to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._metadata["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._metadata["updated_by"] = "skynet_pheromone"

        payload = {
            "trails": self._trails,
            "metadata": self._metadata,
        }
        tmp = str(self._path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(self._path))
        # signed: beta

    # ── core operations ──────────────────────────────────────────

    def deposit_pheromone(
        self,
        worker: str,
        category: str,
        quality: float,
        deposit_rate: float = DEPOSIT_RATE,
    ) -> float:
        """Deposit pheromone on a trail proportional to task quality.

        Args:
            worker:       Worker name (alpha/beta/gamma/delta).
            category:     Task category string.
            quality:      Quality score of the completed task (0.0–1.0 typical,
                          but values > 1.0 are allowed for exceptional work).
            deposit_rate: Multiplier for the deposit (default DEPOSIT_RATE).

        Returns:
            New pheromone strength for this worker+category.

        Raises:
            ValueError: If worker name is invalid or quality is negative.
        """
        worker = worker.lower().strip()
        if worker not in WORKER_NAMES:
            raise ValueError(f"Unknown worker '{worker}'. Valid: {WORKER_NAMES}")
        if quality < 0:
            raise ValueError(f"Quality must be >= 0, got {quality}")

        amount = quality * deposit_rate

        with _lock:
            if category not in self._trails:
                self._trails[category] = {}
            current = self._trails[category].get(worker, MIN_PHEROMONE)
            new_strength = min(current + amount, MAX_PHEROMONE)
            self._trails[category][worker] = round(new_strength, 6)

            # Track deposit count in metadata
            deposit_key = "total_deposits"
            self._metadata[deposit_key] = self._metadata.get(deposit_key, 0) + 1

            self._save()
            return self._trails[category][worker]
        # signed: beta

    def evaporate(self, decay_rate: float = DEFAULT_DECAY_RATE) -> Dict[str, int]:
        """Reduce all pheromone levels by decay_rate, simulating evaporation.

        Args:
            decay_rate: Fraction to remove per cycle (0.05 = 5% reduction).
                        Must be in (0.0, 1.0).

        Returns:
            Dict mapping category -> count of trails that were evaporated.

        Raises:
            ValueError: If decay_rate is not in (0.0, 1.0).
        """
        if not (0.0 < decay_rate < 1.0):
            raise ValueError(f"decay_rate must be in (0.0, 1.0), got {decay_rate}")

        retention = 1.0 - decay_rate
        summary: Dict[str, int] = {}

        with _lock:
            for category, workers in self._trails.items():
                count = 0
                for worker in list(workers.keys()):
                    old = workers[worker]
                    new_val = old * retention
                    # Enforce minimum floor — never fully zero
                    if new_val < MIN_PHEROMONE:
                        new_val = MIN_PHEROMONE
                    workers[worker] = round(new_val, 6)
                    count += 1
                if count:
                    summary[category] = count

            self._metadata["last_evaporation"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            evap_key = "total_evaporations"
            self._metadata[evap_key] = self._metadata.get(evap_key, 0) + 1

            self._save()

        return summary
        # signed: beta

    def get_best_worker(
        self,
        category: str,
        blend_specialization: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Return the worker with the strongest pheromone for a category.

        When blend_specialization=True, the final score is a weighted blend of
        pheromone strength and specialization composite score from
        skynet_specialization.recommend_worker().

        Args:
            category:              Task category to query.
            blend_specialization:  Whether to blend with specialization data.

        Returns:
            Dict with {worker, pheromone, specialization_score, blended_score,
            rank} for the best worker, or None if no data exists.
        """
        with _lock:
            cat_trails = self._trails.get(category, {})

        # Build pheromone scores for all workers
        pheromone_scores: Dict[str, float] = {}
        for w in WORKER_NAMES:
            pheromone_scores[w] = cat_trails.get(w, 0.0)

        # Normalize pheromone to 0–1 range for blending
        max_pher = max(pheromone_scores.values()) if pheromone_scores else 0.0
        norm_pher: Dict[str, float] = {}
        for w in WORKER_NAMES:
            norm_pher[w] = (pheromone_scores[w] / max_pher) if max_pher > 0 else 0.0

        # Get specialization scores if blending
        spec_scores: Dict[str, float] = {}
        if blend_specialization:
            try:
                from tools.skynet_specialization import recommend_worker
                candidates = recommend_worker(category)
                for c in candidates:
                    spec_scores[c["worker"]] = c.get("composite_score", 0.0)
            except Exception:
                # Specialization not available — pheromone only
                for w in WORKER_NAMES:
                    spec_scores[w] = 0.0
        else:
            for w in WORKER_NAMES:
                spec_scores[w] = 0.0

        # Normalize specialization to 0–1
        max_spec = max(spec_scores.values()) if spec_scores else 0.0
        norm_spec: Dict[str, float] = {}
        for w in WORKER_NAMES:
            norm_spec[w] = (spec_scores[w] / max_spec) if max_spec > 0 else 0.0

        # Blend scores
        blended: List[Dict[str, Any]] = []
        for w in WORKER_NAMES:
            score = (PHEROMONE_WEIGHT * norm_pher[w]
                     + SPECIALIZATION_WEIGHT * norm_spec[w])
            blended.append({
                "worker": w,
                "pheromone": round(pheromone_scores[w], 6),
                "pheromone_normalized": round(norm_pher[w], 4),
                "specialization_score": round(spec_scores.get(w, 0.0), 4),
                "blended_score": round(score, 4),
            })

        blended.sort(key=lambda x: x["blended_score"], reverse=True)
        for i, b in enumerate(blended):
            b["rank"] = i + 1

        return blended[0] if blended else None
        # signed: beta

    def get_all_rankings(self, category: str) -> List[Dict[str, Any]]:
        """Return all workers ranked for a category (not just the best)."""
        # Reuse get_best_worker logic but return the full list
        with _lock:
            cat_trails = self._trails.get(category, {})

        pheromone_scores = {w: cat_trails.get(w, 0.0) for w in WORKER_NAMES}
        max_pher = max(pheromone_scores.values()) if pheromone_scores else 0.0

        rankings = []
        for w in WORKER_NAMES:
            norm = (pheromone_scores[w] / max_pher) if max_pher > 0 else 0.0
            rankings.append({
                "worker": w,
                "pheromone": round(pheromone_scores[w], 6),
                "pheromone_normalized": round(norm, 4),
            })
        rankings.sort(key=lambda x: x["pheromone"], reverse=True)
        for i, r in enumerate(rankings):
            r["rank"] = i + 1
        return rankings
        # signed: beta

    def get_status(self) -> Dict[str, Any]:
        """Return full pheromone state summary."""
        with _lock:
            total_trails = sum(
                len(workers) for workers in self._trails.values()
            )
            categories = list(self._trails.keys())

            # Per-worker total pheromone
            worker_totals: Dict[str, float] = {w: 0.0 for w in WORKER_NAMES}
            for workers in self._trails.values():
                for w, strength in workers.items():
                    if w in worker_totals:
                        worker_totals[w] += strength

            return {
                "categories_tracked": len(categories),
                "total_trail_entries": total_trails,
                "categories": categories,
                "worker_totals": {
                    w: round(t, 4) for w, t in worker_totals.items()
                },
                "metadata": dict(self._metadata),
            }
        # signed: beta

    def reset(self) -> None:
        """Clear all pheromone data (fresh start)."""
        with _lock:
            self._trails = {}
            self._metadata = {"reset_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self._save()
        # signed: beta


# ── Module-level convenience functions ────────────────────────────

_instance: Optional[PheromoneTrail] = None
_instance_lock = threading.Lock()


def _get_trail() -> PheromoneTrail:
    """Singleton accessor for the PheromoneTrail instance."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = PheromoneTrail()
    return _instance
    # signed: beta


def deposit_pheromone(
    worker: str, category: str, quality: float,
    deposit_rate: float = DEPOSIT_RATE,
) -> float:
    """Deposit pheromone — module-level convenience wrapper."""
    return _get_trail().deposit_pheromone(worker, category, quality, deposit_rate)


def evaporate(decay_rate: float = DEFAULT_DECAY_RATE) -> Dict[str, int]:
    """Evaporate all trails — module-level convenience wrapper."""
    return _get_trail().evaporate(decay_rate)


def get_best_worker(
    category: str, blend_specialization: bool = True,
) -> Optional[Dict[str, Any]]:
    """Get best worker for category — module-level convenience wrapper."""
    return _get_trail().get_best_worker(category, blend_specialization)


# ── CLI ───────────────────────────────────────────────────────────

def _cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Skynet ACO pheromone scoring for task routing"
    )
    sub = parser.add_subparsers(dest="command")

    # deposit
    dep = sub.add_parser("deposit", help="Deposit pheromone on a trail")
    dep.add_argument("worker", help="Worker name")
    dep.add_argument("category", help="Task category")
    dep.add_argument("quality", type=float, help="Quality score (0.0-1.0+)")

    # evaporate
    evap = sub.add_parser("evaporate", help="Apply evaporation to all trails")
    evap.add_argument(
        "--decay", type=float, default=DEFAULT_DECAY_RATE,
        help=f"Decay rate (default {DEFAULT_DECAY_RATE})",
    )

    # best
    best = sub.add_parser("best", help="Get best worker for a category")
    best.add_argument("category", help="Task category")
    best.add_argument(
        "--no-blend", action="store_true",
        help="Disable specialization blending",
    )

    # status
    sub.add_parser("status", help="Show pheromone state summary")

    # reset
    sub.add_parser("reset", help="Clear all pheromone data")

    args = parser.parse_args()
    trail = PheromoneTrail()

    if args.command == "deposit":
        strength = trail.deposit_pheromone(args.worker, args.category, args.quality)
        print(f"Deposited: {args.worker}/{args.category} quality={args.quality} -> strength={strength}")

    elif args.command == "evaporate":
        summary = trail.evaporate(args.decay)
        if not summary:
            print("No trails to evaporate.")
        else:
            total = sum(summary.values())
            print(f"Evaporated {total} trail entries across {len(summary)} categories (decay={args.decay})")

    elif args.command == "best":
        result = trail.get_best_worker(args.category, blend_specialization=not args.no_blend)
        if result:
            print(
                f"Best for '{args.category}': {result['worker']} "
                f"(pheromone={result['pheromone']:.4f}, "
                f"spec={result['specialization_score']:.4f}, "
                f"blended={result['blended_score']:.4f})"
            )
            # Show full ranking
            rankings = trail.get_all_rankings(args.category)
            for r in rankings:
                marker = " <-- BEST" if r["rank"] == 1 and r["pheromone"] > 0 else ""
                print(f"  #{r['rank']} {r['worker']}: pheromone={r['pheromone']:.4f}{marker}")
        else:
            print(f"No pheromone data for category '{args.category}'")

    elif args.command == "status":
        status = trail.get_status()
        print(f"Categories tracked: {status['categories_tracked']}")
        print(f"Total trail entries: {status['total_trail_entries']}")
        if status["categories"]:
            print(f"Categories: {', '.join(status['categories'])}")
        print("Worker totals:")
        for w, total in status["worker_totals"].items():
            print(f"  {w}: {total:.4f}")
        meta = status["metadata"]
        if meta.get("total_deposits"):
            print(f"Total deposits: {meta['total_deposits']}")
        if meta.get("total_evaporations"):
            print(f"Total evaporations: {meta['total_evaporations']}")

    elif args.command == "reset":
        trail.reset()
        print("Pheromone trails reset.")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: beta
