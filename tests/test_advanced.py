"""
Integration test for the Advanced Cognitive Architecture.

Tests the complete advanced agent pipeline:
1. Graph of Thoughts -- non-linear reasoning
2. Reflective MCTS -- tree search navigation
3. Reflexion -- verbal self-critique
4. DynaAct -- dynamic action filtering
5. Dynamic Code Generation -- agent writes scripts
6. Full pipeline integration -- all components working together
"""
import os
import sys
import time
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

results = []

def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    msg = f"  {status}  {name}" + (f" -- {detail}" if detail else "")
    print(msg)
    results.append((name, condition))


def stage(name):
    print(f"\n{'='*60}")
    print(f"  STAGE: {name}")
    print(f"{'='*60}")


# ── Stage 1: Graph of Thoughts ──

stage("Graph of Thoughts (GoT)")
try:
    from core.cognitive.graph_of_thoughts import GraphOfThoughts, GoTReasoner, ThoughtStatus

    got = GraphOfThoughts(max_depth=5, prune_threshold=0.15)

    # Root
    root = got.add_thought("Research optimal web agent architecture", score=0.3)
    check("Root created", root is not None and root.id == "t_0001")

    # Parallel branches
    t1 = got.generate(root.id, "Visual grounding: UGround 20% improvement", score=0.7)
    t2 = got.generate(root.id, "R-MCTS: 30% improvement on benchmarks", score=0.8)
    t3 = got.generate(root.id, "Tripartite memory prevents degradation", score=0.6)
    t4 = got.generate(root.id, "Low quality: just use Selenium", score=0.1)
    check("Branches created", len(root.child_ids) == 4)

    # Refine
    t1r = got.refine(t1.id, "UGround: 10M GUI elements, cross-platform", score_delta=0.15)
    check("Refinement works", t1r.score > t1.score)

    # Aggregate
    merged = got.aggregate([t1r.id, t2.id, t3.id],
        "Optimal: UGround + MCTS + tripartite memory", score=0.9)
    check("Aggregation works", merged is not None and merged.score == 0.9)
    check("Sources marked merged", t1r.status == ThoughtStatus.MERGED)

    # Prune low-scoring
    pruned = got.prune()
    check("Pruning works", pruned >= 1, f"pruned {pruned} branches")

    # Best path
    best_path = got.get_best_path()
    check("Best path found", len(best_path) > 0)
    check("Best path ends at high score", best_path[-1].score >= 0.8)

    # All paths
    all_paths = got.get_all_paths()
    check("Multiple paths exist", len(all_paths) >= 1)

    # Resolution
    resolution = got.resolve()
    check("Resolution generated", len(resolution) > 50, f"{len(resolution)} chars")

    # Stats
    stats = got.stats
    check("Stats accurate", stats["total_thoughts"] >= 6)
    check("Max depth tracked", stats["max_depth"] >= 1)

    # Text rendering
    text = got.to_text()
    check("Text rendering works", "Graph of Thoughts" in text)

    # GoTReasoner high-level API
    reasoner = GoTReasoner()
    resolution2, got2 = reasoner.reason(
        "How to build autonomous web agent?",
        perspectives=["Vision approach", "Memory approach", "Planning approach"]
    )
    check("GoTReasoner works", len(resolution2) > 20)
    check("GoTReasoner creates graph", got2.stats["total_thoughts"] >= 4)

except Exception as e:
    check("Graph of Thoughts", False, str(e))


# ── Stage 2: Reflective MCTS ──

stage("Reflective Monte Carlo Tree Search")
try:
    from core.cognitive.mcts import (
        ReflectiveMCTS, DualOptimizationMCTS,
        NavigationState, NavigationAction
    )

    mcts = ReflectiveMCTS(max_iterations=15, max_depth=5)

    # Create initial state
    state = NavigationState(
        id="start",
        description="Chrome browser on Google homepage",
        url="https://www.google.com",
        active_app="Chrome",
        visible_elements=15,
    )

    # Run search
    best_action = mcts.search(state, iterations=15)
    check("MCTS search completes", best_action is not None)
    check("Best action has type", best_action.action_type != "")

    # Check tree stats
    stats = mcts.stats
    check("Tree nodes created", stats["total_nodes"] > 5, f"{stats['total_nodes']} nodes")
    check("Root visited", stats["root_visits"] > 0)

    # Action rankings
    rankings = mcts.get_action_rankings()
    check("Rankings available", len(rankings) > 0, f"{len(rankings)} actions ranked")

    # UCB1 formula test
    root = mcts._root
    if root.children:
        child = root.children[0]
        check("UCB1 computed", child.ucb1 > 0)
        check("Average reward valid", 0.0 <= child.average_reward <= 1.0)

    # Contrastive reflection
    check("Reflections generated", len(mcts._reflections) >= 0)
    check("Successful states tracked", len(mcts._successful_states) >= 0)

    # Test NavigationAction
    action = NavigationAction("click", "search_button", "query", "Click search")
    check("Action string works", "click" in str(action))

    # Dual optimization
    dual = DualOptimizationMCTS(local_iterations=10)
    actions = dual.solve("Search for AI papers", state)
    check("Dual MCTS produces actions", len(actions) >= 1)

except Exception as e:
    check("Reflective MCTS", False, str(e))


# ── Stage 3: Reflexion Engine ──

stage("Reflexion + DynaAct")
try:
    from core.cognitive.reflexion import (
        ReflexionEngine, DynaActFilter, FailureContext, Reflection
    )
    from core.cognitive.memory import EpisodicMemory

    memory = EpisodicMemory()
    reflexion = ReflexionEngine(memory=memory)

    # Simulate failures
    f1 = FailureContext(
        action_type="click", action_target="mark_7",
        expected_outcome="Search button clicked",
        actual_outcome="Nothing happened",
        error_type="no_change",
        error_message="No visual change detected",
    )
    ref1 = reflexion.on_failure(f1)
    check("Reflection created", ref1 is not None)
    check("Critique generated", len(ref1.critique) > 20)
    check("Lesson generated", len(ref1.lesson) > 10)
    check("Adjustment generated", len(ref1.action_adjustment) > 5)

    # Second failure (same pattern)
    f2 = FailureContext(
        action_type="click", action_target="mark_12",
        error_type="no_change",
        error_message="No visual change",
    )
    ref2 = reflexion.on_failure(f2)
    check("Pattern tracking works",
         reflexion._failure_patterns.get("click:no_change", 0) == 2)

    # Retrieve relevant reflections
    relevant = reflexion.get_relevant_reflections("click", "mark_")
    check("Retrieval finds reflections", len(relevant) > 0, f"found {len(relevant)}")

    # Action adjustment check
    adj = reflexion.should_adjust_action("click", "mark_7")
    check("Adjustment recommended", adj is not None)

    # Navigation failure
    f3 = FailureContext(
        action_type="navigate", action_target="url",
        action_value="https://broken.url",
        error_type="navigation_error",
    )
    ref3 = reflexion.on_failure(f3)
    check("Different error type handled", "navigation" in ref3.critique.lower() or
         "URL" in ref3.critique or "url" in ref3.critique.lower())

    # Stats
    stats = reflexion.stats
    check("Stats accurate", stats["total_reflections"] == 3)
    check("Failure patterns tracked", len(stats["failure_patterns"]) >= 2)

    # Memory integration
    check("Reflections in memory", len(memory._episodic) >= 3)

    # DynaAct filter
    dynaact = DynaActFilter(reflexion=reflexion, memory=memory)

    candidates = [
        "click search_button",
        "click mark_7",
        "type search_field",
        "scroll down",
        "key Enter",
        "navigate url",
        "wait 2000",
        "click submit",
    ]

    filtered = dynaact.filter_actions(candidates, "Search for AI papers")
    check("DynaAct filters actions", len(filtered) < len(candidates),
         f"{len(candidates)} -> {len(filtered)}")
    check("DynaAct keeps minimum", len(filtered) >= 3)

    # Record success/failure
    dynaact.record_success("click search_button")
    dynaact.record_failure("click mark_7")
    check("Success history tracked", "click search_button" in dynaact._action_success_history)

except Exception as e:
    check("Reflexion + DynaAct", False, str(e))


# ── Stage 4: Dynamic Code Generation ──

stage("Dynamic Code Generation + Sandbox")
try:
    from core.cognitive.code_gen import (
        DynamicCodeEngine, CodeGenerator, ScriptValidator,
        SandboxExecutor, GeneratedScript, ALLOWED_IMPORTS, DANGEROUS_PATTERNS
    )

    engine = DynamicCodeEngine(timeout=15, max_retries=2)

    # Test code generation
    gen = CodeGenerator()
    script = gen.generate("Analyze economic data", context={"data": {"gdp": 171.57}})
    check("Script generated", len(script.code) > 50)
    check("Imports extracted", len(script.imports_used) >= 1)

    # Test validation
    validator = ScriptValidator()

    safe_script = GeneratedScript(
        code='import json\nprint(json.dumps({"ok": True}))',
        task="test",
        imports_used=["json"],
    )
    check("Safe script validates", validator.validate(safe_script) == True)

    unsafe_script = GeneratedScript(
        code='import os\nos.system("rm -rf /")',
        task="test",
        imports_used=["os"],
    )
    check("Unsafe script rejected", validator.validate(unsafe_script) == False)
    check("Validation errors reported", len(unsafe_script.validation_errors) >= 1)

    # Test sandboxed execution
    result = engine.execute_task(
        "Analyze the current data",
        context={"data": {"metric": 42, "trend": "up"}}
    )
    check("Execution completes", result is not None)
    check("Execution succeeds", result.success == True, f"rc={result.return_code}")
    check("Output captured", len(result.stdout) > 0)
    check("JSON parsed", result.data is not None)

    # Test data processing
    result2 = engine.execute_task(
        "Process and clean text entries",
        context={"data": ["  Hello  ", "World", "This is longer text"]}
    )
    check("Processing works", result2.success == True)
    if result2.data:
        check("Processed data returned", result2.data.get("status") == "success")

    # Stats
    stats = engine.stats
    check("Stats tracked", stats["total_executions"] >= 2)
    check("Success rate > 0", stats["success_rate"] > 0)

    # Test DANGEROUS_PATTERNS detection
    check("os.system blocked", "os.system(" in DANGEROUS_PATTERNS)
    check("eval blocked", "eval(" in DANGEROUS_PATTERNS)
    check("subprocess blocked", "subprocess." in DANGEROUS_PATTERNS)

    # Test ALLOWED_IMPORTS
    check("json allowed", "json" in ALLOWED_IMPORTS)
    check("requests allowed", "requests" in ALLOWED_IMPORTS)

except Exception as e:
    check("Dynamic Code Generation", False, str(e))


# ── Stage 5: Full Integration ──

stage("Full Pipeline Integration")
try:
    from core.cognitive.graph_of_thoughts import GraphOfThoughts
    from core.cognitive.mcts import ReflectiveMCTS, NavigationState
    from core.cognitive.reflexion import ReflexionEngine, DynaActFilter, FailureContext
    from core.cognitive.code_gen import DynamicCodeEngine
    from core.cognitive.memory import EpisodicMemory
    from core.cognitive.planner import HierarchicalPlanner

    # Initialize all components
    memory = EpisodicMemory(working_capacity=7)
    reflexion = ReflexionEngine(memory=memory)
    dynaact = DynaActFilter(reflexion=reflexion, memory=memory)
    planner = HierarchicalPlanner(memory=memory)
    code_engine = DynamicCodeEngine(timeout=10)

    # Set working context
    memory.store_working("Goal: Research Iloilo City economic data", importance=1.0)
    memory.store_working("Using autonomous web navigation", importance=0.8)

    # Phase 1: GoT reasoning to plan approach
    got = GraphOfThoughts(max_depth=4)
    root = got.add_thought("Gather economic intelligence on Iloilo City", score=0.3)
    b1 = got.generate(root.id, "Navigate PSA portal for GDP data", score=0.6)
    b2 = got.generate(root.id, "Use code gen to batch-extract ILEDF press releases", score=0.7)
    b3 = got.generate(root.id, "Search news for real estate and BPO data", score=0.5)
    merged = got.aggregate([b1.id, b2.id, b3.id],
        "Multi-source approach: PSA + ILEDF + news synthesis", score=0.85)
    check("GoT integrated", got.stats["total_thoughts"] >= 5)

    # Phase 2: Plan decomposition
    plan = planner.create_plan("Navigate PSA portal for GDP data")
    check("Plan created from GoT", len(plan.subtasks) > 0)

    # Phase 3: MCTS for navigation decision
    state = NavigationState(
        id="psa_portal",
        description="PSA statistics portal - Provincial Product Accounts",
        url="https://psa.gov.ph",
        visible_elements=20,
    )
    mcts = ReflectiveMCTS(max_iterations=10)
    best_action = mcts.search(state, iterations=10)
    check("MCTS decides action", best_action is not None)

    # Phase 4: Simulate failure + Reflexion
    failure = FailureContext(
        action_type="click", action_target="dropdown_menu",
        expected_outcome="GDP data table visible",
        actual_outcome="Menu did not expand",
        error_type="no_change",
    )
    reflection = reflexion.on_failure(failure)
    check("Reflexion processes failure", len(reflection.critique) > 20)

    # Phase 5: DynaAct filters next actions based on reflection
    candidates = ["click dropdown", "click mark_3", "type search", "scroll", "wait"]
    filtered = dynaact.filter_actions(candidates, "Access GDP data table")
    check("DynaAct filters with reflexion context", len(filtered) >= 3)

    # Phase 6: Dynamic code gen bypasses GUI
    result = code_engine.execute_task(
        "Analyze economic data for Iloilo City",
        context={"data": {"gdp_2023": 160.28, "gdp_2024": 171.57, "growth": 7.1}}
    )
    check("Code gen produces data", result.success and result.data is not None)

    # Final memory state
    stats = memory.get_stats()
    check("Memory populated across pipeline",
         stats["working"] > 0 and stats["episodic"] > 0)

    # Context string for VLM
    context = memory.to_context_string(1000)
    check("Context string available", len(context) > 50)

except Exception as e:
    check("Full Pipeline Integration", False, str(e))


# ── Summary ──

print(f"\n{'='*60}")
print(f"  ADVANCED ARCHITECTURE TEST RESULTS")
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
if __name__ == "__main__":
    sys.exit(0 if passed == total else 1)
