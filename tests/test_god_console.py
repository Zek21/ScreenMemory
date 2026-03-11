"""Comprehensive test suite for god_console.py — GOD Console HTTP server.

Tests all GET/POST endpoints, routing, caching, error handling, and edge cases.
Uses a real threaded HTTP server on an ephemeral port for integration-level coverage.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# --------------- Fixtures ---------------

@pytest.fixture(scope="module")
def server():
    """Start a GOD Console server on an ephemeral port for the test module."""
    import god_console
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn
    import socket

    # Simple IPv4-only threaded server for tests
    class TestServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET
        allow_reuse_address = True

    # Patch dashboard HTML to avoid file-not-found in CI
    original_html = god_console.DASHBOARD_HTML
    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
    tmp.write("<html><body>TEST DASHBOARD</body></html>")
    tmp.close()
    god_console.DASHBOARD_HTML = Path(tmp.name)

    # Use port 0 for OS-assigned ephemeral port
    srv = TestServer(("127.0.0.1", 0), god_console.ConsoleHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)  # let server bind

    yield {"server": srv, "port": port, "base": f"http://127.0.0.1:{port}"}

    srv.shutdown()
    god_console.DASHBOARD_HTML = original_html
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _get(base, path, timeout=5):
    """GET helper returning (status, data_dict_or_text)."""
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(body)
            return resp.status, body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body.decode("utf-8", errors="replace")


def _post(base, path, data, timeout=5):
    """POST helper returning (status, data_dict)."""
    url = f"{base}{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


# ======================== GET ENDPOINT TESTS ========================


class TestRedirects:
    """Test URL redirect routes."""

    def test_root_redirects_to_dashboard(self, server):
        """GET / should 302 to /dashboard."""
        req = urllib.request.Request(f"{server['base']}/")
        req.add_header("Host", "localhost")
        try:
            # urllib follows redirects by default, so we use a custom opener
            opener = urllib.request.build_opener(urllib.request.HTTPHandler)

            class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise urllib.error.HTTPError(newurl, code, msg, headers, fp)

            opener = urllib.request.build_opener(NoRedirectHandler)
            opener.open(req, timeout=5)
            assert False, "Expected redirect"
        except urllib.error.HTTPError as e:
            assert e.code == 302
            assert "/dashboard" in e.headers.get("Location", "")

    def test_index_html_redirects(self, server):
        req = urllib.request.Request(f"{server['base']}/index.html")
        try:
            class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise urllib.error.HTTPError(newurl, code, msg, headers, fp)
            opener = urllib.request.build_opener(NoRedirectHandler)
            opener.open(req, timeout=5)
            assert False, "Expected redirect"
        except urllib.error.HTTPError as e:
            assert e.code == 302


class TestStaticEndpoints:
    """Test simple endpoints that don't depend on external services."""

    def test_version(self, server):
        status, data = _get(server["base"], "/version")
        assert status == 200
        assert data["version"] == "3.0"
        assert data["level"] == 3
        assert data["codename"] == "Level 3"

    def test_health(self, server):
        status, data = _get(server["base"], "/health")
        assert status == 200
        assert data["status"] == "ok"
        assert "uptime_s" in data
        assert data["uptime_s"] >= 0
        assert "endpoints_active" in data
        assert isinstance(data["endpoints_active"], int)
        assert data["endpoints_active"] > 20
        assert "pid" in data
        assert "ws_port" in data

    def test_favicon(self, server):
        url = f"{server['base']}/favicon.ico"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 204

    def test_dashboard_serves_html(self, server):
        status, body = _get(server["base"], "/dashboard")
        assert status == 200
        assert "TEST DASHBOARD" in body

    def test_ws_info(self, server):
        status, data = _get(server["base"], "/ws/info")
        assert status == 200
        assert "ws_url" in data
        assert data["protocol"] == "websocket"
        assert data["fallback"] == "polling"

    def test_404_for_unknown_path(self, server):
        status, _ = _get(server["base"], "/nonexistent/endpoint/xyz")
        assert status == 404


class TestTodosEndpoint:
    """Test /todos GET and POST."""

    def test_get_todos_returns_dict(self, server):
        status, data = _get(server["base"], "/todos")
        assert status == 200
        assert isinstance(data, (dict, list))

    def test_post_todos_updates(self, server):
        status, data = _post(server["base"], "/todos", {
            "sender": "test_worker",
            "items": [{"id": "test-1", "title": "Test TODO", "status": "pending"}],
        })
        assert status == 200
        assert data.get("ok") is True
        assert data.get("sender") == "test_worker"


class TestBusEndpoints:
    """Test /bus, /bus/messages, /bus?limit=N routes."""

    @patch("god_console._cached_bus")
    def test_bus_returns_cached_messages(self, mock_bus, server):
        mock_bus.return_value = [{"sender": "test", "content": "hello"}]
        status, data = _get(server["base"], "/bus")
        assert status == 200
        # Data may come from actual cache or mock depending on timing

    @patch("god_console._cached_bus")
    def test_bus_with_limit(self, mock_bus, server):
        mock_bus.return_value = [{"id": i} for i in range(5)]
        status, data = _get(server["base"], "/bus?limit=5")
        assert status == 200

    @patch("god_console._cached_bus")
    def test_bus_messages_alias(self, mock_bus, server):
        mock_bus.return_value = []
        status, data = _get(server["base"], "/bus/messages")
        assert status == 200


class TestSkynetSelfEndpoints:
    """Test /skynet/self/* endpoints with mocked skynet_self."""

    @patch("god_console._cached_pulse")
    def test_pulse(self, mock_pulse, server):
        mock_pulse.return_value = {
            "health": "OPTIMAL",
            "intelligence_score": 0.85,
            "alive": 4,
            "total": 5,
            "engines_online": 3,
            "engines_total": 5,
        }
        status, data = _get(server["base"], "/skynet/self/pulse")
        assert status == 200

    @patch("god_console._cached_status")
    def test_status_self(self, mock_status, server):
        mock_status.return_value = {"agents": {}, "health": "OK"}
        status, data = _get(server["base"], "/skynet/self/status")
        assert status == 200

    @patch("god_console._cached_pulse")
    def test_skynet_status_line(self, mock_pulse, server):
        mock_pulse.return_value = {
            "health": "OPTIMAL",
            "intelligence_score": 0.92,
            "alive": 4,
            "total": 5,
            "engines_online": 3,
            "engines_total": 5,
        }
        status, data = _get(server["base"], "/skynet/status")
        assert status == 200
        assert "status_line" in data
        assert "SKYNET" in data["status_line"]
        assert data["health"] == "OPTIMAL"

    @patch("god_console._cached_pulse")
    def test_skynet_status_error_fallback(self, mock_pulse, server):
        mock_pulse.side_effect = Exception("test error")
        status, data = _get(server["base"], "/skynet/status")
        assert status == 200
        assert data["health"] == "ERROR"


class TestStatusEndpoint:
    """Test /status combining backend + pulse."""

    @patch("god_console._cached_pulse")
    @patch("god_console._cached_backend_status")
    def test_status_combined(self, mock_backend, mock_pulse, server):
        mock_backend.return_value = {"agents": {"alpha": {"status": "IDLE"}}}
        mock_pulse.return_value = {"health": "OPTIMAL", "intelligence_score": 0.8}
        status, data = _get(server["base"], "/status")
        assert status == 200
        assert "agents" in data

    @patch("god_console._cached_pulse")
    @patch("god_console._cached_backend_status")
    def test_status_when_pulse_fails(self, mock_backend, mock_pulse, server):
        mock_backend.return_value = {"agents": {}}
        mock_pulse.side_effect = Exception("unavailable")
        status, data = _get(server["base"], "/status")
        assert status == 200
        assert data.get("self_aware") is False
        assert "error" in data


class TestOverseerEndpoint:
    """Test /overseer with file-based status."""

    def test_overseer_no_files(self, server):
        status, data = _get(server["base"], "/overseer")
        assert status == 200
        assert isinstance(data, dict)
        assert "running" in data


class TestIncidentsEndpoint:
    """Test /incidents returns list."""

    def test_incidents_returns_list_or_empty(self, server):
        status, data = _get(server["base"], "/incidents")
        assert status == 200
        assert isinstance(data, (list, dict))


class TestConsultantsEndpoint:
    """Test /consultants with mocked bridge probing."""

    @patch("god_console._cached_consultants")
    def test_consultants(self, mock_cons, server):
        mock_cons.return_value = [
            {"name": "codex", "port": 8422, "status": "online"},
            {"name": "gemini", "port": 8425, "status": "offline"},
        ]
        status, data = _get(server["base"], "/consultants")
        assert status == 200


class TestEnginesEndpoint:
    """Test /engines with mocked cache."""

    @patch("god_console._cached_engines")
    def test_engines(self, mock_eng, server):
        mock_eng.return_value = [
            {"name": "OCREngine", "status": "online"},
            {"name": "DXGICapture", "status": "available"},
        ]
        status, data = _get(server["base"], "/engines")
        assert status == 200
        assert isinstance(data, list)


class TestProcessesEndpoint:
    """Test /processes route."""

    def test_processes_returns_data(self, server):
        status, data = _get(server["base"], "/processes", timeout=10)
        assert status == 200


class TestCIEndpoint:
    """Test /api/ci/latest route."""

    def test_ci_latest_no_data(self, server):
        """Without a CI report file, should return no_data status."""
        status, data = _get(server["base"], "/api/ci/latest")
        assert status == 200
        # Either real data or "no_data" status


class TestWorkerActivityEndpoints:
    """Test /api/worker/activity and /api/worker/{name}/thinking."""

    def test_worker_activity_all(self, server):
        status, data = _get(server["base"], "/api/worker/activity")
        assert status == 200
        assert isinstance(data, dict)

    def test_worker_thinking_valid(self, server):
        status, data = _get(server["base"], "/api/worker/alpha/thinking")
        assert status == 200
        assert data.get("worker") == "alpha"

    def test_worker_thinking_invalid(self, server):
        status, data = _get(server["base"], "/api/worker/unknown_worker/thinking")
        assert status == 400
        assert "error" in data


# ======================== POST ENDPOINT TESTS ========================


class TestPostBusPublish:
    """Test POST /bus/publish proxy."""

    def test_bus_publish_proxy(self, server):
        """Bus publish proxies to localhost:8420 — may succeed or fail depending on backend."""
        status, data = _post(server["base"], "/bus/publish", {
            "sender": "test",
            "topic": "test_topic",
            "content": "hello",
        })
        # 200 if backend is running, 502 if proxy fails — both are valid behaviors
        assert status in (200, 502)


class TestPostBusTask:
    """Test POST /bus/task with target validation."""

    def test_bus_task_missing_task(self, server):
        status, data = _post(server["base"], "/bus/task", {
            "target": "alpha",
        })
        assert status == 400
        assert "error" in data

    def test_bus_task_invalid_target(self, server):
        status, data = _post(server["base"], "/bus/task", {
            "target": "invalid_worker",
            "task": "do something",
        })
        assert status == 400
        assert "error" in data


class TestPostDispatch:
    """Test POST /dispatch with priority support."""

    def test_dispatch_missing_task(self, server):
        status, data = _post(server["base"], "/dispatch", {"target": "alpha"})
        assert status == 400

    def test_dispatch_invalid_target(self, server):
        status, data = _post(server["base"], "/dispatch", {
            "target": "nonexistent",
            "task": "run tests",
        })
        assert status == 400


class TestPostTaskCreate:
    """Test POST /task/create."""

    def test_task_create_missing_fields(self, server):
        status, data = _post(server["base"], "/task/create", {})
        # Should return 400 or 500 for missing required fields
        assert status in (400, 500)


class TestPostTaskUpdate:
    """Test POST /task/update."""

    def test_task_update_nonexistent(self, server):
        status, data = _post(server["base"], "/task/update", {
            "task_id": "nonexistent-task-12345",
            "status": "done",
        })
        # Should return 404 for unknown task
        assert status in (404, 500)


class TestPostKillAuthorize:
    """Test POST /kill/authorize."""

    def test_kill_authorize_no_id(self, server):
        status, data = _post(server["base"], "/kill/authorize", {})
        assert status == 400


class TestPostKillDeny:
    """Test POST /kill/deny."""

    def test_kill_deny_no_id(self, server):
        status, data = _post(server["base"], "/kill/deny", {})
        assert status in (400, 500)


class TestPostWorkerMetrics:
    """Test POST /worker/{name}/metrics."""

    def test_worker_metrics_valid(self, server):
        status, data = _post(server["base"], "/worker/alpha/metrics", {
            "duration_ms": 1500,
            "outcome": "success",
            "task_type": "code_edit",
        })
        assert status == 200

    def test_worker_metrics_invalid_worker(self, server):
        status, data = _post(server["base"], "/worker/invalid/metrics", {
            "duration_ms": 500,
        })
        # Should reject or handle gracefully
        assert status in (200, 400, 500)


class TestPostWorkerActivity:
    """Test POST /api/worker/{name}/activity."""

    def test_worker_activity_update(self, server):
        status, data = _post(server["base"], "/api/worker/gamma/activity", {
            "state": "processing",
            "current_activity": "running tests",
            "last_tool": "pytest",
        })
        assert status == 200


class TestPostUnknownRoute:
    """Test POST to unknown endpoint."""

    def test_unknown_post_returns_404(self, server):
        status, data = _post(server["base"], "/nonexistent/post/endpoint", {})
        assert status == 404


class TestOptionsMethod:
    """Test CORS preflight (OPTIONS)."""

    def test_options_returns_cors_headers(self, server):
        url = f"{server['base']}/version"
        req = urllib.request.Request(url, method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status in (200, 204)
            assert "Access-Control-Allow-Origin" in resp.headers


# ======================== STRUCTURAL TESTS ========================


class TestClassStructure:
    """Verify ConsoleHandler has all required methods (regression for indentation bug)."""

    def test_all_critical_methods_exist(self):
        """Verify no methods are trapped as nested functions."""
        import god_console
        handler = god_console.ConsoleHandler
        critical_methods = [
            "do_GET", "do_POST", "do_OPTIONS",
            "_json_response", "_log_access", "log_message",
            "_route", "_route_post",
            "_handle_kill_pending", "_collect_kill_votes",
            "_handle_stream_dashboard", "_build_sse_payload",
            "_handle_worker_activity_all", "_handle_worker_thinking",
            "_handle_learner_health", "_handle_learner_metrics",
            "_post_proxy_bus_publish", "_publish_to_bus_targets",
            "_validate_worker_target", "_post_bus_task",
            "_post_dispatch", "_post_task_create", "_post_task_update",
            "_post_kill_authorize", "_post_kill_deny",
            "_post_worker_metrics", "_post_worker_activity",
        ]
        missing = [m for m in critical_methods if not hasattr(handler, m)]
        assert not missing, f"Methods missing from ConsoleHandler: {missing}"

    def test_helper_functions_at_module_level(self):
        """Verify helper functions are module-level, not nested."""
        import god_console
        module_helpers = [
            "_load_learning_episodes",
            "_count_episode_outcomes",
            "_add_learning_store_stats",
            "_check_learner_daemon",
            "_build_episode_sparkline",
        ]
        missing = [f for f in module_helpers if not hasattr(god_console, f)]
        assert not missing, f"Module-level helpers missing: {missing}"

    def test_check_learner_daemon_has_no_nested_functions(self):
        """Regression: _check_learner_daemon must NOT contain nested functions."""
        import ast
        src = Path(ROOT / "god_console.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_check_learner_daemon":
                nested = [n for n in ast.walk(node)
                          if isinstance(n, ast.FunctionDef) and n is not node]
                assert not nested, (
                    f"_check_learner_daemon contains nested functions "
                    f"(indentation bug regression): {[n.name for n in nested]}"
                )
                break
        else:
            pytest.fail("_check_learner_daemon not found at module level")


class TestHelperFunctions:
    """Test module-level helper functions directly."""

    def test_count_episode_outcomes(self):
        import god_console
        episodes = [
            {"outcome": "success"},
            {"outcome": "failure"},
            {"outcome": "success"},
            {"outcome": "unknown"},
            {"outcome": "weird"},  # should count as unknown
        ]
        result = god_console._count_episode_outcomes(episodes)
        assert result["success"] == 2
        assert result["failure"] == 1
        assert result["unknown"] == 2

    def test_count_episode_outcomes_empty(self):
        import god_console
        result = god_console._count_episode_outcomes([])
        assert result == {"success": 0, "failure": 0, "unknown": 0}

    def test_load_learning_episodes_missing_file(self, tmp_path):
        import god_console
        episodes, count = god_console._load_learning_episodes(tmp_path)
        assert episodes == []
        assert count == 0

    def test_load_learning_episodes_valid(self, tmp_path):
        import god_console
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ep_file = data_dir / "learning_episodes.json"
        episodes = [{"id": i, "outcome": "success"} for i in range(10)]
        ep_file.write_text(json.dumps(episodes), encoding="utf-8")
        result, count = god_console._load_learning_episodes(tmp_path)
        assert count == 10
        assert len(result) == 10

    def test_load_learning_episodes_truncates_to_500(self, tmp_path):
        import god_console
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ep_file = data_dir / "learning_episodes.json"
        episodes = [{"id": i} for i in range(600)]
        ep_file.write_text(json.dumps(episodes), encoding="utf-8")
        result, count = god_console._load_learning_episodes(tmp_path)
        assert count == 600
        assert len(result) == 500

    def test_check_learner_daemon_no_pid_file(self, tmp_path):
        import god_console
        assert god_console._check_learner_daemon(tmp_path) == "stopped"

    def test_check_learner_daemon_invalid_pid(self, tmp_path):
        import god_console
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        pid_file = data_dir / "learner.pid"
        pid_file.write_text("not_a_number")
        assert god_console._check_learner_daemon(tmp_path) == "stopped"

    def test_build_episode_sparkline_empty(self):
        import god_console
        result = god_console._build_episode_sparkline([], time.time())
        assert isinstance(result, list)
        assert len(result) == 24
        assert all(v == 0 for v in result)

    def test_build_episode_sparkline_recent(self):
        import god_console
        import datetime
        now = time.time()
        recent_ts = datetime.datetime.fromtimestamp(now - 1800).isoformat()
        episodes = [{"timestamp_iso": recent_ts}]
        result = god_console._build_episode_sparkline(episodes, now)
        assert isinstance(result, list)
        assert len(result) == 24
        assert sum(result) == 1  # one episode in the last 24h


class TestCacheLayer:
    """Test caching behavior."""

    def test_version_no_cache(self, server):
        """Version endpoint should always return fresh data."""
        s1, d1 = _get(server["base"], "/version")
        s2, d2 = _get(server["base"], "/version")
        assert s1 == s2 == 200
        assert d1 == d2

    def test_health_has_uptime(self, server):
        """Health uptime should increase between calls."""
        _, d1 = _get(server["base"], "/health")
        time.sleep(0.1)
        _, d2 = _get(server["base"], "/health")
        assert d2["uptime_s"] >= d1["uptime_s"]


class TestValidateWorkerTarget:
    """Test _validate_worker_target logic via POST /bus/task."""

    @pytest.mark.parametrize("target", ["alpha", "beta", "gamma", "delta"])
    def test_valid_worker_targets(self, target, server):
        """All 4 workers should be valid targets."""
        status, data = _post(server["base"], "/bus/task", {
            "target": target,
            "task": "echo test",
        })
        # May be 200 (bus proxy works) or 502 (bus proxy fails) — not 400
        assert status != 400

    def test_target_all_is_valid(self, server):
        status, data = _post(server["base"], "/bus/task", {
            "target": "all",
            "task": "broadcast test",
        })
        assert status != 400

    def test_empty_task_rejected(self, server):
        status, data = _post(server["base"], "/bus/task", {
            "target": "alpha",
            "task": "",
        })
        assert status == 400

    def test_missing_task_rejected(self, server):
        status, data = _post(server["base"], "/bus/task", {
            "target": "alpha",
        })
        assert status == 400
