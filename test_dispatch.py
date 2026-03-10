#!/usr/bin/env python
"""Test dispatch of _build_task_command fix"""
import sys, time, json, pathlib, sqlite3
sys.path.insert(0, '.')

from core.god_console import GodConsole, GodDirective
from auto_orchestrator import AutoOrchestrator

print("Step 1: Creating GOD directive...")
god = GodConsole()
directive_id = god.set_directive("Check learning store health and run core module tests", priority=2)
print(f"  Directive ID: {directive_id}")

print("Step 2: Retrieving directive from DB...")
with sqlite3.connect(r'D:\Prospects\ScreenMemory\data\god_console.db') as conn:
    conn.row_factory = sqlite3.Row
    cursor = conn.execute('SELECT * FROM directives WHERE id = ?', (directive_id,))
    row = cursor.fetchone()
    if row:
        print(f"  Found directive: id={row['id']}, goal={row['goal']}, status={row['status']}")
        directive = GodDirective(
            id=row['id'],
            goal=row['goal'],
            priority=row['priority'],
            created_at=row['created_at'],
            status=row['status'],
            sub_tasks=row['sub_tasks'],
            completed_at=row['completed_at'],
            notes=row['notes']
        )
        
        print("Step 3: Processing directive with AutoOrchestrator...")
        orch = AutoOrchestrator()
        orch.process_directive(directive)
        print("  process_directive completed")

print("Step 4: Checking dispatched task queues...")
queue_dir = pathlib.Path('data/agent_queues')
queued_count = 0
for f in sorted(queue_dir.glob('*_queue.json')):
    try:
        data = json.loads(f.read_text())
        cmd_snippet = str(data.get('command', ''))[:80]
        print(f"  Queue {f.stem}: type={data.get('type', '?')} cmd={cmd_snippet}")
        queued_count += 1
    except Exception as e:
        print(f"  Error reading {f.stem}: {e}")

if queued_count == 0:
    print("  WARNING: No queued tasks found!")

print("Step 5: Waiting 8s for agents to process...")
time.sleep(8)

print("Step 6: Checking results...")
result_count = 0
for f in sorted(queue_dir.glob('*_result.json')):
    try:
        data = json.loads(f.read_text())
        output_snippet = str(data.get('output', ''))[:100]
        print(f"  Result {f.stem}: success={data.get('success')} output={output_snippet}")
        result_count += 1
    except Exception as e:
        print(f"  Error reading {f.stem}: {e}")

if result_count == 0:
    print("  WARNING: No results found!")

print("\n=== DISPATCH TEST COMPLETE ===")
print(f"Summary: {queued_count} tasks queued, {result_count} results received")
