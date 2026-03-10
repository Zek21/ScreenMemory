"""Dispatch demo tasks to all 4 agent workers for live dashboard viewing."""
import json, time
from pathlib import Path

q = Path(r'D:\Prospects\ScreenMemory\data\agent_queues')
q.mkdir(parents=True, exist_ok=True)

# ALPHA: Self-Evolution demo
alpha = {
    'type': 'python',
    'description': 'Self-Evolution: Running 10 task simulations',
    'command': '\n'.join([
        'import sys, time, random',
        'sys.path.insert(0, r"D:\\Prospects\\ScreenMemory")',
        'from core.self_evolution import SelfEvolutionSystem',
        'evo = SelfEvolutionSystem()',
        'categories = ["code", "research", "deploy", "navigate"]',
        'for i in range(10):',
        '    cat = random.choice(categories)',
        '    success = random.random() > 0.3',
        '    latency = random.randint(200, 5000)',
        '    quality = random.uniform(0.3, 0.95)',
        '    import uuid',
        '    result = {"task_id": str(uuid.uuid4())[:8], "category": cat, "strategy_id": "default",',
        '              "success": success, "latency_ms": latency, "quality_score": quality,',
        '              "tokens_used": random.randint(50, 500)}',
        '    fitness = evo.record_task(result)',
        '    status = "OK" if success else "FAIL"',
        '    print(f"Task {i+1}/10: {cat:8s} [{status}] fitness={fitness:.3f}")',
        '    time.sleep(0.3)',
        'evo.evolve_all_categories()',
        'print("Evolution cycle complete")',
        'summary = evo.dashboard.summary()',
        'print(f"Generation: {summary.get(\'generation\', 0)}")',
        'print("Self-Evolution Engine operational")',
    ])
}
(q / 'alpha_queue.json').write_text(json.dumps(alpha))
print('ALPHA dispatched: Self-Evolution simulation')
time.sleep(0.5)

# BETA: Tool Synthesizer safety demo
beta = {
    'type': 'python',
    'description': 'Tool Synthesizer: Safety validation suite',
    'command': '\n'.join([
        'import sys, time',
        'sys.path.insert(0, r"D:\\Prospects\\ScreenMemory")',
        'from core.tool_synthesizer import ToolValidator',
        'tv = ToolValidator()',
        'tests = [',
        '    ("def add(a,b): return a+b", True, "Safe arithmetic"),',
        '    ("import math; x = math.sqrt(16)", True, "Safe math import"),',
        '    ("import os; os.system(\'rm -rf /\')", False, "os.system attack"),',
        '    ("eval(input())", False, "eval injection"),',
        '    ("exec(\'print(1)\')", False, "exec injection"),',
        '    ("import subprocess; subprocess.Popen(\'cmd\', shell=True)", False, "subprocess shell"),',
        '    ("open(\'/etc/passwd\').read()", True, "file read OK"),',
        '    ("import shutil; shutil.rmtree(\'/\')", False, "rmtree root"),',
        ']',
        'passed = 0',
        'for code, expected_safe, label in tests:',
        '    ok, issues = tv.validate_safety(code)',
        '    correct = ok == expected_safe',
        '    passed += correct',
        '    icon = "PASS" if correct else "FAIL"',
        '    print(f"[{icon}] {label}: safe={ok}")',
        '    time.sleep(0.4)',
        'print(f"{passed}/{len(tests)} safety checks passed")',
        'print("Tool Synthesizer guard operational")',
    ])
}
(q / 'beta_queue.json').write_text(json.dumps(beta))
print('BETA dispatched: Tool safety validation')
time.sleep(0.5)

# GAMMA: Run full test suite
gamma = {
    'type': 'shell',
    'description': 'Running full test suite: 54 orchestrator tests',
    'command': r'cd /d D:\Prospects\ScreenMemory && D:\Prospects\env\Scripts\python.exe -u -m pytest tests\test_orchestrator.py -v --tb=short 2>&1'
}
(q / 'gamma_queue.json').write_text(json.dumps(gamma))
print('GAMMA dispatched: Full test suite')
time.sleep(0.5)

# DELTA: Learning Store + Knowledge Graph demo
delta = {
    'type': 'python',
    'description': 'Knowledge Graph: Building agent memory',
    'command': '\n'.join([
        'import sys, time',
        'sys.path.insert(0, r"D:\\Prospects\\ScreenMemory")',
        'from core.learning_store import PersistentLearningSystem',
        'pls = PersistentLearningSystem()',
        'print("Learning Store initialized")',
        'facts = [',
        '    ("DAGs need topological sort for execution order", "code", "dag_engine"),',
        '    ("WordPress REST API requires JWT auth token", "deploy", "blog_deploy"),',
        '    ("BM25 with k1=1.5 b=0.75 gives best retrieval", "research", "hybrid_retrieval"),',
        '    ("DAAO routes queries by estimated difficulty", "code", "difficulty_router"),',
        '    ("Factory pattern creates isolated agent instances", "code", "agent_factory"),',
        '    ("3-layer InputGuard blocks prompt injection", "code", "input_guard"),',
        '    ("Genetic algorithm evolves strategies per category", "code", "self_evolution"),',
        '    ("SSH cache flush exits 139 but cache IS flushed", "deploy", "wordpress"),',
        ']',
        'ids = []',
        'for content, cat, source in facts:',
        '    fid = pls.store.learn(content, cat, source)',
        '    ids.append(fid)',
        '    print(f"Learned: {content[:45]}...")',
        '    time.sleep(0.3)',
        'print(f"Total facts stored: {pls.store.stats()[\'total_facts\']}")',
        'results = pls.store.recall("how to deploy blog")',
        'print(f"Recall \\"deploy blog\\": {len(results)} results")',
        'for r in results[:3]:',
        '    print(f"  > {r.content[:50]}")',
        'pls.expertise.update("code", True)',
        'pls.expertise.update("deploy", True)',
        'top = pls.expertise.strongest_domains(3)',
        'print(f"Top expertise: {top}")',
        'print("Knowledge graph populated")',
    ])
}
(q / 'delta_queue.json').write_text(json.dumps(delta))
print('DELTA dispatched: Knowledge Graph')
print('\nALL 4 TASKS DISPATCHED — Watch the dashboard at http://localhost:8420!')
