import json
from types import SimpleNamespace
from unittest.mock import patch

import tools.skynet_draft_inspector as inspector


def test_classify_bus_relay_gate_proposal():
    text = (
        '[BUS RELAY] GATE-PROPOSAL from beta (topic=convene): '
        '"gate_id": "gate_1773252914003_beta", "proposer": "beta", '
        '"report": "DONE: compiled and verified" Reply via bus: import requests'
    )
    assert inspector.classify_draft(text) == "bus_relay_convene_gate_proposal"


def test_inspect_workers_reports_typing_worker_and_bus_correlation(tmp_path, monkeypatch):
    workers_file = tmp_path / "workers.json"
    workers_file.write_text(json.dumps({
        "workers": [
            {"name": "alpha", "hwnd": 111, "grid": "top-left"},
            {"name": "beta", "hwnd": 222, "grid": "top-right"},
        ]
    }), encoding="utf-8")
    (tmp_path / "monitor.pid").write_text("999999", encoding="utf-8")
    (tmp_path / "worker_health.json").write_text(json.dumps({
        "updated": "2020-01-01T00:00:00+00:00"
    }), encoding="utf-8")

    monkeypatch.setattr(inspector, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inspector, "WORKERS_FILE", workers_file)
    monkeypatch.setattr(inspector, "MONITOR_PID_FILE", tmp_path / "monitor.pid")
    monkeypatch.setattr(inspector, "WORKER_HEALTH_FILE", tmp_path / "worker_health.json")
    monkeypatch.setattr(inspector, "_fetch_bus_messages", lambda limit=80: [
        {
            "id": "msg_gate",
            "sender": "beta",
            "topic": "convene",
            "type": "gate-proposal",
            "content": 'gate_id=gate_1773252914003_beta report compiled and verified',
            "timestamp": "2026-03-12T02:30:00+08:00",
        }
    ])

    fake_engine = SimpleNamespace(
        scan_all=lambda hwnds, max_workers=4: {
            "alpha": SimpleNamespace(
                hwnd=111, state="IDLE", edit_value="", model="Pick Model, Claude Opus 4.6 (fast mode)",
                agent="Delegate Session - Copilot CLI", scan_ms=12.0,
            ),
            "beta": SimpleNamespace(
                hwnd=222,
                state="TYPING",
                edit_value=(
                    '[BUS RELAY] GATE-PROPOSAL from beta (topic=convene): '
                    '"gate_id": "gate_1773252914003_beta", "proposer": "beta", '
                    '"report": "DONE: compiled and verified" Reply via bus: import requests'
                ),
                model="Pick Model, Claude Opus 4.6 (fast mode)",
                agent="Delegate Session - Copilot CLI",
                scan_ms=18.5,
            ),
        }
    )

    with patch("tools.uia_engine.get_engine", return_value=fake_engine):
        report = inspector.inspect_workers()

    assert report["monitor"]["running"] is False
    assert report["monitor"]["health_stale"] is True
    assert report["typing_count"] == 1
    assert len(report["workers"]) == 1

    beta = report["workers"][0]
    assert beta["name"] == "beta"
    assert beta["state"] == "TYPING"
    assert beta["classification"] == "bus_relay_convene_gate_proposal"
    assert beta["signals"]["gate_id"] == "gate_1773252914003_beta"
    assert beta["bus_correlation"]["match"] == "gate_id"
    assert beta["bus_correlation"]["type"] == "gate-proposal"
