"""Skynet orchestrator compliance rules.

Defines the behavioral constraints the orchestrator must follow and
provides helpers to validate actions against them.
"""

RULES = [
    {
        "id": "TRUTH_PRINCIPLE",
        "rule": "Every piece of data displayed, every metric shown, every status reported, every bus message must reflect REALITY. No fabrication, no decoration, no placeholder data disguised as real data. If unknown, show 'unknown'. If zero, show zero. Silence is truth. Noise without data is a lie.",
        "severity": "sacred",
        "violations": ["fake", "mock", "simulated", "placeholder", "dummy", "random", "ambient", "decorative", "fabricat", "synthetic_data"],
    },
    {
        "id": "NO_DIRECT_EDIT",
        "rule": "Orchestrator NEVER edits files directly. All file changes must be dispatched to workers.",
        "severity": "critical",
        "violations": ["edit", "create", "write", "save", "overwrite", "patch"],
    },
    {
        "id": "DISPATCH_ALL_WORK",
        "rule": "All non-trivial work must be dispatched to workers via skynet_dispatch.py or bus POST.",
        "severity": "critical",
        "violations": ["run_test", "run_script", "execute_code", "build"],
    },
    {
        "id": "CHROME_BRIDGE_PRIMARY",
        "rule": "Chrome Bridge (GodMode/CDP/winctl) is the primary browser automation tool. Never fall back to Playwright or pyautogui when Chrome Bridge is available.",
        "severity": "high",
        "violations": ["playwright", "pyautogui", "selenium"],
    },
    {
        "id": "BUS_POLL_EVERY_TURN",
        "rule": "Orchestrator must poll the message bus on every turn before taking action.",
        "severity": "high",
        "violations": [],
    },
    {
        "id": "WORKER_DELEGATION",
        "rule": "Workers can and should sub-delegate to idle workers for large tasks.",
        "severity": "medium",
        "violations": [],
    },
    {
        "id": "MODEL_GUARD",
        "rule": "All workers and orchestrator must run Claude Opus 4.6 (fast mode) + Copilot CLI at all times.",
        "severity": "critical",
        "violations": ["sonnet", "auto", "haiku", "gpt"],
    },
    {
        "id": "NO_FOCUS_STEAL",
        "rule": "Never steal focus from the orchestrator window. Use Win32 API calls that work without focus.",
        "severity": "high",
        "violations": ["sendkeys", "set_foreground", "activate_window"],
    },
    {
        "id": "REPO_NATIVE_TOOLS",
        "rule": "Always prefer ScreenMemory-native tools over generic alternatives (Desktop over pyautogui, DXGICapture over PIL, OCREngine over raw tesseract).",
        "severity": "medium",
        "violations": ["pyautogui", "PIL.ImageGrab", "subprocess.*tesseract"],
    },
    {
        "id": "WINDOW_HYGIENE",
        "rule": "After every dispatch or operation, verify only Skynet-essential windows remain open. Close stale browser tabs, empty windows, and orphaned processes immediately. The user should NEVER have to ask for cleanup.",
        "severity": "high",
        "violations": [],
    },
    {
        "id": "WIN32_API_ONLY",
        "rule": "All window management uses Win32 API (PostMessage/SendMessage/MoveWindow/ShowWindow via Desktop class or ctypes). NEVER use screen-based input (pyautogui, SendKeys, mouse simulation). Screen-based input is fragile and breaks when the user interacts with the screen.",
        "severity": "critical",
        "violations": ["pyautogui", "sendkeys", "mouse_click", "mouse_move", "keyboard.send"],
    },
]


def get_rules() -> list[dict]:
    """Return the full rules list."""
    return RULES


def get_preamble() -> str:
    """Return a compact preamble string summarising all rules for injection into prompts."""
    lines = ["ORCHESTRATOR COMPLIANCE RULES:"]
    for r in RULES:
        lines.append(f"  [{r['severity'].upper()}] {r['id']}: {r['rule']}")
    return "\n".join(lines)


def validate_compliance(action: str) -> dict:
    """Check an action description against all rules.

    Args:
        action: free-text description of what the orchestrator intends to do.

    Returns:
        dict with 'compliant' (bool), 'violations' (list of triggered rule dicts).
    """
    import re
    action_lower = action.lower()
    triggered = []
    for rule in RULES:
        for keyword in rule.get("violations", []):
            if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', action_lower):
                triggered.append(rule)
                break
    return {
        "compliant": len(triggered) == 0,
        "violations": triggered,
    }
