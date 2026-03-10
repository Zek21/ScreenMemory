"""
Autonomous Web Navigator.

Combines visual grounding, hierarchical planning, episodic memory,
and self-reflective feedback to navigate web interfaces autonomously.

This is the integration layer that connects all cognitive components
into a unified navigation agent.

Architecture:
    ┌─────────────────────────────────────────────────────┐
    │                  NAVIGATOR                          │
    │                                                     │
    │   Goal ──▶ Planner ──▶ Subtask Loop:               │
    │              │           ├─ Capture Screen           │
    │              │           ├─ Ground (SoM Markers)     │
    │              │           ├─ Decide Action (VLM)      │
    │              │           ├─ Execute (click/type/key)  │
    │              │           ├─ Verify (Reflector)        │
    │              │           └─ Store in Memory           │
    │              │                                       │
    │              └─── Replan on failure                   │
    └─────────────────────────────────────────────────────┘

Input Control:
    Uses pyautogui for mouse/keyboard input since we operate at the
    OS level (pixel-based), not through browser DevTools.

LOG FORMAT:
    [NAVIGATOR] goal_received — "Find 5 papers on AI web agents from arxiv"
    [NAVIGATOR] step_1_start — Open Chrome browser
    [NAVIGATOR] capture — 1920x1080, active=Code - Insiders
    [NAVIGATOR] ground — 22 markers overlaid
    [NAVIGATOR] decide — VLM says: "click mark 12 (Chrome in taskbar)"
    [NAVIGATOR] execute — click(1456, 1058)
    [NAVIGATOR] verify — Chrome window opened ✓
    [NAVIGATOR] step_1_complete — 3.2s
"""
import os
import sys
import time
import json
import logging
from typing import Optional, Tuple, List
from pathlib import Path
from PIL import Image

logger = logging.getLogger(__name__)

# Lazy imports for optional dependencies
try:
    import pyautogui
    pyautogui.FAILSAFE = True  # Move mouse to corner to abort
    pyautogui.PAUSE = 0.3     # Brief pause between actions
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False

# Import our modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.grounding.set_of_mark import SetOfMarkGrounding, GroundedScreenshot, UIRegion
from core.cognitive.planner import HierarchicalPlanner, Plan, Subtask, TaskStatus, Action, ActionType
from core.cognitive.memory import EpisodicMemory
from core.activity_log import ActivityLogger, get_logger


class WebNavigator:
    """
    Autonomous web navigator that uses visual grounding and hierarchical
    planning to navigate web interfaces at the pixel level.
    
    Key Capabilities:
    - Visual perception: Sees the screen like a human (screenshots + SoM markers)
    - Spatial interaction: Clicks/types at precise coordinates
    - Self-correction: Verifies outcomes and retries/replans on failure
    - Memory: Remembers what worked and what didn't across sessions
    - Logging: Every action is documented for debugging
    """

    def __init__(self, vlm_analyzer=None, capture_engine=None,
                 change_detector=None, dry_run: bool = False):
        """
        Args:
            vlm_analyzer: ScreenAnalyzer instance for VLM inference
            capture_engine: DXGICapture for screenshots
            change_detector: ChangeDetector for verifying actions
            dry_run: If True, log actions but don't execute (safe testing)
        """
        self.vlm = vlm_analyzer
        self.capture = capture_engine
        self.change_detector = change_detector
        self.dry_run = dry_run

        self.grounder = SetOfMarkGrounding(min_region_size=300, max_regions=25)
        self.memory = EpisodicMemory(working_capacity=7)
        self.planner = HierarchicalPlanner(vlm_analyzer=vlm_analyzer, memory=self.memory)
        self.activity_log = get_logger()

        # State tracking
        self._current_plan: Optional[Plan] = None
        self._action_count = 0
        self._last_screenshot: Optional[Image.Image] = None
        self._last_grounded: Optional[GroundedScreenshot] = None

        if not HAS_PYAUTOGUI and not dry_run:
            logger.warning("pyautogui not installed — navigator will run in dry_run mode")
            self.dry_run = True

    def navigate(self, goal: str) -> dict:
        """
        Execute a full navigation goal autonomously.
        
        Returns:
            dict with: status, steps_completed, total_steps, results, errors
        """
        self.activity_log.log("NAVIGATOR", "goal_received", detail=goal)
        self.memory.store_working(f"Current goal: {goal}", importance=1.0)

        # Create plan
        plan = self.planner.create_plan(goal)
        self._current_plan = plan
        self.activity_log.log("PLANNER", "plan_created",
                              detail=f"{len(plan.subtasks)} subtasks",
                              data={"goal": goal, "steps": [s.description for s in plan.subtasks]})

        # Execute each step
        results = []
        for i in range(len(plan.subtasks)):
            step_result = self._execute_step(plan)
            results.append(step_result)

            if plan.subtasks[plan.current_step - 1].status == TaskStatus.FAILED:
                self.activity_log.log("NAVIGATOR", "step_failed", level="ERROR",
                                      detail=f"Step {i+1} failed, checking if we can continue")
                # Planner handles replanning internally
                if plan.status == TaskStatus.ABORTED:
                    break

        # Summary
        completed = sum(1 for s in plan.subtasks if s.status == TaskStatus.SUCCESS)
        status = "success" if plan.is_complete else "partial" if completed > 0 else "failed"

        summary = {
            "status": status,
            "goal": goal,
            "steps_completed": completed,
            "total_steps": len(plan.subtasks),
            "results": results,
            "errors": [s.error for s in plan.subtasks if s.error],
            "plan_summary": self.planner.get_plan_summary(plan),
        }

        self.activity_log.log("NAVIGATOR", "goal_complete",
                              detail=f"{status}: {completed}/{len(plan.subtasks)} steps",
                              data=summary)
        return summary

    def _execute_step(self, plan: Plan) -> dict:
        """
        Execute one step of the plan using the full perception-action loop:
        1. Capture screenshot
        2. Ground with SoM markers
        3. Decide action (VLM + planner)
        4. Execute action (click/type/key)
        5. Verify outcome
        6. Store in memory
        """
        step_start = time.perf_counter()
        subtask = plan.subtasks[plan.current_step]

        self.activity_log.log("NAVIGATOR", "step_start",
                              detail=f"Step {plan.current_step + 1}/{len(plan.subtasks)}: {subtask.description}")

        # 1. Capture current screen state
        pre_screenshot = self._capture_screen()
        if pre_screenshot is None:
            subtask.status = TaskStatus.FAILED
            subtask.error = "Screen capture failed"
            plan.current_step += 1
            return {"step": subtask.id, "status": "failed", "error": "capture_failed"}

        # 2. Ground with visual markers
        grounded = self.grounder.ground(pre_screenshot)
        self._last_grounded = grounded
        self.activity_log.log("GROUNDING", "markers_overlaid",
                              detail=f"{len(grounded.regions)} regions detected")

        # 3. Decide action based on VLM analysis of marked screenshot
        action = self._decide_action(subtask, grounded)
        self.activity_log.log("NAVIGATOR", "action_decided",
                              detail=f"{action.action_type.value}: {action.target} {action.value}")

        # 4. Execute the action
        self._execute_action(action)
        self._action_count += 1

        # 5. Brief wait for UI to respond
        time.sleep(0.5)

        # 6. Capture post-action screenshot
        post_screenshot = self._capture_screen()

        # 7. Verify outcome
        success = self.planner.verify_outcome(
            subtask,
            pre_screenshot=pre_screenshot,
            post_screenshot=post_screenshot,
            change_detector=self.change_detector,
        )

        if success:
            subtask.status = TaskStatus.SUCCESS
            subtask.completed_at = time.time()
            plan.current_step += 1
        else:
            subtask.retries += 1
            if subtask.retries >= subtask.max_retries:
                subtask.status = TaskStatus.FAILED
                plan.current_step += 1

        # 8. Store in memory
        elapsed = (time.perf_counter() - step_start) * 1000
        self.memory.store_episodic(
            f"Step {subtask.id}: {subtask.description} → {subtask.status.value}",
            tags=["navigation", "step", subtask.status.value],
            source_action="navigate_step",
            importance=0.6 if success else 0.8,
        )

        # Save grounded screenshot for debugging
        debug_path = Path("logs") / f"step_{subtask.id}_{subtask.status.value}.png"
        debug_path.parent.mkdir(exist_ok=True)
        grounded.marked.save(str(debug_path))

        self.activity_log.log("NAVIGATOR", "step_complete",
                              detail=f"{subtask.status.value} ({elapsed:.0f}ms)")

        return {
            "step": subtask.id,
            "description": subtask.description,
            "status": subtask.status.value,
            "action": f"{action.action_type.value}:{action.target}",
            "elapsed_ms": elapsed,
            "regions": len(grounded.regions),
        }

    def _capture_screen(self) -> Optional[Image.Image]:
        """Capture current screen state."""
        try:
            if self.capture:
                img = self.capture.capture()
            else:
                from PIL import ImageGrab
                img = ImageGrab.grab()
            self._last_screenshot = img
            self.activity_log.log("CAPTURE", "frame_acquired",
                                  detail=f"{img.width}x{img.height}")
            return img
        except Exception as e:
            self.activity_log.log("CAPTURE", "frame_failed", level="ERROR", detail=str(e))
            return None

    def _decide_action(self, subtask: Subtask, grounded: GroundedScreenshot) -> Action:
        """
        Use VLM to analyze the grounded screenshot and decide what action to take.
        Falls back to heuristic-based decision if VLM unavailable.
        """
        if self.vlm and self.vlm.is_available:
            # Build context-aware prompt
            memory_context = self.memory.to_context_string(500)
            prompt_parts = [
                f"Task: {subtask.description}",
                f"The screenshot has {len(grounded.regions)} numbered markers on interactive elements.",
                "What action should I take? Reply with: CLICK <mark_number> or TYPE <text> or KEY <key_combo>",
            ]
            if memory_context:
                prompt_parts.append(f"\nContext:\n{memory_context}")

            # Analyze marked screenshot
            analysis = self.vlm.analyze(grounded.marked, detailed=False)
            if analysis:
                logger.info(f"VLM decision: {analysis.description[:100]}")
                return self._parse_vlm_action(analysis.description, grounded)

        # Fallback: simple heuristic
        return self._heuristic_action(subtask, grounded)

    def _parse_vlm_action(self, vlm_response: str, grounded: GroundedScreenshot) -> Action:
        """Parse VLM response into an executable action."""
        response = vlm_response.upper()

        if "CLICK" in response:
            # Extract mark number
            import re
            marks = re.findall(r'CLICK\s*(\d+)', response)
            if marks:
                mark_id = int(marks[0])
                coords = grounded.get_click_coords(mark_id)
                if coords:
                    return Action(
                        action_type=ActionType.CLICK,
                        target=f"mark_{mark_id}",
                        value=f"{coords[0]},{coords[1]}",
                        expected_outcome=f"Clicked element at mark {mark_id}",
                    )

        if "TYPE" in response:
            import re
            text_match = re.findall(r'TYPE\s+"([^"]+)"', response)
            if text_match:
                return Action(
                    action_type=ActionType.TYPE,
                    target="active_element",
                    value=text_match[0],
                    expected_outcome=f"Typed: {text_match[0]}",
                )

        if "KEY" in response:
            import re
            key_match = re.findall(r'KEY\s+(\S+)', response)
            if key_match:
                return Action(
                    action_type=ActionType.KEY,
                    target="keyboard",
                    value=key_match[0],
                    expected_outcome=f"Pressed: {key_match[0]}",
                )

        # Default: click the first region
        if grounded.regions:
            r = grounded.regions[0]
            return Action(
                action_type=ActionType.CLICK,
                target="mark_1",
                value=f"{r.center_x},{r.center_y}",
                expected_outcome="Clicked first detected element",
            )

        return Action(action_type=ActionType.WAIT, target="", value="1000")

    def _heuristic_action(self, subtask: Subtask, grounded: GroundedScreenshot) -> Action:
        """Fallback action selection based on subtask description."""
        desc = subtask.description.lower()

        if "open" in desc or "launch" in desc or "click" in desc:
            if grounded.regions:
                r = grounded.regions[0]
                return Action(
                    action_type=ActionType.CLICK,
                    target="mark_1",
                    value=f"{r.center_x},{r.center_y}",
                    expected_outcome="Clicked target element",
                )

        if "type" in desc or "enter" in desc or "search" in desc:
            query = subtask.description.split(":")[-1].strip() if ":" in subtask.description else ""
            return Action(
                action_type=ActionType.TYPE,
                target="active_field",
                value=query,
                expected_outcome=f"Typed query: {query}",
            )

        if "navigate" in desc or "go to" in desc:
            return Action(
                action_type=ActionType.KEY,
                target="keyboard",
                value="ctrl+l",  # Focus address bar
                expected_outcome="Address bar focused",
            )

        # Default: wait and observe
        return Action(action_type=ActionType.WAIT, target="", value="1000")

    def _execute_action(self, action: Action):
        """
        Execute an action using pyautogui (or simulate in dry_run mode).
        
        LOG FORMAT:
            [NAVIGATOR] execute_click — (1456, 1058) mark_12
            [NAVIGATOR] execute_type — "AI agent research papers"
            [NAVIGATOR] execute_key — ctrl+enter
            [NAVIGATOR] dry_run — click(1456, 1058) [NOT EXECUTED]
        """
        if self.dry_run:
            self.activity_log.log("NAVIGATOR", "dry_run",
                                  detail=f"{action.action_type.value}: {action.target} = {action.value}")
            return

        if not HAS_PYAUTOGUI:
            logger.warning("pyautogui not available, skipping action")
            return

        if action.action_type == ActionType.CLICK:
            coords = action.value.split(",")
            if len(coords) == 2:
                x, y = int(coords[0]), int(coords[1])
                pyautogui.click(x, y)
                self.activity_log.log("NAVIGATOR", "execute_click",
                                      detail=f"({x}, {y}) {action.target}")

        elif action.action_type == ActionType.TYPE:
            pyautogui.typewrite(action.value, interval=0.02)
            self.activity_log.log("NAVIGATOR", "execute_type",
                                  detail=f'"{action.value[:50]}"')

        elif action.action_type == ActionType.KEY:
            keys = action.value.lower().split("+")
            pyautogui.hotkey(*keys)
            self.activity_log.log("NAVIGATOR", "execute_key",
                                  detail=action.value)

        elif action.action_type == ActionType.SCROLL:
            amount = int(action.value) if action.value else -3
            pyautogui.scroll(amount)
            self.activity_log.log("NAVIGATOR", "execute_scroll",
                                  detail=f"amount={amount}")

        elif action.action_type == ActionType.WAIT:
            wait_ms = int(action.value) if action.value else 1000
            time.sleep(wait_ms / 1000)
            self.activity_log.log("NAVIGATOR", "execute_wait",
                                  detail=f"{wait_ms}ms")

    def get_status(self) -> dict:
        """Get navigator status for monitoring."""
        return {
            "has_plan": self._current_plan is not None,
            "plan_progress": self._current_plan.progress if self._current_plan else "N/A",
            "actions_executed": self._action_count,
            "memory": self.memory.get_stats(),
            "dry_run": self.dry_run,
            "vlm_available": self.vlm.is_available if self.vlm else False,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== Autonomous Web Navigator Test (Dry Run) ===\n")

    nav = WebNavigator(dry_run=True)
    result = nav.navigate("Search for latest AI agent research papers")

    print(f"\nResult: {result['status']}")
    print(f"Steps: {result['steps_completed']}/{result['total_steps']}")
    print(f"\n{result['plan_summary']}")
    print(f"\nMemory: {json.dumps(nav.memory.get_stats())}")
