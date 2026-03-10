import sys
sys.path.insert(0, '.')

# Test 1: GOD Console
from core.god_console import GodConsole, SystemAwareness
god = GodConsole()
print('1. GodConsole initialized')

# Add a directive
did = god.set_directive('Upgrade dashboard design to 9/10', priority=1)
print(f'2. Directive set: {did}')

# Add an approval request
aid = god.add_approval('deploy_to_production', 'gamma', 'critical', 'Deploy blog to WordPress')
print(f'3. Approval queued: {aid}')

# Check system state
state = god.get_system_state()
print(f'4. System state: {len(state)} keys')

# Risk classification
from core.god_console import classify_risk
print(f'5. Risk: delete={classify_risk("delete all files")}')
print(f'   Risk: run tests={classify_risk("run pytest")}')
print(f'   Risk: deploy={classify_risk("deploy to production")}')
print(f'   Risk: research={classify_risk("analyze codebase")}')

# Check pending
pending = god.get_pending()
print(f'6. Pending approvals: {len(pending)}')

# Approve it
god.approve(aid)
print(f'7. Approved: {aid}')

# Test 2: Auto Orchestrator
from auto_orchestrator import AutoOrchestrator
orch = AutoOrchestrator()
print('8. AutoOrchestrator initialized')

# Check it can read directives
directives = god.get_active_directives()
print(f'9. Active directives: {len(directives)}')

# Test awareness
sa = SystemAwareness()
health = sa.get_agent_health()
print(f'10. Agent health: {list(health.keys())}')

print('ALL TESTS PASSED')
