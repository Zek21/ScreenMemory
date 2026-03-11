#!/usr/bin/env python3
"""Tests for skynet_missions.py -- Mission and MissionControl classes."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from skynet_missions import Mission, MissionControl, MissionStatus


class TestMissionStatus(unittest.TestCase):
    def test_all_statuses_exist(self):
        expected = {"planned", "active", "paused", "completed", "failed", "cancelled"}
        actual = {s.value for s in MissionStatus}
        self.assertEqual(expected, actual)

    def test_status_is_str_enum(self):
        self.assertIsInstance(MissionStatus.ACTIVE, str)
        self.assertEqual(MissionStatus.ACTIVE, "active")


class TestMissionCreation(unittest.TestCase):
    def test_default_mission(self):
        m = Mission(title="Test mission")
        self.assertEqual(m.title, "Test mission")
        self.assertEqual(m.status, "planned")
        self.assertEqual(m.owner, "orchestrator")
        self.assertEqual(m.priority, 2)
        self.assertTrue(m.mission_id.startswith("mission-"))
        self.assertEqual(len(m.timeline), 1)
        self.assertEqual(m.timeline[0]["event"], "Mission created")

    def test_custom_fields(self):
        m = Mission(
            title="Custom",
            mission_id="m-123",
            status="active",
            owner="alpha",
            priority=1,
            description="A test",
            tags=["security", "wave5"],
            workers=["alpha", "beta"],
        )
        self.assertEqual(m.mission_id, "m-123")
        self.assertEqual(m.status, "active")
        self.assertEqual(m.owner, "alpha")
        self.assertEqual(m.priority, 1)
        self.assertEqual(m.tags, ["security", "wave5"])
        self.assertEqual(m.workers, ["alpha", "beta"])

    def test_to_dict_roundtrip(self):
        m = Mission(title="Roundtrip", priority=1, tags=["a"])
        d = m.to_dict()
        m2 = Mission.from_dict(d)
        self.assertEqual(m.mission_id, m2.mission_id)
        self.assertEqual(m.title, m2.title)
        self.assertEqual(m.status, m2.status)
        self.assertEqual(m.priority, m2.priority)
        self.assertEqual(m.tags, m2.tags)

    def test_from_dict_defaults(self):
        m = Mission.from_dict({})
        self.assertEqual(m.title, "Untitled")
        self.assertEqual(m.status, "planned")
        self.assertEqual(m.priority, 2)


class TestMissionEvents(unittest.TestCase):
    def test_add_event(self):
        m = Mission(title="Events test")
        entry = m.add_event("Task dispatched", actor="orchestrator")
        self.assertEqual(entry["event"], "Task dispatched")
        self.assertEqual(entry["actor"], "orchestrator")
        self.assertIn("timestamp", entry)
        self.assertEqual(len(m.timeline), 2)

    def test_set_status_creates_event(self):
        m = Mission(title="Status test")
        m.set_status("active", actor="delta")
        self.assertEqual(m.status, "active")
        self.assertEqual(len(m.timeline), 2)
        self.assertIn("planned -> active", m.timeline[-1]["event"])

    def test_set_invalid_status_raises(self):
        m = Mission(title="Invalid")
        with self.assertRaises(ValueError):
            m.set_status("nonexistent")

    def test_assign_worker(self):
        m = Mission(title="Workers")
        m.assign_worker("alpha")
        self.assertIn("alpha", m.workers)
        self.assertEqual(len(m.timeline), 2)

    def test_assign_worker_no_duplicate(self):
        m = Mission(title="NoDup")
        m.assign_worker("beta")
        m.assign_worker("beta")
        self.assertEqual(m.workers.count("beta"), 1)

    def test_is_terminal(self):
        m = Mission(title="Terminal")
        self.assertFalse(m.is_terminal())
        m.status = "completed"
        self.assertTrue(m.is_terminal())
        m.status = "failed"
        self.assertTrue(m.is_terminal())
        m.status = "cancelled"
        self.assertTrue(m.is_terminal())
        m.status = "active"
        self.assertFalse(m.is_terminal())

    def test_duration_s(self):
        m = Mission(title="Duration")
        # created_at and updated_at are same on creation
        self.assertAlmostEqual(m.duration_s(), 0.0, places=0)


class TestMissionControl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.missions_file = Path(self.tmpdir) / "missions.json"
        self.mc = MissionControl(missions_file=self.missions_file)

    def test_create_mission(self):
        m = self.mc.create(title="First mission")
        self.assertEqual(m.title, "First mission")
        self.assertTrue(self.missions_file.exists())

    def test_get_mission(self):
        m = self.mc.create(title="Get test")
        retrieved = self.mc.get(m.mission_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.title, "Get test")

    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(self.mc.get("nonexistent"))

    def test_list_missions(self):
        self.mc.create(title="A", priority=2)
        self.mc.create(title="B", priority=1)
        missions = self.mc.list_missions()
        self.assertEqual(len(missions), 2)
        # Sorted by priority
        self.assertEqual(missions[0].priority, 1)

    def test_list_filter_by_status(self):
        m1 = self.mc.create(title="Active1")
        m1.set_status("active")
        self.mc._save()
        self.mc.create(title="Planned1")
        active = self.mc.list_missions(status="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].title, "Active1")

    def test_active_missions(self):
        m1 = self.mc.create(title="Act")
        self.mc.update_status(m1.mission_id, "active")
        self.mc.create(title="Plan")
        active = self.mc.active_missions()
        self.assertEqual(len(active), 1)

    def test_update_status(self):
        m = self.mc.create(title="Update")
        result = self.mc.update_status(m.mission_id, "active")
        self.assertEqual(result.status, "active")

    def test_update_nonexistent_returns_none(self):
        self.assertIsNone(self.mc.update_status("bad-id", "active"))

    def test_add_event(self):
        m = self.mc.create(title="Event")
        entry = self.mc.add_event(m.mission_id, "Test event", actor="delta")
        self.assertEqual(entry["event"], "Test event")

    def test_add_event_nonexistent_returns_none(self):
        self.assertIsNone(self.mc.add_event("bad", "event"))

    def test_assign_worker(self):
        m = self.mc.create(title="Assign")
        result = self.mc.assign_worker(m.mission_id, "gamma")
        self.assertIn("gamma", result.workers)

    def test_get_timeline(self):
        m = self.mc.create(title="Timeline")
        self.mc.add_event(m.mission_id, "Step 1")
        tl = self.mc.get_timeline(m.mission_id)
        self.assertEqual(len(tl), 2)

    def test_get_timeline_nonexistent(self):
        self.assertIsNone(self.mc.get_timeline("bad"))

    def test_stats(self):
        self.mc.create(title="A")
        m2 = self.mc.create(title="B")
        self.mc.update_status(m2.mission_id, "active")
        stats = self.mc.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["active_count"], 1)

    def test_delete(self):
        m = self.mc.create(title="Delete me")
        self.assertTrue(self.mc.delete(m.mission_id))
        self.assertIsNone(self.mc.get(m.mission_id))

    def test_delete_nonexistent(self):
        self.assertFalse(self.mc.delete("nope"))

    def test_persistence_reload(self):
        m = self.mc.create(title="Persist", priority=1, tags=["test"])
        self.mc.update_status(m.mission_id, "active")
        # Reload from disk
        mc2 = MissionControl(missions_file=self.missions_file)
        m2 = mc2.get(m.mission_id)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.title, "Persist")
        self.assertEqual(m2.status, "active")
        self.assertEqual(m2.tags, ["test"])

    def test_to_dict_list(self):
        self.mc.create(title="A")
        self.mc.create(title="B")
        dl = self.mc.to_dict_list()
        self.assertEqual(len(dl), 2)
        self.assertIsInstance(dl[0], dict)


class TestSubtasks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.missions_file = Path(self.tmpdir) / "missions.json"
        self.mc = MissionControl(missions_file=self.missions_file)

    def test_decompose_mission(self):
        m = self.mc.create(title="Decompose test")
        result = self.mc.decompose_mission(m.mission_id, [
            {"title": "Step 1", "description": "First step"},
            {"title": "Step 2"},
        ])
        self.assertIsNotNone(result)
        self.assertEqual(len(result.subtasks), 2)
        self.assertEqual(result.subtasks[0]["title"], "Step 1")
        self.assertEqual(result.subtasks[0]["status"], "pending")
        self.assertEqual(result.subtasks[1]["idx"], 1)

    def test_decompose_nonexistent(self):
        self.assertIsNone(self.mc.decompose_mission("bad-id", [{"title": "X"}]))

    def test_assign_subtask(self):
        m = self.mc.create(title="Assign sub")
        self.mc.decompose_mission(m.mission_id, [{"title": "Task A"}])
        st = self.mc.assign_subtask(m.mission_id, 0, "alpha")
        self.assertIsNotNone(st)
        self.assertEqual(st["assigned_worker"], "alpha")
        self.assertEqual(st["status"], "active")
        # Mission should auto-activate
        updated = self.mc.get(m.mission_id)
        self.assertEqual(updated.status, "active")
        self.assertIn("alpha", updated.workers)

    def test_assign_subtask_bad_index(self):
        m = self.mc.create(title="Bad idx")
        self.mc.decompose_mission(m.mission_id, [{"title": "Only one"}])
        self.assertIsNone(self.mc.assign_subtask(m.mission_id, 5, "beta"))
        self.assertIsNone(self.mc.assign_subtask(m.mission_id, -1, "beta"))

    def test_assign_subtask_nonexistent_mission(self):
        self.assertIsNone(self.mc.assign_subtask("bad-id", 0, "alpha"))

    def test_complete_subtask(self):
        m = self.mc.create(title="Complete sub")
        self.mc.decompose_mission(m.mission_id, [{"title": "Only task"}])
        self.mc.assign_subtask(m.mission_id, 0, "gamma")
        result = self.mc.complete_subtask(m.mission_id, 0, "Done successfully")
        self.assertIsNotNone(result)
        self.assertEqual(result.subtasks[0]["status"], "completed")
        self.assertEqual(result.subtasks[0]["result"], "Done successfully")
        # Single subtask = mission auto-completes
        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.completed_at)

    def test_complete_subtask_partial(self):
        m = self.mc.create(title="Partial")
        self.mc.decompose_mission(m.mission_id, [
            {"title": "A"}, {"title": "B"},
        ])
        self.mc.assign_subtask(m.mission_id, 0, "alpha")
        result = self.mc.complete_subtask(m.mission_id, 0, "A done")
        # Mission should still be active since B is not done
        self.assertNotEqual(result.status, "completed")

    def test_complete_all_subtasks_completes_mission(self):
        m = self.mc.create(title="Full")
        self.mc.decompose_mission(m.mission_id, [
            {"title": "A"}, {"title": "B"},
        ])
        self.mc.assign_subtask(m.mission_id, 0, "alpha")
        self.mc.assign_subtask(m.mission_id, 1, "beta")
        self.mc.complete_subtask(m.mission_id, 0, "A done")
        result = self.mc.complete_subtask(m.mission_id, 1, "B done")
        self.assertEqual(result.status, "completed")

    def test_complete_subtask_bad_index(self):
        m = self.mc.create(title="Bad")
        self.mc.decompose_mission(m.mission_id, [{"title": "X"}])
        self.assertIsNone(self.mc.complete_subtask(m.mission_id, 99, "nope"))

    def test_subtask_dependencies_block_assignment(self):
        m = self.mc.create(title="Deps")
        self.mc.decompose_mission(m.mission_id, [
            {"title": "A"},
            {"title": "B", "dependencies": [0]},  # B depends on A
        ])
        # Cannot assign B before A is completed
        result = self.mc.assign_subtask(m.mission_id, 1, "beta")
        self.assertIsNone(result)

    def test_subtask_dependencies_satisfied(self):
        m = self.mc.create(title="Deps OK")
        self.mc.decompose_mission(m.mission_id, [
            {"title": "A"},
            {"title": "B", "dependencies": [0]},
        ])
        self.mc.assign_subtask(m.mission_id, 0, "alpha")
        self.mc.complete_subtask(m.mission_id, 0, "A done")
        # Now B's dependency is satisfied
        result = self.mc.assign_subtask(m.mission_id, 1, "beta")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")

    def test_subtask_progress(self):
        m = Mission(title="Progress")
        self.assertEqual(m.subtask_progress()["total"], 0)
        m.subtasks = [
            {"idx": 0, "title": "A", "status": "completed"},
            {"idx": 1, "title": "B", "status": "pending"},
            {"idx": 2, "title": "C", "status": "active"},
        ]
        p = m.subtask_progress()
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["completed"], 1)
        self.assertEqual(p["pending"], 2)
        self.assertAlmostEqual(p["pct"], 33.3, places=1)

    def test_results_stored(self):
        m = self.mc.create(title="Results")
        self.mc.decompose_mission(m.mission_id, [{"title": "A"}, {"title": "B"}])
        self.mc.assign_subtask(m.mission_id, 0, "alpha")
        self.mc.complete_subtask(m.mission_id, 0, "Result A")
        updated = self.mc.get(m.mission_id)
        self.assertEqual(updated.results["0"], "Result A")

    def test_get_mission_timeline_gantt(self):
        m = self.mc.create(title="Gantt test")
        self.mc.decompose_mission(m.mission_id, [{"title": "Sub1"}, {"title": "Sub2"}])
        self.mc.assign_subtask(m.mission_id, 0, "alpha")
        self.mc.complete_subtask(m.mission_id, 0, "OK")
        gantt = self.mc.get_mission_timeline()
        self.assertEqual(len(gantt), 1)
        entry = gantt[0]
        self.assertEqual(entry["mission_id"], m.mission_id)
        self.assertIn("start", entry)
        self.assertIn("end", entry)
        self.assertEqual(len(entry["subtasks"]), 2)
        self.assertEqual(entry["subtasks"][0]["status"], "completed")

    def test_persistence_with_subtasks(self):
        m = self.mc.create(title="Persist subs")
        self.mc.decompose_mission(m.mission_id, [
            {"title": "A"}, {"title": "B", "dependencies": [0]},
        ])
        self.mc.assign_subtask(m.mission_id, 0, "alpha")
        # Reload
        mc2 = MissionControl(missions_file=self.missions_file)
        m2 = mc2.get(m.mission_id)
        self.assertEqual(len(m2.subtasks), 2)
        self.assertEqual(m2.subtasks[0]["assigned_worker"], "alpha")
        self.assertEqual(m2.subtasks[1]["dependencies"], [0])


if __name__ == "__main__":
    unittest.main()
