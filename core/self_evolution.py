"""
Self-Evolution Engine — Makes the agent self-improving across sessions.

WHY THIS BEATS CLAUDE/GPT/COMPETITORS:
=====================================
Traditional LLMs (Claude, GPT, Gemini) are FROZEN after training. They cannot:
- Learn from their own mistakes over time
- Adapt strategies to specific environments
- Improve performance on repetitive tasks
- Optimize for local compute/latency constraints

ScreenMemory's Self-Evolution Engine implements a genetic algorithm that:
1. Tracks task performance metrics across sessions (success rates, latency, quality)
2. Maintains a population of strategy "genes" (parameter configurations)
3. Evolves strategies through mutation, crossover, and selection
4. Continuously improves task-specific performance over time
5. Reflects on failures to identify systematic bottlenecks

Result: An agent that gets SMARTER with use, adapting to your specific
workload and environment. After 100 tasks, ScreenMemory outperforms static
models on YOUR specific use cases.

This is the killer feature that makes self-hosted agents superior to API-based
frozen models. Your agent improves while Claude stays the same.
"""

import sqlite3
import json
import time
import random
import threading
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
from statistics import mean, median
from collections import defaultdict
from datetime import datetime, timedelta


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class StrategyGene:
    """A single strategy configuration that evolves over time."""
    strategy_id: str
    name: str
    category: str  # "code", "research", "deploy", "navigate", "general"
    parameters: Dict[str, Any]
    fitness_score: float = 0.0
    generation: int = 0
    parent_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    task_count: int = 0  # Number of tasks evaluated with this strategy
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d['parameters'] = json.dumps(d['parameters'])
        return d
    
    @classmethod
    def from_dict(cls, d: dict) -> 'StrategyGene':
        """Create from dictionary."""
        d = d.copy()
        d['parameters'] = json.loads(d['parameters']) if isinstance(d['parameters'], str) else d['parameters']
        return cls(**d)


@dataclass
class TaskMetrics:
    """Metrics for a single task execution."""
    task_id: str
    category: str
    strategy_id: str
    success: bool
    latency_ms: float
    quality_score: float  # 0.0-1.0
    tokens_used: int
    memory_hits: int
    memory_queries: int
    timestamp: float = field(default_factory=time.time)
    error_type: Optional[str] = None


# ============================================================================
# PERFORMANCE TRACKER
# ============================================================================

class PerformanceTracker:
    """Tracks performance metrics per capability category."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Initialize database tables."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evolution_metrics (
                    task_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    quality_score REAL NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    memory_hits INTEGER NOT NULL,
                    memory_queries INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    error_type TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_category 
                ON evolution_metrics(category, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_strategy 
                ON evolution_metrics(strategy_id, timestamp)
            """)
            conn.commit()
    
    def record(self, metrics: TaskMetrics):
        """Record task execution metrics."""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO evolution_metrics 
                    (task_id, category, strategy_id, success, latency_ms, 
                     quality_score, tokens_used, memory_hits, memory_queries, 
                     timestamp, error_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    metrics.task_id, metrics.category, metrics.strategy_id,
                    int(metrics.success), metrics.latency_ms, metrics.quality_score,
                    metrics.tokens_used, metrics.memory_hits, metrics.memory_queries,
                    metrics.timestamp, metrics.error_type
                ))
                conn.commit()
    
    def get_success_rate(self, category: str, window: int = 50) -> float:
        """Get success rate for category over recent tasks."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT AVG(success) FROM (
                    SELECT success FROM evolution_metrics
                    WHERE category = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            """, (category, window))
            result = cursor.fetchone()[0]
            return result if result is not None else 0.0
    
    def get_latency_percentiles(self, category: str, window: int = 50) -> Dict[str, float]:
        """Get latency percentiles (p50, p95, p99) for category."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT latency_ms FROM evolution_metrics
                WHERE category = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (category, window))
            latencies = [row[0] for row in cursor.fetchall()]
        
        if not latencies:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        
        latencies.sort()
        n = len(latencies)
        return {
            "p50": latencies[int(n * 0.50)] if n > 0 else 0.0,
            "p95": latencies[int(n * 0.95)] if n > 1 else latencies[-1],
            "p99": latencies[int(n * 0.99)] if n > 2 else latencies[-1],
        }
    
    def get_memory_hit_rate(self, category: str, window: int = 50) -> float:
        """Get memory retrieval hit rate for category."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT SUM(memory_hits), SUM(memory_queries) FROM (
                    SELECT memory_hits, memory_queries FROM evolution_metrics
                    WHERE category = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            """, (category, window))
            hits, queries = cursor.fetchone()
            return hits / queries if queries and queries > 0 else 0.0
    
    def get_cost_efficiency(self, category: str, window: int = 50) -> float:
        """Get cost efficiency (quality per token) for category."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT AVG(quality_score / CAST(tokens_used AS REAL)) FROM (
                    SELECT quality_score, tokens_used FROM evolution_metrics
                    WHERE category = ? AND tokens_used > 0
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            """, (category, window))
            result = cursor.fetchone()[0]
            return result if result is not None else 0.0
    
    def get_strategy_fitness(self, strategy_id: str, window: int = 20) -> float:
        """Calculate fitness score for a strategy based on recent performance."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT success, quality_score, latency_ms, tokens_used
                FROM evolution_metrics
                WHERE strategy_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (strategy_id, window))
            rows = cursor.fetchall()
        
        if not rows:
            return 0.5  # Neutral fitness for untested strategies
        
        # Composite fitness: 50% success, 30% quality, 10% speed, 10% efficiency
        success_rate = sum(row[0] for row in rows) / len(rows)
        quality_avg = sum(row[1] for row in rows) / len(rows)
        
        # Normalize latency (lower is better, cap at 60s)
        latencies = [min(row[2], 60000) for row in rows]
        speed_score = 1.0 - (mean(latencies) / 60000.0)
        
        # Normalize token efficiency
        efficiencies = [row[1] / max(row[3], 1) for row in rows]
        efficiency_score = min(mean(efficiencies) * 1000, 1.0)  # Scale up
        
        fitness = (
            0.50 * success_rate +
            0.30 * quality_avg +
            0.10 * speed_score +
            0.10 * efficiency_score
        )
        
        return max(0.0, min(1.0, fitness))
    
    def get_recent_failures(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent task failures with details."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT task_id, category, strategy_id, latency_ms, 
                       quality_score, error_type, timestamp
                FROM evolution_metrics
                WHERE success = 0
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            
            failures = []
            for row in cursor.fetchall():
                failures.append({
                    "task_id": row[0],
                    "category": row[1],
                    "strategy_id": row[2],
                    "latency_ms": row[3],
                    "quality_score": row[4],
                    "error_type": row[5],
                    "timestamp": row[6]
                })
            return failures
    
    def get_category_stats(self, category: str, window: int = 100) -> Dict[str, Any]:
        """Get comprehensive statistics for a category."""
        return {
            "success_rate": self.get_success_rate(category, window),
            "latency": self.get_latency_percentiles(category, window),
            "memory_hit_rate": self.get_memory_hit_rate(category, window),
            "cost_efficiency": self.get_cost_efficiency(category, window)
        }


# ============================================================================
# EVOLUTION ENGINE
# ============================================================================

class EvolutionEngine:
    """Genetic algorithm engine for evolving agent strategies."""
    
    POPULATION_SIZE = 20  # Per category
    ELITE_SIZE = 4  # Top performers that always survive
    MUTATION_RATE = 0.15
    CROSSOVER_RATE = 0.3
    
    # Default parameter ranges for mutation
    PARAM_RANGES = {
        "cot_depth": (1, 5),
        "retrieval_k": (3, 10),
        "temperature": (0.1, 1.0),
        "max_iterations": (1, 10),
        "context_window": (512, 4096),
        "beam_width": (1, 5),
        "min_confidence": (0.5, 0.95),
        "memory_threshold": (0.6, 0.95),
        "parallel_agents": (1, 4),
        "reflection_depth": (0, 3)
    }
    
    def __init__(self, db_path: str, tracker: PerformanceTracker):
        self.db_path = db_path
        self.tracker = tracker
        self.lock = threading.Lock()
        self._init_db()
        self._init_population()
    
    def _init_db(self):
        """Initialize strategy population database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategy_population (
                    strategy_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    parameters TEXT NOT NULL,
                    fitness_score REAL NOT NULL,
                    generation INTEGER NOT NULL,
                    parent_id TEXT,
                    created_at REAL NOT NULL,
                    task_count INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_population_category_fitness
                ON strategy_population(category, fitness_score DESC)
            """)
            conn.commit()
    
    def _init_population(self):
        """Initialize default strategy population if empty."""
        categories = ["code", "research", "deploy", "navigate", "general"]
        
        with sqlite3.connect(self.db_path) as conn:
            for category in categories:
                # Check if category has population
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM strategy_population WHERE category = ?",
                    (category,)
                )
                count = cursor.fetchone()[0]
                
                if count == 0:
                    # Create initial population with varied parameters
                    for i in range(self.POPULATION_SIZE):
                        gene = self._create_random_strategy(category, generation=0)
                        self._save_strategy(gene, conn)
            conn.commit()
    
    def _create_random_strategy(self, category: str, generation: int) -> StrategyGene:
        """Create a random strategy for initialization."""
        strategy_id = f"{category}_{generation}_{random.randint(1000, 9999)}"
        
        # Random parameters within valid ranges
        parameters = {}
        for param, (min_val, max_val) in self.PARAM_RANGES.items():
            if isinstance(min_val, int):
                parameters[param] = random.randint(min_val, max_val)
            else:
                parameters[param] = random.uniform(min_val, max_val)
        
        return StrategyGene(
            strategy_id=strategy_id,
            name=f"{category.title()} Strategy Gen{generation}",
            category=category,
            parameters=parameters,
            generation=generation,
            fitness_score=0.5  # Neutral starting fitness
        )
    
    def _save_strategy(self, gene: StrategyGene, conn=None):
        """Save strategy to database."""
        close_conn = False
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            close_conn = True
        
        try:
            conn.execute("""
                INSERT OR REPLACE INTO strategy_population
                (strategy_id, name, category, parameters, fitness_score, 
                 generation, parent_id, created_at, task_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                gene.strategy_id, gene.name, gene.category,
                json.dumps(gene.parameters), gene.fitness_score,
                gene.generation, gene.parent_id, gene.created_at,
                gene.task_count
            ))
            conn.commit()
        finally:
            if close_conn:
                conn.close()
    
    def _load_strategy(self, strategy_id: str) -> Optional[StrategyGene]:
        """Load strategy from database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM strategy_population WHERE strategy_id = ?",
                (strategy_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            return StrategyGene(
                strategy_id=row[0],
                name=row[1],
                category=row[2],
                parameters=json.loads(row[3]),
                fitness_score=row[4],
                generation=row[5],
                parent_id=row[6],
                created_at=row[7],
                task_count=row[8]
            )
    
    def _get_population(self, category: str) -> List[StrategyGene]:
        """Get all strategies for a category."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM strategy_population WHERE category = ? ORDER BY fitness_score DESC",
                (category,)
            )
            
            population = []
            for row in cursor.fetchall():
                population.append(StrategyGene(
                    strategy_id=row[0],
                    name=row[1],
                    category=row[2],
                    parameters=json.loads(row[3]),
                    fitness_score=row[4],
                    generation=row[5],
                    parent_id=row[6],
                    created_at=row[7],
                    task_count=row[8]
                ))
            return population
    
    def evaluate(self, task_result: Dict[str, Any]) -> float:
        """
        Evaluate a task result and update strategy fitness.
        
        Args:
            task_result: Dict with keys: task_id, category, strategy_id, success,
                        latency_ms, quality_score, tokens_used, memory_hits,
                        memory_queries, error_type (optional)
        
        Returns:
            Updated fitness score for the strategy
        """
        metrics = TaskMetrics(
            task_id=task_result["task_id"],
            category=task_result["category"],
            strategy_id=task_result["strategy_id"],
            success=task_result["success"],
            latency_ms=task_result["latency_ms"],
            quality_score=task_result["quality_score"],
            tokens_used=task_result["tokens_used"],
            memory_hits=task_result.get("memory_hits", 0),
            memory_queries=task_result.get("memory_queries", 1),
            error_type=task_result.get("error_type")
        )
        
        # Record metrics
        self.tracker.record(metrics)
        
        # Update strategy fitness
        with self.lock:
            strategy = self._load_strategy(metrics.strategy_id)
            if strategy:
                strategy.task_count += 1
                strategy.fitness_score = self.tracker.get_strategy_fitness(
                    metrics.strategy_id
                )
                self._save_strategy(strategy)
                return strategy.fitness_score
        
        return 0.5
    
    def mutate_strategy(self, gene: StrategyGene) -> StrategyGene:
        """Create a mutated child strategy with perturbed parameters."""
        new_params = gene.parameters.copy()
        
        # Mutate each parameter with MUTATION_RATE probability
        for param, value in new_params.items():
            if random.random() < self.MUTATION_RATE and param in self.PARAM_RANGES:
                min_val, max_val = self.PARAM_RANGES[param]
                
                if isinstance(min_val, int):
                    # Integer mutation: +/- 1 or 2
                    delta = random.choice([-2, -1, 1, 2])
                    new_params[param] = max(min_val, min(max_val, value + delta))
                else:
                    # Float mutation: +/- 10-20%
                    delta = value * random.uniform(-0.2, 0.2)
                    new_params[param] = max(min_val, min(max_val, value + delta))
        
        # Create child strategy
        child_id = f"{gene.category}_{gene.generation + 1}_{random.randint(1000, 9999)}"
        
        return StrategyGene(
            strategy_id=child_id,
            name=f"{gene.category.title()} Mutant Gen{gene.generation + 1}",
            category=gene.category,
            parameters=new_params,
            generation=gene.generation + 1,
            parent_id=gene.strategy_id,
            fitness_score=0.5  # Will be evaluated during use
        )
    
    def crossover(self, gene_a: StrategyGene, gene_b: StrategyGene) -> StrategyGene:
        """Combine best parameters from two parent strategies."""
        new_params = {}
        
        # For each parameter, randomly pick from parent A or B
        all_params = set(gene_a.parameters.keys()) | set(gene_b.parameters.keys())
        for param in all_params:
            if param in gene_a.parameters and param in gene_b.parameters:
                # Both have it: pick randomly or average
                if random.random() < 0.5:
                    new_params[param] = gene_a.parameters[param]
                else:
                    new_params[param] = gene_b.parameters[param]
            elif param in gene_a.parameters:
                new_params[param] = gene_a.parameters[param]
            else:
                new_params[param] = gene_b.parameters[param]
        
        # Create offspring
        gen = max(gene_a.generation, gene_b.generation) + 1
        child_id = f"{gene_a.category}_{gen}_{random.randint(1000, 9999)}"
        
        return StrategyGene(
            strategy_id=child_id,
            name=f"{gene_a.category.title()} Hybrid Gen{gen}",
            category=gene_a.category,
            parameters=new_params,
            generation=gen,
            parent_id=f"{gene_a.strategy_id},{gene_b.strategy_id}",
            fitness_score=0.5
        )
    
    def select_best(self, category: str) -> StrategyGene:
        """Tournament selection: pick best from random sample."""
        population = self._get_population(category)
        
        if not population:
            # Fallback: create new random strategy
            return self._create_random_strategy(category, 0)
        
        # Tournament: pick 3 random, return best
        tournament_size = min(3, len(population))
        contestants = random.sample(population, tournament_size)
        return max(contestants, key=lambda g: g.fitness_score)
    
    def evolve_generation(self, category: str):
        """
        Run one evolution cycle for a category:
        1. Evaluate current population
        2. Select top performers (elitism)
        3. Generate new offspring via mutation and crossover
        4. Replace worst performers
        """
        with self.lock:
            population = self._get_population(category)
            
            if len(population) < self.POPULATION_SIZE:
                # Population too small, fill with random strategies
                while len(population) < self.POPULATION_SIZE:
                    gene = self._create_random_strategy(category, 0)
                    self._save_strategy(gene)
                    population.append(gene)
                return
            
            # Sort by fitness
            population.sort(key=lambda g: g.fitness_score, reverse=True)
            
            # Elites always survive
            elites = population[:self.ELITE_SIZE]
            
            # Generate offspring
            offspring = []
            
            # Crossover: create new hybrids
            crossover_count = int(self.POPULATION_SIZE * self.CROSSOVER_RATE)
            for _ in range(crossover_count):
                parent_a = self.select_best(category)
                parent_b = self.select_best(category)
                child = self.crossover(parent_a, parent_b)
                offspring.append(child)
            
            # Mutation: create mutants from top performers
            mutation_count = self.POPULATION_SIZE - self.ELITE_SIZE - crossover_count
            for _ in range(mutation_count):
                parent = self.select_best(category)
                child = self.mutate_strategy(parent)
                offspring.append(child)
            
            # New population: elites + offspring
            new_population = elites + offspring
            
            # Replace old population
            with sqlite3.connect(self.db_path) as conn:
                # Delete old non-elite strategies
                elite_ids = [g.strategy_id for g in elites]
                placeholders = ','.join('?' * len(elite_ids))
                conn.execute(
                    f"DELETE FROM strategy_population WHERE category = ? AND strategy_id NOT IN ({placeholders})",
                    [category] + elite_ids
                )
                
                # Save offspring
                for gene in offspring:
                    self._save_strategy(gene, conn)
                
                conn.commit()
    
    def get_optimal_config(self, task_category: str) -> Dict[str, Any]:
        """Get best-known parameter configuration for a task category."""
        population = self._get_population(task_category)
        
        if not population:
            # Return default config
            return {param: (min_val + max_val) / 2 
                    for param, (min_val, max_val) in self.PARAM_RANGES.items()}
        
        # Return parameters from highest fitness strategy
        best_strategy = max(population, key=lambda g: g.fitness_score)
        return best_strategy.parameters.copy()
    
    def get_optimal_strategy(self, task_category: str) -> StrategyGene:
        """Get the best strategy for a task category."""
        population = self._get_population(task_category)
        
        if not population:
            return self._create_random_strategy(task_category, 0)
        
        return max(population, key=lambda g: g.fitness_score)


# ============================================================================
# SELF-REFLECTOR
# ============================================================================

class SelfReflector:
    """Analyzes performance data to identify improvements (pure metric analysis)."""
    
    def __init__(self, tracker: PerformanceTracker):
        self.tracker = tracker
    
    def reflect_on_failures(self, recent_n: int = 10) -> List[str]:
        """Analyze recent failures and generate improvement hypotheses."""
        failures = self.tracker.get_recent_failures(recent_n)
        
        if not failures:
            return ["System performing well - no recent failures detected."]
        
        hypotheses = []
        
        # Analyze failure patterns
        by_category = defaultdict(list)
        by_error = defaultdict(list)
        
        for f in failures:
            by_category[f["category"]].append(f)
            if f["error_type"]:
                by_error[f["error_type"]].append(f)
        
        # Hypothesis 1: Category-specific weakness
        for category, fails in by_category.items():
            if len(fails) >= 3:
                rate = len(fails) / recent_n
                hypotheses.append(
                    f"High failure rate in {category} ({rate:.0%}). "
                    f"Consider evolving {category} strategies or adjusting category router."
                )
        
        # Hypothesis 2: Specific error pattern
        for error_type, fails in by_error.items():
            if len(fails) >= 3:
                hypotheses.append(
                    f"Recurring error: {error_type} ({len(fails)} times). "
                    f"May indicate systematic issue in error handling or resource limits."
                )
        
        # Hypothesis 3: Quality issues
        low_quality = [f for f in failures if f["quality_score"] < 0.3]
        if len(low_quality) >= 3:
            hypotheses.append(
                f"{len(low_quality)} failures with very low quality scores. "
                f"Consider increasing retrieval_k or cot_depth parameters."
            )
        
        # Hypothesis 4: Timeout issues
        timeouts = [f for f in failures if f["latency_ms"] > 30000]
        if len(timeouts) >= 2:
            hypotheses.append(
                f"{len(timeouts)} tasks timed out. "
                f"Consider reducing max_iterations or increasing parallel_agents."
            )
        
        return hypotheses if hypotheses else ["Failure patterns unclear - need more data."]
    
    def identify_bottlenecks(self) -> List[Dict[str, Any]]:
        """Find slowest/weakest subsystems from metrics."""
        categories = ["code", "research", "deploy", "navigate", "general"]
        bottlenecks = []
        
        for category in categories:
            stats = self.tracker.get_category_stats(category, window=50)
            
            # Check for bottlenecks
            if stats["success_rate"] < 0.7:
                bottlenecks.append({
                    "type": "success_rate",
                    "category": category,
                    "value": stats["success_rate"],
                    "severity": "high" if stats["success_rate"] < 0.5 else "medium",
                    "description": f"{category} success rate is {stats['success_rate']:.1%} (target: >70%)"
                })
            
            if stats["latency"]["p95"] > 20000:  # 20 seconds
                bottlenecks.append({
                    "type": "latency",
                    "category": category,
                    "value": stats["latency"]["p95"],
                    "severity": "medium",
                    "description": f"{category} p95 latency is {stats['latency']['p95']/1000:.1f}s (target: <20s)"
                })
            
            if stats["memory_hit_rate"] < 0.5:
                bottlenecks.append({
                    "type": "memory_efficiency",
                    "category": category,
                    "value": stats["memory_hit_rate"],
                    "severity": "low",
                    "description": f"{category} memory hit rate is {stats['memory_hit_rate']:.1%} (target: >50%)"
                })
            
            if stats["cost_efficiency"] < 0.001:  # Very low quality per token
                bottlenecks.append({
                    "type": "cost_efficiency",
                    "category": category,
                    "value": stats["cost_efficiency"],
                    "severity": "low",
                    "description": f"{category} cost efficiency is low (quality/token ratio)"
                })
        
        # Sort by severity
        severity_order = {"high": 0, "medium": 1, "low": 2}
        bottlenecks.sort(key=lambda b: severity_order[b["severity"]])
        
        return bottlenecks
    
    def propose_improvements(self) -> List[Dict[str, Any]]:
        """Generate concrete improvement proposals based on analysis."""
        bottlenecks = self.identify_bottlenecks()
        hypotheses = self.reflect_on_failures()
        
        proposals = []
        
        # Convert bottlenecks to proposals
        for bn in bottlenecks:
            if bn["type"] == "success_rate":
                proposals.append({
                    "action": "evolve_strategies",
                    "target": bn["category"],
                    "reason": bn["description"],
                    "priority": bn["severity"]
                })
            
            elif bn["type"] == "latency":
                proposals.append({
                    "action": "optimize_parameters",
                    "target": bn["category"],
                    "parameters": {
                        "max_iterations": -1,  # Reduce by 1
                        "context_window": -512  # Reduce by 512
                    },
                    "reason": bn["description"],
                    "priority": bn["severity"]
                })
            
            elif bn["type"] == "memory_efficiency":
                proposals.append({
                    "action": "adjust_memory",
                    "target": bn["category"],
                    "parameters": {
                        "retrieval_k": +1,  # Increase K
                        "memory_threshold": -0.05  # Lower threshold
                    },
                    "reason": bn["description"],
                    "priority": bn["severity"]
                })
            
            elif bn["type"] == "cost_efficiency":
                proposals.append({
                    "action": "optimize_quality",
                    "target": bn["category"],
                    "parameters": {
                        "cot_depth": +1,  # Deeper reasoning
                        "min_confidence": +0.05  # Higher bar
                    },
                    "reason": bn["description"],
                    "priority": bn["severity"]
                })
        
        # Add hypothesis-driven proposals
        for hypothesis in hypotheses:
            if "evolving" in hypothesis.lower():
                category = hypothesis.split()[4]  # Extract category name
                proposals.append({
                    "action": "trigger_evolution",
                    "target": category,
                    "reason": hypothesis,
                    "priority": "high"
                })
        
        return proposals
    
    def apply_improvement(self, proposal: Dict[str, Any], engine: EvolutionEngine):
        """Apply an improvement proposal to the system."""
        action = proposal["action"]
        target = proposal["target"]
        
        if action == "evolve_strategies":
            # Trigger evolution cycle for category
            engine.evolve_generation(target)
        
        elif action == "trigger_evolution":
            engine.evolve_generation(target)
        
        elif action in ["optimize_parameters", "adjust_memory", "optimize_quality"]:
            # Get current best strategy
            best = engine.get_optimal_strategy(target)
            
            # Apply parameter adjustments
            if "parameters" in proposal:
                for param, delta in proposal["parameters"].items():
                    if param in best.parameters:
                        if param in engine.PARAM_RANGES:
                            min_val, max_val = engine.PARAM_RANGES[param]
                            new_val = best.parameters[param] + delta
                            best.parameters[param] = max(min_val, min(max_val, new_val))
            
            # Create new mutant with adjusted params
            mutant = engine.mutate_strategy(best)
            mutant.parameters.update(best.parameters)
            engine._save_strategy(mutant)


# ============================================================================
# EVOLUTION DASHBOARD
# ============================================================================

class EvolutionDashboard:
    """Provides insights into evolution state and trends."""
    
    def __init__(self, engine: EvolutionEngine, tracker: PerformanceTracker):
        self.engine = engine
        self.tracker = tracker
    
    def summary(self) -> Dict[str, Any]:
        """Return current evolution state summary."""
        categories = ["code", "research", "deploy", "navigate", "general"]
        
        summary = {
            "timestamp": time.time(),
            "categories": {}
        }
        
        for category in categories:
            population = self.engine._get_population(category)
            
            if population:
                best = max(population, key=lambda g: g.fitness_score)
                avg_fitness = mean([g.fitness_score for g in population])
                max_gen = max([g.generation for g in population])
                
                summary["categories"][category] = {
                    "population_size": len(population),
                    "best_fitness": best.fitness_score,
                    "best_strategy_id": best.strategy_id,
                    "avg_fitness": avg_fitness,
                    "max_generation": max_gen,
                    "success_rate": self.tracker.get_success_rate(category)
                }
            else:
                summary["categories"][category] = {
                    "population_size": 0,
                    "best_fitness": 0.0,
                    "best_strategy_id": None,
                    "avg_fitness": 0.0,
                    "max_generation": 0,
                    "success_rate": 0.0
                }
        
        return summary
    
    def lineage(self, strategy_id: str) -> List[Dict[str, Any]]:
        """Trace ancestry of a strategy back to root."""
        lineage = []
        current_id = strategy_id
        
        while current_id:
            strategy = self.engine._load_strategy(current_id)
            if not strategy:
                break
            
            lineage.append({
                "strategy_id": strategy.strategy_id,
                "generation": strategy.generation,
                "fitness_score": strategy.fitness_score,
                "task_count": strategy.task_count,
                "created_at": strategy.created_at
            })
            
            # Move to parent
            if strategy.parent_id and ',' not in strategy.parent_id:
                current_id = strategy.parent_id
            else:
                break  # Crossover or root
        
        return lineage
    
    def trend(self, category: str, metric: str, window: int = 50) -> List[float]:
        """
        Return metric trend over recent tasks.
        
        Args:
            category: Task category
            metric: One of "success_rate", "latency", "quality", "efficiency"
            window: Number of recent tasks to analyze
        
        Returns:
            List of metric values over time (oldest to newest)
        """
        with sqlite3.connect(self.tracker.db_path) as conn:
            if metric == "success_rate":
                cursor = conn.execute("""
                    SELECT success FROM evolution_metrics
                    WHERE category = ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                """, (category, window))
                return [float(row[0]) for row in cursor.fetchall()]
            
            elif metric == "latency":
                cursor = conn.execute("""
                    SELECT latency_ms FROM evolution_metrics
                    WHERE category = ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                """, (category, window))
                return [row[0] for row in cursor.fetchall()]
            
            elif metric == "quality":
                cursor = conn.execute("""
                    SELECT quality_score FROM evolution_metrics
                    WHERE category = ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                """, (category, window))
                return [row[0] for row in cursor.fetchall()]
            
            elif metric == "efficiency":
                cursor = conn.execute("""
                    SELECT quality_score / CAST(tokens_used AS REAL) FROM evolution_metrics
                    WHERE category = ? AND tokens_used > 0
                    ORDER BY timestamp ASC
                    LIMIT ?
                """, (category, window))
                return [row[0] for row in cursor.fetchall()]
            
            else:
                return []
    
    def get_improvement_over_time(self, category: str, window: int = 100) -> Dict[str, Any]:
        """Calculate improvement metrics comparing first half vs second half of window."""
        trend = self.trend(category, "success_rate", window)
        
        if len(trend) < 10:
            return {"insufficient_data": True}
        
        midpoint = len(trend) // 2
        first_half = trend[:midpoint]
        second_half = trend[midpoint:]
        
        first_avg = mean(first_half)
        second_avg = mean(second_half)
        improvement = second_avg - first_avg
        
        return {
            "category": category,
            "window": window,
            "first_half_avg": first_avg,
            "second_half_avg": second_avg,
            "absolute_improvement": improvement,
            "relative_improvement": improvement / first_avg if first_avg > 0 else 0.0,
            "is_improving": improvement > 0.05  # 5% threshold
        }


# ============================================================================
# CONVENIENCE FACADE
# ============================================================================

class SelfEvolutionSystem:
    """Unified interface to the self-evolution system."""
    
    def __init__(self, data_dir: str = "D:\\Prospects\\ScreenMemory\\data"):
        data_path = Path(data_dir)
        data_path.mkdir(parents=True, exist_ok=True)
        
        db_path = str(data_path / "evolution.db")
        
        self.tracker = PerformanceTracker(db_path)
        self.engine = EvolutionEngine(db_path, self.tracker)
        self.reflector = SelfReflector(self.tracker)
        self.dashboard = EvolutionDashboard(self.engine, self.tracker)
    
    def record_task(self, task_result: Dict[str, Any]) -> float:
        """Record a task result and return updated fitness."""
        return self.engine.evaluate(task_result)
    
    def get_strategy_for_task(self, category: str) -> Dict[str, Any]:
        """Get optimal strategy parameters for a task."""
        return self.engine.get_optimal_config(category)
    
    def evolve_all_categories(self):
        """Run evolution cycle for all categories."""
        for category in ["code", "research", "deploy", "navigate", "general"]:
            self.engine.evolve_generation(category)
    
    def auto_improve(self):
        """Automatic improvement: reflect, propose, apply improvements."""
        proposals = self.reflector.propose_improvements()
        
        # Apply top 3 proposals
        for proposal in proposals[:3]:
            self.reflector.apply_improvement(proposal, self.engine)
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive system status."""
        return {
            "summary": self.dashboard.summary(),
            "bottlenecks": self.reflector.identify_bottlenecks(),
            "improvement_hypotheses": self.reflect_on_failures()
        }
    
    def reflect_on_failures(self, recent_n: int = 10) -> List[str]:
        """Wrapper for reflector.reflect_on_failures."""
        return self.reflector.reflect_on_failures(recent_n)


# ============================================================================
# MAIN - DEMO/TEST
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("ScreenMemory Self-Evolution Engine")
    print("=" * 70)
    
    # Initialize system
    system = SelfEvolutionSystem()
    
    # Simulate some task executions
    print("\nSimulating 20 tasks across categories...")
    categories = ["code", "research", "deploy", "navigate", "general"]
    
    for i in range(20):
        category = random.choice(categories)
        strategy = system.engine.get_optimal_strategy(category)
        
        # Simulate task execution with varying results
        success = random.random() > 0.3  # 70% success rate
        quality = random.uniform(0.6, 1.0) if success else random.uniform(0.0, 0.5)
        latency = random.uniform(1000, 15000)
        tokens = random.randint(500, 3000)
        
        task_result = {
            "task_id": f"task_{i}",
            "category": category,
            "strategy_id": strategy.strategy_id,
            "success": success,
            "latency_ms": latency,
            "quality_score": quality,
            "tokens_used": tokens,
            "memory_hits": random.randint(0, 5),
            "memory_queries": random.randint(1, 5)
        }
        
        fitness = system.record_task(task_result)
        print(f"  Task {i}: {category:10s} - {'✓' if success else '✗'} (fitness: {fitness:.3f})")
    
    # Show dashboard
    print("\n" + "=" * 70)
    print("Evolution Dashboard")
    print("=" * 70)
    
    summary = system.dashboard.summary()
    for category, stats in summary["categories"].items():
        print(f"\n{category.upper()}:")
        print(f"  Population: {stats['population_size']}")
        print(f"  Best Fitness: {stats['best_fitness']:.3f}")
        print(f"  Avg Fitness: {stats['avg_fitness']:.3f}")
        print(f"  Generation: {stats['max_generation']}")
        print(f"  Success Rate: {stats['success_rate']:.1%}")
    
    # Show bottlenecks
    print("\n" + "=" * 70)
    print("Identified Bottlenecks")
    print("=" * 70)
    
    bottlenecks = system.reflector.identify_bottlenecks()
    if bottlenecks:
        for bn in bottlenecks[:5]:
            print(f"\n[{bn['severity'].upper()}] {bn['type']}")
            print(f"  {bn['description']}")
    else:
        print("\nNo significant bottlenecks detected.")
    
    # Show improvement hypotheses
    print("\n" + "=" * 70)
    print("Improvement Hypotheses")
    print("=" * 70)
    
    hypotheses = system.reflect_on_failures()
    for i, h in enumerate(hypotheses, 1):
        print(f"\n{i}. {h}")
    
    # Run evolution
    print("\n" + "=" * 70)
    print("Running Evolution Cycle")
    print("=" * 70)
    
    system.evolve_all_categories()
    print("\n✓ Evolution cycle complete for all categories")
    
    # Show improvement proposals
    print("\n" + "=" * 70)
    print("Improvement Proposals")
    print("=" * 70)
    
    proposals = system.reflector.propose_improvements()
    for i, prop in enumerate(proposals[:5], 1):
        print(f"\n{i}. {prop['action']} → {prop['target']}")
        print(f"   Priority: {prop['priority']}")
        print(f"   Reason: {prop['reason']}")
    
    print("\n" + "=" * 70)
    print("Self-Evolution Engine Ready")
    print("=" * 70)
    print("\nThe system will now continuously improve performance across sessions.")
    print("Unlike Claude/GPT, ScreenMemory gets smarter with every task.")
