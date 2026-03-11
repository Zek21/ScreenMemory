import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CRITICAL_DIR = ROOT / "tools" / "critical"


def _load_wrapper(name: str):
    sys.path.insert(0, str(CRITICAL_DIR))
    spec = importlib.util.spec_from_file_location(f"critical_{name}", CRITICAL_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_wrapper_targets_exist():
    wrappers = {
        "monitor": "skynet_monitor.py",
        "watchdog": "skynet_watchdog.py",
        "realtime": "skynet_realtime.py",
        "bus_relay": "skynet_bus_relay.py",
        "worker_check": "skynet_worker_check.py",
        "stuck_detector": "skynet_stuck_detector.py",
        "draft_inspector": "skynet_draft_inspector.py",
        "convene_gate": "convene_gate.py",
    }
    for wrapper_name, target_script in wrappers.items():
        wrapper = _load_wrapper(wrapper_name)
        assert wrapper.TARGET_SCRIPT == target_script
        assert (ROOT / "tools" / target_script).exists()


def test_wrapper_main_delegates_to_bootstrap(monkeypatch):
    wrapper = _load_wrapper("draft_inspector")
    seen = []

    monkeypatch.setattr(wrapper, "run_tool", lambda script_name: seen.append(script_name) or 0)

    assert wrapper.main() == 0
    assert seen == ["skynet_draft_inspector.py"]
