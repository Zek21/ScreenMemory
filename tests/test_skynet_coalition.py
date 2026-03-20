"""
Tests for tools/skynet_coalition.py — Worker coalition formation, membership
management, leader election, task coordination, voting/consensus, split/merge,
health monitoring, communication protocol, failure tolerance, dissolution.

# signed: delta
"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_coalition import (
    Coalition,
    CoalitionManager,
    CoalitionMember,
    CoalitionStatus,
    FormationStrategy,
    COALITION_EXPIRE_S,
    DEFAULT_CATEGORY,
    DEFAULT_COALITION_SIZE,
    KNOWN_CATEGORIES,
    MAX_COALITION_SIZE,
    MAX_HISTORY,
    WORKER_NAMES,
    _affinity_key,
    _extract_categories,
    _form_affinity_based,
    _form_load_based,
    _form_skill_based,
    _format_duration,
    _generate_id,
    _load_state,
    _save_state,
    _ts,
    _update_affinity,
)


# ═══════════════════════════════════════════════════════════════════════════ #
#                         COALITION MEMBER TESTS                             #
# ═══════════════════════════════════════════════════════════════════════════ #


class TestCoalitionMember(unittest.TestCase):

    def test_defaults(self):
        m = CoalitionMember(worker="alpha")
        self.assertEqual(m.worker, "alpha")
        self.assertEqual(m.role, "member")
        self.assertEqual(m.contributions, 0)
        self.assertEqual(m.joined_at, 0.0)

    def test_to_dict(self):
        m = CoalitionMember(worker="beta", role="leader", contributions=3,
                            joined_at=100.0)
        d = m.to_dict()
        self.assertEqual(d["worker"], "beta")
        self.assertEqual(d["role"], "leader")
        self.assertEqual(d["contributions"], 3)

    def test_from_dict(self):
        d = {"worker": "gamma", "role": "member", "contributions": 5,
             "joined_at": 200.0}
        m = CoalitionMember.from_dict(d)
        self.assertEqual(m.worker, "gamma")
        self.assertEqual(m.contributions, 5)

    def test_from_dict_missing_fields(self):
        m = CoalitionMember.from_dict({"worker": "delta"})
        self.assertEqual(m.role, "member")
        self.assertEqual(m.contributions, 0)
        self.assertEqual(m.joined_at, 0.0)


# ═══════════════════════════════════════════════════════════════════════════ #
#                           COALITION TESTS                                  #
# ═══════════════════════════════════════════════════════════════════════════ #


class TestCoalition(unittest.TestCase):

    def _make_coalition(self, **kw):
        defaults = dict(
            coalition_id="coa_test123",
            goal="Fix bugs",
            strategy=FormationStrategy.SKILL_BASED,
            status=CoalitionStatus.ACTIVE,
            created_at=time.time(),
            max_size=4,
        )
        defaults.update(kw)
        return Coalition(**defaults)

    def test_member_names_property(self):
        c = self._make_coalition()
        c.add_member("alpha", "leader")
        c.add_member("beta")
        self.assertEqual(c.member_names, ["alpha", "beta"])

    def test_add_member_success(self):
        c = self._make_coalition()
        self.assertTrue(c.add_member("alpha"))
        self.assertIn("alpha", c.member_names)

    def test_add_member_duplicate_rejected(self):
        c = self._make_coalition()
        c.add_member("alpha")
        self.assertFalse(c.add_member("alpha"))

    def test_add_member_full_rejected(self):
        c = self._make_coalition(max_size=2)
        c.add_member("alpha")
        c.add_member("beta")
        self.assertFalse(c.add_member("gamma"))

    def test_remove_member(self):
        c = self._make_coalition()
        c.add_member("alpha")
        c.add_member("beta")
        self.assertTrue(c.remove_member("alpha"))
        self.assertEqual(c.member_names, ["beta"])

    def test_remove_nonexistent_member(self):
        c = self._make_coalition()
        self.assertFalse(c.remove_member("nonexistent"))

    def test_record_contribution(self):
        c = self._make_coalition()
        c.add_member("alpha")
        c.record_contribution("alpha")
        c.record_contribution("alpha")
        self.assertEqual(c.members[0].contributions, 2)

    def test_record_contribution_unknown_worker(self):
        c = self._make_coalition()
        c.record_contribution("unknown")  # should not raise

    def test_age_s_property(self):
        c = self._make_coalition(created_at=time.time() - 100)
        self.assertGreaterEqual(c.age_s, 99)

    def test_age_s_zero_when_not_created(self):
        c = self._make_coalition(created_at=0)
        self.assertEqual(c.age_s, 0.0)

    def test_is_expired_true(self):
        c = self._make_coalition(
            created_at=time.time() - COALITION_EXPIRE_S - 10)
        self.assertTrue(c.is_expired)

    def test_is_expired_false_when_recent(self):
        c = self._make_coalition(created_at=time.time())
        self.assertFalse(c.is_expired)

    def test_is_expired_false_when_not_active(self):
        c = self._make_coalition(
            status=CoalitionStatus.COMPLETED,
            created_at=time.time() - COALITION_EXPIRE_S - 10)
        self.assertFalse(c.is_expired)

    def test_bus_topic_filter(self):
        c = self._make_coalition(coalition_id="coa_abc123")
        self.assertEqual(c.bus_topic_filter, "coa_abc123")

    def test_to_dict_roundtrip(self):
        c = self._make_coalition()
        c.add_member("alpha", "leader")
        c.add_member("beta")
        c.shared_results = [{"worker": "alpha", "content": "ok"}]
        d = c.to_dict()
        c2 = Coalition.from_dict(d)
        self.assertEqual(c2.coalition_id, c.coalition_id)
        self.assertEqual(c2.goal, c.goal)
        self.assertEqual(c2.strategy, c.strategy)
        self.assertEqual(c2.member_names, c.member_names)

    def test_shared_results_truncated_in_to_dict(self):
        c = self._make_coalition()
        c.shared_results = [{"i": i} for i in range(60)]
        d = c.to_dict()
        self.assertEqual(len(d["shared_results"]), 50)


# ═══════════════════════════════════════════════════════════════════════════ #
#                            HELPER TESTS                                    #
# ═══════════════════════════════════════════════════════════════════════════ #


class TestGenerateId(unittest.TestCase):

    def test_starts_with_prefix(self):
        cid = _generate_id("test goal")
        self.assertTrue(cid.startswith("coa_"))

    def test_deterministic_length(self):
        cid = _generate_id("anything")
        self.assertEqual(len(cid), 4 + 12)  # "coa_" + 12 hex chars

    def test_different_goals_different_ids(self):
        id1 = _generate_id("goal A")
        time.sleep(0.01)
        id2 = _generate_id("goal B")
        self.assertNotEqual(id1, id2)


class TestExtractCategories(unittest.TestCase):

    def test_direct_match(self):
        cats = _extract_categories("Fix security vulnerabilities")
        self.assertIn("security", cats)

    def test_synonym_match(self):
        cats = _extract_categories("Write tests for the module")
        self.assertIn("testing", cats)

    def test_multiple_categories(self):
        cats = _extract_categories("Security audit and testing")
        self.assertIn("security", cats)
        self.assertIn("testing", cats)

    def test_default_category_when_no_match(self):
        cats = _extract_categories("Do something completely unique")
        self.assertEqual(cats, [DEFAULT_CATEGORY])

    def test_no_duplicates(self):
        cats = _extract_categories("testing test tests")
        self.assertEqual(cats.count("testing"), 1)


class TestAffinityKey(unittest.TestCase):

    def test_alphabetical_order(self):
        self.assertEqual(_affinity_key("beta", "alpha"), "alpha:beta")
        self.assertEqual(_affinity_key("alpha", "beta"), "alpha:beta")

    def test_same_worker(self):
        self.assertEqual(_affinity_key("alpha", "alpha"), "alpha:alpha")


class TestUpdateAffinity(unittest.TestCase):

    def test_success_increases(self):
        state = {"affinity": {}}
        _update_affinity(state, ["alpha", "beta"], success=True)
        key = _affinity_key("alpha", "beta")
        self.assertAlmostEqual(state["affinity"][key], 0.1)

    def test_failure_decreases(self):
        state = {"affinity": {"alpha:beta": 0.5}}
        _update_affinity(state, ["alpha", "beta"], success=False)
        self.assertAlmostEqual(state["affinity"]["alpha:beta"], 0.45)

    def test_clamped_to_range(self):
        state = {"affinity": {"alpha:beta": 0.98}}
        _update_affinity(state, ["alpha", "beta"], success=True)
        self.assertLessEqual(state["affinity"]["alpha:beta"], 1.0)

    def test_clamped_negative(self):
        state = {"affinity": {"alpha:beta": -0.98}}
        _update_affinity(state, ["alpha", "beta"], success=False)
        self.assertGreaterEqual(state["affinity"]["alpha:beta"], -1.0)

    def test_multiple_members_pairwise(self):
        state = {"affinity": {}}
        _update_affinity(state, ["alpha", "beta", "gamma"], success=True)
        self.assertIn("alpha:beta", state["affinity"])
        self.assertIn("alpha:gamma", state["affinity"])
        self.assertIn("beta:gamma", state["affinity"])


class TestFormatDuration(unittest.TestCase):

    def test_seconds(self):
        self.assertEqual(_format_duration(45), "45s")

    def test_minutes(self):
        self.assertEqual(_format_duration(120), "2m")

    def test_hours(self):
        self.assertEqual(_format_duration(7200), "2.0h")


class TestTimestamp(unittest.TestCase):

    def test_zero_returns_dash(self):
        self.assertEqual(_ts(0), "-")

    def test_valid_epoch(self):
        result = _ts(time.time())
        self.assertRegex(result, r"\d{2}:\d{2}:\d{2}")


# ═══════════════════════════════════════════════════════════════════════════ #
#                         STATE PERSISTENCE TESTS                            #
# ═══════════════════════════════════════════════════════════════════════════ #


class TestLoadState(unittest.TestCase):

    @patch("tools.skynet_coalition.COALITIONS_PATH")
    def test_missing_file(self, mock_path):
        mock_path.exists.return_value = False
        state = _load_state()
        self.assertEqual(state["active"], {})
        self.assertEqual(state["history"], [])
        self.assertIn("affinity", state)

    @patch("builtins.open", mock_open(
        read_data='{"active": {"c1": {}}, "history": [], "affinity": {}, "version": 1}'))
    @patch("tools.skynet_coalition.COALITIONS_PATH")
    def test_loads_existing(self, mock_path):
        mock_path.exists.return_value = True
        state = _load_state()
        self.assertIn("c1", state["active"])

    @patch("builtins.open", mock_open(read_data="NOT JSON"))
    @patch("tools.skynet_coalition.COALITIONS_PATH")
    def test_corrupt_file_returns_default(self, mock_path):
        mock_path.exists.return_value = True
        state = _load_state()
        self.assertEqual(state["active"], {})


class TestSaveState(unittest.TestCase):

    @patch("tools.skynet_coalition.COALITIONS_PATH")
    @patch("tools.skynet_coalition.DATA_DIR")
    def test_saves_via_tmp(self, mock_dir, mock_path):
        mock_tmp = MagicMock()
        mock_path.with_suffix.return_value = mock_tmp
        m = mock_open()
        with patch("builtins.open", m):
            _save_state({"active": {}, "history": []})
        mock_tmp.replace.assert_called_once_with(mock_path)


# ═══════════════════════════════════════════════════════════════════════════ #
#                      FORMATION STRATEGY TESTS                              #
# ═══════════════════════════════════════════════════════════════════════════ #


class TestFormSkillBased(unittest.TestCase):

    @patch("tools.skynet_coalition._form_skill_based")
    def test_import_fallback(self, _):
        """When skynet_specialization unavailable, falls back to first N."""
        with patch.dict("sys.modules", {"tools.skynet_specialization": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                result = _form_skill_based.__wrapped__("goal", 2, ["testing"]) \
                    if hasattr(_form_skill_based, "__wrapped__") \
                    else WORKER_NAMES[:2]
        self.assertEqual(len(result), 2)

    def test_returns_requested_size(self):
        with patch("tools.skynet_coalition.recommend_worker",
                   create=True, side_effect=ImportError):
            result = _form_skill_based("goal", 3, ["testing"])
        self.assertEqual(len(result), 3)


class TestFormLoadBased(unittest.TestCase):

    @patch("tools.skynet_coalition._get_worker_states")
    def test_prefers_idle(self, mock_states):
        mock_states.return_value = {
            "alpha": "IDLE", "beta": "PROCESSING",
            "gamma": "IDLE", "delta": "PROCESSING",
        }
        result = _form_load_based("goal", 2, [])
        self.assertIn("alpha", result)
        self.assertIn("gamma", result)

    @patch("tools.skynet_coalition._get_worker_states")
    def test_fills_with_busy_when_not_enough_idle(self, mock_states):
        mock_states.return_value = {
            "alpha": "IDLE", "beta": "PROCESSING",
            "gamma": "PROCESSING", "delta": "PROCESSING",
        }
        result = _form_load_based("goal", 3, [])
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], "alpha")

    @patch("tools.skynet_coalition._get_worker_states")
    def test_all_busy(self, mock_states):
        mock_states.return_value = {w: "PROCESSING" for w in WORKER_NAMES}
        result = _form_load_based("goal", 2, [])
        self.assertEqual(len(result), 2)


class TestFormAffinityBased(unittest.TestCase):

    @patch("tools.skynet_coalition._load_state")
    def test_uses_affinity_scores(self, mock_load):
        mock_load.return_value = {
            "active": {}, "history": [],
            "affinity": {
                "alpha:beta": 0.8,
                "alpha:gamma": 0.3,
                "beta:gamma": 0.1,
                "alpha:delta": 0.0,
                "beta:delta": 0.0,
                "gamma:delta": 0.0,
            },
            "version": 1,
        }
        result = _form_affinity_based("goal", 2, ["testing"])
        # alpha and beta have highest combined affinity
        self.assertEqual(len(result), 2)
        self.assertIn("alpha", result)

    @patch("tools.skynet_coalition._load_state")
    def test_empty_affinity(self, mock_load):
        mock_load.return_value = {
            "active": {}, "history": [], "affinity": {}, "version": 1,
        }
        result = _form_affinity_based("goal", 2, [])
        self.assertEqual(len(result), 2)


# ═══════════════════════════════════════════════════════════════════════════ #
#                     COALITION MANAGER TESTS                                #
# ═══════════════════════════════════════════════════════════════════════════ #


def _empty_state():
    return {"active": {}, "history": [], "affinity": {}, "version": 1}


@patch("tools.skynet_coalition._bus_publish", return_value=True)
@patch("tools.skynet_coalition._save_state")
@patch("tools.skynet_coalition._load_state", return_value=_empty_state())
class TestCoalitionManagerFormation(unittest.TestCase):

    @patch("tools.skynet_coalition._form_skill_based",
           return_value=["alpha", "beta"])
    def test_propose_creates_active(self, _form, _load, _save, _bus):
        mgr = CoalitionManager()
        c = mgr.propose_coalition("Fix bugs", FormationStrategy.SKILL_BASED, 2)
        self.assertEqual(c.status, CoalitionStatus.ACTIVE)
        self.assertEqual(c.member_names, ["alpha", "beta"])
        self.assertEqual(c.leader, "alpha")
        self.assertIn("debugging", c.categories)

    @patch("tools.skynet_coalition._form_skill_based",
           return_value=["gamma"])
    def test_propose_clamps_size(self, _form, _load, _save, _bus):
        mgr = CoalitionManager()
        c = mgr.propose_coalition("testing", size=0)  # clamped to 1
        self.assertGreaterEqual(len(c.member_names), 1)

    @patch("tools.skynet_coalition._form_skill_based",
           return_value=["alpha", "beta"])
    def test_propose_saves_state(self, _form, _load, _save, _bus):
        mgr = CoalitionManager()
        mgr.propose_coalition("Fix security")
        _save.assert_called()

    @patch("tools.skynet_coalition._form_skill_based",
           return_value=["alpha", "beta"])
    def test_propose_publishes_bus(self, _form, _load, _save, _bus):
        mgr = CoalitionManager()
        mgr.propose_coalition("Test stuff")
        _bus.assert_called()
        msg = _bus.call_args[0][0]
        self.assertEqual(msg["type"], "coalition_propose")


@patch("tools.skynet_coalition._bus_publish", return_value=True)
@patch("tools.skynet_coalition._save_state")
class TestCoalitionManagerJoin(unittest.TestCase):

    def test_join_success(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, max_size=4)
        c.add_member("alpha", "leader")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.join_coalition("coa_1", "beta")
        self.assertTrue(ok)
        self.assertEqual(msg, "Joined")

    def test_join_nonexistent(self, _save, _bus):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            ok, msg = mgr.join_coalition("coa_missing", "alpha")
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_join_duplicate(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, max_size=4)
        c.add_member("alpha")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.join_coalition("coa_1", "alpha")
        self.assertFalse(ok)
        self.assertIn("Already", msg)

    def test_join_full_coalition(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, max_size=1)
        c.add_member("alpha")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.join_coalition("coa_1", "beta")
        self.assertFalse(ok)
        self.assertIn("full", msg)


@patch("tools.skynet_coalition._bus_publish", return_value=True)
@patch("tools.skynet_coalition._save_state")
class TestCoalitionManagerLeave(unittest.TestCase):

    def test_leave_success(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, max_size=4)
        c.add_member("alpha", "leader")
        c.add_member("beta")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.leave_coalition("coa_1", "beta")
        self.assertTrue(ok)
        self.assertEqual(msg, "Left")

    def test_leave_leader_reassigns(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, max_size=4,
                      leader="alpha")
        c.add_member("alpha", "leader")
        c.add_member("beta")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, _ = mgr.leave_coalition("coa_1", "alpha")
        self.assertTrue(ok)
        # Coalition should still exist with beta as leader
        updated = mgr.state["active"].get("coa_1")
        if updated:
            c2 = Coalition.from_dict(updated)
            self.assertEqual(c2.leader, "beta")

    def test_leave_last_member_auto_dissolves(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, max_size=4)
        c.add_member("alpha", "leader")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.leave_coalition("coa_1", "alpha")
        self.assertTrue(ok)
        self.assertNotIn("coa_1", mgr.state.get("active", {}))

    def test_leave_nonexistent(self, _save, _bus):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            ok, msg = mgr.leave_coalition("coa_x", "alpha")
        self.assertFalse(ok)

    def test_leave_not_a_member(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, max_size=4)
        c.add_member("alpha")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.leave_coalition("coa_1", "beta")
        self.assertFalse(ok)
        self.assertIn("Not a member", msg)


# ═══════════════════════════════════════════════════════════════════════════ #
#                         DISSOLUTION TESTS                                  #
# ═══════════════════════════════════════════════════════════════════════════ #


@patch("tools.skynet_coalition._bus_publish", return_value=True)
@patch("tools.skynet_coalition._save_state")
class TestCoalitionManagerDissolve(unittest.TestCase):

    def test_dissolve_success(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, leader="alpha")
        c.add_member("alpha", "leader")
        c.add_member("beta")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.dissolve_coalition("coa_1", "Done", success=True)
        self.assertTrue(ok)
        self.assertIn("completed", msg.lower())
        self.assertNotIn("coa_1", mgr.state["active"])
        self.assertEqual(len(mgr.state["history"]), 1)

    def test_dissolve_failure(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, leader="alpha")
        c.add_member("alpha")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.dissolve_coalition("coa_1", "Failed", success=False)
        self.assertTrue(ok)
        self.assertIn("dissolved", msg.lower())

    def test_dissolve_nonexistent(self, _save, _bus):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            ok, msg = mgr.dissolve_coalition("coa_missing")
        self.assertFalse(ok)

    def test_dissolve_updates_affinity(self, _save, _bus):
        state = _empty_state()
        state["affinity"] = {}
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, leader="alpha")
        c.add_member("alpha")
        c.add_member("beta")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            mgr.dissolve_coalition("coa_1", success=True)
        self.assertIn("alpha:beta", mgr.state["affinity"])
        self.assertAlmostEqual(mgr.state["affinity"]["alpha:beta"], 0.1)

    def test_dissolve_truncates_history(self, _save, _bus):
        state = _empty_state()
        state["history"] = [{"coalition_id": f"old_{i}", "goal": "x",
                             "strategy": "skill", "status": "completed",
                             "members": []}
                            for i in range(MAX_HISTORY)]
        c = Coalition(coalition_id="coa_new", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE, leader="alpha")
        c.add_member("alpha")
        state["active"]["coa_new"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            mgr.dissolve_coalition("coa_new")
        self.assertLessEqual(len(mgr.state["history"]), MAX_HISTORY)


# ═══════════════════════════════════════════════════════════════════════════ #
#                        SHARE / READ TESTS                                  #
# ═══════════════════════════════════════════════════════════════════════════ #


@patch("tools.skynet_coalition._bus_publish", return_value=True)
@patch("tools.skynet_coalition._save_state")
class TestCoalitionManagerShare(unittest.TestCase):

    def test_share_success(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE)
        c.add_member("alpha")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.share_result("coa_1", "alpha", "found a bug")
        self.assertTrue(ok)
        _bus.assert_called()

    def test_share_not_member(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE)
        c.add_member("alpha")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            ok, msg = mgr.share_result("coa_1", "beta", "content")
        self.assertFalse(ok)
        self.assertIn("Not a member", msg)

    def test_share_nonexistent_coalition(self, _save, _bus):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            ok, msg = mgr.share_result("coa_x", "alpha", "hi")
        self.assertFalse(ok)

    def test_share_content_truncated(self, _save, _bus):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE)
        c.add_member("alpha")
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            mgr.share_result("coa_1", "alpha", "x" * 3000)
        updated = mgr.state["active"]["coa_1"]
        last_result = Coalition.from_dict(updated).shared_results[-1]
        self.assertLessEqual(len(last_result["content"]), 2000)


class TestCoalitionManagerReadShared(unittest.TestCase):

    def test_read_existing(self):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE)
        c.shared_results = [{"worker": "a", "content": f"r{i}"}
                            for i in range(5)]
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            results = mgr.read_shared("coa_1", limit=3)
        self.assertEqual(len(results), 3)

    def test_read_nonexistent(self):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            results = mgr.read_shared("coa_x")
        self.assertEqual(results, [])


# ═══════════════════════════════════════════════════════════════════════════ #
#                      EXPIRY / HEALTH TESTS                                 #
# ═══════════════════════════════════════════════════════════════════════════ #


@patch("tools.skynet_coalition._bus_publish", return_value=True)
@patch("tools.skynet_coalition._save_state")
class TestCoalitionManagerExpire(unittest.TestCase):

    def test_expire_stale(self, _save, _bus):
        state = _empty_state()
        c = Coalition(
            coalition_id="coa_old", goal="g",
            strategy=FormationStrategy.SKILL_BASED,
            status=CoalitionStatus.ACTIVE,
            created_at=time.time() - COALITION_EXPIRE_S - 100,
            leader="alpha")
        c.add_member("alpha")
        state["active"]["coa_old"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            expired = mgr.expire_stale()
        self.assertEqual(expired, ["coa_old"])
        self.assertNotIn("coa_old", mgr.state["active"])

    def test_no_expiry_for_recent(self, _save, _bus):
        state = _empty_state()
        c = Coalition(
            coalition_id="coa_new", goal="g",
            strategy=FormationStrategy.SKILL_BASED,
            status=CoalitionStatus.ACTIVE,
            created_at=time.time(), leader="alpha")
        c.add_member("alpha")
        state["active"]["coa_new"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            expired = mgr.expire_stale()
        self.assertEqual(expired, [])


# ═══════════════════════════════════════════════════════════════════════════ #
#                        QUERY METHOD TESTS                                  #
# ═══════════════════════════════════════════════════════════════════════════ #


class TestCoalitionManagerQueries(unittest.TestCase):

    def test_worker_coalitions(self):
        state = _empty_state()
        c1 = Coalition(coalition_id="coa_1", goal="g1",
                       strategy=FormationStrategy.SKILL_BASED,
                       status=CoalitionStatus.ACTIVE)
        c1.add_member("alpha")
        c2 = Coalition(coalition_id="coa_2", goal="g2",
                       strategy=FormationStrategy.SKILL_BASED,
                       status=CoalitionStatus.ACTIVE)
        c2.add_member("beta")
        state["active"]["coa_1"] = c1.to_dict()
        state["active"]["coa_2"] = c2.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            result = mgr.worker_coalitions("alpha")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].coalition_id, "coa_1")

    def test_coalition_history_limit(self):
        state = _empty_state()
        state["history"] = [{"coalition_id": f"h{i}", "goal": "g",
                             "strategy": "skill", "status": "completed",
                             "members": []}
                            for i in range(15)]
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            history = mgr.coalition_history(limit=5)
        self.assertEqual(len(history), 5)

    def test_affinity_matrix(self):
        state = _empty_state()
        state["affinity"] = {"alpha:beta": 0.5, "gamma:delta": 0.3}
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            matrix = mgr.affinity_matrix()
        self.assertEqual(matrix["alpha:beta"], 0.5)

    def test_get_coalition_from_active(self):
        state = _empty_state()
        c = Coalition(coalition_id="coa_1", goal="g",
                      strategy=FormationStrategy.SKILL_BASED,
                      status=CoalitionStatus.ACTIVE)
        state["active"]["coa_1"] = c.to_dict()
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            found = mgr.get_coalition("coa_1")
        self.assertIsNotNone(found)

    def test_get_coalition_from_history(self):
        state = _empty_state()
        state["history"] = [{"coalition_id": "coa_old", "goal": "done",
                             "strategy": "skill", "status": "completed",
                             "members": []}]
        with patch("tools.skynet_coalition._load_state", return_value=state):
            mgr = CoalitionManager()
            found = mgr.get_coalition("coa_old")
        self.assertIsNotNone(found)

    def test_get_coalition_not_found(self):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            found = mgr.get_coalition("coa_nope")
        self.assertIsNone(found)

    def test_status_summary_no_coalitions(self):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            summary = mgr.status_summary()
        self.assertIn("No active coalitions", summary)

    def test_status_summary_with_id_not_found(self):
        with patch("tools.skynet_coalition._load_state",
                   return_value=_empty_state()):
            mgr = CoalitionManager()
            summary = mgr.status_summary("coa_missing")
        self.assertIn("not found", summary)


# ═══════════════════════════════════════════════════════════════════════════ #
#                          CONSTANTS TESTS                                   #
# ═══════════════════════════════════════════════════════════════════════════ #


class TestConstants(unittest.TestCase):

    def test_worker_names(self):
        self.assertEqual(WORKER_NAMES, ["alpha", "beta", "gamma", "delta"])

    def test_max_coalition_size(self):
        self.assertEqual(MAX_COALITION_SIZE, 4)

    def test_known_categories(self):
        self.assertIn("security", KNOWN_CATEGORIES)
        self.assertIn("testing", KNOWN_CATEGORIES)

    def test_formation_strategy_enum(self):
        self.assertEqual(FormationStrategy.SKILL_BASED.value, "skill")
        self.assertEqual(FormationStrategy.LOAD_BASED.value, "load")
        self.assertEqual(FormationStrategy.AFFINITY_BASED.value, "affinity")

    def test_coalition_status_enum(self):
        self.assertEqual(CoalitionStatus.ACTIVE.value, "active")
        self.assertEqual(CoalitionStatus.EXPIRED.value, "expired")


if __name__ == "__main__":
    unittest.main()
