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

def sync_strategies(worker_name: str) -> dict:
    """Broadcast top-performing strategies and absorb better ones from peers.

    1. Read local SelfEvolutionSystem strategies
    2. Broadcast those with fitness > 0.7 to bus topic='collective'
    3. Poll bus for strategies from other workers
    4. Merge any that outperform local equivalents
    """
    system = _get_evolution_system()
    engine = system.engine

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
                    "strategy_id": gene.strategy_id,
                    "name": gene.name,
                    "category": gene.category,
                    "parameters": gene.parameters,
                    "fitness_score": gene.fitness_score,
                    "generation": gene.generation,
                    "task_count": gene.task_count,
                    "origin": worker_name,
                })

    if broadcast:
        _bus_post(worker_name, "collective", "strategy", json.dumps(broadcast))

    # Absorb remote strategies
    messages = _bus_poll(topic="collective")
    remote_strategies = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "strategy" and msg.get("sender") != worker_name:
            try:
                strats = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if isinstance(strats, list):
                    remote_strategies.extend(strats)
            except (json.JSONDecodeError, KeyError):
                continue

    merged = 0
    if remote_strategies:
        merged = merge_population(worker_name, remote_strategies)

    result = {
        "broadcast": len(broadcast),
        "received_remote": len(remote_strategies),
        "merged": merged,
    }
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

def collective_health() -> dict:
    """Aggregate health metrics across all workers."""
    # Query Skynet status
    try:
        r = requests.get(STATUS_URL, timeout=5)
        status = r.json() if r.ok else {}
    except Exception:
        status = {}

    workers = status.get("agents", status.get("workers", {}))
    total_tasks = 0
    worker_count = len(workers)

    for w in workers.values():
        total_tasks += w.get("tasks_completed", 0)

    # Local evolution fitness
    avg_fitness = 0.0
    strategy_count = 0
    try:
        system = _get_evolution_system()
        s = system.get_status()
        avg_fitness = s.get("average_fitness", 0.0)
        strategy_count = s.get("total_strategies", 0)
    except Exception:
        pass

    # Knowledge count
    knowledge_count = 0
    try:
        store = _get_learning_store()
        stats = store.stats()
        knowledge_count = stats.get("total_facts", 0)
    except Exception:
        pass

    # Strategy diversity from bus
    all_strategies = set()
    messages = _bus_poll(topic="collective")
    for msg in messages:
        if isinstance(msg, dict) and msg.get("type") == "strategy":
            try:
                strats = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if isinstance(strats, list):
                    for s in strats:
                        all_strategies.add(s.get("name", ""))
            except (json.JSONDecodeError, KeyError):
                pass
    strategy_diversity = len(all_strategies) + strategy_count

    # Convene sessions
    convene_msgs = [m for m in _bus_poll(topic="convene") if isinstance(m, dict)]
    convene_initiated = sum(1 for m in convene_msgs if m.get("type") == "request")
    convene_resolved = sum(1 for m in convene_msgs if m.get("type") == "resolved")

    result = {
        "worker_count": worker_count,
        "total_tasks": total_tasks,
        "avg_fitness": round(avg_fitness, 3),
        "knowledge_count": knowledge_count,
        "strategy_diversity": strategy_diversity,
        "convene_initiated": convene_initiated,
        "convene_resolved": convene_resolved,
        "timestamp": time.time(),
    }
    return result


def intelligence_score() -> dict:
    """Composite intelligence score aligned with compute_iq() formula.

    Weights (matching skynet_self.py compute_iq):
      - workers_alive:    25% -- alive workers / total
      - engines_online:   25% -- online engines / total (via GOD Console)
      - bus_healthy:      10% -- bus reachable = 1.0
      - knowledge_facts:  15% -- min(facts / 500, 1.0)
      - uptime:           10% -- min(uptime_h / 24, 1.0)
      - capability_ratio: 15% -- engines importable / total
    """
    health = collective_health()

    # Workers alive (25%)
    worker_count = max(health.get("worker_count", 0), 0)
    expected_workers = 5  # alpha, beta, gamma, delta, orchestrator
    workers_score = min(worker_count / expected_workers, 1.0) * 0.25

    # Engines online (25%) -- query GOD Console
    engines_score_val = 0.0
    engines_total = 0
    engines_online = 0
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://localhost:8421/engines", timeout=3) as r:
            edata = _json.loads(r.read())
            summary = edata.get("summary", {})
            engines_online = summary.get("online", 0)
            engines_total = max(summary.get("total", 1), 1)
            engines_score_val = (engines_online / engines_total) * 0.25
    except Exception:
        pass

    # Bus healthy (10%)
    bus_score = 0.10  # if we got health data, bus is up

    # Knowledge facts (15%)
    knowledge_norm = min(health.get("knowledge_count", 0) / 500, 1.0)
    knowledge_component = knowledge_norm * 0.15

    # Uptime (10%)
    uptime_score = 0.0
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://localhost:8420/health", timeout=2) as r:
            hdata = _json.loads(r.read())
            uptime_s = hdata.get("uptime_s", 0)
            uptime_score = min(uptime_s / 86400, 1.0) * 0.10
    except Exception:
        pass

    # Capability ratio (15%)
    cap_score = (engines_online / max(engines_total, 1)) * 0.15

    score = workers_score + engines_score_val + bus_score + knowledge_component + uptime_score + cap_score

    result = {
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
    return result


# ── SWARM COMMANDS ────────────────────────────────────────────────────

def swarm_evolve(category: str, generations: int = 5, worker_name: str = "orchestrator") -> dict:
    """Coordinate all workers to evolve strategies for one category over N generations."""
    results = []

    for gen in range(generations):
        # Evolve locally
        try:
            system = _get_evolution_system()
            system.engine.evolve_generation(category)
        except Exception as e:
            results.append({"generation": gen, "error": str(e)})
            continue

        # Share best strategy
        try:
            best = system.engine.get_optimal_strategy(category)
            if best and best.fitness_score > 0.5:
                _bus_post(worker_name, "collective", "strategy", json.dumps([{
                    "strategy_id": best.strategy_id,
                    "name": best.name,
                    "category": category,
                    "parameters": best.parameters,
                    "fitness_score": best.fitness_score,
                    "generation": best.generation,
                    "task_count": best.task_count,
                    "origin": worker_name,
                }]))
        except Exception:
            pass

        # Absorb from peers
        messages = _bus_poll(topic="collective")
        remote = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("type") == "strategy" and msg.get("sender") != worker_name:
                try:
                    strats = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                    if isinstance(strats, list):
                        remote.extend([s for s in strats if s.get("category") == category])
                except (json.JSONDecodeError, KeyError):
                    pass

        merged = 0
        if remote:
            merged = merge_population(worker_name, remote)

        try:
            pop = system.engine._get_population(category)
            best_fitness = max((g.fitness_score for g in pop), default=0)
        except Exception:
            best_fitness = 0

        results.append({
            "generation": gen,
            "best_fitness": round(best_fitness, 3),
            "remote_absorbed": merged,
        })
        print(f"[swarm] gen {gen}: best_fitness={best_fitness:.3f}, merged={merged}")

    return {"category": category, "generations": results}


def swarm_validate(fact_content: str, worker_name: str = "orchestrator") -> dict:
    """Broadcast a fact to all workers for validation, collect votes, return consensus."""
    vote_id = f"vote_{int(time.time())}_{hash(fact_content) % 10000}"

    _bus_post(worker_name, "collective", "validate_request", json.dumps({
        "vote_id": vote_id,
        "fact": fact_content,
        "requester": worker_name,
    }))

    # Also cast own vote via LearningStore recall
    own_vote = "abstain"
    try:
        store = _get_learning_store()
        related = store.recall(fact_content, top_k=3)
        if related:
            avg_conf = sum(f.confidence for f in related) / len(related)
            own_vote = "agree" if avg_conf > 0.5 else "disagree"
    except Exception:
        pass

    # Poll for responses (with brief wait)
    time.sleep(2)
    messages = _bus_poll(topic="collective")
    votes = {"agree": 0, "disagree": 0, "abstain": 0}
    if own_vote in votes:
        votes[own_vote] += 1

    for msg in messages:
        if isinstance(msg, dict) and msg.get("type") == "validate_response":
            try:
                data = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if data.get("vote_id") == vote_id:
                    v = data.get("vote", "abstain")
                    if v in votes:
                        votes[v] += 1
            except (json.JSONDecodeError, KeyError):
                continue

    total = sum(votes.values())
    consensus = max(votes, key=votes.get) if total > 0 else "unknown"

    result = {
        "vote_id": vote_id,
        "fact": fact_content,
        "votes": votes,
        "consensus": consensus,
        "total_votes": total,
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
