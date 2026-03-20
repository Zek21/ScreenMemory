"""
╔══════════════════════════════════════════════════════════════════════════╗
║                    A U T O N O M O U S   A G E N T                      ║
║                                                                          ║
║  The complete autonomous execution loop for AI-driven web navigation.    ║
║  Perceives → Decides → Executes → Loops until task complete.             ║
║                                                                          ║
║  Components:                                                             ║
║    1. StealthLauncher   — Invisible Chrome (headless/hidden/background)  ║
║    2. ActionProtocol    — Structured LLM ↔ Agent communication format    ║
║    3. ActionExecutor    — Maps LLM decisions → CDP commands              ║
║    4. AutonomousAgent   — The perceive→decide→execute→verify loop        ║
║    5. SessionManager    — Persistent state, history, rollback            ║
║                                                                          ║
║  Sits on top of: god_mode.py (perception) + cdp.py (execution)          ║
║                                                                          ║
║  Rules:                                                                  ║
║    • NEVER steal window focus or move physical mouse                     ║
║    • All execution via CDP Input domain or Win32 PostMessage             ║
║    • All perception via structural analysis (zero screenshots)           ║
║    • LLM communication via structured JSON action protocol               ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import json
import time
import os
import sys
import subprocess
import logging
import hashlib
from typing import Optional, List, Dict, Tuple, Any, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger('agent')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from cdp import CDP, CDPError
    from god_mode import GodMode, ElementEmbedding, SpatialReasoner
except ImportError as e:
    logger.error(f"Required modules not found: {e}")
    raise


# ═══════════════════════════════════════════════════════════════════════
# MODULE 1: STEALTH LAUNCHER
# ═══════════════════════════════════════════════════════════════════════

class StealthLauncher:
    """
    Launch Chrome completely invisible to the user.

    Three stealth modes:
      1. HEADLESS  — Chrome --headless=new (no window at all, fastest)
      2. HIDDEN    — Normal Chrome + Win32 SW_HIDE (bypasses bot detection)
      3. OFFSCREEN — Normal Chrome moved to -32000,-32000 (fully rendered but invisible)

    All modes use --remote-debugging-port for CDP control.
    All modes support specific Chrome profiles.
    """

    # Chrome profile paths (auto-detected)
    CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]

    class Mode(Enum):
        HEADLESS = "headless"     # No window, fastest, some sites detect it
        HIDDEN = "hidden"         # Real window hidden via Win32 API
        OFFSCREEN = "offscreen"   # Real window at -32000,-32000

    def __init__(self, port: int = 9222):
        self._port = port
        self._process = None
        self._hwnd = None

    @staticmethod
    def find_chrome() -> Optional[str]:
        """Find Chrome executable on this system."""
        for path in StealthLauncher.CHROME_PATHS:
            resolved = os.path.expandvars(path)
            if os.path.exists(resolved):
                return resolved
        return None

    @staticmethod
    def get_user_data_dir() -> str:
        """Get default Chrome user data directory.

        NOTE: This returns the REAL default Chrome dir, used for reading
        profile metadata (Local State, info_cache). Do NOT pass this to
        --user-data-dir when launching Chrome with CDP — Chrome v146+
        refuses to bind --remote-debugging-port on the default dir.
        Use get_cdp_user_data_dir() for CDP launches instead.
        """
        return os.path.join(
            os.environ.get('LOCALAPPDATA', ''),
            "Google", "Chrome", "User Data"
        )

    @staticmethod
    def get_cdp_user_data_dir() -> str:
        """Get non-default user data directory for CDP-enabled Chrome launches.

        Chrome v146+ refuses to bind --remote-debugging-port when
        --user-data-dir points to the default User Data directory. It prints:
        'DevTools remote debugging requires a non-default data directory'
        and silently skips the debug port.

        This returns a separate directory under the ScreenMemory data/ folder.
        Cookie encryption is path-bound — users must log in fresh when
        switching to this non-default directory.
        """  # signed: alpha
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cdp_dir = os.path.join(repo_root, "data", "chrome_cdp_userdata")
        os.makedirs(cdp_dir, exist_ok=True)
        return cdp_dir

    @staticmethod
    def list_profiles() -> List[Dict]:
        """List all Chrome profiles with their display names.

        Reads from Local State → profile.info_cache which holds the
        actual display names (e.g. "SOCIALS", "Mak", "SERVERS")
        instead of the per-profile Preferences file which often
        just says "Your Chrome".
        """
        user_data = StealthLauncher.get_user_data_dir()
        local_state_path = os.path.join(user_data, "Local State")
        profiles = []

        if not os.path.isfile(local_state_path):
            return profiles

        try:
            with open(local_state_path, 'r', encoding='utf-8') as f:
                local_state = json.load(f)
            info_cache = local_state.get('profile', {}).get('info_cache', {})
            for directory, data in info_cache.items():
                profiles.append({
                    'directory': directory,
                    'name': data.get('name', directory),
                    'shortcut': data.get('shortcut_name', ''),
                    'gaia': data.get('gaia_name', ''),
                    'path': os.path.join(user_data, directory),
                })
        except (json.JSONDecodeError, IOError):
            pass

        return profiles

    @staticmethod
    def resolve_profile(query: str) -> Dict:
        """Resolve a profile by display name, directory, shortcut, or gaia name.

        Supports exact match first, then substring match.
        E.g. "SOCIALS" → Profile 17, "Mak" → Profile 3.

        Raises:
            ValueError: If no matching profile is found.
        """
        profiles = StealthLauncher.list_profiles()
        lowered = query.strip().lower()

        # Exact match first
        for p in profiles:
            candidates = [p['directory'], p['name'], p.get('shortcut', ''), p.get('gaia', '')]
            if any(lowered == c.lower() for c in candidates if c):
                return p

        # Substring match
        for p in profiles:
            candidates = [p['directory'], p['name'], p.get('shortcut', ''), p.get('gaia', '')]
            if any(lowered in c.lower() for c in candidates if c):
                return p

        available = ", ".join(
            f'{p["directory"]} ({p["name"]})' for p in sorted(profiles, key=lambda p: p["directory"])
        )
        raise ValueError(f'Profile "{query}" not found. Available: {available}')

    @staticmethod
    def is_profile_running(profile_dir: str) -> bool:
        """Check if Chrome is already running with this profile directory."""
        try:
            import subprocess as _sp
            result = _sp.run(
                ["wmic", "process", "where", "name='chrome.exe'", "get", "CommandLine"],
                capture_output=True, text=True, timeout=5,
            )
            return f"--profile-directory={profile_dir}" in result.stdout
        except Exception:
            return False

    @classmethod
    def install_extension(cls, profile: str = "SOCIALS",
                          url: str = "chrome://extensions") -> bool:
        """Load Chrome Bridge extension into a real Chrome profile via --load-extension.

        Launches Chrome with the REAL user data directory (not the CDP sandbox)
        and the --load-extension flag. This registers the extension in the target
        profile without any chrome://extensions UI automation.

        No --remote-debugging-port is used because Chrome v146+ refuses CDP
        on the default user data dir. The extension registers on launch anyway.

        Args:
            profile: Profile display name (e.g. "SOCIALS") or directory
                     (e.g. "Profile 17"). Resolved via resolve_profile().
            url: URL to open after launch (default: chrome://extensions for
                 visual verification).

        Returns:
            True if Chrome launched successfully, False otherwise.
        """  # signed: gamma
        chrome_path = cls.find_chrome()
        if not chrome_path:
            logger.error("Chrome not found")
            return False

        ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extension')
        if not os.path.isfile(os.path.join(ext_dir, 'manifest.json')):
            logger.error(f"Extension manifest not found at {ext_dir}")
            return False

        # Resolve profile name to directory
        try:
            resolved = cls.resolve_profile(profile)
            profile_dir = resolved['directory']
            profile_name = resolved['name']
        except ValueError:
            profile_dir = profile
            profile_name = profile

        user_data_dir = cls.get_user_data_dir()
        args = [
            chrome_path,
            f'--user-data-dir={user_data_dir}',
            f'--profile-directory="{profile_dir}"',
            f'--load-extension={ext_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            url,
        ]

        logger.info(f"Installing Chrome Bridge extension to profile "
                     f"{profile_dir} ({profile_name})")
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=creation_flags)
            logger.info(f"Chrome launched with --load-extension for {profile_dir}")
            return True
        except Exception as e:
            logger.error(f"Failed to launch Chrome: {e}")
            return False

    _BASE_CHROME_ARGS = [
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
    ]

    # Auto-resolve Chrome Bridge extension path
    _EXTENSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extension')

    _MODE_ARGS = {
        'HEADLESS': ["--headless=new", "--disable-gpu", "--window-size=1920,1080"],
        'OFFSCREEN': ["--window-position=-32000,-32000", "--window-size=1920,1080"],
        'HIDDEN': ["--window-size=1920,1080"],
    }

    def launch(self, mode: 'StealthLauncher.Mode' = None,
               profile: str = "Default",
               url: str = "about:blank",
               extra_args: List[str] = None) -> CDP:
        """Launch Chrome invisibly and return a CDP connection."""
        if mode is None:
            mode = self.Mode.HEADLESS

        chrome_path = self.find_chrome()
        if not chrome_path:
            raise FileNotFoundError("Chrome not found")

        args = self._build_chrome_args(chrome_path, profile, mode, url, extra_args)
        self._spawn_chrome(args, mode, profile)

        cdp = self._wait_for_cdp(timeout=15)
        if mode == self.Mode.HIDDEN and sys.platform == 'win32':
            self._hide_window()
        return cdp

    def _build_chrome_args(self, chrome_path, profile, mode, url, extra_args):
        """Assemble Chrome command-line arguments.

        Uses get_cdp_user_data_dir() instead of the default Chrome dir because
        Chrome v146+ refuses --remote-debugging-port on the default directory.
        """  # signed: alpha
        args = [
            chrome_path,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self.get_cdp_user_data_dir()}",
            f'--profile-directory="{profile}"',
            *self._BASE_CHROME_ARGS,
            *self._MODE_ARGS.get(mode.name, []),
            *(extra_args or []),
            url,
        ]
        # Auto-load Chrome Bridge extension
        if not any('load-extension' in a for a in args):
            if os.path.isfile(os.path.join(self._EXTENSION_DIR, 'manifest.json')):
                args.insert(-1, f'--load-extension={self._EXTENSION_DIR}')
        return args

    def _spawn_chrome(self, args, mode, profile):
        """Launch the Chrome subprocess."""
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        logger.info(f"Launching Chrome in {mode.value} mode, profile={profile}")
        self._process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )

    def _wait_for_cdp(self, timeout: int = 15) -> CDP:
        """Wait for Chrome CDP port to become available."""
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                cdp = CDP(port=self._port)
                cdp.tabs()  # Verify connection
                return cdp
            except Exception as e:
                last_error = e
                time.sleep(0.5)
        raise TimeoutError(
            f"Chrome CDP not available on port {self._port} after {timeout}s: {last_error}"
        )

    def _hide_window(self):
        """Hide Chrome window using Win32 API (SW_HIDE)."""
        try:
            import ctypes
            import ctypes.wintypes

            SW_HIDE = 0
            pid = self._process.pid

            # EnumWindows callback to find windows belonging to our process
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.wintypes.BOOL,
                ctypes.wintypes.HWND,
                ctypes.wintypes.LPARAM,
            )

            def callback(hwnd, lparam):
                proc_id = ctypes.wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(
                    hwnd, ctypes.byref(proc_id)
                )
                if proc_id.value == pid:
                    ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
                    self._hwnd = hwnd
                return True

            # Wait briefly for window to appear
            time.sleep(1.0)
            ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)

            if self._hwnd:
                logger.info(f"Chrome window hidden (hwnd={self._hwnd})")
            else:
                # Try child processes too (Chrome spawns renderers)
                logger.warning("Could not find Chrome window to hide")

        except ImportError:
            logger.warning("Win32 API not available, cannot hide window")

    def stop(self):
        """Terminate the launched Chrome process."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            logger.info("Chrome process terminated")

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()


# ═══════════════════════════════════════════════════════════════════════
# MODULE 2: ACTION PROTOCOL
# ═══════════════════════════════════════════════════════════════════════

class ActionType(Enum):
    """All possible actions an LLM can instruct."""
    CLICK = "click"
    TYPE = "type"
    PRESS = "press"
    SCROLL = "scroll"
    NAVIGATE = "navigate"
    WAIT = "wait"
    SELECT = "select"
    HOVER = "hover"
    DISMISS = "dismiss"          # Dismiss overlays/modals
    EXTRACT = "extract"          # Extract text from element
    SCREENSHOT = "screenshot"    # Verification screenshot
    DONE = "done"                # Task complete
    FAIL = "fail"                # Task failed, give up


@dataclass
class Action:
    """
    A single action the LLM wants to execute.

    The LLM outputs this as JSON, the executor maps it to CDP commands.

    Examples:
        {"action": "click", "target": "Submit"}
        {"action": "type", "target": "Email", "value": "me@site.com"}
        {"action": "press", "value": "Enter"}
        {"action": "scroll", "direction": "down", "amount": 300}
        {"action": "navigate", "value": "https://example.com"}
        {"action": "wait", "target": "Welcome", "timeout": 10}
        {"action": "done", "reason": "Form submitted successfully"}
    """
    action: str                      # ActionType value
    target: Optional[str] = None     # Semantic target (concept, text, or UID)
    value: Optional[str] = None      # Text to type, URL, key name, etc.
    direction: Optional[str] = None  # For scroll: up/down/left/right
    amount: Optional[int] = None     # For scroll: pixel amount
    timeout: Optional[int] = None    # For wait: seconds
    reason: Optional[str] = None     # For done/fail: explanation
    uid: Optional[int] = None        # Direct element UID reference

    def to_dict(self) -> Dict:
        """Convert to dict, omitting None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @staticmethod
    def from_dict(d: Dict) -> 'Action':
        """Parse from LLM JSON output."""
        return Action(
            action=d.get('action', 'fail'),
            target=d.get('target'),
            value=d.get('value'),
            direction=d.get('direction', 'down'),
            amount=d.get('amount', 300),
            timeout=d.get('timeout', 10),
            reason=d.get('reason'),
            uid=d.get('uid'),
        )

    @staticmethod
    def from_json(text: str) -> 'Action':
        """Parse action from raw LLM text output (extracts JSON from text)."""
        # Try to find JSON in the text
        text = text.strip()

        # Direct JSON
        if text.startswith('{'):
            try:
                return Action.from_dict(json.loads(text))
            except json.JSONDecodeError:
                pass

        # JSON inside markdown code block
        import re
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            try:
                return Action.from_dict(json.loads(json_match.group(1)))
            except json.JSONDecodeError:
                pass

        # JSON anywhere in text
        json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', text)
        if json_match:
            try:
                return Action.from_dict(json.loads(json_match.group(0)))
            except json.JSONDecodeError:
                pass

        # Could not parse — treat as failure
        return Action(action='fail', reason=f'Could not parse action from: {text[:200]}')


@dataclass
class Observation:
    """
    What the agent observed after executing an action.
    Sent back to the LLM as context for the next decision.
    """
    success: bool
    page_url: str = ""
    page_title: str = ""
    page_type: str = ""
    scene: str = ""                  # Compressed scene for LLM
    elements_count: int = 0
    action_result: str = ""          # What happened
    error: str = ""                  # Error if failed
    step: int = 0
    elapsed_ms: int = 0

    def to_prompt(self) -> str:
        """Format observation as LLM prompt context."""
        lines = [
            f"STEP {self.step} RESULT: {'SUCCESS' if self.success else 'FAILED'}",
        ]
        if self.action_result:
            lines.append(f"RESULT: {self.action_result}")
        if self.error:
            lines.append(f"ERROR: {self.error}")
        lines.append(f"URL: {self.page_url}")
        if self.page_type:
            lines.append(f"PAGE TYPE: {self.page_type}")
        lines.append(f"ELEMENTS: {self.elements_count} interactive")
        if self.scene:
            lines.append("")
            lines.append(self.scene)
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return asdict(self)


class ActionProtocol:
    """
    Generates the system prompt and formats perception data for LLM consumption.
    This is the communication protocol between the autonomous agent and any LLM.
    """

    SYSTEM_PROMPT = """You are an autonomous web navigation agent. You receive a structured description of the current web page and must decide the next action to take.

RULES:
1. Respond with EXACTLY ONE JSON action object per turn
2. Use semantic targets (element text/labels), not coordinates
3. Always dismiss overlays/modals before interacting with background elements
4. After typing in a field, press Enter or click Submit to proceed
5. When the task is complete, respond with {"action": "done", "reason": "..."}
6. If stuck after 3 attempts, respond with {"action": "fail", "reason": "..."}

ACTION FORMAT:
{"action": "click", "target": "Button Text"}
{"action": "type", "target": "Field Label", "value": "text to type"}
{"action": "press", "value": "Enter"}
{"action": "scroll", "direction": "down", "amount": 500}
{"action": "navigate", "value": "https://url.com"}
{"action": "wait", "target": "text to wait for", "timeout": 10}
{"action": "dismiss"}
{"action": "done", "reason": "Task completed successfully"}
{"action": "fail", "reason": "Cannot proceed because..."}

Respond with ONLY the JSON action. No explanations."""

    @staticmethod
    def format_task_prompt(task: str, observation: Observation) -> str:
        """Format a complete prompt for the LLM with task + current state."""
        return f"TASK: {task}\n\n{observation.to_prompt()}\n\nWhat is your next action?"

    @staticmethod
    def format_initial_prompt(task: str, scene: str, url: str = "",
                              page_type: str = "") -> str:
        """Format the initial prompt before any actions taken."""
        lines = [
            f"TASK: {task}",
            "",
            f"CURRENT PAGE:",
            f"URL: {url}",
        ]
        if page_type:
            lines.append(f"TYPE: {page_type}")
        lines.append("")
        lines.append(scene)
        lines.append("")
        lines.append("What is your first action?")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# MODULE 3: ACTION EXECUTOR
# ═══════════════════════════════════════════════════════════════════════

class ActionExecutor:
    """
    Executes actions decided by the LLM against a live browser via GOD MODE.

    Maps semantic action descriptions to precise CDP commands.
    Handles UID-based and concept-based targeting.
    Tracks element UIDs across perception cycles.
    """

    def __init__(self, god: GodMode):
        self.god = god
        self._uid_map: Dict[int, Dict] = {}   # UID → element dict
        self._last_elements: List[Dict] = []

    def update_elements(self, elements: List[Dict]):
        """Update the UID→element mapping after a perception cycle."""
        self._last_elements = elements
        self._uid_map = {}
        for i, el in enumerate(elements):
            el['uid'] = i
            self._uid_map[i] = el

    def execute(self, action: Action, tab_id: str = None) -> Dict:
        """
        Execute a single action.

        Returns:
            {'success': bool, 'result': str, 'error': str}
        """
        t0 = time.time()
        action_type = action.action.lower()

        try:
            if action_type == 'click':
                return self._exec_click(action, tab_id)
            elif action_type == 'type':
                return self._exec_type(action, tab_id)
            elif action_type == 'press':
                return self._exec_press(action, tab_id)
            elif action_type == 'scroll':
                return self._exec_scroll(action, tab_id)
            elif action_type == 'navigate':
                return self._exec_navigate(action, tab_id)
            elif action_type == 'wait':
                return self._exec_wait(action, tab_id)
            elif action_type == 'hover':
                return self._exec_hover(action, tab_id)
            elif action_type == 'dismiss':
                return self._exec_dismiss(tab_id)
            elif action_type == 'select':
                return self._exec_select(action, tab_id)
            elif action_type == 'extract':
                return self._exec_extract(action, tab_id)
            elif action_type == 'screenshot':
                return self._exec_screenshot(action, tab_id)
            elif action_type == 'done':
                return {'success': True, 'result': f'Task complete: {action.reason}'}
            elif action_type == 'fail':
                return {'success': False, 'result': '', 'error': f'Agent gave up: {action.reason}'}
            else:
                return {'success': False, 'error': f'Unknown action: {action_type}'}

        except CDPError as e:
            return {'success': False, 'error': f'CDP error: {e}'}
        except Exception as e:
            return {'success': False, 'error': f'Execution error: {e}'}

    def _resolve_target(self, action: Action) -> Optional[Dict]:
        """Resolve an action target to an element dict with coordinates."""
        # By UID
        if action.uid is not None and action.uid in self._uid_map:
            return self._uid_map[action.uid]

        # By semantic concept via GOD MODE
        if action.target:
            results = self.god.find(action.target)
            if results:
                return results[0]

        return None

    def _center_of(self, element: Dict) -> Tuple[int, int]:
        """Get center coordinates of an element."""
        x = int(element.get('x', 0) + element.get('w', 0) / 2)
        y = int(element.get('y', 0) + element.get('h', 0) / 2)
        return x, y

    def _exec_click(self, action: Action, tab_id: str) -> Dict:
        """Execute a click action."""
        el = self._resolve_target(action)
        if not el:
            # Fallback: try god.click which handles string concepts
            ok = self.god.click(action.target or "", tab_id)
            if ok:
                return {'success': True, 'result': f'Clicked "{action.target}"'}
            return {'success': False, 'error': f'Element not found: "{action.target}"'}

        x, y = self._center_of(el)
        tab_id = tab_id or self.god._get_active_tab()
        self.god.cdp.click(tab_id, x, y)
        name = el.get('name', '') or el.get('role', '')
        return {'success': True, 'result': f'Clicked "{name}" at ({x},{y})'}

    def _exec_type(self, action: Action, tab_id: str) -> Dict:
        """Execute a type action (click field + type text)."""
        if not action.value:
            return {'success': False, 'error': 'No value to type'}

        if action.target:
            ok = self.god.find_and_fill(action.target, action.value, tab_id)
            if ok:
                return {'success': True, 'result': f'Typed "{action.value}" into "{action.target}"'}

            # Fallback: find element, click it, type
            el = self._resolve_target(action)
            if el:
                x, y = self._center_of(el)
                tab_id = tab_id or self.god._get_active_tab()
                self.god.cdp.click(tab_id, x, y)
                time.sleep(0.1)
                self.god.cdp.press_key(tab_id, 'a', modifiers=2)  # Ctrl+A
                time.sleep(0.05)
                self.god.cdp.type_text(tab_id, action.value)
                return {'success': True, 'result': f'Typed "{action.value}" into element'}

            return {'success': False, 'error': f'Input not found: "{action.target}"'}

        # No target — type into currently focused element
        tab_id = tab_id or self.god._get_active_tab()
        self.god.cdp.type_text(tab_id, action.value)
        return {'success': True, 'result': f'Typed "{action.value}"'}

    def _exec_press(self, action: Action, tab_id: str) -> Dict:
        """Execute a key press."""
        key = action.value or 'Enter'
        tab_id = tab_id or self.god._get_active_tab()
        self.god.cdp.press_key(tab_id, key)
        return {'success': True, 'result': f'Pressed {key}'}

    def _exec_scroll(self, action: Action, tab_id: str) -> Dict:
        """Execute a scroll action."""
        direction = action.direction or 'down'
        amount = action.amount or 300
        self.god.scroll(direction, amount, tab_id)
        return {'success': True, 'result': f'Scrolled {direction} {amount}px'}

    def _exec_navigate(self, action: Action, tab_id: str) -> Dict:
        """Execute a navigation action."""
        url = action.value
        if not url:
            return {'success': False, 'error': 'No URL provided'}
        self.god.navigate(url, tab_id)
        time.sleep(1)  # Brief wait for navigation
        return {'success': True, 'result': f'Navigated to {url}'}

    def _exec_wait(self, action: Action, tab_id: str) -> Dict:
        """Execute a wait action."""
        target = action.target
        timeout = action.timeout or 10
        ok = self.god.wait_for(text=target, timeout=timeout, tab_id=tab_id)
        if ok:
            return {'success': True, 'result': f'Found "{target}"'}
        return {'success': False, 'error': f'Timeout waiting for "{target}"'}

    def _exec_hover(self, action: Action, tab_id: str) -> Dict:
        """Execute a hover action."""
        el = self._resolve_target(action)
        if not el:
            return {'success': False, 'error': f'Element not found: "{action.target}"'}
        x, y = self._center_of(el)
        tab_id = tab_id or self.god._get_active_tab()
        self.god.cdp.hover(tab_id, x, y)
        return {'success': True, 'result': f'Hovered over "{action.target}" at ({x},{y})'}

    def _exec_dismiss(self, tab_id: str) -> Dict:
        """Dismiss overlays/modals."""
        count = self.god.dismiss_overlays(tab_id)
        if count > 0:
            return {'success': True, 'result': f'Dismissed {count} overlay(s)'}
        return {'success': True, 'result': 'No overlays to dismiss'}

    def _exec_select(self, action: Action, tab_id: str) -> Dict:
        """Select an option from a dropdown."""
        el = self._resolve_target(action)
        if not el:
            return {'success': False, 'error': f'Dropdown not found: "{action.target}"'}
        x, y = self._center_of(el)
        tab_id = tab_id or self.god._get_active_tab()
        self.god.cdp.click(tab_id, x, y)
        time.sleep(0.3)
        # Type the option to filter, then press Enter
        if action.value:
            self.god.cdp.type_text(tab_id, action.value)
            time.sleep(0.2)
            self.god.cdp.press_key(tab_id, 'Enter')
        return {'success': True, 'result': f'Selected "{action.value}" in "{action.target}"'}

    def _exec_extract(self, action: Action, tab_id: str) -> Dict:
        """Extract text from the page or a specific element."""
        tab_id = tab_id or self.god._get_active_tab()
        if action.target:
            # Find element and extract its text
            el = self._resolve_target(action)
            if el:
                text = el.get('name', '') or el.get('value', '')
                return {'success': True, 'result': f'Extracted: "{text}"'}
        # Extract page title
        title = self.god.cdp.eval(tab_id, 'document.title') or ''
        return {'success': True, 'result': f'Page title: "{title}"'}

    def _exec_screenshot(self, action: Action, tab_id: str) -> Dict:
        """Take a verification screenshot."""
        filepath = action.value or os.path.join("screenshots", f"screenshot_{int(time.time())}.png")
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        self.god.screenshot(filepath, tab_id)
        return {'success': True, 'result': f'Screenshot saved: {filepath}'}


# ═══════════════════════════════════════════════════════════════════════
# MODULE 4: SESSION MANAGER
# ═══════════════════════════════════════════════════════════════════════

class SessionManager:
    """
    Tracks the complete session state across multiple action cycles.
    Provides history, rollback, and persistence.
    """

    def __init__(self):
        self.session_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
        self.steps: List[Dict] = []
        self.task: str = ""
        self.start_time: float = 0
        self.status: str = "idle"     # idle, running, done, failed
        self.max_steps: int = 50
        self.urls_visited: List[str] = []

    def start(self, task: str, max_steps: int = 50):
        """Start a new session."""
        self.task = task
        self.start_time = time.time()
        self.status = "running"
        self.max_steps = max_steps
        self.steps = []
        self.urls_visited = []
        logger.info(f"Session {self.session_id} started: {task}")

    def record_step(self, step_num: int, action: Action, observation: Observation):
        """Record a completed step."""
        self.steps.append({
            'step': step_num,
            'action': action.to_dict(),
            'success': observation.success,
            'result': observation.action_result,
            'error': observation.error,
            'url': observation.page_url,
            'page_type': observation.page_type,
            'elapsed_ms': observation.elapsed_ms,
            'timestamp': time.time(),
        })
        if observation.page_url and observation.page_url not in self.urls_visited:
            self.urls_visited.append(observation.page_url)

    def finish(self, success: bool, reason: str = ""):
        """Mark session as complete."""
        self.status = "done" if success else "failed"
        elapsed = time.time() - self.start_time
        logger.info(
            f"Session {self.session_id} {self.status}: {reason} "
            f"({len(self.steps)} steps, {elapsed:.1f}s)"
        )

    def should_stop(self) -> Optional[str]:
        """Check if the session should be stopped (safety limits)."""
        if len(self.steps) >= self.max_steps:
            return f"Maximum steps ({self.max_steps}) reached"

        # Detect loops: same action on same URL 3+ times
        if len(self.steps) >= 3:
            last3 = self.steps[-3:]
            actions = [s['action'].get('action') for s in last3]
            targets = [s['action'].get('target') for s in last3]
            urls = [s['url'] for s in last3]
            if (len(set(map(str, actions))) == 1 and
                    len(set(map(str, targets))) == 1 and
                    len(set(urls)) == 1):
                return "Detected action loop (same action repeated 3x on same page)"

        # Detect continuous failures
        if len(self.steps) >= 5:
            last5 = self.steps[-5:]
            if all(not s['success'] for s in last5):
                return "5 consecutive failures"

        return None

    def summary(self) -> Dict:
        """Generate session summary."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        successes = sum(1 for s in self.steps if s['success'])
        failures = sum(1 for s in self.steps if not s['success'])
        return {
            'session_id': self.session_id,
            'task': self.task,
            'status': self.status,
            'total_steps': len(self.steps),
            'successes': successes,
            'failures': failures,
            'urls_visited': self.urls_visited,
            'elapsed_seconds': round(elapsed, 1),
            'steps': self.steps,
        }

    def save(self, filepath: str = None):
        """Save session to disk."""
        if not filepath:
            filepath = f"session_{self.session_id}.json"
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self.summary(), f, indent=2, default=str)
            logger.info(f"Session saved: {filepath}")
        except IOError as e:
            logger.error(f"Failed to save session: {e}")


# ═══════════════════════════════════════════════════════════════════════
# MODULE 5: AUTONOMOUS AGENT
# ═══════════════════════════════════════════════════════════════════════

class AutonomousAgent:
    """
    The complete autonomous execution loop.

    Perception (GOD MODE) → Decision (LLM) → Execution (CDP) → Verification → Loop

    Usage:
        agent = AutonomousAgent()

        # With a local LLM function:
        agent.run("Search for AI on Wikipedia", decide_fn=my_llm_function)

        # With manual step-by-step control:
        obs = agent.perceive()
        action = Action(action='click', target='Search')
        obs = agent.act(action)

        # Pre-scripted automation (no LLM needed):
        agent.run_script([
            {"action": "navigate", "value": "https://google.com"},
            {"action": "type", "target": "Search", "value": "AI"},
            {"action": "press", "value": "Enter"},
            {"action": "wait", "target": "results"},
            {"action": "done", "reason": "Searched for AI"}
        ])
    """

    def __init__(self, cdp_port: int = 9222, god: GodMode = None):
        """
        Initialize the autonomous agent.

        Args:
            cdp_port: Chrome DevTools Protocol port
            god: Existing GodMode instance (or creates new one)
        """
        self.god = god or GodMode(cdp_port=cdp_port)
        self.executor = ActionExecutor(self.god)
        self.session = SessionManager()
        self.protocol = ActionProtocol()

        # Callbacks
        self._on_step: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_complete: Optional[Callable] = None

    def perceive(self, tab_id: str = None, depth: str = 'standard') -> Observation:
        """Run a full perception cycle via GOD MODE."""
        t0 = time.time()
        try:
            self.god._ensure_modules()
            tab_id = tab_id or self.god._get_active_tab()
            result = self.god.see(tab_id=tab_id, depth=depth)
            elements = result.get('elements', [])
            self.executor.update_elements(elements)
            scene = self.god.scene(tab_id=tab_id)
            url = self._safe_get_url(tab_id)
            title = self._safe_get_title(tab_id)
            return Observation(
                success=True, page_url=url, page_title=title,
                page_type=result.get('page_type', ''), scene=scene,
                elements_count=len(elements),
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            return Observation(
                success=False, error=str(e),
                elapsed_ms=int((time.time() - t0) * 1000),
            )

    def _safe_get_url(self, tab_id: str) -> str:
        """Get tab URL with fallback."""
        try:
            return self.god.cdp.get_url(tab_id) or ""
        except Exception:
            try:
                return self.god.cdp.eval(tab_id, 'window.location.href') or ""
            except Exception:
                return ""

    def _safe_get_title(self, tab_id: str) -> str:
        """Get tab title safely."""
        try:
            return self.god.cdp.eval(tab_id, 'document.title') or ""
        except Exception:
            return ""

    def act(self, action: Action, tab_id: str = None) -> Observation:
        """
        Execute a single action and return the resulting observation.

        This is the core step: execute → wait → perceive → return.
        """
        t0 = time.time()

        # Execute
        exec_result = self.executor.execute(action, tab_id)

        # Wait for page to settle after action
        settle_time = 0.3
        if action.action in ('navigate', 'click'):
            settle_time = 1.0
        elif action.action in ('type', 'press'):
            settle_time = 0.5
        time.sleep(settle_time)

        # Re-perceive
        obs = self.perceive(tab_id)
        obs.success = exec_result.get('success', False)
        obs.action_result = exec_result.get('result', '')
        obs.error = exec_result.get('error', '')
        obs.elapsed_ms = int((time.time() - t0) * 1000)

        return obs

    def run(self, task: str, decide_fn: Callable, max_steps: int = 30,
            tab_id: str = None, on_step: Callable = None) -> Dict:
        """Run the full autonomous loop with an LLM decision function."""
        self.session.start(task, max_steps)
        self._on_step = on_step

        try:
            obs = self.perceive(tab_id)
            if not obs.success:
                self.session.finish(False, f"Initial perception failed: {obs.error}")
                return self.session.summary()

            user_prompt = self.protocol.format_initial_prompt(
                task, obs.scene, obs.page_url, obs.page_type
            )
            self._agent_loop(task, decide_fn, tab_id, obs, user_prompt)
        except KeyboardInterrupt:
            self.session.finish(False, "Interrupted by user")
        except Exception as e:
            self.session.finish(False, f"Unexpected error: {e}")

        return self.session.summary()

    def _agent_loop(self, task, decide_fn, tab_id, obs, user_prompt):
        """Core agent step loop."""
        step = 0
        while True:
            step += 1
            stop_reason = self.session.should_stop()
            if stop_reason:
                self.session.finish(False, stop_reason)
                break

            llm_response = self._get_llm_decision(decide_fn, user_prompt)
            if llm_response is None:
                break

            action = Action.from_json(llm_response)

            if action.action in ('done', 'fail'):
                self._handle_terminal_action(action, step, obs)
                break

            obs = self._execute_step(action, tab_id, step)
            user_prompt = self.protocol.format_task_prompt(task, obs)

    def _get_llm_decision(self, decide_fn, user_prompt):
        """Call LLM for a decision, finishing session on error."""
        try:
            return decide_fn(self.protocol.SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            self.session.finish(False, f"LLM error: {e}")
            return None

    def _handle_terminal_action(self, action, step, obs):
        """Process done/fail terminal actions."""
        success = action.action == 'done'
        reason = action.reason or ("Complete" if success else "Agent gave up")
        term_obs = Observation(
            success=success, step=step,
            page_url=obs.page_url, page_type=obs.page_type,
            **({"action_result": reason} if success else {"error": reason}),
        )
        self.session.record_step(step, action, term_obs)
        self.session.finish(success, reason)

    def _execute_step(self, action, tab_id, step):
        """Execute one action step and record it."""
        obs = self.act(action, tab_id)
        obs.step = step
        self.session.record_step(step, action, obs)
        if self._on_step:
            try:
                self._on_step(step, action, obs)
            except Exception:
                pass
        status = "\u2713" if obs.success else "\u2717"
        logger.info(f"Step {step} {status}: {action.action} \u2192 {obs.action_result or obs.error}")
        return obs

    def run_script(self, actions: List[Dict], tab_id: str = None,
                   delay: float = 0.5) -> Dict:
        """
        Execute a pre-scripted sequence of actions (no LLM needed).

        Args:
            actions: List of action dicts, e.g.:
                [
                    {"action": "navigate", "value": "https://google.com"},
                    {"action": "type", "target": "Search", "value": "AI"},
                    {"action": "press", "value": "Enter"},
                    {"action": "done", "reason": "Searched for AI"}
                ]
            tab_id: Target tab
            delay: Seconds between actions

        Returns:
            Session summary dict
        """
        task = f"Script with {len(actions)} actions"
        self.session.start(task, max_steps=len(actions) + 5)

        for i, action_dict in enumerate(actions, 1):
            action = Action.from_dict(action_dict)

            if action.action in ('done', 'fail'):
                obs = Observation(
                    success=(action.action == 'done'),
                    action_result=action.reason or "",
                    step=i,
                )
                self.session.record_step(i, action, obs)
                self.session.finish(action.action == 'done', action.reason)
                return self.session.summary()

            obs = self.act(action, tab_id)
            obs.step = i
            self.session.record_step(i, action, obs)

            status = "✓" if obs.success else "✗"
            print(f"  [{i}/{len(actions)}] {status} {action.action}: {obs.action_result or obs.error}")

            if not obs.success:
                logger.warning(f"Step {i} failed: {obs.error}")

            time.sleep(delay)

        self.session.finish(True, "Script completed")
        return self.session.summary()

    _INTERACTIVE_BANNER = (
        "╔══════════════════════════════════════╗\n"
        "║  GOD MODE Interactive Agent          ║\n"
        "║  Type actions or 'quit' to exit      ║\n"
        "╚══════════════════════════════════════╝\n"
    )

    def interactive(self, tab_id: str = None):
        """Interactive REPL mode -- type actions manually."""
        print(self._INTERACTIVE_BANNER)
        while True:
            try:
                raw = input("agent> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not raw or raw == 'quit':
                break

            if self._handle_builtin_cmd(raw, tab_id):
                continue

            action_dict = self._parse_interactive_action(raw)
            if action_dict is None:
                continue
            action = Action.from_dict(action_dict)
            obs = self.act(action, tab_id)
            status = "\u2713" if obs.success else "\u2717"
            print(f"  {status} {obs.action_result or obs.error}")
        print("Bye.")

    def _handle_builtin_cmd(self, raw, tab_id) -> bool:
        """Handle built-in REPL commands (see, scene, find). Returns True if handled."""
        if raw == 'see':
            obs = self.perceive(tab_id)
            print(f"URL: {obs.page_url}\nType: {obs.page_type}\nElements: {obs.elements_count}")
            return True
        if raw == 'scene':
            obs = self.perceive(tab_id)
            print(obs.scene)
            return True
        if raw.startswith('find '):
            results = self.god.find(raw[5:], tab_id)
            for r in results[:5]:
                print(f"  [{r.get('similarity', 0):.2f}] {r.get('role', '')} "
                      f"\"{r.get('name', '')}\" @({r.get('x', 0)},{r.get('y', 0)})")
            return True
        return False

    def _parse_interactive_action(self, raw) -> dict:
        """Parse a natural-text action into an action dict, or None on failure."""
        parts = raw.split(maxsplit=2)
        action_type = parts[0].lower()

        parsers = {
            'click':    lambda p: {'target': ' '.join(p[1:])} if len(p) >= 2 else {},
            'type':     lambda p: {'target': p[1], 'value': p[2]} if len(p) >= 3 else {},
            'press':    lambda p: {'value': p[1]} if len(p) >= 2 else {},
            'scroll':   lambda p: self._parse_scroll(p),
            'navigate': lambda p: {'value': p[1]} if len(p) >= 2 else {},
            'wait':     lambda p: {'target': ' '.join(p[1:])} if len(p) >= 2 else {},
            'dismiss':  lambda p: {},
        }

        if action_type in parsers:
            d = {'action': action_type}
            d.update(parsers[action_type](parts))
            return d

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            print(f"Unknown: {raw}")
            return None

    @staticmethod
    def _parse_scroll(parts):
        d = {'direction': parts[1]} if len(parts) >= 2 else {}
        if len(parts) >= 3:
            try:
                d['amount'] = int(parts[2])
            except ValueError:
                pass
        return d


# ═══════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def quick_script(url: str, actions: List[Dict], profile: str = None,
                 port: int = 9222) -> Dict:
    """
    One-liner to run a scripted automation.

    Args:
        url: Starting URL
        actions: List of action dicts
        profile: Chrome profile (None = use existing Chrome)
        port: CDP port

    Returns:
        Session summary

    Example:
        quick_script("https://google.com", [
            {"action": "type", "target": "Search", "value": "AI"},
            {"action": "press", "value": "Enter"},
            {"action": "wait", "target": "results"},
            {"action": "done", "reason": "Searched"}
        ])
    """
    launcher = None
    try:
        if profile:
            # Resolve display name to directory
            try:
                resolved = StealthLauncher.resolve_profile(profile)
                profile_dir = resolved['directory']
            except ValueError:
                profile_dir = profile
            launcher = StealthLauncher(port=port)
            launcher.launch(
                mode=StealthLauncher.Mode.HEADLESS,
                profile=profile_dir,
                url=url
            )
        else:
            # Use existing Chrome
            god = GodMode(cdp_port=port)
            god.navigate(url)
            time.sleep(1)

        agent = AutonomousAgent(cdp_port=port)
        return agent.run_script(actions)

    finally:
        if launcher:
            launcher.stop()


def stealth_open(url: str, profile: str = "Default",
                 mode: str = "headless", port: int = 9222) -> Tuple[StealthLauncher, GodMode]:
    """
    Open a URL stealthily and return launcher + god mode for interactive use.

    Args:
        url: URL to open
        profile: Chrome profile directory or display name (e.g., "SOCIALS", "Mak", "Profile 3")
        mode: "headless", "hidden", or "offscreen"
        port: CDP port

    Returns:
        (StealthLauncher, GodMode) — caller must call launcher.stop() when done

    Example:
        launcher, god = stealth_open("https://facebook.com", "SOCIALS")
        scene = god.scene()
        god.click("Login")
        launcher.stop()
    """
    mode_map = {
        'headless': StealthLauncher.Mode.HEADLESS,
        'hidden': StealthLauncher.Mode.HIDDEN,
        'offscreen': StealthLauncher.Mode.OFFSCREEN,
    }
    # Resolve display name to directory
    try:
        resolved = StealthLauncher.resolve_profile(profile)
        profile_dir = resolved['directory']
    except ValueError:
        profile_dir = profile  # Fallback to raw value

    launcher = StealthLauncher(port=port)
    launcher.launch(
        mode=mode_map.get(mode, StealthLauncher.Mode.HEADLESS),
        profile=profile_dir,
        url=url,
    )
    god = GodMode(cdp_port=port)
    time.sleep(2)  # Let page load
    return launcher, god


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def _cmd_interactive(args):
    if args.url and args.profile:
        launcher, god = stealth_open(args.url, args.profile, args.mode, args.port)
        try:
            AutonomousAgent(god=god).interactive()
        finally:
            launcher.stop()
    else:
        agent = AutonomousAgent(cdp_port=args.port)
        if args.url:
            agent.god.navigate(args.url)
            time.sleep(1)
        agent.interactive()


def _cmd_script(args):
    if not args.script:
        print("Usage: agent.py script --script actions.json [--url URL] [--profile PROFILE]")
        return
    with open(args.script, 'r') as f:
        actions = json.load(f)
    if args.profile:
        result = quick_script(args.url or 'about:blank', actions, profile=args.profile, port=args.port)
    else:
        agent = AutonomousAgent(cdp_port=args.port)
        if args.url:
            agent.god.navigate(args.url)
            time.sleep(1)
        result = agent.run_script(actions)
    print(json.dumps(result, indent=2, default=str))


def _cmd_stealth(args):
    if not args.url:
        print("Usage: agent.py stealth --url URL [--profile PROFILE] [--mode headless]")
        return
    profile = args.profile or 'Default'
    launcher, god = stealth_open(args.url, profile, args.mode, args.port)
    try:
        print(f"Connected to {args.url} (stealth/{args.mode})\nProfile: {profile}\n")
        print(god.scene())
        print("\nPress Enter to close...")
        input()
    finally:
        launcher.stop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Autonomous Agent -- AI-driven web navigation')
    parser.add_argument('command', choices=['interactive', 'script', 'profiles',
                                            'stealth', 'perceive', 'status',
                                            'install-ext'],
                        help='Command to run')
    parser.add_argument('--url', default=None, help='Target URL')
    parser.add_argument('--profile', default=None,
                        help='Chrome profile directory or display name')
    parser.add_argument('--port', type=int, default=9222, help='CDP port')
    parser.add_argument('--mode', default='headless',
                        choices=['headless', 'hidden', 'offscreen'], help='Stealth mode')
    parser.add_argument('--script', default=None, help='Path to JSON script file')
    args = parser.parse_args()

    handlers = {
        'profiles':    lambda: [print(f"  {p['directory']:20s} -> {p['name']}") for p in StealthLauncher.list_profiles()],
        'status':      lambda: print(json.dumps(AutonomousAgent(cdp_port=args.port).god.status(), indent=2)),
        'perceive':    lambda: print(AutonomousAgent(cdp_port=args.port).perceive().to_prompt()),
        'interactive': lambda: _cmd_interactive(args),
        'script':      lambda: _cmd_script(args),
        'stealth':     lambda: _cmd_stealth(args),
        'install-ext': lambda: _cmd_install_ext(args),
    }
    handlers[args.command]()


def _cmd_install_ext(args):
    """Install Chrome Bridge extension into a Chrome profile via --load-extension."""  # signed: gamma
    profile = args.profile or 'SOCIALS'
    print(f"Installing Chrome Bridge extension to profile: {profile}")
    ok = StealthLauncher.install_extension(profile=profile)
    if ok:
        print(f"Chrome launched with --load-extension for {profile}")
        print("Extension should appear in chrome://extensions within seconds.")
    else:
        print("FAILED: Could not launch Chrome for extension install")


if __name__ == '__main__':
    main()
