"""Integration tests: episode → verification → LearningStore pipeline.

Tests the full end-to-end flow: log_episode() writes to disk,
verify_episode() classifies correctly, LearningStore stores and queries
facts, and strategy_id propagates through the entire pipeline.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.skynet_episode import log_episode, list_episodes, load_episode
from tools.skynet_verifier import SimpleVerifier, verify_episode, Outcome, clear_verifiers
from core.learning_store import LearningStore
from tools.skynet_brain import query_episodes_by_strategy


# ── Fixtures ────────────────────────────────────────────


@pytest.fixture
def isolated_env(tmp_path):
    """Provide an isolated temp environment for episodes and learning DB."""
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    db_path = str(tmp_path / "test_learning.db")
    index_path = tmp_path / "learning_episodes.json"

    with patch("tools.skynet_episode.EPISODES_DIR", episodes_dir), \
         patch("tools.skynet_brain.EPISODES_DIR", episodes_dir), \
         patch("tools.skynet_brain.EPISODES_INDEX", index_path):
        yield {
            "episodes_dir": episodes_dir,
            "db_path": db_path,
            "index_path": index_path,
        }


@pytest.fixture
def store(isolated_env):
    """Return a LearningStore backed by a temp SQLite DB."""
    return LearningStore(db_path=isolated_env["db_path"])


# ── 1. Episode Logging Tests ───────────────────────────


class TestEpisodeLogging:
    def test_log_episode_writes_file(self, isolated_env):
        """log_episode() creates a JSON file in the episodes directory."""
        ep = log_episode(
            task="build widget",
            result="Widget built successfully",
            outcome="success",
            worker="alpha",
        )
        files = list(isolated_env["episodes_dir"].glob("*.json"))
        assert len(files) == 1
        assert ep["task"] == "build widget"
        assert ep["outcome"] == "success"

    def test_log_episode_file_content_matches(self, isolated_env):
        """Episode file on disk matches the returned dict."""
        ep = log_episode(
            task="deploy service",
            result="Deployment complete",
            outcome="success",
            worker="beta",
        )
        files = list(isolated_env["episodes_dir"].glob("*.json"))
        on_disk = json.loads(files[0].read_text(encoding="utf-8"))
        assert on_disk["task"] == ep["task"]
        assert on_disk["result"] == ep["result"]
        assert on_disk["worker"] == "beta"

    def test_log_episode_with_strategy_id(self, isolated_env):
        """strategy_id is persisted in the episode file."""
        ep = log_episode(
            task="optimize query",
            result="Query optimized, 3x faster",
            outcome="success",
            strategy_id="strat-abc123",
            worker="gamma",
        )
        files = list(isolated_env["episodes_dir"].glob("*.json"))
        on_disk = json.loads(files[0].read_text(encoding="utf-8"))
        assert on_disk["strategy_id"] == "strat-abc123"
        assert ep["strategy_id"] == "strat-abc123"

    def test_log_episode_with_metadata(self, isolated_env):
        """Metadata dict is stored in the episode."""
        meta = {"duration_ms": 1500, "files_changed": 3}
        ep = log_episode(
            task="refactor module",
            result="Done refactoring",
            outcome="success",
            worker="delta",
            metadata=meta,
        )
        assert ep["metadata"]["duration_ms"] == 1500
        assert ep["metadata"]["files_changed"] == 3

    def test_list_episodes_returns_logged(self, isolated_env):
        """list_episodes() returns episodes written by log_episode()."""
        log_episode(task="task A", result="done A", outcome="success", worker="alpha")
        log_episode(task="task B", result="done B", outcome="failure", worker="beta")
        episodes = list_episodes()
        assert len(episodes) >= 2
        tasks = {ep["task"] for ep in episodes}
        assert "task A" in tasks
        assert "task B" in tasks


# ── 2. Verification Tests ──────────────────────────────


class TestVerification:
    def test_verify_success_episode(self, isolated_env):
        """SimpleVerifier classifies a success result correctly."""
        ep = log_episode(
            task="run tests",
            result="All 15 tests passed successfully",
            outcome="success",
            worker="alpha",
        )
        outcome = verify_episode(ep, verifiers=[SimpleVerifier()])
        assert outcome == Outcome.SUCCESS

    def test_verify_failure_episode(self, isolated_env):
        """SimpleVerifier classifies a failure result correctly."""
        ep = log_episode(
            task="compile project",
            result="Traceback: SyntaxError in main.py line 42",
            outcome="failure",
            worker="beta",
        )
        outcome = verify_episode(ep, verifiers=[SimpleVerifier()])
        assert outcome == Outcome.FAILURE

    def test_verify_unknown_episode(self, isolated_env):
        """SimpleVerifier returns UNKNOWN for ambiguous results."""
        ep = log_episode(
            task="check status",
            result="System is running with 3 pending items",
            outcome="unknown",
            worker="gamma",
        )
        outcome = verify_episode(ep, verifiers=[SimpleVerifier()])
        assert outcome == Outcome.UNKNOWN


# ── 3. LearningStore Tests ─────────────────────────────


class TestLearningStore:
    def test_store_and_recall_fact(self, store):
        """Facts stored via learn() are retrievable via recall()."""
        fact_id = store.learn(
            content="Always run linter before commit",
            category="procedure",
            source="episode:test",
            tags=["lint", "workflow"],
        )
        assert fact_id is not None
        facts = store.recall("linter before commit", top_k=5)
        assert any(f.fact_id == fact_id for f in facts)

    def test_store_stats_increment(self, store):
        """Stats reflect the number of facts stored."""
        initial = store.stats()["total_facts"]
        store.learn(content="fact one", category="concept", source="test")
        store.learn(content="fact two", category="pattern", source="test")
        updated = store.stats()["total_facts"]
        assert updated == initial + 2


# ── 4. Full Pipeline Integration ───────────────────────


class TestFullPipeline:
    def test_episode_to_verification_to_store(self, isolated_env, store):
        """Full pipeline: log → verify → store → recall."""
        # Step 1: Log
        ep = log_episode(
            task="implement caching layer",
            result="Caching layer added. Done with Redis backend",
            outcome="success",
            worker="alpha",
        )

        # Step 2: Verify
        outcome = verify_episode(ep, verifiers=[SimpleVerifier()])
        assert outcome == Outcome.SUCCESS

        # Step 3: Store
        fact_id = store.learn(
            content=f"Caching layer implementation succeeded: {ep['result']}",
            category="procedure",
            source=f"episode:{ep['timestamp']}",
            tags=["caching", "redis"],
        )

        # Step 4: Recall
        facts = store.recall("caching Redis", top_k=5)
        matched = [f for f in facts if f.fact_id == fact_id]
        assert len(matched) == 1
        assert "caching" in matched[0].content.lower()

    def test_failure_pipeline_stores_correction(self, isolated_env, store):
        """Failed episodes store corrections via learn()."""
        ep = log_episode(
            task="deploy to production",
            result="Fatal error: database migration failed, rollback initiated",
            outcome="failure",
            worker="beta",
        )
        outcome = verify_episode(ep, verifiers=[SimpleVerifier()])
        assert outcome == Outcome.FAILURE

        fact_id = store.learn(
            content="Production deploy failed due to database migration -- always run migrations in staging first",
            category="correction",
            source=f"episode:{ep['timestamp']}",
            tags=["deploy", "migration", "failure"],
        )
        facts = store.recall("migration staging deploy", top_k=5)
        assert any(f.fact_id == fact_id for f in facts)

    def test_multiple_episodes_pipeline(self, isolated_env, store):
        """Multiple episodes processed through the full pipeline."""
        episodes_data = [
            ("fix login bug", "Login bug fixed, all auth tests passed", "success"),
            ("update docs", "Documentation updated and complete", "success"),
            ("scale database", "Error: connection pool exhausted, unable to scale", "failure"),
        ]

        results = []
        for task, result, expected in episodes_data:
            ep = log_episode(task=task, result=result, outcome=expected, worker="delta")
            outcome = verify_episode(ep, verifiers=[SimpleVerifier()])
            fact_id = store.learn(
                content=f"{task}: {result}",
                category="procedure" if outcome == Outcome.SUCCESS else "correction",
                source=f"episode:{ep['timestamp']}",
            )
            results.append({"outcome": outcome, "fact_id": fact_id})

        assert results[0]["outcome"] == Outcome.SUCCESS
        assert results[1]["outcome"] == Outcome.SUCCESS
        assert results[2]["outcome"] == Outcome.FAILURE
        assert store.stats()["total_facts"] >= 3


# ── 5. Strategy ID Propagation ─────────────────────────


class TestStrategyPropagation:
    def test_strategy_id_persists_through_pipeline(self, isolated_env, store):
        """strategy_id flows from log → disk → load → verify → store."""
        strategy = "strat-pipeline-001"
        ep = log_episode(
            task="build API endpoint",
            result="Endpoint /api/v2/users created. Done",
            outcome="success",
            strategy_id=strategy,
            worker="alpha",
        )
        assert ep["strategy_id"] == strategy

        # Load from disk and verify strategy persists
        files = list(isolated_env["episodes_dir"].glob("*.json"))
        loaded = load_episode(str(files[0]))
        assert loaded["strategy_id"] == strategy

        # Verify
        outcome = verify_episode(loaded, verifiers=[SimpleVerifier()])
        assert outcome == Outcome.SUCCESS

        # Store with strategy tag
        fact_id = store.learn(
            content=f"Strategy {strategy}: API endpoint created",
            category="procedure",
            source=f"strategy:{strategy}",
            tags=[strategy],
        )
        facts = store.recall(strategy, top_k=5)
        assert any(f.fact_id == fact_id for f in facts)

    def test_query_episodes_by_strategy(self, isolated_env):
        """query_episodes_by_strategy() returns matching episodes."""
        strategy = "strat-query-test-42"
        log_episode(task="sub-task 1", result="done", outcome="success",
                    strategy_id=strategy, worker="alpha")
        log_episode(task="sub-task 2", result="done", outcome="success",
                    strategy_id=strategy, worker="beta")
        log_episode(task="unrelated", result="done", outcome="success",
                    strategy_id="other-strat", worker="gamma")

        matched = query_episodes_by_strategy(strategy)
        assert len(matched) == 2
        assert all(ep["strategy_id"] == strategy for ep in matched)

    def test_query_episodes_by_strategy_empty(self, isolated_env):
        """query_episodes_by_strategy() returns empty for unknown strategy."""
        matched = query_episodes_by_strategy("nonexistent-strategy-xyz")
        assert matched == []

    def test_strategy_id_none_not_matched(self, isolated_env):
        """Episodes without strategy_id are not returned by strategy query."""
        log_episode(task="no-strategy task", result="done", outcome="success",
                    worker="delta")
        matched = query_episodes_by_strategy("any-strategy")
        assert len(matched) == 0
