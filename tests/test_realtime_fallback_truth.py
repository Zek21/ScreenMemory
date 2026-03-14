import json
import time


def _write_sse_backed_realtime_state(data_dir, sse_pid=4242):
    (data_dir / "realtime.json").write_text(
        json.dumps(
            {
                "timestamp": time.time(),
                "last_update": "2026-03-14T05:36:31+00:00",
                "update_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "sse_daemon.pid").write_text(str(sse_pid), encoding="utf-8")
    return sse_pid


def test_arch_verify_accepts_sse_realtime_fallback(monkeypatch, tmp_path):
    import tools.skynet_arch_verify as arch_verify

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sse_pid = _write_sse_backed_realtime_state(data_dir)

    monkeypatch.setattr(arch_verify, "ROOT", tmp_path)
    monkeypatch.setattr(arch_verify, "DATA", data_dir)
    monkeypatch.setattr(arch_verify, "EXPECTED_DAEMONS", {"skynet_realtime": "data/realtime.pid"})
    monkeypatch.setattr(arch_verify, "_pid_alive", lambda pid: pid == sse_pid)

    result = arch_verify.check_daemon_ecosystem()

    assert result["status"] == "PASS"
    assert any("FALLBACK_OK" in detail for detail in result["details"])
    # signed: consultant


def test_daemon_health_marks_realtime_alive_from_sse_fallback(monkeypatch, tmp_path):
    import tools.daemon_health as daemon_health

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sse_pid = _write_sse_backed_realtime_state(data_dir)

    monkeypatch.setattr(daemon_health, "DATA", data_dir)
    monkeypatch.setattr(daemon_health, "_alive", lambda pid: pid == sse_pid)

    result = daemon_health.check_daemon("realtime")

    assert result["alive"] is True
    assert result["fallback"] is True
    assert "sse_daemon" in result["detail"]
    # signed: consultant


def test_daemon_health_skips_realtime_fix_when_sse_fallback_is_live(monkeypatch, tmp_path):
    import tools.daemon_health as daemon_health

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sse_pid = _write_sse_backed_realtime_state(data_dir)

    monkeypatch.setattr(daemon_health, "ROOT", tmp_path)
    monkeypatch.setattr(daemon_health, "DATA", data_dir)
    monkeypatch.setattr(daemon_health, "_alive", lambda pid: pid == sse_pid)

    popen_called = {"value": False}

    def fake_popen(*args, **kwargs):
        popen_called["value"] = True
        raise AssertionError("realtime fallback should skip subprocess start")

    monkeypatch.setattr(daemon_health.subprocess, "Popen", fake_popen)

    assert daemon_health.fix_daemon("realtime") is True
    assert popen_called["value"] is False
    # signed: consultant


def test_daemon_health_respects_disabled_sentinel(monkeypatch, tmp_path):
    import tools.daemon_health as daemon_health

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "self_prompt.disabled").write_text("", encoding="utf-8")

    monkeypatch.setattr(daemon_health, "DATA", data_dir)

    result = daemon_health.check_daemon("self_prompt")

    assert result["disabled"] is True
    assert result["alive"] is False
    assert "disabled" in result["detail"]
    # signed: consultant


def test_skynet_start_respects_disabled_sentinel(monkeypatch, tmp_path):
    import tools.skynet_start as skynet_start

    data_dir = tmp_path / "data"
    tools_dir = tmp_path / "tools"
    data_dir.mkdir()
    tools_dir.mkdir()

    pid_file = data_dir / "self_prompt.pid"
    script_path = tools_dir / "skynet_self_prompt.py"
    script_path.write_text("print('noop')\n", encoding="utf-8")
    (data_dir / "self_prompt.disabled").write_text("", encoding="utf-8")

    popen_called = {"value": False}

    def fake_popen(*args, **kwargs):
        popen_called["value"] = True
        raise AssertionError("disabled daemon should not be started")

    monkeypatch.setattr(skynet_start, "DATA_DIR", data_dir)
    monkeypatch.setattr(skynet_start.subprocess, "Popen", fake_popen)

    result = skynet_start._start_daemon_safe(str(script_path), pid_file, "Self-prompt daemon", extra_args=["start"])

    assert result is None
    assert popen_called["value"] is False
    # signed: consultant


def test_daemon_status_marks_disabled_sentinel(monkeypatch, tmp_path):
    import tools.skynet_daemon_status as daemon_status

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "self_prompt.disabled").write_text("", encoding="utf-8")

    monkeypatch.setattr(daemon_status, "ROOT", tmp_path)
    monkeypatch.setattr(daemon_status, "DATA_DIR", data_dir)

    daemon = {
        "name": "self_prompt",
        "label": "Self-Prompt Heartbeat",
        "criticality": "MODERATE",
        "pid_file": "data/self_prompt.pid",
        "port": None,
        "health_url": None,
    }
    result = daemon_status.check_daemon(daemon)

    assert result["disabled"] is True
    assert result["alive"] is False
    assert result["disabled_file"] == "data/self_prompt.disabled"
    # signed: consultant


def test_daemon_status_restart_skips_disabled(monkeypatch):
    import tools.skynet_daemon_status as daemon_status

    daemon = {
        "name": "self_prompt",
        "label": "Self-Prompt Heartbeat",
        "criticality": "MODERATE",
        "restart_cmd": [daemon_status.PYTHON, "tools/skynet_self_prompt.py", "start"],
    }

    popen_called = {"value": False}

    def fake_popen(*args, **kwargs):
        popen_called["value"] = True
        raise AssertionError("disabled daemon should not be restarted")

    monkeypatch.setattr(daemon_status, "DAEMON_REGISTRY", [daemon])
    monkeypatch.setattr(daemon_status.subprocess, "Popen", fake_popen)

    result = daemon_status.restart_dead_daemons([
        {"name": "self_prompt", "label": "Self-Prompt Heartbeat", "alive": False, "disabled": True}
    ])

    assert result == [{"name": "self_prompt", "action": "skip", "reason": "disabled"}]
    assert popen_called["value"] is False
    # signed: consultant


def test_daemon_health_uses_start_mode_for_overseer():
    import tools.daemon_health as daemon_health

    assert daemon_health.DAEMONS["overseer"][1] == ["skynet_overseer.py", "start"]
    # signed: consultant
