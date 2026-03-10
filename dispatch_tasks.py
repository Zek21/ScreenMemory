import json
from pathlib import Path

q = Path(r"D:\Prospects\ScreenMemory\data\agent_queues")

# ALPHA: Test the NEW PersistentLearningSystem
alpha = {
    "type": "python",
    "description": "Testing Persistent Learning Store",
    "command": (
        "import sys\n"
        "sys.path.insert(0, r'D:\\Prospects\\ScreenMemory')\n"
        "print('=== Persistent Learning Store Test ===')\n"
        "from core.learning_store import PersistentLearningSystem, LearningStore, ExpertiseProfile, KnowledgeGraph\n"
        "print('1. All classes imported')\n"
        "pls = PersistentLearningSystem()\n"
        "print('2. PersistentLearningSystem initialized')\n"
        "fids = pls.learn_from_task('Build DAG executor', 'code', True, ['DAGs need topological sort', 'Retry with exponential backoff'])\n"
        "print(f'3. Learned from task: {len(fids)} facts stored')\n"
        "fids2 = pls.learn_from_task('Deploy blog article', 'deploy', True, ['WordPress REST API needs auth token', 'Cache flush via SSH'])\n"
        "print(f'4. Learned from deploy: {len(fids2)} facts')\n"
        "context = pls.get_context_for_task('build a workflow engine')\n"
        "print(f'5. Context recall: {len(context)} chars')\n"
        "summary = pls.get_expertise_summary()\n"
        "print(f'6. Expertise: {summary}')\n"
        "pls.run_maintenance()\n"
        "print('7. Maintenance cycle complete')\n"
        "stats = pls.store.stats()\n"
        "print(f'8. Stats: {stats}')\n"
        "print('=== ALL 8 TESTS PASSED ===')\n"
    )
}

# BETA: Full integration test — import ALL new modules together
beta = {
    "type": "python",
    "description": "Full integration: all 3 upgrade modules",
    "command": (
        "import sys\n"
        "sys.path.insert(0, r'D:\\Prospects\\ScreenMemory')\n"
        "print('=== FULL INTEGRATION TEST ===')\n"
        "from core.self_evolution import SelfEvolutionSystem\n"
        "print('1. SelfEvolutionSystem imported')\n"
        "from core.tool_synthesizer import ToolSynthesizer, ToolValidator\n"
        "print('2. ToolSynthesizer imported')\n"
        "from core.learning_store import PersistentLearningSystem\n"
        "print('3. PersistentLearningSystem imported')\n"
        "from core.orchestrator import Orchestrator\n"
        "print('4. Orchestrator imported')\n"
        "from core.difficulty_router import DAAORouter\n"
        "print('5. DAAORouter imported')\n"
        "from core.agent_factory import AgentFactory\n"
        "print('6. AgentFactory imported')\n"
        "from core.dag_engine import DAGExecutor\n"
        "print('7. DAGExecutor imported')\n"
        "from core.input_guard import InputGuard\n"
        "print('8. InputGuard imported')\n"
        "from core.hybrid_retrieval import HybridRetriever\n"
        "print('9. HybridRetriever imported')\n"
        "evo = SelfEvolutionSystem()\n"
        "pls = PersistentLearningSystem()\n"
        "tv = ToolValidator()\n"
        "print('10. All systems instantiated')\n"
        "print('=== ALL 10 INTEGRATION CHECKS PASSED ===')\n"
    )
}

# GAMMA: Run test suite (CWD now fixed to ScreenMemory)
gamma = {
    "type": "shell",
    "description": "Running 54 orchestrator tests",
    "command": r"cd /d D:\Prospects\ScreenMemory && D:\Prospects\env\Scripts\python.exe -u -m pytest tests\test_orchestrator.py -v --tb=short 2>&1"
}

# DELTA: Architecture scan with UTF-8 encoding
delta = {
    "type": "python",
    "description": "Full architecture LOC scan",
    "command": (
        "import os\n"
        "print('=== ScreenMemory Architecture Scan ===')\n"
        "core = r'D:\\Prospects\\ScreenMemory\\core'\n"
        "cog = os.path.join(core, 'cognitive')\n"
        "core_files = [f for f in os.listdir(core) if f.endswith('.py')]\n"
        "cog_files = [f for f in os.listdir(cog) if f.endswith('.py')]\n"
        "total = 0\n"
        "print(f'Core modules: {len(core_files)}')\n"
        "for f in sorted(core_files):\n"
        "    lines = len(open(os.path.join(core, f), encoding='utf-8', errors='ignore').readlines())\n"
        "    total += lines\n"
        "    print(f'  {f:30s} {lines:5d} lines')\n"
        "print(f'Cognitive modules: {len(cog_files)}')\n"
        "for f in sorted(cog_files):\n"
        "    lines = len(open(os.path.join(cog, f), encoding='utf-8', errors='ignore').readlines())\n"
        "    total += lines\n"
        "    print(f'  {f:30s} {lines:5d} lines')\n"
        "print(f'TOTAL: {len(core_files)+len(cog_files)} modules, {total} LOC')\n"
        "print('=== SCAN COMPLETE ===')\n"
    )
}

for name, task in [("alpha", alpha), ("beta", beta), ("gamma", gamma), ("delta", delta)]:
    (q / f"{name}_queue.json").write_text(json.dumps(task))
    print(f"Dispatched to {name.upper()}")

print("All 4 agents dispatched - WATCH THE TERMINALS!")
