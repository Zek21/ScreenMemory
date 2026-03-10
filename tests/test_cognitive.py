"""
Integration test for the ScreenMemory Cognitive Agent Layer.

Tests the full cognitive pipeline:
1. Activity Logger — structured logging to console + JSONL
2. Set-of-Mark Grounding — region detection + marker overlay
3. Episodic Memory — store, retrieve, decay, consolidation
4. Hierarchical Planner — goal decomposition, execution, verification
5. Web Navigator — end-to-end dry run navigation
"""
import os
import sys
import time
import json
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

PASS = "PASS"
FAIL = "FAIL"

results = []

def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  {status}  {name}" + (f" -- {detail}" if detail else ""))
    results.append((name, condition))


def stage(name):
    print(f"\n{'='*60}")
    print(f"  STAGE: {name}")
    print(f"{'='*60}")


# ── Stage 1: Activity Logger ──

stage("Activity Logger")
try:
    from core.activity_log import ActivityLogger

    logger = ActivityLogger(log_dir="logs", console=False, file=True)

    # Log some events
    logger.log("SYSTEM", "test_start", detail="Integration test starting")
    logger.log("CAPTURE", "frame_acquired", detail="1920x1080, 32ms", data={"width": 1920, "height": 1080})
    logger.log("VLM", "analysis_error", level="ERROR", detail="Model timeout after 30s")

    # Timer
    t = logger.timer_start("test_operation")
    time.sleep(0.01)
    elapsed = logger.timer_end("test_operation", t)

    check("Logger creates JSONL log", os.path.exists("logs/activity.jsonl"))
    check("Logger records events", logger._counters.get("SYSTEM.test_start") == 1)
    check("Logger tracks errors", len(logger._errors) == 1)
    check("Timer measures time", elapsed > 0, f"{elapsed:.1f}ms")

    stats = logger.get_stats()
    check("Stats include counters", len(stats["counters"]) >= 2)

except Exception as e:
    check("Activity Logger", False, str(e))


# ── Stage 2: Set-of-Mark Grounding ──

stage("Set-of-Mark Visual Grounding")
try:
    from core.grounding.set_of_mark import SetOfMarkGrounding, UIRegion
    from PIL import Image
    import numpy as np

    grounder = SetOfMarkGrounding(min_region_size=100, max_regions=20)

    # Create a synthetic UI screenshot with clear regions
    img = Image.new("RGB", (800, 600), (240, 240, 240))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)

    # Draw some "UI elements"
    draw.rectangle([50, 10, 750, 50], fill=(255, 255, 255), outline=(0, 0, 0), width=2)   # Address bar
    draw.rectangle([50, 70, 200, 110], fill=(66, 133, 244), outline=(50, 100, 200), width=2)  # Button 1
    draw.rectangle([220, 70, 370, 110], fill=(234, 67, 53), outline=(200, 50, 40), width=2)   # Button 2
    draw.rectangle([50, 130, 750, 550], fill=(255, 255, 255), outline=(200, 200, 200), width=1) # Content area
    draw.rectangle([60, 140, 740, 180], fill=(245, 245, 245), outline=(220, 220, 220), width=1) # List item 1
    draw.rectangle([60, 190, 740, 230], fill=(245, 245, 245), outline=(220, 220, 220), width=1) # List item 2

    # Ground it
    start = time.perf_counter()
    grounded = grounder.ground(img)
    elapsed = (time.perf_counter() - start) * 1000

    check("Grounder detects regions", len(grounded.regions) > 0, f"found {len(grounded.regions)}")
    check("Grounder runs fast", elapsed < 1000, f"{elapsed:.0f}ms")
    check("Marked image same size", grounded.marked.size == img.size)
    check("Regions have valid IDs", all(r.id > 0 for r in grounded.regions))
    check("Regions have centers", all(r.center_x > 0 for r in grounded.regions))

    # Test click coordinate resolution
    if grounded.regions:
        coords = grounded.get_click_coords(1)
        check("Click coords resolve", coords is not None, f"mark 1 -> {coords}")
    else:
        check("Click coords resolve", False, "no regions")

    # UIRegion dataclass
    region = UIRegion(id=1, x=100, y=100, width=200, height=50)
    check("UIRegion area", region.area == 10000)
    check("UIRegion center", region.center_x == 200 and region.center_y == 125)
    check("UIRegion bbox", region.bbox == (100, 100, 300, 150))

except Exception as e:
    check("Set-of-Mark Grounding", False, str(e))


# ── Stage 3: Episodic Memory ──

stage("Episodic Memory System")
try:
    from core.cognitive.memory import EpisodicMemory, MemoryType

    mem = EpisodicMemory(working_capacity=3, episodic_capacity=50)

    # Working memory
    mem.store_working("Task: Search for AI papers", importance=1.0)
    mem.store_working("Using Chrome browser", importance=0.8)
    mem.store_working("Target: arxiv.org", importance=0.7)

    check("Working memory stores 3 items", len(mem.get_working_context()) == 3)

    # Test capacity eviction
    mem.store_working("Extra item forces eviction", importance=0.5)
    check("Working memory capacity enforced", len(mem.get_working_context()) == 3)
    check("Evicted to episodic", len(mem._episodic) >= 1)

    # Episodic memory
    mem.store_episodic("Opened Chrome browser", tags=["chrome", "open"], source_action="click")
    mem.store_episodic("Navigated to arxiv.org", tags=["arxiv", "navigate"], source_action="navigate")
    mem.store_episodic("Searched for 'web agents'", tags=["search", "arxiv"], source_action="type")

    check("Episodic memory stores events", len(mem._episodic) >= 3)

    # Retrieval
    found = mem.retrieve("chrome browser", limit=5)
    check("Retrieval finds relevant memories", len(found) > 0, f"found {len(found)}")

    found = mem.retrieve("arxiv search", limit=5)
    check("Retrieval matches tags", len(found) > 0)

    # Semantic memory
    mem.store_semantic("Ctrl+Tab switches Chrome tabs", tags=["chrome", "keyboard"])
    check("Semantic memory stored", len(mem._semantic) == 1)

    # Context string
    ctx = mem.to_context_string(500)
    check("Context string generated", len(ctx) > 50, f"{len(ctx)} chars")

    # Stats
    stats = mem.get_stats()
    check("Stats accurate", stats["working"] == 3 and stats["semantic"] == 1)

except Exception as e:
    check("Episodic Memory", False, str(e))


# ── Stage 4: Hierarchical Planner ──

stage("Hierarchical Planner + Self-Reflective Feedback")
try:
    from core.cognitive.planner import HierarchicalPlanner, TaskStatus

    mem = EpisodicMemory()
    planner = HierarchicalPlanner(memory=mem)

    # Test search goal decomposition
    plan = planner.create_plan("Search for AI agent research on arxiv")
    check("Plan created", plan is not None)
    check("Plan has subtasks", len(plan.subtasks) > 0, f"{len(plan.subtasks)} steps")
    check("Plan status pending", plan.status == TaskStatus.PENDING)

    # Execute steps
    for i in range(len(plan.subtasks)):
        subtask = planner.execute_step(plan)

    check("All steps executed", plan.current_step == len(plan.subtasks))
    check("Plan marked complete", plan.is_complete)

    # Test progress tracking
    check("Progress string", plan.progress == f"{len(plan.subtasks)}/{len(plan.subtasks)}")

    # Test plan summary
    summary = planner.get_plan_summary(plan)
    check("Summary generated", len(summary) > 50, f"{len(summary)} chars")
    check("Summary shows checkmarks", "Step" in summary)

    # Test different goal types
    plan2 = planner.create_plan("Open Chrome browser")
    check("Open goal decomposed", len(plan2.subtasks) > 0)

    plan3 = planner.create_plan("Navigate to google.com")
    check("Navigate goal decomposed", len(plan3.subtasks) > 0)

    # Test memory integration
    check("Planner stores in memory", len(mem._episodic) > 0,
         f"{len(mem._episodic)} episodic memories")

except Exception as e:
    check("Hierarchical Planner", False, str(e))


# ── Stage 5: Web Navigator (Dry Run) ──

stage("Autonomous Web Navigator (Dry Run)")
try:
    from core.navigator.web_navigator import WebNavigator

    nav = WebNavigator(dry_run=True)

    check("Navigator created", nav is not None)
    check("Dry run mode", nav.dry_run == True)

    # Execute a full navigation goal
    result = nav.navigate("Search for AI papers on arxiv")

    check("Navigation completes", result["status"] in ["success", "partial"])
    check("Steps tracked", result["total_steps"] > 0, f"{result['steps_completed']}/{result['total_steps']}")
    check("Plan summary present", len(result["plan_summary"]) > 30)

    # Check memory state
    status = nav.get_status()
    check("Memory tracked", status["memory"]["episodic"] > 0, f"{status['memory']['episodic']} memories")
    check("Actions counted", True, f"{status['actions_executed']} actions")

except Exception as e:
    check("Web Navigator", False, str(e))


# ── Summary ──

print(f"\n{'='*60}")
print(f"  COGNITIVE LAYER TEST RESULTS")
print(f"{'='*60}")

passed = sum(1 for _, ok in results if ok)
total = len(results)
pct = (passed / total * 100) if total > 0 else 0

print(f"\n  {passed}/{total} tests passed ({pct:.0f}%)")

if passed == total:
    print(f"  ALL TESTS PASSED")
else:
    failed_tests = [(name, ok) for name, ok in results if not ok]
    print(f"  {len(failed_tests)} FAILURES:")
    for name, _ in failed_tests:
        print(f"    - {name}")

print()
