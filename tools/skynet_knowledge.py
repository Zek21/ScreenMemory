#!/usr/bin/env python3
"""
skynet_knowledge.py -- Knowledge Sharing Protocol for Skynet workers.

Bus-based collective intelligence: workers broadcast learnings, absorb
knowledge from peers, share high-performing strategies, and validate facts
through consensus.

Usage:
    python tools/skynet_knowledge.py --absorb alpha
    python tools/skynet_knowledge.py --share alpha
    python tools/skynet_knowledge.py --broadcast "fact text" --category pattern
    python tools/skynet_knowledge.py --status
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUS_URL = "http://localhost:8420"
KNOWLEDGE_TOPIC = "knowledge"


# ─── Bus Helpers ───────────────────────────────────────

def _bus_post(message: dict) -> bool:
    """POST a message to the Skynet bus. Returns True on success."""
    from tools.shared.bus import bus_post
    return bus_post(message)


def _bus_get(topic: Optional[str] = None, limit: int = 100) -> List[dict]:
    """GET messages from the Skynet bus."""
    try:
        url = f"{BUS_URL}/bus/messages?limit={limit}"
        if topic:
            url += f"&topic={topic}"
        with urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            return data if isinstance(data, list) else []
    except Exception:
        return []


# ─── Knowledge Broadcasting ───────────────────────────

def broadcast_learning(sender: str, fact: str, category: str, tags: Optional[List[str]] = None) -> bool:
    """Post a learned fact to the bus for all workers to absorb.

    Args:
        sender: Worker name (e.g. "alpha").
        fact: The learned fact text.
        category: Category (e.g. "pattern", "bug", "optimization", "architecture").
        tags: Optional list of tags for filtering.

    Returns:
        True if published successfully.
    """
    content = json.dumps({
        "fact": fact,
        "category": category,
        "tags": tags or [],
        "learned_at": time.time(),
    })
    return _bus_post({
        "sender": sender,
        "topic": KNOWLEDGE_TOPIC,
        "type": "learning",
        "content": content,
    })


def broadcast_strategy(sender: str, category: str, strategy_params: dict, fitness_score: float) -> bool:
    """Share a high-performing strategy configuration via bus.

    Args:
        sender: Worker name.
        category: Strategy category (e.g. "code", "research", "deploy").
        strategy_params: The parameter dict of the strategy.
        fitness_score: The fitness score achieved.

    Returns:
        True if published successfully.
    """
    content = json.dumps({
        "category": category,
        "params": strategy_params,
        "fitness": fitness_score,
        "shared_at": time.time(),
    })
    return _bus_post({
        "sender": sender,
        "topic": KNOWLEDGE_TOPIC,
        "type": "strategy",
        "content": content,
    })


def poll_knowledge(since_timestamp: Optional[float] = None) -> List[dict]:
    """Retrieve knowledge messages from the bus.

    Args:
        since_timestamp: Only return messages newer than this Unix timestamp.

    Returns:
        List of knowledge messages, each with parsed content.
    """
    messages = _bus_get(topic=KNOWLEDGE_TOPIC)
    results = []
    for msg in messages:
        try:
            content = msg.get("content", "")
            parsed = json.loads(content) if isinstance(content, str) else content
            entry = {
                "sender": msg.get("sender", "unknown"),
                "type": msg.get("type", "unknown"),
                "id": msg.get("id", ""),
                "timestamp": msg.get("timestamp", ""),
                **parsed,
            }
            if since_timestamp:
                msg_time = parsed.get("learned_at") or parsed.get("shared_at") or 0
                if msg_time <= since_timestamp:
                    continue
            results.append(entry)
        except (json.JSONDecodeError, TypeError):
            continue
    return results


# ─── Learning Store Integration ────────────────────────

def _get_learning_store():
    """Lazy-load LearningStore."""
    from core.learning_store import LearningStore
    return LearningStore()


def _get_evolution_system():
    """Lazy-load SelfEvolutionSystem."""
    from core.self_evolution import SelfEvolutionSystem
    return SelfEvolutionSystem()


def absorb_learnings(worker_name: str) -> int:
    """Poll bus for knowledge, filter out own messages, store via LearningStore.

    Args:
        worker_name: This worker's name (to skip own messages).

    Returns:
        Number of new facts absorbed.
    """
    store = _get_learning_store()
    messages = poll_knowledge()
    absorbed = 0

    for msg in messages:
        if msg.get("sender") == worker_name:
            continue
        if msg.get("type") != "learning":
            continue

        fact = msg.get("fact", "")
        category = msg.get("category", "general")
        tags = msg.get("tags", [])
        source = f"bus:{msg.get('sender', 'unknown')}"

        if not fact:
            continue

        try:
            store.learn(
                content=fact,
                category=category,
                source=source,
                tags=tags,
            )
            absorbed += 1
        except Exception:
            continue

    return absorbed


def share_best_strategies(worker_name: str, top_n: int = 3) -> int:
    """Read SelfEvolutionSystem optimal configs and broadcast top strategies.

    Args:
        worker_name: This worker's name.
        top_n: Number of top strategies to share per category.

    Returns:
        Number of strategies shared.
    """
    evo = _get_evolution_system()
    shared = 0

    categories = ["code", "research", "deploy", "navigate", "general"]
    for cat in categories:
        try:
            config = evo.get_strategy_for_task(cat)
            if not config:
                continue
            fitness = config.get("fitness_score", 0.0)
            if fitness <= 0:
                continue
            if broadcast_strategy(worker_name, cat, config, fitness):
                shared += 1
        except Exception:
            continue

    return shared


def validate_fact(fact_id: str, validator_name: str, agrees: bool) -> bool:
    """Post a validation vote for a fact to the bus.

    If 3+ workers agree on a fact, it gets reinforced in LearningStore.

    Args:
        fact_id: The fact ID to validate.
        validator_name: Name of the validating worker.
        agrees: Whether this worker agrees the fact is correct.

    Returns:
        True if the vote was published.
    """
    content = json.dumps({
        "fact_id": fact_id,
        "agrees": agrees,
        "validated_at": time.time(),
    })
    published = _bus_post({
        "sender": validator_name,
        "topic": KNOWLEDGE_TOPIC,
        "type": "validation",
        "content": content,
    })

    if not published:
        return False

    # Check if consensus reached (3+ agreeing votes)
    messages = poll_knowledge()
    votes = {}
    for msg in messages:
        if msg.get("type") != "validation":
            continue
        fid = msg.get("fact_id")
        if fid != fact_id:
            continue
        voter = msg.get("sender", "")
        if msg.get("agrees"):
            votes[voter] = True

    if len(votes) >= 3:
        try:
            store = _get_learning_store()
            store.reinforce(fact_id)
        except Exception:
            pass

    return True


# ─── Collective Intelligence ──────────────────────────

def get_collective_expertise() -> Dict[str, Any]:
    """Query PersistentLearningSystem for all expertise domains.

    Returns:
        Merged view of all expertise domains with scores.
    """
    from core.learning_store import PersistentLearningSystem
    try:
        system = PersistentLearningSystem()
        return system.get_expertise_summary()
    except Exception as e:
        return {"error": str(e), "domains": []}


def suggest_strategy(category: str) -> Optional[Dict[str, Any]]:
    """Check bus for recent strategy broadcasts, return highest-fitness one.

    Args:
        category: Strategy category to search for.

    Returns:
        The highest-fitness strategy dict, or None if none found.
    """
    messages = poll_knowledge()
    best = None
    best_fitness = -1.0

    for msg in messages:
        if msg.get("type") != "strategy":
            continue
        if msg.get("category") != category:
            continue
        fitness = msg.get("fitness", 0.0)
        if fitness > best_fitness:
            best_fitness = fitness
            best = msg

    return best


# ─── Improvement Proposals ─────────────────────────────

PROPOSALS_FILE = ROOT / "data" / "proposals.json"


def _load_proposals() -> List[dict]:
    if PROPOSALS_FILE.exists():
        try:
            return json.loads(PROPOSALS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_proposals(proposals: List[dict]):
    PROPOSALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS_FILE.write_text(json.dumps(proposals, indent=2, default=str), encoding="utf-8")


def propose_improvement(sender: str, title: str, description: str,
                        target_files: Optional[List[str]] = None,
                        priority: str = "normal") -> bool:
    """Post an improvement proposal to the bus and persist locally.

    Workers call this after completing a task when they notice something
    that could be improved. Proposals are posted to topic=planning type=proposal
    so the orchestrator and other workers can see and act on them.

    Args:
        sender: Worker name (e.g. "gamma").
        title: Short proposal title.
        description: What should be improved and why.
        target_files: Optional list of files that would be changed.
        priority: "low", "normal", "high", or "critical".

    Returns:
        True if published to bus successfully.
    """
    proposal = {
        "id": f"prop_{int(time.time())}_{sender}",
        "sender": sender,
        "title": title,
        "description": description,
        "target_files": target_files or [],
        "priority": priority,
        "status": "proposed",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Persist locally
    proposals = _load_proposals()
    proposals.append(proposal)
    if len(proposals) > 200:
        proposals = proposals[-200:]
    _save_proposals(proposals)

    # Post to bus for orchestrator/workers
    ok = _bus_post({
        "sender": sender,
        "topic": "planning",
        "type": "proposal",
        "content": json.dumps(proposal),
    })

    return ok


def list_proposals(status: Optional[str] = None) -> List[dict]:
    """List all proposals, optionally filtered by status."""
    proposals = _load_proposals()
    if status:
        proposals = [p for p in proposals if p.get("status") == status]
    return proposals


# ─── Status ────────────────────────────────────────────

def get_status() -> Dict[str, Any]:
    """Get knowledge system status."""
    messages = poll_knowledge()
    learnings = [m for m in messages if m.get("type") == "learning"]
    strategies = [m for m in messages if m.get("type") == "strategy"]
    validations = [m for m in messages if m.get("type") == "validation"]

    senders = set(m.get("sender", "") for m in messages)
    categories = set(m.get("category", "") for m in learnings if m.get("category"))

    expertise = {}
    try:
        expertise = get_collective_expertise()
    except Exception:
        pass

    proposals = list_proposals()
    return {
        "total_knowledge_messages": len(messages),
        "learnings": len(learnings),
        "strategies": len(strategies),
        "validations": len(validations),
        "proposals": len(proposals),
        "proposals_pending": len([p for p in proposals if p.get("status") == "proposed"]),
        "contributing_workers": sorted(senders - {""}),
        "categories": sorted(categories),
        "expertise": expertise,
    }


# ─── Incident Memory ──────────────────────────────────

INCIDENTS_FILE = ROOT / "data" / "incidents.json"

def _load_incidents() -> List[dict]:
    """Load incidents from disk."""
    if INCIDENTS_FILE.exists():
        try:
            return json.loads(INCIDENTS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []

def _save_incidents(incidents: List[dict]):
    """Persist incidents to disk."""
    INCIDENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    INCIDENTS_FILE.write_text(json.dumps(incidents, indent=2, default=str), encoding="utf-8")

def learn_incident(incident_id: str, what_happened: str, root_cause: str,
                   fix_applied: str, rule_created: str) -> dict:
    """Store an incident in institutional memory. Returns the created entry."""
    entry = {
        "id": incident_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "what_happened": what_happened,
        "root_cause": root_cause,
        "fix_applied": fix_applied,
        "rule_created": rule_created,
    }
    incidents = _load_incidents()
    # Update existing or append
    for i, inc in enumerate(incidents):
        if inc.get("id") == incident_id:
            incidents[i] = entry
            break
    else:
        incidents.append(entry)
    _save_incidents(incidents)
    # Also broadcast to bus
    _bus_post({
        "sender": "knowledge",
        "topic": "knowledge",
        "type": "incident",
        "content": json.dumps(entry),
    })
    return entry

def get_incidents() -> List[dict]:
    """Return all recorded incidents."""
    return _load_incidents()


# ─── KNOWLEDGE GRAPH ────────────────────────────────────

KNOWLEDGE_GRAPH_FILE = ROOT / "data" / "knowledge_graph.json"

def _load_graph() -> dict:
    """Load persistent knowledge graph from disk."""
    if KNOWLEDGE_GRAPH_FILE.exists():
        try:
            return json.loads(KNOWLEDGE_GRAPH_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"facts": {}, "relationships": [], "incidents": []}

def _save_graph(graph: dict):
    KNOWLEDGE_GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_GRAPH_FILE.write_text(
        json.dumps(graph, indent=2, default=str), encoding="utf-8"
    )

def add_fact(key: str, value: Any, source: str = "unknown",
             category: str = "general", related_files: List[str] = None,
             related_keys: List[str] = None) -> dict:
    """Add or update a fact in the knowledge graph.

    Args:
        key: Unique fact identifier (e.g., "dispatch_uses_clipboard").
        value: The fact content (string, dict, list, etc.).
        source: Worker or system that discovered this fact.
        category: Classification (pattern, bug, architecture, config, etc.).
        related_files: Code files this fact relates to.
        related_keys: Other fact keys this fact connects to.

    Returns:
        The created/updated fact entry.
    """
    graph = _load_graph()
    entry = {
        "key": key,
        "value": value,
        "source": source,
        "category": category,
        "related_files": related_files or [],
        "related_keys": related_keys or [],
        "created_at": graph["facts"].get(key, {}).get("created_at",
                      time.strftime("%Y-%m-%dT%H:%M:%S")),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "validations": graph["facts"].get(key, {}).get("validations", 0),
    }
    graph["facts"][key] = entry

    # Auto-create relationships from related_files
    if related_files and len(related_files) > 1:
        for i, f1 in enumerate(related_files):
            for f2 in related_files[i + 1:]:
                rel = {"from": f1, "to": f2, "type": "co-referenced",
                       "via_fact": key, "source": source}
                if rel not in graph["relationships"]:
                    graph["relationships"].append(rel)

    # Auto-create relationships from related_keys
    if related_keys:
        for rk in related_keys:
            rel = {"from": key, "to": rk, "type": "related_fact",
                   "source": source}
            if rel not in graph["relationships"]:
                graph["relationships"].append(rel)

    _save_graph(graph)
    return entry


def query_facts(pattern: str = None, category: str = None,
                source: str = None, limit: int = 50) -> List[dict]:
    """Query facts from the knowledge graph.

    Args:
        pattern: Substring match against key or value (case-insensitive).
        category: Filter by category.
        source: Filter by discovering worker/source.
        limit: Max results to return.

    Returns:
        List of matching fact entries.
    """
    graph = _load_graph()
    results = []

    for key, fact in graph["facts"].items():
        if category and fact.get("category") != category:
            continue
        if source and fact.get("source") != source:
            continue
        if pattern:
            p = pattern.lower()
            val_str = str(fact.get("value", "")).lower()
            if p not in key.lower() and p not in val_str:
                continue
        results.append(fact)
        if len(results) >= limit:
            break

    return results


def get_related(key: str, depth: int = 1) -> dict:
    """Get all entities related to a fact key, file path, or incident.

    Args:
        key: A fact key, file path, or incident ID to find connections for.
        depth: How many hops to traverse (1 = direct, 2 = second-degree).

    Returns:
        Dict with related_facts, related_files, related_incidents, and relationships.
    """
    graph = _load_graph()
    visited_keys = set()
    related_facts = []
    related_files = set()
    related_rels = []

    frontier = {key}

    for _ in range(depth):
        next_frontier = set()
        for k in frontier:
            if k in visited_keys:
                continue
            visited_keys.add(k)

            # Check if k is a fact key
            if k in graph["facts"]:
                fact = graph["facts"][k]
                related_facts.append(fact)
                related_files.update(fact.get("related_files", []))
                next_frontier.update(fact.get("related_keys", []))

            # Check relationships where k appears
            for rel in graph["relationships"]:
                if rel.get("from") == k or rel.get("to") == k:
                    related_rels.append(rel)
                    other = rel["to"] if rel["from"] == k else rel["from"]
                    next_frontier.add(other)
                    # If 'other' looks like a file path, add to files
                    if "/" in other or "\\" in other or other.endswith(".py"):
                        related_files.add(other)

        frontier = next_frontier - visited_keys

    # Find related incidents
    related_incidents = []
    for inc in graph.get("incidents", []):
        inc_id = inc.get("id", "")
        root = inc.get("root_cause", "")
        if key in inc_id or key in root or key in str(inc.get("related_facts", [])):
            related_incidents.append(inc)

    return {
        "query": key,
        "related_facts": related_facts,
        "related_files": sorted(related_files),
        "related_incidents": related_incidents,
        "relationships": related_rels,
    }


def add_relationship(from_entity: str, to_entity: str, rel_type: str,
                     source: str = "unknown") -> dict:
    """Add a direct relationship between two entities (files, facts, etc.)."""
    graph = _load_graph()
    rel = {"from": from_entity, "to": to_entity, "type": rel_type,
           "source": source, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    # Avoid exact duplicates
    for existing in graph["relationships"]:
        if (existing.get("from") == from_entity and
            existing.get("to") == to_entity and
            existing.get("type") == rel_type):
            return existing
    graph["relationships"].append(rel)
    _save_graph(graph)
    return rel


def add_graph_incident(incident_id: str, root_cause: str,
                       related_facts: List[str] = None,
                       related_files: List[str] = None,
                       source: str = "unknown") -> dict:
    """Link an incident to the knowledge graph with root cause and connections."""
    graph = _load_graph()
    entry = {
        "id": incident_id,
        "root_cause": root_cause,
        "related_facts": related_facts or [],
        "related_files": related_files or [],
        "source": source,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # Update or append
    for i, inc in enumerate(graph["incidents"]):
        if inc.get("id") == incident_id:
            graph["incidents"][i] = entry
            break
    else:
        graph["incidents"].append(entry)
    _save_graph(graph)
    return entry


def graph_stats() -> dict:
    """Return knowledge graph statistics."""
    graph = _load_graph()
    facts = graph.get("facts", {})
    categories = {}
    sources = {}
    for f in facts.values():
        cat = f.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
        src = f.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1

    return {
        "total_facts": len(facts),
        "total_relationships": len(graph.get("relationships", [])),
        "total_incidents": len(graph.get("incidents", [])),
        "categories": categories,
        "sources": sources,
    }


# ─── CLI ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet Knowledge Sharing Protocol")
    parser.add_argument("--absorb", type=str, metavar="WORKER", help="Absorb learnings from bus (exclude own)")
    parser.add_argument("--share", type=str, metavar="WORKER", help="Share best strategies to bus")
    parser.add_argument("--broadcast", type=str, metavar="FACT", help="Broadcast a learning to bus")
    parser.add_argument("--category", type=str, default="general", help="Category for broadcast (default: general)")
    parser.add_argument("--tags", type=str, nargs="*", help="Tags for broadcast")
    parser.add_argument("--validate", type=str, metavar="FACT_ID", help="Validate a fact")
    parser.add_argument("--validator", type=str, default="cli", help="Validator name for --validate")
    parser.add_argument("--agree", action="store_true", default=True, help="Agree with fact (default)")
    parser.add_argument("--disagree", action="store_true", help="Disagree with fact")
    parser.add_argument("--suggest", type=str, metavar="CATEGORY", help="Suggest best strategy for category")
    parser.add_argument("--propose", type=str, metavar="TITLE", help="Propose an improvement")
    parser.add_argument("--propose-desc", type=str, default="", help="Description for --propose")
    parser.add_argument("--propose-files", type=str, nargs="*", help="Target files for --propose")
    parser.add_argument("--propose-priority", type=str, default="normal", help="Priority for --propose")
    parser.add_argument("--proposals", action="store_true", help="List all improvement proposals")
    parser.add_argument("--status", action="store_true", help="Show knowledge system status")
    parser.add_argument("--incidents", action="store_true", help="Show all recorded incidents")
    args = parser.parse_args()

    if args.incidents:
        incidents = get_incidents()
        if not incidents:
            print("No incidents recorded.")
        else:
            for inc in incidents:
                print(f"[{inc['id']}] {inc['timestamp']}")
                print(f"  What: {inc['what_happened']}")
                print(f"  Cause: {inc['root_cause']}")
                print(f"  Fix: {inc['fix_applied']}")
                print(f"  Rule: {inc['rule_created']}")
                print()
        return

    if args.status:
        status = get_status()
        print(json.dumps(status, indent=2, default=str))
        return

    if args.absorb:
        count = absorb_learnings(args.absorb)
        print(f"Absorbed {count} new learnings for worker {args.absorb}")
        return

    if args.share:
        count = share_best_strategies(args.share)
        print(f"Shared {count} strategies from worker {args.share}")
        return

    if args.broadcast:
        ok = broadcast_learning("cli", args.broadcast, args.category, args.tags)
        print(f"Broadcast {'succeeded' if ok else 'FAILED'}: {args.broadcast}")
        return

    if args.validate:
        agrees = not args.disagree
        ok = validate_fact(args.validate, args.validator, agrees)
        print(f"Validation {'posted' if ok else 'FAILED'} for {args.validate} (agrees={agrees})")
        return

    if args.suggest:
        strategy = suggest_strategy(args.suggest)
        if strategy:
            print(json.dumps(strategy, indent=2, default=str))
        else:
            print(f"No strategies found for category: {args.suggest}")
        return

    if args.propose:
        sender = args.validator if args.validator != "cli" else "cli"
        ok = propose_improvement(
            sender=sender,
            title=args.propose,
            description=args.propose_desc or args.propose,
            target_files=args.propose_files,
            priority=args.propose_priority,
        )
        print(f"Proposal {'posted' if ok else 'FAILED'}: {args.propose}")
        return

    if args.proposals:
        props = list_proposals()
        if not props:
            print("No proposals found.")
        else:
            for p in props:
                print(f"  [{p.get('priority','?').upper()}] [{p.get('status','?')}] {p.get('title','?')} (by {p.get('sender','?')}, {p.get('created_at','')})")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
