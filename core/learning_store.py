"""
Persistent Learning Store — Cross-session knowledge accumulation.
Claude forgets everything between conversations. GPT has no persistent learning.
ScreenMemory builds cumulative expertise that compounds over time.
"""

import sqlite3
import json
import math
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any
from uuid import uuid4
from collections import Counter, defaultdict
from pathlib import Path


@dataclass
class LearnedFact:
    """A single learned fact with metadata and reinforcement tracking."""
    fact_id: str
    content: str
    category: str  # procedure, concept, preference, correction, pattern, skill
    confidence: float
    source: str
    reinforcement_count: int = 0
    contradiction_count: int = 0
    first_learned: str = ""
    last_accessed: str = ""
    tags: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.first_learned:
            self.first_learned = datetime.now().isoformat()
        if not self.last_accessed:
            self.last_accessed = datetime.now().isoformat()


class ExpertiseProfile:
    """Tracks domain competency scores with Bayesian updating."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Initialize expertise profile table."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS expertise_profile (
                    domain TEXT PRIMARY KEY,
                    score REAL NOT NULL,
                    successes INTEGER DEFAULT 0,
                    failures INTEGER DEFAULT 0,
                    last_updated TEXT
                )
            """)
            conn.commit()
    
    def update(self, domain: str, success: bool):
        """Bayesian update of domain competency."""
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                cursor = conn.execute(
                    "SELECT score, successes, failures FROM expertise_profile WHERE domain = ?",
                    (domain,)
                )
                row = cursor.fetchone()
                
                if row:
                    score, successes, failures = row
                else:
                    score, successes, failures = 0.5, 0, 0
                
                if success:
                    score = score + 0.05 * (1 - score)
                    successes += 1
                else:
                    score = score - 0.03 * score
                    failures += 1
                
                score = max(0.0, min(1.0, score))
                
                conn.execute("""
                    INSERT OR REPLACE INTO expertise_profile 
                    (domain, score, successes, failures, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (domain, score, successes, failures, datetime.now().isoformat()))
                conn.commit()
    
    def strongest_domains(self, n: int = 3) -> List[Tuple[str, float]]:
        """Return top N domains by score."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute(
                "SELECT domain, score FROM expertise_profile ORDER BY score DESC LIMIT ?",
                (n,)
            )
            return cursor.fetchall()
    
    def weakest_domains(self, n: int = 3) -> List[Tuple[str, float]]:
        """Return bottom N domains by score."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute(
                "SELECT domain, score FROM expertise_profile ORDER BY score ASC LIMIT ?",
                (n,)
            )
            return cursor.fetchall()
    
    def total_experience(self) -> int:
        """Total number of tasks across all domains."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute(
                "SELECT SUM(successes + failures) FROM expertise_profile"
            )
            result = cursor.fetchone()[0]
            return result if result else 0
    
    def get_score(self, domain: str) -> float:
        """Get score for a specific domain."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute(
                "SELECT score FROM expertise_profile WHERE domain = ?",
                (domain,)
            )
            row = cursor.fetchone()
            return row[0] if row else 0.5


class LearningStore:
    """Main persistent storage for learned facts with BM25 search."""
    
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = r"D:\Prospects\ScreenMemory\data\learning.db"
        
        self.db_path = db_path
        self.lock = threading.Lock()
        
        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        self._init_db()
    
    def _init_db(self):
        """Initialize learned facts table."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learned_facts (
                    fact_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    reinforcement_count INTEGER DEFAULT 0,
                    contradiction_count INTEGER DEFAULT 0,
                    first_learned TEXT NOT NULL,
                    last_accessed TEXT NOT NULL,
                    tags TEXT
                )
            """)
            conn.commit()
    
    def learn(self, content: str, category: str, source: str, tags: Optional[List[str]] = None) -> str:
        """Store a new learned fact."""
        fact_id = str(uuid4())
        tags = tags or []
        now = datetime.now().isoformat()
        
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.execute("""
                    INSERT INTO learned_facts 
                    (fact_id, content, category, confidence, source, reinforcement_count, 
                     contradiction_count, first_learned, last_accessed, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (fact_id, content, category, 0.7, source, 0, 0, now, now, json.dumps(tags)))
                conn.commit()
        
        return fact_id
    
    def reinforce(self, fact_id: str):
        """Reinforce a fact, increasing confidence."""
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                cursor = conn.execute(
                    "SELECT confidence, reinforcement_count FROM learned_facts WHERE fact_id = ?",
                    (fact_id,)
                )
                row = cursor.fetchone()
                
                if row:
                    confidence, count = row
                    new_confidence = confidence + 0.1 * (1 - confidence)
                    new_confidence = min(1.0, new_confidence)
                    
                    conn.execute("""
                        UPDATE learned_facts 
                        SET confidence = ?, reinforcement_count = ?, last_accessed = ?
                        WHERE fact_id = ?
                    """, (new_confidence, count + 1, datetime.now().isoformat(), fact_id))
                    conn.commit()
    
    def contradict(self, fact_id: str, correction: str) -> str:
        """Mark a fact as contradicted and create a corrected version."""
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                # Increment contradiction count on old fact
                cursor = conn.execute(
                    "SELECT contradiction_count FROM learned_facts WHERE fact_id = ?",
                    (fact_id,)
                )
                row = cursor.fetchone()
                
                if row:
                    conn.execute("""
                        UPDATE learned_facts 
                        SET contradiction_count = ?
                        WHERE fact_id = ?
                    """, (row[0] + 1, fact_id))
                    conn.commit()
        
        # Create corrected fact
        new_fact_id = self.learn(correction, "correction", f"correction_of:{fact_id}")
        return new_fact_id
    
    def recall(self, query: str, top_k: int = 5) -> List[LearnedFact]:
        """BM25 search for relevant facts."""
        query_tokens = self._tokenize(query)
        
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute("SELECT * FROM learned_facts")
            rows = cursor.fetchall()
        
        if not rows:
            return []
        
        # Build document corpus
        docs = []
        doc_ids = []
        for row in rows:
            docs.append(self._tokenize(row[1]))  # content is at index 1
            doc_ids.append(row[0])  # fact_id at index 0
        
        # Calculate BM25 scores
        scores = self._bm25_score(query_tokens, docs)
        
        # Get top-k
        scored_docs = list(zip(doc_ids, scores))
        scored_docs.sort(key=lambda x: x[1], reverse=True)
        top_doc_ids = [doc_id for doc_id, score in scored_docs[:top_k]]
        
        # Fetch full facts and update last_accessed
        results = []
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                for fact_id in top_doc_ids:
                    cursor = conn.execute(
                        "SELECT * FROM learned_facts WHERE fact_id = ?",
                        (fact_id,)
                    )
                    row = cursor.fetchone()
                    if row:
                        tags = json.loads(row[9]) if row[9] else []
                        fact = LearnedFact(
                            fact_id=row[0], content=row[1], category=row[2],
                            confidence=row[3], source=row[4], reinforcement_count=row[5],
                            contradiction_count=row[6], first_learned=row[7],
                            last_accessed=row[8], tags=tags
                        )
                        results.append(fact)
                        
                        # Update last_accessed
                        conn.execute(
                            "UPDATE learned_facts SET last_accessed = ? WHERE fact_id = ?",
                            (datetime.now().isoformat(), fact_id)
                        )
                conn.commit()
        
        return results
    
    def recall_by_category(self, category: str, top_k: int = 10) -> List[LearnedFact]:
        """Retrieve facts by category, sorted by confidence."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute("""
                SELECT * FROM learned_facts 
                WHERE category = ? 
                ORDER BY confidence DESC 
                LIMIT ?
            """, (category, top_k))
            rows = cursor.fetchall()
        
        results = []
        for row in rows:
            tags = json.loads(row[9]) if row[9] else []
            fact = LearnedFact(
                fact_id=row[0], content=row[1], category=row[2],
                confidence=row[3], source=row[4], reinforcement_count=row[5],
                contradiction_count=row[6], first_learned=row[7],
                last_accessed=row[8], tags=tags
            )
            results.append(fact)
        
        return results
    
    def forget(self, min_confidence: float = 0.1):
        """Remove low-confidence, highly-contradicted facts."""
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.execute("""
                    DELETE FROM learned_facts 
                    WHERE confidence < ? AND contradiction_count > reinforcement_count
                """, (min_confidence,))
                deleted = conn.total_changes
                conn.commit()
        
        return deleted
    
    def consolidate(self):
        """Merge similar facts with high word overlap."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute("SELECT fact_id, content, tags, confidence FROM learned_facts")
            rows = cursor.fetchall()
        
        if len(rows) < 2:
            return 0
        
        merged_count = 0
        to_delete = set()
        
        for i, row_a in enumerate(rows):
            if row_a[0] in to_delete:
                continue
                
            for row_b in rows[i+1:]:
                if row_b[0] in to_delete:
                    continue
                
                # Calculate word overlap
                words_a = set(self._tokenize(row_a[1]))
                words_b = set(self._tokenize(row_b[1]))
                
                if not words_a or not words_b:
                    continue
                
                overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
                
                if overlap > 0.7:
                    # Merge facts
                    tags_a = json.loads(row_a[2]) if row_a[2] else []
                    tags_b = json.loads(row_b[2]) if row_b[2] else []
                    merged_tags = list(set(tags_a + tags_b))
                    
                    avg_confidence = (row_a[3] + row_b[3]) / 2
                    
                    with self.lock:
                        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                            # Update first fact
                            conn.execute("""
                                UPDATE learned_facts 
                                SET tags = ?, confidence = ?
                                WHERE fact_id = ?
                            """, (json.dumps(merged_tags), avg_confidence, row_a[0]))
                            
                            # Delete second fact
                            conn.execute("DELETE FROM learned_facts WHERE fact_id = ?", (row_b[0],))
                            conn.commit()
                    
                    to_delete.add(row_b[0])
                    merged_count += 1
        
        return merged_count
    
    def export_knowledge(self, domain: Optional[str] = None) -> str:
        """Export knowledge as formatted text for context injection."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            if domain:
                cursor = conn.execute("""
                    SELECT category, content, confidence, tags 
                    FROM learned_facts 
                    WHERE tags LIKE ?
                    ORDER BY confidence DESC
                """, (f'%{domain}%',))
            else:
                cursor = conn.execute("""
                    SELECT category, content, confidence, tags 
                    FROM learned_facts 
                    ORDER BY confidence DESC
                """)
            rows = cursor.fetchall()
        
        if not rows:
            return "No knowledge available."
        
        output = ["=== LEARNED KNOWLEDGE ===\n"]
        
        by_category = defaultdict(list)
        for row in rows:
            by_category[row[0]].append((row[1], row[2]))
        
        for category, facts in by_category.items():
            output.append(f"\n[{category.upper()}]")
            for content, confidence in facts:
                output.append(f"  • {content} (confidence: {confidence:.2f})")
        
        return "\n".join(output)
    
    def stats(self) -> Dict[str, Any]:
        """Return statistics about stored knowledge."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = conn.execute("SELECT COUNT(*), AVG(confidence) FROM learned_facts")
            total, avg_conf = cursor.fetchone()
            
            cursor = conn.execute("""
                SELECT category, COUNT(*) 
                FROM learned_facts 
                GROUP BY category
            """)
            by_category = dict(cursor.fetchall())
        
        return {
            "total_facts": total or 0,
            "average_confidence": avg_conf or 0.0,
            "by_category": by_category
        }
    
    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization."""
        return text.lower().split()
    
    def _bm25_score(self, query_tokens: List[str], docs: List[List[str]], k1: float = 1.5, b: float = 0.75) -> List[float]:
        """Calculate BM25 scores for documents."""
        N = len(docs)
        avgdl = sum(len(doc) for doc in docs) / N if N > 0 else 0
        
        # Calculate document frequencies
        df = Counter()
        for doc in docs:
            df.update(set(doc))
        
        # Calculate IDF
        idf = {}
        for term in query_tokens:
            idf[term] = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1)
        
        # Calculate scores
        scores = []
        for doc in docs:
            score = 0.0
            doc_len = len(doc)
            term_freqs = Counter(doc)
            
            for term in query_tokens:
                if term in term_freqs:
                    tf = term_freqs[term]
                    numerator = idf[term] * tf * (k1 + 1)
                    denominator = tf + k1 * (1 - b + b * doc_len / avgdl)
                    score += numerator / denominator
            
            scores.append(score)
        
        return scores


class PatternDetector:
    """Detect recurring patterns in learned facts."""
    
    def __init__(self, store: LearningStore):
        self.store = store
    
    def detect_recurring(self, category: str, min_occurrences: int = 3) -> List[Dict[str, Any]]:
        """Find common word sequences in facts of a category."""
        facts = self.store.recall_by_category(category, top_k=100)
        
        if len(facts) < min_occurrences:
            return []
        
        # Extract bigrams and trigrams
        bigrams = Counter()
        trigrams = Counter()
        
        for fact in facts:
            tokens = self.store._tokenize(fact.content)
            
            for i in range(len(tokens) - 1):
                bigrams[f"{tokens[i]} {tokens[i+1]}"] += 1
            
            for i in range(len(tokens) - 2):
                trigrams[f"{tokens[i]} {tokens[i+1]} {tokens[i+2]}"] += 1
        
        patterns = []
        
        for phrase, count in bigrams.most_common(10):
            if count >= min_occurrences:
                patterns.append({
                    "type": "bigram",
                    "phrase": phrase,
                    "occurrences": count
                })
        
        for phrase, count in trigrams.most_common(10):
            if count >= min_occurrences:
                patterns.append({
                    "type": "trigram",
                    "phrase": phrase,
                    "occurrences": count
                })
        
        return patterns
    
    def detect_failure_patterns(self) -> List[Dict[str, Any]]:
        """Find common patterns in correction facts."""
        return self.detect_recurring("correction", min_occurrences=2)
    
    def detect_success_patterns(self) -> List[Dict[str, Any]]:
        """Find patterns in high-confidence facts."""
        with sqlite3.connect(self.store.db_path, check_same_thread=False) as conn:
            cursor = conn.execute("""
                SELECT content FROM learned_facts 
                WHERE confidence > 0.8
            """)
            rows = cursor.fetchall()
        
        if len(rows) < 3:
            return []
        
        bigrams = Counter()
        trigrams = Counter()
        
        for row in rows:
            tokens = self.store._tokenize(row[0])
            
            for i in range(len(tokens) - 1):
                bigrams[f"{tokens[i]} {tokens[i+1]}"] += 1
            
            for i in range(len(tokens) - 2):
                trigrams[f"{tokens[i]} {tokens[i+1]} {tokens[i+2]}"] += 1
        
        patterns = []
        
        for phrase, count in bigrams.most_common(10):
            if count >= 3:
                patterns.append({
                    "type": "bigram",
                    "phrase": phrase,
                    "occurrences": count
                })
        
        for phrase, count in trigrams.most_common(10):
            if count >= 3:
                patterns.append({
                    "type": "trigram",
                    "phrase": phrase,
                    "occurrences": count
                })
        
        return patterns


class KnowledgeGraph:
    """Lightweight knowledge graph for fact relationships."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Initialize graph tables."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_nodes (
                    fact_id TEXT PRIMARY KEY
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_edges (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    created TEXT,
                    PRIMARY KEY (source_id, target_id, relation)
                )
            """)
            conn.commit()
    
    def add_relation(self, fact_a_id: str, fact_b_id: str, relation: str):
        """Add a relation between two facts."""
        with self.lock:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                # Ensure nodes exist
                conn.execute("INSERT OR IGNORE INTO knowledge_nodes (fact_id) VALUES (?)", (fact_a_id,))
                conn.execute("INSERT OR IGNORE INTO knowledge_nodes (fact_id) VALUES (?)", (fact_b_id,))
                
                # Add edge
                conn.execute("""
                    INSERT OR REPLACE INTO knowledge_edges 
                    (source_id, target_id, relation, created)
                    VALUES (?, ?, ?, ?)
                """, (fact_a_id, fact_b_id, relation, datetime.now().isoformat()))
                conn.commit()
    
    def get_related(self, fact_id: str, depth: int = 2) -> List[str]:
        """BFS traversal to get related facts."""
        visited = set()
        queue = [(fact_id, 0)]
        related = []
        
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            while queue:
                current_id, current_depth = queue.pop(0)
                
                if current_id in visited or current_depth > depth:
                    continue
                
                visited.add(current_id)
                
                if current_id != fact_id:
                    related.append(current_id)
                
                if current_depth < depth:
                    # Get neighbors
                    cursor = conn.execute("""
                        SELECT target_id FROM knowledge_edges WHERE source_id = ?
                        UNION
                        SELECT source_id FROM knowledge_edges WHERE target_id = ?
                    """, (current_id, current_id))
                    
                    for row in cursor.fetchall():
                        if row[0] not in visited:
                            queue.append((row[0], current_depth + 1))
        
        return related
    
    def find_path(self, fact_a_id: str, fact_b_id: str) -> List[str]:
        """BFS shortest path between two facts."""
        if fact_a_id == fact_b_id:
            return [fact_a_id]
        
        visited = set()
        queue = [(fact_a_id, [fact_a_id])]
        
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            while queue:
                current_id, path = queue.pop(0)
                
                if current_id in visited:
                    continue
                
                visited.add(current_id)
                
                # Get neighbors
                cursor = conn.execute("""
                    SELECT target_id FROM knowledge_edges WHERE source_id = ?
                    UNION
                    SELECT source_id FROM knowledge_edges WHERE target_id = ?
                """, (current_id, current_id))
                
                for row in cursor.fetchall():
                    neighbor_id = row[0]
                    
                    if neighbor_id == fact_b_id:
                        return path + [neighbor_id]
                    
                    if neighbor_id not in visited:
                        queue.append((neighbor_id, path + [neighbor_id]))
        
        return []


class PersistentLearningSystem:
    """Unified facade for the entire persistent learning system."""
    
    def __init__(self, data_dir: str = r"D:\Prospects\ScreenMemory\data"):
        db_path = str(Path(data_dir) / "learning.db")
        
        self.store = LearningStore(db_path)
        self.expertise = ExpertiseProfile(db_path)
        self.patterns = PatternDetector(self.store)
        self.graph = KnowledgeGraph(db_path)
    
    def learn_from_task(
        self, 
        task_description: str, 
        category: str, 
        success: bool, 
        insights: List[str]
    ) -> List[str]:
        """Learn facts from a completed task and update expertise."""
        fact_ids = []
        
        # Learn each insight
        for insight in insights:
            fact_id = self.store.learn(
                content=insight,
                category=category,
                source=f"task:{task_description}",
                tags=[category, "success" if success else "failure"]
            )
            fact_ids.append(fact_id)
        
        # Update expertise based on task category
        self.expertise.update(category, success)
        
        # Auto-link related facts
        if len(fact_ids) > 1:
            for i in range(len(fact_ids) - 1):
                self.graph.add_relation(fact_ids[i], fact_ids[i+1], "extends")
        
        return fact_ids
    
    def get_context_for_task(self, task_description: str, top_k: int = 5) -> str:
        """Recall relevant knowledge for an upcoming task."""
        relevant_facts = self.store.recall(task_description, top_k=top_k)
        
        if not relevant_facts:
            return "No prior knowledge found for this task."
        
        context = ["=== RELEVANT PRIOR KNOWLEDGE ===\n"]
        
        for fact in relevant_facts:
            context.append(f"• [{fact.category}] {fact.content}")
            context.append(f"  Confidence: {fact.confidence:.2f} | Reinforced: {fact.reinforcement_count}x\n")
        
        return "\n".join(context)
    
    def get_expertise_summary(self) -> Dict[str, Any]:
        """Full expertise and learning statistics."""
        return {
            "strongest_domains": self.expertise.strongest_domains(5),
            "weakest_domains": self.expertise.weakest_domains(3),
            "total_experience": self.expertise.total_experience(),
            "knowledge_stats": self.store.stats(),
            "failure_patterns": self.patterns.detect_failure_patterns(),
            "success_patterns": self.patterns.detect_success_patterns()
        }
    
    def run_maintenance(self) -> Dict[str, int]:
        """Forget low-confidence facts, consolidate similar ones, detect patterns."""
        forgotten = self.store.forget(min_confidence=0.2)
        consolidated = self.store.consolidate()
        
        return {
            "facts_forgotten": forgotten,
            "facts_consolidated": consolidated
        }


# Convenience function for quick initialization
def initialize_learning_system(data_dir: str = r"D:\Prospects\ScreenMemory\data") -> PersistentLearningSystem:
    """Initialize and return a ready-to-use learning system."""
    return PersistentLearningSystem(data_dir)


if __name__ == "__main__":
    # Quick test
    system = initialize_learning_system()
    
    # Learn some facts
    fact_ids = system.learn_from_task(
        task_description="Deploy Python application",
        category="deployment",
        success=True,
        insights=[
            "Always check Python version compatibility before deployment",
            "Use virtual environments to isolate dependencies",
            "Test on staging before production"
        ]
    )
    
    print(f"Learned {len(fact_ids)} facts")
    print("\nContext for similar task:")
    print(system.get_context_for_task("deploying python app"))
    print("\nExpertise summary:")
    print(system.get_expertise_summary())
