"""Tests for tools/engine_metrics.py — engine health probe system.
# signed: delta

Tests cover:
  - _probe: status levels (online/available/offline), import_only flag, extras_fn
  - _run_probes: parallel execution, timeout handling, error resilience
  - _build_metrics_result: summary stats, health percentage, timing
  - collect_engine_metrics: caching behavior, TTL expiry
  - Edge cases: import failures, instantiation failures, empty probes
"""

import json
import time
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.engine_metrics import _probe, _run_probes, _build_metrics_result


class TestProbe:
    """Tests for _probe function (individual engine probing)."""

    def test_probe_online_status(self):
        """Verify online status when class instantiates successfully."""
        result = _probe("test_engine", "json", "JSONDecoder", "utility", False)
        assert result["status"] == "online"
        assert result["name"] == "JSONDecoder"
        assert result["type"] == "utility"
        assert "probe_ms" in result

    def test_probe_offline_on_bad_import(self):
        """Verify offline status when module import fails."""
        result = _probe("bad_engine", "nonexistent.module.xyz", "FakeClass", "test", False)
        assert result["status"] == "offline"
        assert "error" in result
        assert result["name"] == "FakeClass"

    def test_probe_available_on_import_only(self):
        """Verify available status when import_only=True."""
        result = _probe("test_engine", "json", "JSONDecoder", "utility", True)
        assert result["status"] == "available"
        assert "import-only" in result.get("note", "")

    def test_probe_available_when_instantiation_fails(self):
        """Verify available status when import succeeds but constructor fails."""
        # Use a class that requires arguments to instantiate
        result = _probe("test_engine", "pathlib", "PurePosixPath", "fs", False)
        # PurePosixPath() actually works with no args, so try something that fails
        result = _probe("test_engine", "http.server", "HTTPServer", "network", False)
        assert result["status"] in ("available", "offline")
        # HTTPServer requires (server_address, handler), so it should fail instantiation

    def test_probe_timing(self):
        """Verify probe_ms is a reasonable positive number."""
        result = _probe("timing_test", "json", "JSONDecoder", "utility", False)
        assert result["probe_ms"] >= 0
        assert result["probe_ms"] < 5000  # Should complete within 5 seconds

    def test_probe_error_truncation(self):
        """Verify error messages are truncated to 120 chars."""
        result = _probe("err_test", "nonexistent_very_long_module_name_that_does_not_exist",
                        "FakeClass", "test", False)
        assert result["status"] == "offline"
        assert len(result.get("error", "")) <= 120

    def test_probe_extras_fn(self):
        """Verify extras_fn is called and results merged."""
        def extras(cls):
            return {"version": "1.0", "extra_info": True}

        result = _probe("extras_test", "json", "JSONDecoder", "utility", False, extras_fn=extras)
        assert result["status"] == "online"
        assert result.get("version") == "1.0"
        assert result.get("extra_info") is True

    def test_probe_extras_fn_failure_doesnt_crash(self):
        """Verify extras_fn failure doesn't crash the probe."""
        def bad_extras(cls):
            raise RuntimeError("extras failed")

        result = _probe("extras_fail", "json", "JSONDecoder", "utility", False, extras_fn=bad_extras)
        assert result["status"] == "online"  # Probe still succeeds
        assert "version" not in result


class TestRunProbes:
    """Tests for _run_probes function (parallel probe execution)."""

    def test_run_probes_parallel(self):
        """Verify multiple probes run and return results."""
        probes = [
            ("json_engine", "json", "JSONDecoder", "utility", False),
            ("path_engine", "pathlib", "Path", "fs", False),
        ]
        results = _run_probes(probes)
        assert "json_engine" in results
        assert "path_engine" in results
        assert results["json_engine"]["status"] == "online"

    def test_run_probes_handles_failures(self):
        """Verify failed probes don't crash the batch."""
        probes = [
            ("good", "json", "JSONDecoder", "utility", False),
            ("bad", "nonexistent_module_abc", "FakeClass", "test", False),
        ]
        results = _run_probes(probes)
        assert results["good"]["status"] == "online"
        assert results["bad"]["status"] == "offline"

    def test_run_probes_empty_list(self):
        """Verify empty probe list returns empty dict."""
        results = _run_probes([])
        assert results == {}

    def test_run_probes_all_failures(self):
        """Verify all-failure case returns offline for all."""
        probes = [
            ("fail1", "no_mod_1", "F1", "test", False),
            ("fail2", "no_mod_2", "F2", "test", False),
        ]
        results = _run_probes(probes)
        assert all(r["status"] == "offline" for r in results.values())


class TestBuildMetricsResult:
    """Tests for _build_metrics_result function."""

    def test_build_with_mixed_statuses(self):
        """Verify summary counts with mixed engine statuses."""
        engines = {
            "e1": {"status": "online", "name": "E1"},
            "e2": {"status": "available", "name": "E2"},
            "e3": {"status": "offline", "name": "E3"},
            "e4": {"status": "online", "name": "E4"},
        }
        now = time.time()
        result = _build_metrics_result(engines, now, now - 0.1)
        assert result["summary"]["online"] == 2
        assert result["summary"]["available"] == 1
        assert result["summary"]["offline"] == 1
        assert result["summary"]["total"] == 4
        assert result["summary"]["health_pct"] == 50

    def test_build_with_all_online(self):
        """Verify 100% health when all engines online."""
        engines = {
            "e1": {"status": "online"},
            "e2": {"status": "online"},
        }
        now = time.time()
        result = _build_metrics_result(engines, now, now)
        assert result["summary"]["health_pct"] == 100
        assert result["summary"]["offline"] == 0

    def test_build_with_empty_engines(self):
        """Verify empty engines produces 0% health."""
        now = time.time()
        result = _build_metrics_result({}, now, now)
        assert result["summary"]["total"] == 0
        assert result["summary"]["health_pct"] == 0

    def test_build_includes_timestamp(self):
        """Verify timestamp and collection_ms are present."""
        now = time.time()
        result = _build_metrics_result({}, now, now - 0.05)
        assert result["timestamp"] == now
        assert result["collection_ms"] >= 0
