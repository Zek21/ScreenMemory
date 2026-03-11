"""
skynet_collective.py -- Collective Intelligence Engine for cross-worker evolution.

Enables strategy federation, bottleneck sharing, swarm evolution, and
collective health metrics across all Skynet workers.
"""
import sys
import time
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests

BUS_URL = "http://localhost:8420/bus/publish"
BUS_POLL = "http://localhost:8420/bus/messages"
STATUS_URL = "http://localhost:8420/status"


def _bus_post(sender, topic, msg_type, content):
    """Publish a message to the Skynet bus."""
    from tools.shared.bus import bus_post_fields
    if not isinstance(content, str):
        content = json.dumps(content)
    if not bus_post_fields(sender, topic, msg_type, content):
        print(f"[collective] bus post failed")


def _bus_poll(topic=None, limit=50):
    """Poll bus messages, optionally filtered by topic."""
    try:
        params = {"limit": limit}
        if topic:
            params["topic"] = topic
        r = requests.get(BUS_POLL, params=params, timeout=5)
        return r.json() if r.ok else []
    except Exception:
        return []


_evolution_system = None
def _get_evolution_system():
    """Instantiate SelfEvolutionSystem (cached singleton)."""
    global _evolution_system
    if _evolution_system is None:
        from core.self_evolution import SelfEvolutionSystem
        _evolution_system = SelfEvolutionSystem()
    return _evolution_system


_learning_store = None
def _get_learning_store():
    """Instantiate LearningStore (cached singleton)."""
    global _learning_store
    if _learning_store is None:
        from core.learning_store import LearningStore
        _learning_store = LearningStore()
    return _learning_store


# ── STRATEGY FEDERATION ──────────────────────────────────────────────

def _collect_high_fitness_strategies(worker_name: str) -> list:
    """Collect local strategies with fitness > 0.7 across all categories."""
    engine = _get_evolution_system().engine
    categories = ["code", "research", "deploy", "navigate", "general"]
    broadcast = []
    for cat in categories:
        try:
            population = engine._get_population(cat)
        except Exception:
            continue
        for gene in population:
            if gene.fitness_score > 0.7:
                broadcast.append({
                    "strategy_id": gene.strategy_id, "name": gene.name,
                    "category": gene.category, "parameters": gene.parameters,
                    "fitness_score": gene.fitness_score, "generation": gene.generation,
                    "task_count": gene.task_count, "origin": worker_name,
                })
    return broadcast


def _absorb_peer_strategies(worker_name: str) -> list:
    """Poll bus for strategies from other workers."""
    remote = []
    for msg in _bus_poll(topic="collective"):
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "strategy" and msg.get("sender") != worker_name:
            try:
                strats = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if isinstance(strats, list):
                    remote.extend(strats)
            except (json.JSONDecodeError, KeyError):
                continue
    return remote


def sync_strategies(worker_name: str) -> dict:
    """Broadcast top-performing strategies and absorb better ones from peers."""
    broadcast = _collect_high_fitness_strategies(worker_name)
    if broadcast:
        _bus_post(worker_name, "collective", "strategy", json.dumps(broadcast))

    remote_strategies = _absorb_peer_strategies(worker_name)
    merged = merge_population(worker_name, remote_strategies) if remote_strategies else 0

    result = {"broadcast": len(broadcast), "received_remote": len(remote_strategies), "merged": merged}
    print(f"[collective] sync: broadcast={len(broadcast)}, remote={len(remote_strategies)}, merged={merged}")
    return result


def merge_population(worker_name: str, remote_strategies: list) -> int:
    """Tournament selection: keep the better of local vs remote for each category slot."""
    system = _get_evolution_system()
    engine = system.engine
    merged = 0

    for remote in remote_strategies:
        if not isinstance(remote, dict) or "category" not in remote:
            continue
        cat = remote["category"]
        remote_fitness = remote.get("fitness_score", 0)

        try:
            population = engine._get_population(cat)
        except Exception:
            continue

        if not population:
            continue

        # Find weakest local strategy in this category
        weakest = min(population, key=lambda g: g.fitness_score)

        # Tournament: replace weakest if remote is better
        if remote_fitness > weakest.fitness_score:
            try:
                from core.self_evolution import StrategyGene
                new_gene = StrategyGene(
                    strategy_id=f"imported_{remote.get('strategy_id', 'unknown')}_{int(time.time())}",
                    name=f"[{remote.get('origin', '?')}] {remote.get('name', 'imported')}",
                    category=cat,
                    parameters=remote.get("parameters", {}),
                    fitness_score=remote_fitness * 0.9,  # slight penalty for untested import
                    generation=remote.get("generation", 0),
                    parent_id=remote.get("strategy_id"),
                    created_at=time.time(),
                    task_count=0,
                )
                engine._save_strategy(new_gene)
                merged += 1
            except Exception:
                pass

    return merged


# ── BOTTLENECK SHARING ────────────────────────────────────────────────

def share_bottlenecks(worker_name: str) -> list:
    """Identify local bottlenecks and broadcast to collective."""
    system = _get_evolution_system()
    bottlenecks = []

    try:
        bottlenecks = system.reflector.identify_bottlenecks()
    except Exception as e:
        print(f"[collective] bottleneck identification failed: {e}")
        return []

    if bottlenecks:
        _bus_post(worker_name, "collective", "bottleneck", json.dumps({
            "worker": worker_name,
            "bottlenecks": bottlenecks,
            "timestamp": time.time(),
        }))
        print(f"[collective] shared {len(bottlenecks)} bottlenecks")

    return bottlenecks


def absorb_bottlenecks(worker_name: str) -> dict:
    """Poll bus for peer bottlenecks and auto-evolve weak categories."""
    messages = _bus_poll(topic="collective")
    weak_categories = set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "bottleneck" and msg.get("sender") != worker_name:
            try:
                data = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                for b in data.get("bottlenecks", []):
                    cat = b.get("category")
                    if cat and b.get("severity") in ("high", "critical"):
                        weak_categories.add(cat)
            except (json.JSONDecodeError, KeyError):
                continue

    evolved = []
    if weak_categories:
        system = _get_evolution_system()
        for cat in weak_categories:
            try:
                system.engine.evolve_generation(cat)
                evolved.append(cat)
            except Exception:
                pass

    result = {"weak_categories_found": list(weak_categories), "evolved": evolved}
    print(f"[collective] absorbed bottlenecks: weak={list(weak_categories)}, evolved={evolved}")
    return result


# ── COLLECTIVE METRICS ────────────────────────────────────────────────

def _fetch_evolution_stats() -> tuple:
    """Return (avg_fitness, strategy_count) from local evolution system."""
    try:
        s = _get_evolution_system().get_status()
        return s.get("average_fitness", 0.0), s.get("total_strategies", 0)
    except Exception:
        return 0.0, 0


def _fetch_knowledge_count() -> int:
    """Return total facts from LearningStore."""
    try:
        return _get_learning_store().stats().get("total_facts", 0)
    except Exception:
        return 0


def _count_bus_strategy_diversity(local_count: int) -> int:
    """Count unique strategy names from bus messages plus local count."""
    all_strategies = set()
    for msg in _bus_poll(topic="collective"):
        if isinstance(msg, dict) and msg.get("type") == "strategy":
            try:
                strats = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if isinstance(strats, list):
                    for s in strats:
                        all_strategies.add(s.get("name", ""))
            except (json.JSONDecodeError, KeyError):
                pass
    return len(all_strategies) + local_count


def collective_health() -> dict:
    """Aggregate health metrics across all workers."""
    try:
        r = requests.get(STATUS_URL, timeout=5)
        status = r.json() if r.ok else {}
    except Exception:
        status = {}

    workers = status.get("agents", status.get("workers", {}))
    worker_count = len(workers)
    total_tasks = sum(w.get("tasks_completed", 0) for w in workers.values())

    avg_fitness, strategy_count = _fetch_evolution_stats()

    convene_msgs = [m for m in _bus_poll(topic="convene") if isinstance(m, dict)]

    return {
        "worker_count": worker_count,
        "total_tasks": total_tasks,
        "avg_fitness": round(avg_fitness, 3),
        "knowledge_count": _fetch_knowledge_count(),
        "strategy_diversity": _count_bus_strategy_diversity(strategy_count),
        "convene_initiated": sum(1 for m in convene_msgs if m.get("type") == "request"),
        "convene_resolved": sum(1 for m in convene_msgs if m.get("type") == "resolved"),
        "timestamp": time.time(),
    }


def _query_engines_metrics() -> tuple:
    """Query GOD Console for engine counts. Returns (online, total)."""
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://localhost:8421/engines", timeout=3) as r:
            summary = _json.loads(r.read()).get("summary", {})
            return summary.get("online", 0), max(summary.get("total", 1), 1)
    except Exception:
        return 0, 1


def _query_uptime_seconds() -> float:
    """Query Skynet backend uptime in seconds."""
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://localhost:8420/health", timeout=2) as r:
            return _json.loads(r.read()).get("uptime_s", 0)
    except Exception:
        return 0


def intelligence_score() -> dict:
    """Composite intelligence score aligned with compute_iq() formula.

    Weights (matching skynet_self.py compute_iq):
      - workers_alive:    25%
      - engines_online:   25%
      - bus_healthy:      10%
      - knowledge_facts:  15%
      - uptime:           10%
      - capability_ratio: 15%
    """
    health = collective_health()

    worker_count = max(health.get("worker_count", 0), 0)
    workers_score = min(worker_count / 5, 1.0) * 0.25

    engines_online, engines_total = _query_engines_metrics()
    engines_score_val = (engines_online / engines_total) * 0.25

    bus_score = 0.10
    knowledge_component = min(health.get("knowledge_count", 0) / 500, 1.0) * 0.15
    uptime_score = min(_query_uptime_seconds() / 86400, 1.0) * 0.10
    cap_score = (engines_online / max(engines_total, 1)) * 0.15

    score = workers_score + engines_score_val + bus_score + knowledge_component + uptime_score + cap_score

    return {
        "intelligence_score": round(score, 3),
        "components": {
            "workers": round(workers_score, 3),
            "engines": round(engines_score_val, 3),
            "bus": round(bus_score, 3),
            "knowledge": round(knowledge_component, 3),
            "uptime": round(uptime_score, 3),
            "capability": round(cap_score, 3),
        },
        "raw": health,
    }


# ── SWARM COMMANDS ────────────────────────────────────────────────────

def _run_swarm_generation(system, category: str, gen: int, worker_name: str) -> dict:
    """Execute one swarm generation: evolve locally, share best, absorb peers."""
    try:
        system.engine.evolve_generation(category)
    except Exception as e:
        return {"generation": gen, "error": str(e)}

    # Share best strategy
    try:
        best = system.engine.get_optimal_strategy(category)
        if best and best.fitness_score > 0.5:
            _bus_post(worker_name, "collective", "strategy", json.dumps([{
                "strategy_id": best.strategy_id, "name": best.name,
                "category": category, "parameters": best.parameters,
                "fitness_score": best.fitness_score, "generation": best.generation,
                "task_count": best.task_count, "origin": worker_name,
            }]))
    except Exception:
        pass

    # Absorb from peers (filtered to this category)
    remote = [s for s in _absorb_peer_strategies(worker_name) if s.get("category") == category]
    merged = merge_population(worker_name, remote) if remote else 0

    try:
        pop = system.engine._get_population(category)
        best_fitness = max((g.fitness_score for g in pop), default=0)
    except Exception:
        best_fitness = 0

    print(f"[swarm] gen {gen}: best_fitness={best_fitness:.3f}, merged={merged}")
    return {"generation": gen, "best_fitness": round(best_fitness, 3), "remote_absorbed": merged}


def swarm_evolve(category: str, generations: int = 5, worker_name: str = "orchestrator") -> dict:
    """Coordinate all workers to evolve strategies for one category over N generations."""
    system = _get_evolution_system()
    results = [_run_swarm_generation(system, category, gen, worker_name) for gen in range(generations)]
    return {"category": category, "generations": results}


def _cast_self_vote(fact_content: str) -> str:
    """Cast own validation vote using LearningStore recall."""
    try:
        related = _get_learning_store().recall(fact_content, top_k=3)
        if related:
            avg_conf = sum(f.confidence for f in related) / len(related)
            return "agree" if avg_conf > 0.5 else "disagree"
    except Exception:
        pass
    return "abstain"


def _tally_peer_votes(vote_id: str, own_vote: str) -> dict:
    """Poll bus for validation responses and tally votes including own."""
    time.sleep(2)
    votes = {"agree": 0, "disagree": 0, "abstain": 0}
    if own_vote in votes:
        votes[own_vote] += 1

    for msg in _bus_poll(topic="collective"):
        if isinstance(msg, dict) and msg.get("type") == "validate_response":
            try:
                data = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if data.get("vote_id") == vote_id:
                    v = data.get("vote", "abstain")
                    if v in votes:
                        votes[v] += 1
            except (json.JSONDecodeError, KeyError):
                continue
    return votes


def swarm_validate(fact_content: str, worker_name: str = "orchestrator") -> dict:
    """Broadcast a fact to all workers for validation, collect votes, return consensus."""
    vote_id = f"vote_{int(time.time())}_{hash(fact_content) % 10000}"

    _bus_post(worker_name, "collective", "validate_request", json.dumps({
        "vote_id": vote_id, "fact": fact_content, "requester": worker_name,
    }))

    own_vote = _cast_self_vote(fact_content)
    votes = _tally_peer_votes(vote_id, own_vote)
    total = sum(votes.values())
    consensus = max(votes, key=votes.get) if total > 0 else "unknown"

    result = {
        "vote_id": vote_id, "fact": fact_content,
        "votes": votes, "consensus": consensus, "total_votes": total,
    }
    print(f"[swarm] validate: consensus={consensus}, votes={votes}")
    return result


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet Collective Intelligence Engine")
    parser.add_argument("--sync", metavar="WORKER", help="Sync strategies for a worker")
    parser.add_argument("--health", action="store_true", help="Show collective health metrics")
    parser.add_argument("--score", action="store_true", help="Show composite intelligence score")
    parser.add_argument("--bottlenecks", metavar="WORKER", help="Share bottlenecks for a worker")
    parser.add_argument("--absorb", metavar="WORKER", help="Absorb peer bottlenecks")
    parser.add_argument("--swarm-evolve", metavar="CATEGORY", help="Swarm evolve a category")
    parser.add_argument("--generations", type=int, default=5, help="Generations for swarm-evolve")
    parser.add_argument("--swarm-validate", metavar="FACT", help="Swarm validate a fact")
    parser.add_argument("--worker", default="orchestrator", help="Worker name for swarm commands")
    args = parser.parse_args()

    if args.sync:
        result = sync_strategies(args.sync)
        print(json.dumps(result, indent=2))
    elif args.health:
        result = collective_health()
        print(json.dumps(result, indent=2))
    elif args.score:
        result = intelligence_score()
        print(json.dumps(result, indent=2))
    elif args.bottlenecks:
        result = share_bottlenecks(args.bottlenecks)
        print(json.dumps(result, indent=2, default=str))
    elif args.absorb:
        result = absorb_bottlenecks(args.absorb)
        print(json.dumps(result, indent=2))
    elif args.swarm_evolve:
        result = swarm_evolve(args.swarm_evolve, args.generations, args.worker)
        print(json.dumps(result, indent=2))
    elif args.swarm_validate:
        result = swarm_validate(args.swarm_validate, args.worker)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
