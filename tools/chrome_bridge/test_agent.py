"""Quick test for agent.py — no CDP needed."""
import sys
sys.path.insert(0, '.')

from agent import (
    StealthLauncher, ActionType, Action, Observation,
    ActionProtocol, ActionExecutor, SessionManager, AutonomousAgent,
)

def test_action_protocol():
    # Direct dict
    a1 = Action(action='click', target='Submit')
    d = a1.to_dict()
    assert d == {'action': 'click', 'target': 'Submit'}, f"Bad dict: {d}"

    # From dict
    a2 = Action.from_dict({'action': 'type', 'target': 'Email', 'value': 'me@x.com'})
    assert a2.action == 'type' and a2.target == 'Email' and a2.value == 'me@x.com'

    # From raw JSON string
    a3 = Action.from_json('{"action": "click", "target": "Login"}')
    assert a3.action == 'click' and a3.target == 'Login'

    # From JSON in markdown code block
    text = 'I think:\n```json\n{"action": "press", "value": "Enter"}\n```'
    a4 = Action.from_json(text)
    assert a4.action == 'press' and a4.value == 'Enter', f"Got: {a4.action}/{a4.value}"

    # From JSON embedded in prose
    text2 = 'I will now {"action": "scroll", "direction": "down"} the page'
    a5 = Action.from_json(text2)
    assert a5.action == 'scroll' and a5.direction == 'down'

    # Unparseable → fail
    a6 = Action.from_json('garbage text with no json')
    assert a6.action == 'fail'

    print("OK: ActionProtocol (6 tests)")

def test_observation():
    obs = Observation(
        success=True, page_url='https://test.com',
        page_type='login', elements_count=12,
        scene='ELEMENTS:\n[1] button "Login"', step=3,
    )
    prompt = obs.to_prompt()
    assert 'STEP 3' in prompt
    assert 'https://test.com' in prompt
    assert 'login' in prompt
    print(f"OK: Observation (prompt={len(prompt)} chars)")

def test_session_manager():
    sm = SessionManager()
    sm.start('Test task', max_steps=10)
    assert sm.status == 'running'

    # Record 3 identical steps → loop detection
    for i in range(3):
        sm.record_step(i+1,
            Action(action='click', target='btn'),
            Observation(success=True, page_url='http://same')
        )
    stop = sm.should_stop()
    assert stop and 'loop' in stop.lower(), f"Expected loop detection, got: {stop}"

    # Max steps
    sm2 = SessionManager()
    sm2.start('Test', max_steps=2)
    for i in range(2):
        sm2.record_step(i+1, Action(action='click', target='x'),
                        Observation(success=True, page_url=f'http://p{i}'))
    stop2 = sm2.should_stop()
    assert stop2 and 'Maximum' in stop2

    # 5 consecutive failures
    sm3 = SessionManager()
    sm3.start('Test', max_steps=100)
    for i in range(5):
        sm3.record_step(i+1, Action(action='click', target=f'el{i}'),
                        Observation(success=False, page_url=f'http://p{i}'))
    stop3 = sm3.should_stop()
    assert stop3 and 'failures' in stop3.lower()

    print("OK: SessionManager (3 tests)")

def test_profiles():
    profiles = StealthLauncher.list_profiles()
    assert isinstance(profiles, list)
    print(f"OK: Found {len(profiles)} Chrome profiles")
    for p in profiles[:5]:
        print(f"    {p['directory']:20s} -> {p['name']}")

def test_system_prompt():
    sp = ActionProtocol.SYSTEM_PROMPT
    assert 'JSON' in sp and 'action' in sp
    print(f"OK: System prompt ({len(sp)} chars)")

    # Format initial prompt
    prompt = ActionProtocol.format_initial_prompt(
        "Search for AI", "[1] button Search", "https://test.com", "search"
    )
    assert 'Search for AI' in prompt
    assert 'https://test.com' in prompt
    print(f"OK: Initial prompt ({len(prompt)} chars)")

if __name__ == '__main__':
    test_action_protocol()
    test_observation()
    test_session_manager()
    test_profiles()
    test_system_prompt()
    print()
    print("ALL TESTS PASSED")
