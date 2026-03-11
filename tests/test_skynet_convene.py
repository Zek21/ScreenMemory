"""
Tests for tools/skynet_convene.py -- Multi-worker collaboration sessions,
ConveneGate governance, consensus voting, and orchestration.
# signed: delta
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tools.skynet_convene as sc

ARCH_BACKED_ENGINE_REPORT = (
    "tools/engine_metrics.py _probe() instantiates analyzer, embedder, ocr, and capture during "
    "collect_engine_metrics() via the /engines path, so constructor cost dominates probe latency. "
    "Use import_only in _PROBES for those slow engines instead of instantiating them on every probe."
)
ALT_BACKED_ENGINE_REPORT = (
    "tools/engine_metrics.py _probe() instantiates analyzer, embedder, ocr, and capture during "
    "collect_engine_metrics() on the /engines path, so constructor cost dominates the metrics probe. "
    "Use import_only in _PROBES for those engines instead of instantiating them on every probe."
)

ARCH_BACKED_CACHE_REPORT = (
    "dashboard.html pollEngines() renders /engines tiles from engine_metrics.collect_engine_metrics() "
    "but ignores timestamp/cache staleness, so status cards present cached engine data as fresh. "
    "Add a cache-age badge when timestamp age exceeds one second."
)

ARCH_UNBACKED_AUTH_REPORT = (
    "Auth module tokens are not rotated after refresh; session fixation risk remains active."
)
ALT_UNBACKED_AUTH_REPORT = (
    "Auth refresh token rotation is missing after refresh requests; session fixation risk remains active."
)


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_sessions(tmp_path, monkeypatch):
    """Redirect session/gate files to tmp dir for every test."""
    sessions_file = tmp_path / "convene_sessions.json"
    gate_file = tmp_path / "convene_gate.json"
    todos_file = tmp_path / "todos.json"
    todos_file.write_text(json.dumps({"todos": [], "version": 1}), encoding="utf-8")
    monkeypatch.setattr(sc, "SESSIONS_FILE", sessions_file)
    monkeypatch.setattr(sc, "GATE_FILE", gate_file)
    monkeypatch.setattr(sc, "TODOS_FILE", todos_file)
    monkeypatch.setattr(sc, "DATA", tmp_path)
    import tools.skynet_todos as todos
    monkeypatch.setattr(todos, "TODOS_FILE", todos_file)
    yield tmp_path
    # signed: delta


def _ok_response(json_data=None, status_code=200):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.ok = status_code < 400
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    return resp
    # signed: delta


def _error_response(status_code=500):
    return _ok_response(status_code=status_code)


# ══════════════════════════════════════════════════════════════════
# Section 1: Session Lifecycle Functions
# ══════════════════════════════════════════════════════════════════

class TestInitiateConvene:
    """Tests for initiate_convene()."""

    @patch("tools.skynet_convene.requests.post")
    def test_success_returns_session_id(self, mock_post):
        mock_post.return_value = _ok_response({"session_id": "sess_123"})
        sid = sc.initiate_convene("alpha", "review", "audit code", 3)
        assert sid == "sess_123"
        mock_post.assert_called_once()
        body = mock_post.call_args[1]["json"]
        assert body["initiator"] == "alpha"
        assert body["topic"] == "review"
        assert body["context"] == "audit code"
        assert body["need_workers"] == 3
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_http_error_returns_none(self, mock_post):
        mock_post.return_value = _error_response(500)
        assert sc.initiate_convene("alpha", "t", "c") is None
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_network_error_returns_none(self, mock_post):
        import requests as req
        mock_post.side_effect = req.RequestException("conn refused")
        assert sc.initiate_convene("alpha", "t", "c") is None
        # signed: delta


class TestDiscoverSessions:
    """Tests for discover_sessions()."""

    @patch("tools.skynet_convene.requests.get")
    def test_returns_session_list(self, mock_get):
        sessions = [{"id": "s1", "status": "active"}, {"id": "s2", "status": "resolved"}]
        mock_get.return_value = _ok_response(sessions)
        result = sc.discover_sessions()
        assert len(result) == 2
        assert result[0]["id"] == "s1"
        # signed: delta

    @patch("tools.skynet_convene.requests.get")
    def test_non_list_returns_empty(self, mock_get):
        mock_get.return_value = _ok_response({"not": "a list"})
        assert sc.discover_sessions() == []
        # signed: delta

    @patch("tools.skynet_convene.requests.get")
    def test_network_error_returns_empty(self, mock_get):
        import requests as req
        mock_get.side_effect = req.RequestException("timeout")
        assert sc.discover_sessions() == []
        # signed: delta


class TestJoinSession:
    """Tests for join_session()."""

    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.requests.patch")
    def test_join_posts_patch_and_publish(self, mock_patch, mock_post):
        mock_patch.return_value = _ok_response()
        mock_post.return_value = _ok_response()
        result = sc.join_session("beta", "sess_123")
        assert result is True
        mock_patch.assert_called_once()
        patch_body = mock_patch.call_args[1]["json"]
        assert patch_body["session_id"] == "sess_123"
        assert patch_body["worker"] == "beta"
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.requests.patch")
    def test_join_network_error(self, mock_patch, mock_post):
        import requests as req
        mock_patch.side_effect = req.RequestException("fail")
        assert sc.join_session("beta", "s1") is False
        # signed: delta


class TestPostUpdate:
    """Tests for post_update()."""

    @patch("tools.skynet_convene.requests.post")
    def test_posts_update_message(self, mock_post):
        mock_post.return_value = _ok_response()
        result = sc.post_update("gamma", "sess_1", "my findings")
        assert result is True
        body = mock_post.call_args[1]["json"]
        assert body["sender"] == "gamma"
        assert body["type"] == "update"
        payload = json.loads(body["content"])
        assert payload["session_id"] == "sess_1"
        assert payload["content"] == "my findings"
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_network_error_returns_false(self, mock_post):
        import requests as req
        mock_post.side_effect = req.RequestException("err")
        assert sc.post_update("gamma", "s1", "x") is False
        # signed: delta


class TestResolveSession:
    """Tests for resolve_session()."""

    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.requests.delete")
    def test_resolve_deletes_and_publishes(self, mock_del, mock_post):
        mock_del.return_value = _ok_response()
        mock_post.return_value = _ok_response()
        result = sc.resolve_session("alpha", "sess_1", "all done")
        assert result is True
        mock_del.assert_called_once()
        assert "sess_1" in mock_del.call_args[0][0]
        body = mock_post.call_args[1]["json"]
        assert body["type"] == "resolve"
        payload = json.loads(body["content"])
        assert payload["summary"] == "all done"
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 2: Auto-Discovery (poll_and_join)
# ══════════════════════════════════════════════════════════════════

class TestPollAndJoin:
    """Tests for poll_and_join()."""

    @patch("tools.skynet_convene.join_session", return_value=True)
    @patch("tools.skynet_convene.discover_sessions")
    def test_joins_active_sessions_needing_workers(self, mock_disc, mock_join):
        mock_disc.return_value = [
            {"id": "s1", "status": "active", "participants": ["alpha"], "need_workers": 3},
        ]
        joined = sc.poll_and_join("beta")
        assert joined == ["s1"]
        mock_join.assert_called_once_with("beta", "s1")
        # signed: delta

    @patch("tools.skynet_convene.join_session")
    @patch("tools.skynet_convene.discover_sessions")
    def test_skips_resolved_sessions(self, mock_disc, mock_join):
        mock_disc.return_value = [
            {"id": "s1", "status": "resolved", "participants": [], "need_workers": 2},
        ]
        joined = sc.poll_and_join("beta")
        assert joined == []
        mock_join.assert_not_called()
        # signed: delta

    @patch("tools.skynet_convene.join_session")
    @patch("tools.skynet_convene.discover_sessions")
    def test_skips_already_joined(self, mock_disc, mock_join):
        mock_disc.return_value = [
            {"id": "s1", "status": "active", "participants": ["beta"], "need_workers": 2},
        ]
        joined = sc.poll_and_join("beta")
        assert joined == []
        # signed: delta

    @patch("tools.skynet_convene.join_session")
    @patch("tools.skynet_convene.discover_sessions")
    def test_skips_full_sessions(self, mock_disc, mock_join):
        mock_disc.return_value = [
            {"id": "s1", "status": "active", "participants": ["alpha", "gamma"], "need_workers": 2},
        ]
        joined = sc.poll_and_join("beta")
        assert joined == []
        # signed: delta

    @patch("tools.skynet_convene.join_session", return_value=True)
    @patch("tools.skynet_convene.discover_sessions")
    def test_interest_filter_matches(self, mock_disc, mock_join):
        mock_disc.return_value = [
            {"id": "s1", "status": "active", "participants": [], "need_workers": 2,
             "topic": "security audit", "context": "review auth"},
            {"id": "s2", "status": "active", "participants": [], "need_workers": 2,
             "topic": "docs update", "context": "readme"},
        ]
        joined = sc.poll_and_join("beta", interests=["security"])
        assert joined == ["s1"]
        # signed: delta

    @patch("tools.skynet_convene.join_session", return_value=True)
    @patch("tools.skynet_convene.discover_sessions")
    def test_no_interests_joins_all(self, mock_disc, mock_join):
        mock_disc.return_value = [
            {"id": "s1", "status": "active", "participants": [], "need_workers": 2,
             "topic": "test", "context": ""},
            {"id": "s2", "status": "active", "participants": [], "need_workers": 2,
             "topic": "docs", "context": ""},
        ]
        joined = sc.poll_and_join("beta")
        assert len(joined) == 2
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 3: Persistent Session Store
# ══════════════════════════════════════════════════════════════════

class TestSessionPersistence:
    """Tests for _load_sessions / _save_sessions."""

    def test_load_empty_returns_dict(self, clean_sessions):
        result = sc._load_sessions()
        assert result == {}
        # signed: delta

    def test_save_and_load_roundtrip(self, clean_sessions):
        data = {"s1": {"id": "s1", "topic": "test", "status": "active"}}
        sc._save_sessions(data)
        loaded = sc._load_sessions()
        assert loaded["s1"]["topic"] == "test"
        # signed: delta

    def test_load_corrupt_json_returns_empty(self, clean_sessions):
        sc.SESSIONS_FILE.write_text("not json", encoding="utf-8")
        result = sc._load_sessions()
        assert result == {}
        # signed: delta

    def test_save_creates_data_dir(self, tmp_path):
        subdir = tmp_path / "nested" / "data"
        sc.DATA = subdir
        sc.SESSIONS_FILE = subdir / "convene_sessions.json"
        sc._save_sessions({"x": {"id": "x"}})
        assert sc.SESSIONS_FILE.exists()
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 4: ConveneSession Class
# ══════════════════════════════════════════════════════════════════

class TestConveneSessionClass:
    """Tests for ConveneSession."""

    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.initiate_convene", return_value="sess_42")
    def test_initiate_creates_and_persists(self, mock_init, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        cs = sc.ConveneSession()
        sid = cs.initiate("review", ["alpha", "beta"], "audit code")
        assert sid == "sess_42"
        assert cs.session_id == "sess_42"
        sessions = sc._load_sessions()
        assert "sess_42" in sessions
        assert sessions["sess_42"]["status"] == "active"
        assert sessions["sess_42"]["participants"] == ["alpha", "beta"]
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.initiate_convene", return_value=None)
    def test_initiate_fallback_local_id(self, mock_init, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        cs = sc.ConveneSession()
        sid = cs.initiate("review", ["alpha"], "ctx")
        assert sid.startswith("local_")
        # signed: delta

    @patch("tools.skynet_convene.post_update")
    def test_contribute_and_get(self, mock_update, clean_sessions):
        # Set up session
        sc._save_sessions({"s1": {
            "id": "s1", "topic": "t", "participants": ["alpha", "beta"],
            "contributions": {}, "votes": {}, "status": "active", "created_at": 0,
        }})
        cs = sc.ConveneSession("s1")
        result = cs.contribute("alpha", "my analysis")
        assert result is True
        contribs = cs.get_contributions()
        assert "alpha" in contribs
        assert contribs["alpha"]["content"] == "my analysis"
        # signed: delta

    @patch("tools.skynet_convene.post_update")
    def test_contribute_missing_session_returns_false(self, mock_update, clean_sessions):
        cs = sc.ConveneSession("nonexistent")
        assert cs.contribute("alpha", "x") is False
        # signed: delta

    @patch("tools.skynet_convene.resolve_session")
    def test_resolve_with_custom_summary(self, mock_resolve, clean_sessions):
        sc._save_sessions({"s1": {
            "id": "s1", "topic": "t", "participants": ["a"],
            "contributions": {"a": {"content": "done"}},
            "votes": {}, "status": "active", "created_at": 0,
        }})
        cs = sc.ConveneSession("s1")
        summary = cs.resolve("custom summary")
        assert summary == "custom summary"
        sessions = sc._load_sessions()
        assert sessions["s1"]["status"] == "resolved"
        assert sessions["s1"]["resolution"] == "custom summary"
        assert "resolved_at" in sessions["s1"]
        # signed: delta

    @patch("tools.skynet_convene.resolve_session")
    def test_resolve_auto_summary(self, mock_resolve, clean_sessions):
        sc._save_sessions({"s1": {
            "id": "s1", "topic": "t", "participants": ["a", "b"],
            "contributions": {
                "a": {"content": "analysis A"},
                "b": {"content": "analysis B"},
            },
            "votes": {}, "status": "active", "created_at": 0,
        }})
        cs = sc.ConveneSession("s1")
        summary = cs.resolve()
        assert "a:" in summary
        assert "b:" in summary
        # signed: delta

    @patch("tools.skynet_convene.resolve_session")
    def test_resolve_missing_session(self, mock_resolve, clean_sessions):
        cs = sc.ConveneSession("nope")
        assert cs.resolve() == "Session not found"
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 5: ConveneGate Governance
# ══════════════════════════════════════════════════════════════════

class TestConveneGate:
    """Tests for ConveneGate governance protocol."""

    def test_initial_state(self, clean_sessions):
        gate = sc.ConveneGate()
        assert gate.get_pending() == {}
        stats = gate.get_stats()
        assert stats["total_proposed"] == 0
        assert stats["total_elevated"] == 0
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_propose_creates_pending(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        result = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        assert result["action"] == "proposed"
        assert "gate_id" in result
        assert result["votes"] == 1  # auto-YES from proposer
        assert result["needed"] == 2
        pending = gate.get_pending()
        assert len(pending) == 1
        gid = result["gate_id"]
        assert pending[gid]["proposer"] == "alpha"
        assert pending[gid]["votes"]["alpha"] == "YES"
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_propose_urgent_bypasses_gate(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        result = gate.propose("alpha", "CRITICAL: system down", urgent=True)
        assert result["action"] == "bypassed"
        assert result["delivered"] is True
        assert gate.get_pending() == {}
        assert gate.get_stats()["total_bypassed"] == 1
        # verify bus message was posted with [URGENT BYPASS]
        body = mock_post.call_args[1]["json"]
        assert body["type"] == "urgent"
        assert "[URGENT BYPASS]" in body["content"]
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_vote_gate_approve_elevates(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        r1 = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gid = r1["gate_id"]
        # Alpha already voted YES (auto). Beta votes YES → majority (2)
        with patch("tools.skynet_convene.requests.post", return_value=_ok_response()):
            r2 = gate.vote_gate(gid, "beta", approve=True)
        assert r2["action"] == "elevated"
        assert "alpha" in r2["voters"]
        assert "beta" in r2["voters"]
        assert r2["delivery_type"] == "elevated_digest"
        assert r2["queued"] is True
        assert gate.get_pending() == {}  # removed from pending
        gate._state = gate._load()
        assert len(gate._state["delivery_queue"]) == 1
        assert gate._state["delivery_queue"][0]["status"] == "pending"
        assert not any(
            kwargs["json"].get("topic") == "orchestrator"
            and kwargs["json"].get("sender") == "convene-gate"
            for _args, kwargs in mock_post.call_args_list
        )
        stats = gate.get_stats()
        assert stats["total_elevated"] == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_vote_gate_reject(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        # Create proposal, then manually set up the state for rejection
        r1 = gate.propose("alpha", "Trivial variable rename in docs header with no functional value.")
        gid = r1["gate_id"]
        # Change alpha's vote to NO, then beta votes NO → majority rejection
        gate._state = gate._load()
        gate._state["pending"][gid]["votes"]["alpha"] = "NO"
        gate._save()
        with patch("tools.skynet_convene.requests.post", return_value=_ok_response()):
            r2 = gate.vote_gate(gid, "beta", approve=False)
        assert r2["action"] == "rejected"
        assert gate.get_pending() == {}
        assert gate.get_stats()["total_rejected"] == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_vote_gate_not_found(self, mock_post, clean_sessions):
        gate = sc.ConveneGate()
        result = gate.vote_gate("nonexistent", "alpha", True)
        assert "error" in result
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_vote_gate_already_resolved(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        r1 = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gid = r1["gate_id"]
        # Manually mark as elevated
        gate._state["pending"][gid]["status"] = "elevated"
        gate._save()
        result = gate.vote_gate(gid, "beta", True)
        assert "error" in result
        assert "already" in result["error"]
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_vote_gate_partial_no_action_yet(self, mock_post, clean_sessions):
        """Vote that doesn't reach majority keeps proposal pending."""
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        # Raise threshold so 2 isn't enough
        gate.MAJORITY_THRESHOLD = 3
        r1 = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gid = r1["gate_id"]
        r2 = gate.vote_gate(gid, "beta", approve=True)
        assert r2["action"] == "voted"
        assert r2["yes"] == 2
        assert r2["needed"] == 3
        assert len(gate.get_pending()) == 1
        gate.MAJORITY_THRESHOLD = 2  # restore
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_generic_report_is_queued_for_cross_validation(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        result = gate.propose("alpha", "fix needed")
        assert result["action"] == "queued_for_cross_validation"
        assert result["reason"] == "generic_report"
        assert gate.get_pending() == {}
        assert gate.get_stats()["total_queued"] == 1
        assert len(gate._state["queued"]) == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_architecture_sensitive_report_without_backing_is_queued_for_architecture_review(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        result = gate.propose("alpha", ARCH_UNBACKED_AUTH_REPORT)
        assert result["action"] == "queued_for_architecture_review"
        assert result["reason"] == "architecture_review_required"
        assert result["review_kind"] == "architecture_review"
        assert gate.get_pending() == {}
        assert gate.get_stats()["total_architecture_review_queued"] == 1
        assert gate._state["queued"][0]["review_kind"] == "architecture_review"
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_semantic_repeat_of_architecture_review_is_suppressed(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        first = gate.propose("alpha", ARCH_UNBACKED_AUTH_REPORT)
        assert first["action"] == "queued_for_architecture_review"
        second = gate.propose("beta", ALT_UNBACKED_AUTH_REPORT)
        assert second["action"] == "suppressed"
        assert second["reason"] == "action_detected"
        assert second["issue_key"]
        assert gate.get_stats()["total_architecture_review_queued"] == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.get")
    @patch("tools.skynet_convene.requests.post")
    def test_duplicate_elevation_suppressed_for_fifteen_minutes_without_action(self, mock_post, mock_get, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_get.return_value = _ok_response([])
        gate = sc.ConveneGate()
        report = ARCH_BACKED_ENGINE_REPORT
        gid = gate.propose("alpha", report)["gate_id"]
        gate.vote_gate(gid, "beta", approve=True)

        result = gate.propose("alpha", report)
        assert result["action"] == "suppressed"
        assert result["reason"] == "awaiting_action_cooldown"
        assert result["retry_after_s"] > 0
        assert gate.get_stats()["total_suppressed"] == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.get")
    @patch("tools.skynet_convene.requests.post")
    def test_duplicate_elevation_allowed_after_cooldown_without_action(self, mock_post, mock_get, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_get.return_value = _ok_response([])
        gate = sc.ConveneGate()
        report = ARCH_BACKED_CACHE_REPORT
        gid = gate.propose("alpha", report)["gate_id"]
        gate.vote_gate(gid, "beta", approve=True)

        gate._state = gate._load()
        fingerprint = gate._report_fingerprint(report)
        gate._state["active_findings"][fingerprint]["last_elevated_at"] = time.time() - 901
        gate._save()

        result = gate.propose("alpha", report)
        assert result["action"] == "proposed"
        assert "gate_id" in result
        # signed: delta

    @patch("tools.skynet_convene.requests.get")
    @patch("tools.skynet_convene.requests.post")
    def test_semantically_equivalent_architecture_reports_share_cooldown(self, mock_post, mock_get, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_get.return_value = _ok_response([])
        gate = sc.ConveneGate()
        gid = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)["gate_id"]
        gate.vote_gate(gid, "beta", approve=True)

        result = gate.propose("alpha", ALT_BACKED_ENGINE_REPORT)
        assert result["action"] == "suppressed"
        assert result["reason"] == "awaiting_action_cooldown"
        assert result["issue_key"]
        # signed: delta

    @patch("skynet_delivery.deliver_elevated_digest")
    @patch("tools.skynet_convene.requests.get")
    @patch("tools.skynet_convene.requests.post")
    def test_digest_not_sent_before_half_hour_window(self, mock_post, mock_get, mock_deliver, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_get.return_value = _ok_response([])
        mock_deliver.return_value = {
            "success": True,
            "delivery_type": "elevated_digest",
            "count": 1,
        }
        gate = sc.ConveneGate()
        gid = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)["gate_id"]
        gate.vote_gate(gid, "beta", approve=True)

        result = gate.flush_due_digest()
        assert result["action"] == "noop"
        assert result["reason"] == "cooldown"
        assert result["retry_after_s"] > 0
        mock_deliver.assert_not_called()
        # signed: delta

    @patch("skynet_delivery.deliver_elevated_digest")
    @patch("tools.skynet_convene.requests.get")
    @patch("tools.skynet_convene.requests.post")
    def test_digest_delivers_consolidated_half_hour_batch(self, mock_post, mock_get, mock_deliver, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_get.return_value = _ok_response([])
        mock_deliver.return_value = {
            "success": True,
            "delivery_type": "elevated_digest",
            "count": 2,
        }
        gate = sc.ConveneGate()
        gid1 = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)["gate_id"]
        gate.vote_gate(gid1, "beta", approve=True)
        gid2 = gate.propose("gamma", ARCH_BACKED_CACHE_REPORT)["gate_id"]
        gate.vote_gate(gid2, "delta", approve=True)

        gate._state = gate._load()
        for entry in gate._state["delivery_queue"]:
            entry["queued_at"] = time.time() - 1801
            entry["last_elevated_at"] = time.time() - 1801
        gate._save()

        result = gate.flush_due_digest()
        assert result["action"] == "delivered"
        assert result["delivery_type"] == "elevated_digest"
        assert result["count"] == 2
        mock_deliver.assert_called_once()
        delivered_entries = mock_deliver.call_args.args[0]
        assert len(delivered_entries) == 2
        gate._state = gate._load()
        assert all(entry["status"] == "delivered" for entry in gate._state["delivery_queue"])
        assert gate.get_stats()["total_digest_deliveries"] == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.get")
    @patch("tools.skynet_convene.requests.post")
    def test_repeat_issue_merges_into_single_pending_digest_entry(self, mock_post, mock_get, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_get.return_value = _ok_response([])
        gate = sc.ConveneGate()
        gid = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)["gate_id"]
        gate.vote_gate(gid, "beta", approve=True)

        gate._state = gate._load()
        fingerprint = gate._report_fingerprint(ARCH_BACKED_ENGINE_REPORT)
        gate._state["active_findings"][fingerprint]["last_elevated_at"] = time.time() - 901
        gate._save()

        gid2 = gate.propose("alpha", ALT_BACKED_ENGINE_REPORT)["gate_id"]
        gate.vote_gate(gid2, "beta", approve=True)

        gate._state = gate._load()
        pending_entries = [entry for entry in gate._state["delivery_queue"] if entry["status"] == "pending"]
        assert len(pending_entries) == 1
        assert pending_entries[0]["repeat_count"] == 2
        assert pending_entries[0]["issue_key"]
        # signed: delta

    @patch("tools.skynet_convene.requests.get")
    @patch("tools.skynet_convene.requests.post")
    def test_duplicate_is_suppressed_when_action_detected(self, mock_post, mock_get, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        report = ARCH_BACKED_ENGINE_REPORT
        gid = gate.propose("alpha", report)["gate_id"]
        gate.vote_gate(gid, "beta", approve=True)

        after = datetime.now(timezone.utc).isoformat()
        mock_get.return_value = _ok_response([
            {
                "id": "msg_x",
                "sender": "orchestrator",
                "topic": "workers",
                "type": "directive",
                "content": "Dispatch engine_metrics.py _probe() import_only fix for analyzer embedder ocr capture immediately.",
                "timestamp": after,
            }
        ])

        result = gate.propose("alpha", report)
        assert result["action"] == "suppressed"
        assert result["reason"] == "action_detected"
        gate._state = gate._load()
        fingerprint = gate._report_fingerprint(report)
        assert gate._state["active_findings"][fingerprint]["action_taken"] is True
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_expire_stale_proposals(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        r1 = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gid = r1["gate_id"]
        # Backdate the proposal
        gate._state["pending"][gid]["created_at"] = time.time() - 600
        gate._save()
        expired = gate.expire_stale(max_age_s=300)
        assert gid in expired
        assert gate.get_pending() == {}
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_expire_keeps_fresh(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        gate.propose("alpha", ARCH_BACKED_CACHE_REPORT)
        expired = gate.expire_stale(max_age_s=300)
        assert expired == []
        assert len(gate.get_pending()) == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_stats_increment(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        gate = sc.ConveneGate()
        gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gate.propose("beta", ARCH_BACKED_CACHE_REPORT)
        gate.propose("gamma", "CRITICAL system down", urgent=True)
        stats = gate.get_stats()
        assert stats["total_proposed"] == 3
        assert stats["total_bypassed"] == 1
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 6: Consensus Voting (vote & consensus functions)
# ══════════════════════════════════════════════════════════════════

class TestVoting:
    """Tests for vote() and consensus()."""

    @patch("tools.skynet_convene.requests.post")
    def test_vote_valid_choices(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        sc._save_sessions({"s1": {
            "id": "s1", "participants": ["alpha", "beta", "gamma", "delta"],
            "votes": {}, "status": "active",
        }})
        assert sc.vote("s1", "alpha", "use REST", "YES") is True
        assert sc.vote("s1", "beta", "use REST", "NO") is True
        assert sc.vote("s1", "gamma", "use REST", "ABSTAIN") is True
        sessions = sc._load_sessions()
        votes = sessions["s1"]["votes"]["use REST"]
        assert votes["alpha"]["choice"] == "YES"
        assert votes["beta"]["choice"] == "NO"
        assert votes["gamma"]["choice"] == "ABSTAIN"
        # signed: delta

    def test_vote_invalid_choice(self, clean_sessions):
        assert sc.vote("s1", "alpha", "p", "MAYBE") is False
        # signed: delta

    def test_vote_missing_session(self, clean_sessions):
        assert sc.vote("nonexistent", "alpha", "p", "YES") is False
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_vote_creates_votes_dict_if_missing(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        sc._save_sessions({"s1": {"id": "s1", "participants": ["a"], "status": "active"}})
        assert sc.vote("s1", "alpha", "proposal1", "YES") is True
        sessions = sc._load_sessions()
        assert "votes" in sessions["s1"]
        assert "proposal1" in sessions["s1"]["votes"]
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_consensus_approved(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        sc._save_sessions({"s1": {
            "id": "s1", "participants": ["a", "b", "c", "d"],
            "votes": {"p1": {
                "a": {"choice": "YES"},
                "b": {"choice": "YES"},
                "c": {"choice": "NO"},
            }},
        }})
        result = sc.consensus("s1", "p1")
        assert result["result"] == "APPROVED"
        assert result["quorum_met"] is True
        assert result["yes"] == 2
        assert result["no"] == 1
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_consensus_rejected(self, mock_post, clean_sessions):
        mock_post.return_value = _ok_response()
        sc._save_sessions({"s1": {
            "id": "s1", "participants": ["a", "b", "c", "d"],
            "votes": {"p1": {
                "a": {"choice": "NO"},
                "b": {"choice": "NO"},
                "c": {"choice": "YES"},
            }},
        }})
        result = sc.consensus("s1", "p1")
        assert result["result"] == "REJECTED"
        assert result["no"] == 2
        # signed: delta

    def test_consensus_pending_no_quorum(self, clean_sessions):
        sc._save_sessions({"s1": {
            "id": "s1", "participants": ["a", "b", "c", "d"],
            "votes": {"p1": {"a": {"choice": "YES"}}},
        }})
        result = sc.consensus("s1", "p1")
        assert result["result"] == "PENDING"
        assert result["quorum_met"] is False
        assert result["votes_cast"] == 1
        # signed: delta

    def test_consensus_tied(self, clean_sessions):
        sc._save_sessions({"s1": {
            "id": "s1", "participants": ["a", "b", "c", "d"],
            "votes": {"p1": {
                "a": {"choice": "YES"},
                "b": {"choice": "NO"},
                "c": {"choice": "ABSTAIN"},
            }},
        }})
        result = sc.consensus("s1", "p1")
        assert result["result"] == "TIED"
        assert result["yes"] == 1
        assert result["no"] == 1
        assert result["abstain"] == 1
        # signed: delta

    def test_consensus_missing_session(self, clean_sessions):
        result = sc.consensus("nope", "p1")
        assert result["result"] == "PENDING"
        assert result["votes_cast"] == 0
        # signed: delta

    def test_consensus_quorum_calculation(self, clean_sessions):
        """With 4 participants at 50% quorum, need 3 votes (int(4*50/100)+1=3)."""
        sc._save_sessions({"s1": {
            "id": "s1", "participants": ["a", "b", "c", "d"],
            "votes": {"p1": {
                "a": {"choice": "YES"},
                "b": {"choice": "YES"},
            }},
        }})
        result = sc.consensus("s1", "p1", quorum_pct=50.0)
        assert result["quorum_needed"] == 3
        assert result["votes_cast"] == 2
        assert result["quorum_met"] is False
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 7: Orchestrator-Managed Convene
# ══════════════════════════════════════════════════════════════════

class TestDispatchConveneTasks:
    """Tests for _dispatch_convene_tasks()."""

    @patch("tools.skynet_convene.requests.post")
    def test_dispatches_to_all_workers(self, mock_post):
        mock_post.return_value = _ok_response()
        sc._dispatch_convene_tasks(["alpha", "beta"], "sess_1", "do the thing")
        assert mock_post.call_count == 2
        bodies = [c[1]["json"] for c in mock_post.call_args_list]
        workers_dispatched = [json.loads(b["content"])["worker"] for b in bodies]
        assert set(workers_dispatched) == {"alpha", "beta"}
        # signed: delta

    @patch("tools.skynet_convene.requests.post")
    def test_handles_dispatch_failure(self, mock_post):
        mock_post.side_effect = Exception("network down")
        # Should not raise
        sc._dispatch_convene_tasks(["alpha"], "s1", "task")
        # signed: delta


class TestOrchestrateConvene:
    """Tests for orchestrate_convene()."""

    @patch("tools.skynet_convene.resolve_session")
    @patch("tools.skynet_convene._collect_contributions")
    @patch("tools.skynet_convene._dispatch_convene_tasks")
    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.initiate_convene", return_value="orch_sess_1")
    def test_full_orchestrate_flow(self, mock_init, mock_post, mock_dispatch,
                                   mock_collect, mock_resolve, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_collect.return_value = []  # no missing workers
        result = sc.orchestrate_convene("code review", "review all files", timeout=10,
                                        workers=["alpha", "beta"])
        assert result["session_id"] == "orch_sess_1"
        assert result["topic"] == "code review"
        assert result["missing"] == []
        mock_dispatch.assert_called_once()
        # signed: delta

    @patch("tools.skynet_convene.resolve_session")
    @patch("tools.skynet_convene._collect_contributions")
    @patch("tools.skynet_convene._dispatch_convene_tasks")
    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.initiate_convene", return_value="s2")
    def test_orchestrate_with_missing_workers(self, mock_init, mock_post,
                                              mock_dispatch, mock_collect,
                                              mock_resolve, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_collect.return_value = ["gamma"]  # gamma didn't contribute
        result = sc.orchestrate_convene("audit", "scan", workers=["alpha", "gamma"])
        assert result["missing"] == ["gamma"]
        # signed: delta

    @patch("tools.skynet_convene.resolve_session")
    @patch("tools.skynet_convene._collect_contributions")
    @patch("tools.skynet_convene._dispatch_convene_tasks")
    @patch("tools.skynet_convene.requests.post")
    @patch("tools.skynet_convene.initiate_convene", return_value="s3")
    def test_orchestrate_uses_default_workers(self, mock_init, mock_post,
                                              mock_dispatch, mock_collect,
                                              mock_resolve, clean_sessions):
        mock_post.return_value = _ok_response()
        mock_collect.return_value = []
        sc.orchestrate_convene("topic", "task")
        dispatch_args = mock_dispatch.call_args[0]
        assert dispatch_args[0] == sc.WORKER_NAMES  # all 4 workers
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 8: collect_updates & _wait_for_participants
# ══════════════════════════════════════════════════════════════════

class TestCollectUpdates:
    """Tests for collect_updates() with mocked time."""

    @patch("tools.skynet_convene.time.sleep")
    @patch("tools.skynet_convene.time.time")
    @patch("tools.skynet_convene.requests.get")
    def test_collects_matching_updates(self, mock_get, mock_time, mock_sleep):
        # Simulate: first poll returns 2 updates, second poll returns same (dedup)
        mock_time.side_effect = [0, 0, 10, 35]  # start, check, check, > deadline
        bus_msgs = [
            {"id": "m1", "type": "update", "sender": "alpha",
             "content": json.dumps({"session_id": "s1", "content": "analysis A"})},
            {"id": "m2", "type": "update", "sender": "beta",
             "content": json.dumps({"session_id": "s1", "content": "analysis B"})},
            {"id": "m3", "type": "update", "sender": "gamma",
             "content": json.dumps({"session_id": "s2", "content": "wrong session"})},
        ]
        mock_get.return_value = _ok_response(bus_msgs)
        result = sc.collect_updates("s1", timeout=30, expected=2)
        assert len(result) == 2
        assert result[0]["sender"] == "alpha"
        assert result[1]["sender"] == "beta"
        # signed: delta

    @patch("tools.skynet_convene.time.sleep")
    @patch("tools.skynet_convene.time.time")
    @patch("tools.skynet_convene.requests.get")
    def test_deduplicates_message_ids(self, mock_get, mock_time, mock_sleep):
        mock_time.side_effect = [0, 0, 5, 35]
        msg = {"id": "m1", "type": "update", "sender": "alpha",
               "content": json.dumps({"session_id": "s1", "content": "x"})}
        mock_get.return_value = _ok_response([msg, msg])  # duplicate
        result = sc.collect_updates("s1", timeout=30, expected=2)
        assert len(result) == 1
        # signed: delta


class TestWaitForParticipants:
    """Tests for _wait_for_participants()."""

    @patch("tools.skynet_convene.time.sleep")
    @patch("tools.skynet_convene.time.time")
    @patch("tools.skynet_convene.requests.get")
    def test_collects_join_messages(self, mock_get, mock_time, mock_sleep):
        mock_time.side_effect = [0, 0, 35]
        join_msg = {"id": "j1", "type": "join", "sender": "beta",
                    "content": json.dumps({"session_id": "s1", "worker": "beta"})}
        mock_get.return_value = _ok_response([join_msg])
        result = sc._wait_for_participants("s1", need=1, timeout=30)
        assert result == ["beta"]
        # signed: delta

    @patch("tools.skynet_convene.time.sleep")
    @patch("tools.skynet_convene.time.time")
    @patch("tools.skynet_convene.requests.get")
    def test_deduplicates_participants(self, mock_get, mock_time, mock_sleep):
        mock_time.side_effect = [0, 0, 5, 35]
        msgs = [
            {"id": "j1", "type": "join", "sender": "beta",
             "content": json.dumps({"session_id": "s1", "worker": "beta"})},
            {"id": "j2", "type": "join", "sender": "beta",
             "content": json.dumps({"session_id": "s1", "worker": "beta"})},
        ]
        mock_get.return_value = _ok_response(msgs)
        result = sc._wait_for_participants("s1", need=2, timeout=30)
        assert len(result) == 1  # beta counted once
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 9: convene_and_work high-level workflow
# ══════════════════════════════════════════════════════════════════

class TestConveneAndWork:
    """Tests for convene_and_work()."""

    @patch("tools.skynet_convene.resolve_session")
    @patch("tools.skynet_convene.collect_updates")
    @patch("tools.skynet_convene.post_update")
    @patch("tools.skynet_convene._wait_for_participants")
    @patch("tools.skynet_convene.initiate_convene")
    def test_full_workflow(self, mock_init, mock_wait, mock_post,
                          mock_collect, mock_resolve):
        mock_init.return_value = "sess_w1"
        mock_wait.return_value = ["beta"]
        mock_post.return_value = True
        mock_collect.return_value = [
            {"sender": "beta", "content": "beta result", "timestamp": "t1"}
        ]
        mock_resolve.return_value = True

        def work_fn(sid, ctx, participants):
            return f"alpha did {ctx}"

        sid = sc.convene_and_work("alpha", "review", "audit", work_fn)
        assert sid == "sess_w1"
        mock_init.assert_called_once_with("alpha", "review", "audit", 2)
        mock_post.assert_called_once_with("alpha", "sess_w1", "alpha did audit")
        mock_resolve.assert_called_once()
        # signed: delta

    @patch("tools.skynet_convene.initiate_convene", return_value=None)
    def test_fails_on_no_session(self, mock_init):
        def work_fn(sid, ctx, parts):
            return "x"
        result = sc.convene_and_work("alpha", "t", "c", work_fn)
        assert result is None
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 10: Constants and Module-level
# ══════════════════════════════════════════════════════════════════

class TestConstants:
    """Tests for module constants."""

    def test_worker_names(self):
        assert sc.WORKER_NAMES == ["alpha", "beta", "gamma", "delta"]
        # signed: delta

    def test_skynet_urls(self):
        assert "8420" in sc.SKYNET
        assert sc.BUS_PUBLISH.endswith("/bus/publish")
        assert sc.BUS_MESSAGES.endswith("/bus/messages")
        assert sc.BUS_CONVENE.endswith("/bus/convene")
        # signed: delta

    def test_convene_gate_majority_threshold(self):
        gate = sc.ConveneGate()
        assert gate.MAJORITY_THRESHOLD == 2
        # signed: delta


# ══════════════════════════════════════════════════════════════════
# Section 11: _print_session_detail and CLI
# ══════════════════════════════════════════════════════════════════

class TestPrintSessionDetail:
    """Tests for _print_session_detail output."""

    def test_prints_full_session(self, capsys, clean_sessions):
        session = {
            "id": "s1", "status": "resolved", "topic": "audit",
            "context": "review security", "initiator": "alpha",
            "participants": ["alpha", "beta"],
            "need_workers": 2,
            "contributions": {"alpha": {"content": "found 3 bugs"}},
            "votes": {"proposal1": {"alpha": {"choice": "YES"}}},
            "resolution": "bugs fixed",
        }
        sc._print_session_detail("s1", session)
        out = capsys.readouterr().out
        assert "s1" in out
        assert "resolved" in out
        assert "audit" in out
        assert "alpha" in out
        assert "found 3 bugs" in out
        assert "YES" in out
        assert "bugs fixed" in out
        # signed: delta

    def test_prints_session_with_messages(self, capsys, clean_sessions):
        session = {
            "id": "s2", "status": "active", "topic": "test",
            "messages": [
                {"sender": "gamma", "content": "working on it"},
            ],
        }
        sc._print_session_detail("s2", session)
        out = capsys.readouterr().out
        assert "gamma" in out
        assert "working on it" in out
        # signed: delta


class TestCLICmdStatus:
    """Tests for _cmd_status."""

    @patch("tools.skynet_convene.discover_sessions")
    def test_status_merges_local_and_remote(self, mock_disc, capsys, clean_sessions):
        mock_disc.return_value = [
            {"id": "remote1", "status": "active", "topic": "remote"},
        ]
        sc._save_sessions({"local1": {
            "id": "local1", "status": "active", "topic": "local",
        }})
        sc._cmd_status()
        out = capsys.readouterr().out
        assert "remote1" in out
        assert "local1" in out
        # signed: delta

    @patch("tools.skynet_convene.discover_sessions")
    def test_status_empty(self, mock_disc, capsys, clean_sessions):
        mock_disc.return_value = []
        sc._cmd_status()
        out = capsys.readouterr().out
        assert "No sessions" in out
        # signed: delta
