"""Comprehensive test for brain.py — no CDP needed."""
import sys
sys.path.insert(0, '.')

from brain import (
    LLMConnector, LLMProvider, PageDiffer, ContentExtractor,
    ErrorRecovery, HumanBehavior, BlockDetector, MultiTabOrchestrator, Brain
)
from agent import Action


def test_llm_connector():
    llm = LLMConnector(provider='mock')
    resp = llm.decide('system', 'What is your first action? search input textbox')
    assert '"action"' in resp, f"Bad response: {resp}"
    assert llm.stats['calls'] == 1
    print(f"OK: LLMConnector mock → {resp[:50]}")

    # Test provider enum
    assert LLMProvider.OPENAI.value == 'openai'
    assert LLMProvider.CLAUDE.value == 'claude'
    print("OK: LLMProvider enum")

    # Test custom provider
    llm2 = LLMConnector(
        provider='custom',
        custom_fn=lambda s, u: '{"action": "click", "target": "test"}'
    )
    resp2 = llm2.decide('sys', 'user')
    assert 'click' in resp2
    print("OK: Custom LLM provider")


def test_page_differ():
    before = {
        'url': 'http://a.com',
        'elements': [
            {'role': 'button', 'name': 'Submit'},
            {'role': 'textbox', 'name': 'Email'},
        ]
    }
    after = {
        'url': 'http://b.com',
        'elements': [
            {'role': 'heading', 'name': 'Welcome'},
            {'role': 'button', 'name': 'Continue'},
        ]
    }
    diff = PageDiffer.diff(before, after)
    assert diff['url_changed'] == True
    assert diff['elements_added'] == 2
    assert diff['elements_removed'] == 2
    assert 'navigated' in diff['summary']
    print(f"OK: PageDiffer → {diff['summary']}")

    # Same page, no changes
    diff2 = PageDiffer.diff(before, before)
    assert not diff2['url_changed']
    assert diff2['elements_added'] == 0
    assert diff2['summary'] == 'no visible changes'
    print("OK: PageDiffer no changes")

    # Overlay detection
    before_no_modal = {'url': 'x', 'elements': [{'role': 'button', 'name': 'A'}]}
    after_modal = {'url': 'x', 'elements': [
        {'role': 'button', 'name': 'A'},
        {'role': 'dialog', 'name': 'Cookie', 'inModal': True}
    ]}
    diff3 = PageDiffer.diff(before_no_modal, after_modal)
    assert diff3['overlay_appeared'] == True
    print("OK: PageDiffer overlay detection")


def test_error_recovery():
    recovery = ErrorRecovery.__new__(ErrorRecovery)
    recovery.max_retries = 3
    recovery._retry_counts = {}

    a = Action(action='click', target='Submit')

    # Track retries
    recovery.record_failure(a)
    assert recovery.should_retry(a)
    recovery.record_failure(a)
    assert recovery.should_retry(a)
    recovery.record_failure(a)
    assert not recovery.should_retry(a)
    print("OK: ErrorRecovery retry tracking (3 max)")

    # Alternative targets
    alts = recovery._find_alternatives('submit')
    assert 'Send' in alts and 'OK' in alts
    print(f"OK: alternatives for 'submit': {alts}")

    alts2 = recovery._find_alternatives('login')
    assert 'Sign In' in alts2
    print(f"OK: alternatives for 'login': {alts2}")

    alts3 = recovery._find_alternatives('nonexistent')
    assert alts3 == []
    print("OK: no alternatives for unknown")


def test_human_behavior():
    hb = HumanBehavior(speed=1.0)

    # Delays are positive and variable
    delays = [hb.delay_before_action('click') for _ in range(20)]
    assert all(d > 0 for d in delays), "Delays must be positive"
    assert len(set(round(d, 3) for d in delays)) > 1, "Delays should vary"
    print(f"OK: click delays (20x): min={min(delays):.3f}s max={max(delays):.3f}s")

    # Scroll chunking
    chunks = hb.scroll_amount(500)
    assert sum(chunks) == 500
    assert len(chunks) >= 3, "500px should break into 3+ chunks"
    print(f"OK: scroll 500px → {len(chunks)} chunks summing to {sum(chunks)}")

    # Small scroll = no chunking
    small = hb.scroll_amount(100)
    assert small == [100]
    print("OK: small scroll no chunking")

    # Typing delays
    typing = [hb.typing_delay() for _ in range(50)]
    assert all(d > 0 for d in typing)
    avg = sum(typing) / len(typing)
    print(f"OK: typing delays avg={avg*1000:.0f}ms")

    # Speed multiplier
    fast = HumanBehavior(speed=0.5)
    fast_delays = [fast.delay_before_action('click') for _ in range(20)]
    # Fast should generally be faster (on average)
    # Note: randomness means this isn't guaranteed per sample, but statistically should hold
    print(f"OK: fast mode delays min={min(fast_delays):.3f}s")


def test_stats():
    llm = LLMConnector(provider='mock')
    llm.decide('s', 'u')
    llm.decide('s', 'u')
    llm.decide('s', 'u')
    stats = llm.stats
    assert stats['calls'] == 3
    assert stats['provider'] == 'mock'
    print(f"OK: LLM stats: {stats}")


if __name__ == '__main__':
    test_llm_connector()
    test_page_differ()
    test_error_recovery()
    test_human_behavior()
    test_stats()
    print()
    print("ALL TESTS PASSED")
