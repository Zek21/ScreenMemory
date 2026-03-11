import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_process_guard as process_guard


class _FakeProcess:
    def __init__(self, pid, cmdline, exe, children=None):
        self.pid = pid
        self._cmdline = list(cmdline)
        self._exe = exe
        self._children = list(children or [])

    def cmdline(self):
        return list(self._cmdline)

    def exe(self):
        return self._exe

    def children(self, recursive=False):
        return list(self._children)


class ProcessGuardRegistryTests(unittest.TestCase):
    def test_collapse_wrapper_pids_keeps_real_child_only(self):
        child = _FakeProcess(
            30504,
            ["D:\\Prospects\\env\\Scripts\\python.exe", "tools\\skynet_agent_telemetry.py", "start"],
            "C:\\Python313\\python.exe",
        )
        parent = _FakeProcess(
            29752,
            ["D:\\Prospects\\env\\Scripts\\python.exe", "tools\\skynet_agent_telemetry.py", "start"],
            "D:\\Prospects\\env\\Scripts\\python.exe",
            children=[child],
        )
        fake_psutil = SimpleNamespace(Process=lambda pid: {29752: parent, 30504: child}[pid])
        with mock.patch.object(process_guard, "psutil", fake_psutil):
            self.assertEqual(process_guard._collapse_wrapper_pids([29752, 30504]), [30504])

    def test_collapse_wrapper_pids_keeps_true_duplicates(self):
        first = _FakeProcess(1001, ["python.exe", "god_console.py"], "C:\\Python313\\python.exe")
        second = _FakeProcess(1002, ["python.exe", "god_console.py"], "C:\\Python313\\python.exe")
        fake_psutil = SimpleNamespace(Process=lambda pid: {1001: first, 1002: second}[pid])
        with mock.patch.object(process_guard, "psutil", fake_psutil):
            self.assertEqual(process_guard._collapse_wrapper_pids([1001, 1002]), [1001, 1002])


if __name__ == "__main__":
    unittest.main()
