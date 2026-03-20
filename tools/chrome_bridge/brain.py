"""
╔══════════════════════════════════════════════════════════════════════════╗
║                          A I   B R A I N                                ║
║                                                                          ║
║  The cognitive layer that connects any LLM to the autonomous agent.      ║
║  Handles: LLM routing, state diffing, error recovery, content            ║
║  extraction, human-like behavior, CAPTCHA detection, and multi-tab       ║
║  orchestration.                                                          ║
║                                                                          ║
║  Components:                                                             ║
║    1. LLMConnector      — Universal LLM interface (OpenAI/Claude/        ║
║                           Gemini/Ollama/mock)                            ║
║    2. PageDiffer        — Detect what changed after each action          ║
║    3. ContentExtractor  — Pull structured data from pages                ║
║    4. ErrorRecovery     — Smart retry, scroll-to-find, alt paths         ║
║    5. HumanBehavior     — Realistic delays, patterns, anti-detection     ║
║    6. BlockDetector     — CAPTCHA, rate-limit, and block detection       ║
║    7. MultiTabOrch      — Coordinate actions across multiple tabs        ║
║    8. Brain             — Unified cognitive controller                   ║
║                                                                          ║
║  Sits on top of: agent.py (execution) + god_mode.py (perception)        ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import json
import time
import random
import math
import hashlib
import os
import sys
import logging
import re
from typing import Optional, List, Dict, Tuple, Any, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger('brain')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from god_mode import GodMode, ElementEmbedding
    from agent import (
        AutonomousAgent, Action, Observation, ActionProtocol,
        StealthLauncher, SessionManager
    )
except ImportError as e:
    logger.error(f"Required modules not found: {e}")
    raise


# ═══════════════════════════════════════════════════════════════════════
# MODULE 1: UNIVERSAL LLM CONNECTOR
# ═══════════════════════════════════════════════════════════════════════

class LLMProvider(Enum):
    OPENAI = "openai"
    CLAUDE = "claude"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    MOCK = "mock"
    CUSTOM = "custom"


class LLMConnector:
    """
    Universal LLM interface. Connects to any provider with a single API.

    Usage:
        # OpenAI (key from env OPENAI_API_KEY or data/secrets.json)
        llm = LLMConnector(provider="openai", model="gpt-4o")

        # Claude (key from env ANTHROPIC_API_KEY or data/secrets.json)
        llm = LLMConnector(provider="claude", model="claude-sonnet-4-20250514")

        # Gemini (key from env GOOGLE_API_KEY or data/secrets.json)
        llm = LLMConnector(provider="gemini", model="gemini-2.0-flash")

        # Local Ollama
        llm = LLMConnector(provider="ollama", model="llama3.1")

        # Mock (for testing)
        llm = LLMConnector(provider="mock")

        # Any provider
        response = llm.decide(system_prompt, user_prompt)
    """

    # Provider configuration tables
    _ENV_KEYS = {
        LLMProvider.OPENAI: 'OPENAI_API_KEY',
        LLMProvider.CLAUDE: 'ANTHROPIC_API_KEY',
        LLMProvider.GEMINI: 'GOOGLE_API_KEY',
    }
    _DEFAULT_MODELS = {
        LLMProvider.OPENAI: 'gpt-4o-mini',
        LLMProvider.CLAUDE: 'claude-sonnet-4-20250514',
        LLMProvider.GEMINI: 'gemini-2.0-flash',
        LLMProvider.OLLAMA: 'llama3.1',
        LLMProvider.MOCK: 'mock',
    }
    _DEFAULT_URLS = {
        LLMProvider.OPENAI: 'https://api.openai.com/v1',
        LLMProvider.CLAUDE: 'https://api.anthropic.com/v1',
        LLMProvider.GEMINI: 'https://generativelanguage.googleapis.com/v1beta',
        LLMProvider.OLLAMA: 'http://localhost:11434',
    }

    def __init__(self, provider: str = "mock", api_key: str = None,
                 model: str = None, base_url: str = None,
                 temperature: float = 0.1, max_tokens: int = 500,
                 custom_fn: Callable = None):
        """
        Initialize LLM connector.

        Args:
            provider: "openai", "claude", "gemini", "ollama", "mock", "custom"
            api_key: API key (or set via env: OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
            model: Model name (defaults per provider)
            base_url: Custom API endpoint (e.g., for Azure OpenAI or local servers)
            temperature: Lower = more deterministic (0.1 recommended for agents)
            max_tokens: Max response tokens
            custom_fn: Custom function(system, user) → str for CUSTOM provider
        """
        self.provider = LLMProvider(provider.lower())
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.custom_fn = custom_fn
        self._call_count = 0
        self._total_tokens = 0

        if not self.api_key:
            env_var = self._ENV_KEYS.get(self.provider)
            if env_var:
                self.api_key = os.environ.get(env_var, '')
            # Fallback: try skynet_secrets loader  # signed: beta
            if not self.api_key:
                try:
                    from tools.skynet_secrets import get_secret
                    if env_var:
                        self.api_key = get_secret(env_var, '') or ''
                except ImportError:
                    pass
        if not self.model:
            self.model = self._DEFAULT_MODELS.get(self.provider, 'default')
        if not self.base_url:
            self.base_url = self._DEFAULT_URLS.get(self.provider, '')

    def decide(self, system_prompt: str, user_prompt: str) -> str:
        """
        Send system + user prompt to LLM, return raw response text.

        This is the function you pass to AutonomousAgent.run() as decide_fn.
        """
        self._call_count += 1

        if self.provider == LLMProvider.MOCK:
            return self._mock_decide(user_prompt)
        elif self.provider == LLMProvider.CUSTOM:
            if self.custom_fn:
                return self.custom_fn(system_prompt, user_prompt)
            return '{"action": "fail", "reason": "No custom function provided"}'
        elif self.provider == LLMProvider.OPENAI:
            return self._openai_decide(system_prompt, user_prompt)
        elif self.provider == LLMProvider.CLAUDE:
            return self._claude_decide(system_prompt, user_prompt)
        elif self.provider == LLMProvider.GEMINI:
            return self._gemini_decide(system_prompt, user_prompt)
        elif self.provider == LLMProvider.OLLAMA:
            return self._ollama_decide(system_prompt, user_prompt)
        else:
            return '{"action": "fail", "reason": "Unknown provider"}'

    def _openai_decide(self, system: str, user: str) -> str:
        """Call OpenAI API (GPT-4o, GPT-4, etc.)."""
        import urllib.request
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }).encode('utf-8')

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                usage = data.get('usage', {})
                self._total_tokens += usage.get('total_tokens', 0)
                return data['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return f'{{"action": "fail", "reason": "OpenAI API error: {e}"}}'

    def _claude_decide(self, system: str, user: str) -> str:
        """Call Anthropic Claude API."""
        import urllib.request
        payload = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [
                {"role": "user", "content": user},
            ],
        }).encode('utf-8')

        req = urllib.request.Request(
            f"{self.base_url}/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                usage = data.get('usage', {})
                self._total_tokens += usage.get('input_tokens', 0) + usage.get('output_tokens', 0)
                content = data.get('content', [{}])
                return content[0].get('text', '') if content else ''
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return f'{{"action": "fail", "reason": "Claude API error: {e}"}}'

    def _gemini_decide(self, system: str, user: str) -> str:
        """Call Google Gemini API."""
        import urllib.request
        payload = json.dumps({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
            },
        }).encode('utf-8')

        url = (f"{self.base_url}/models/{self.model}:generateContent"
               f"?key={self.api_key}")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                candidates = data.get('candidates', [{}])
                if candidates:
                    parts = candidates[0].get('content', {}).get('parts', [{}])
                    return parts[0].get('text', '') if parts else ''
                return '{"action": "fail", "reason": "No Gemini response"}'
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            return f'{{"action": "fail", "reason": "Gemini API error: {e}"}}'

    def _ollama_decide(self, system: str, user: str) -> str:
        """Call local Ollama server."""
        import urllib.request
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }).encode('utf-8')

        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data.get('message', {}).get('content', '')
        except Exception as e:
            logger.error(f"Ollama error: {e}")
            return f'{{"action": "fail", "reason": "Ollama error: {e}"}}'

    def _mock_decide(self, user_prompt: str) -> str:
        """Mock LLM for testing — pattern-matches common tasks."""
        prompt_lower = user_prompt.lower()

        # Detect common patterns and return appropriate actions
        if 'what is your first action' in prompt_lower or 'what is your next action' in prompt_lower:
            if 'login' in prompt_lower or 'sign in' in prompt_lower:
                if 'email' in prompt_lower or 'username' in prompt_lower:
                    return '{"action": "type", "target": "Email", "value": "test@example.com"}'
                return '{"action": "click", "target": "Sign In"}'
            if 'search' in prompt_lower:
                if 'input' in prompt_lower or 'textbox' in prompt_lower:
                    return '{"action": "type", "target": "Search", "value": "test query"}'
                return '{"action": "click", "target": "Search"}'
            if 'SUCCESS' in user_prompt:
                return '{"action": "done", "reason": "Action succeeded"}'

        return '{"action": "done", "reason": "Mock agent complete"}'

    @property
    def stats(self) -> Dict:
        return {
            'provider': self.provider.value,
            'model': self.model,
            'calls': self._call_count,
            'total_tokens': self._total_tokens,
        }


# ═══════════════════════════════════════════════════════════════════════
# MODULE 2: PAGE STATE DIFFER
# ═══════════════════════════════════════════════════════════════════════

class PageDiffer:
    """
    Detects what changed on a page after an action.
    Compares two perception snapshots to identify:
    - New elements that appeared
    - Elements that disappeared
    - URL changes (navigation occurred)
    - Content changes (text updates)
    - Modal/overlay state changes
    """

    @staticmethod
    def _fingerprint(el: Dict) -> str:
        """Element fingerprint: role+name combo."""
        return f"{el.get('role', '')}:{el.get('name', '')[:30]}"

    @staticmethod
    def _count_overlays(elements: List[Dict]) -> int:
        return sum(1 for e in elements
                   if e.get('inModal') or e.get('role') == 'dialog')

    @staticmethod
    def _build_change_summary(url_changed, after_url, added, removed,
                               before_type, after_type, overlay_delta) -> str:
        changes = []
        if url_changed:
            changes.append(f"navigated to {after_url}")
        if added > 0:
            changes.append(f"{added} new elements")
        if removed > 0:
            changes.append(f"{removed} removed")
        if before_type != after_type:
            changes.append(f"page type: {before_type}\u2192{after_type}")
        if overlay_delta > 0:
            changes.append("overlay appeared")
        if overlay_delta < 0:
            changes.append("overlay dismissed")
        return '; '.join(changes) if changes else 'no visible changes'

    @staticmethod
    def diff(before: Dict, after: Dict) -> Dict:
        """
        Compare two perception results (from god.see()).

        Returns dict with: url_changed, new_url, elements_added/removed,
        new_elements, page_type_changed, overlay_appeared/dismissed,
        content_changed, summary.
        """
        before_url = before.get('url', '')
        after_url = after.get('url', '')
        url_changed = before_url != after_url

        before_els = before.get('elements', [])
        after_els = after.get('elements', [])

        fp = PageDiffer._fingerprint
        before_fps = set(fp(e) for e in before_els)
        after_fps = set(fp(e) for e in after_els)
        added_fps = after_fps - before_fps
        removed_fps = before_fps - after_fps
        new_elements = [e for e in after_els if fp(e) in added_fps]

        before_type = before.get('page_type', '')
        after_type = after.get('page_type', '')
        overlay_delta = (PageDiffer._count_overlays(after_els)
                         - PageDiffer._count_overlays(before_els))

        summary = PageDiffer._build_change_summary(
            url_changed, after_url, len(added_fps), len(removed_fps),
            before_type, after_type, overlay_delta)

        return {
            'url_changed': url_changed,
            'new_url': after_url if url_changed else '',
            'elements_added': len(added_fps),
            'elements_removed': len(removed_fps),
            'new_elements': new_elements[:10],
            'page_type_changed': before_type != after_type,
            'new_page_type': after_type,
            'overlay_appeared': overlay_delta > 0,
            'overlay_dismissed': overlay_delta < 0,
            'content_changed': len(added_fps) > 0 or len(removed_fps) > 0,
            'summary': summary,
        }


# ═══════════════════════════════════════════════════════════════════════
# MODULE 3: CONTENT EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════

class ContentExtractor:
    """
    Pulls structured data from pages via CDP.
    Extracts text, tables, forms, links, metadata — all via code analysis.
    """

    # JavaScript to extract visible text content in reading order
    TEXT_EXTRACT_JS = """
    (() => {
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, {
                acceptNode: (node) => {
                    const el = node.parentElement;
                    if (!el) return NodeFilter.FILTER_REJECT;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' ||
                        style.opacity === '0') return NodeFilter.FILTER_REJECT;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return NodeFilter.FILTER_REJECT;
                    const text = node.textContent.trim();
                    if (text.length < 2) return NodeFilter.FILTER_REJECT;
                    return NodeFilter.FILTER_ACCEPT;
                }
            }
        );
        const texts = [];
        let node;
        while (node = walker.nextNode()) {
            texts.push(node.textContent.trim());
        }
        return texts.join('\\n');
    })()
    """

    # JavaScript to extract all links with context
    LINKS_EXTRACT_JS = """
    (() => {
        const links = [];
        document.querySelectorAll('a[href]').forEach(a => {
            const rect = a.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return;
            const style = window.getComputedStyle(a);
            if (style.display === 'none') return;
            links.push({
                text: (a.innerText || a.getAttribute('aria-label') || '').trim().substring(0, 100),
                href: a.href,
                x: Math.round(rect.left), y: Math.round(rect.top),
            });
        });
        return JSON.stringify(links);
    })()
    """

    # JavaScript to extract table data
    TABLE_EXTRACT_JS = """
    (() => {
        const tables = [];
        document.querySelectorAll('table').forEach((table, idx) => {
            const rows = [];
            table.querySelectorAll('tr').forEach(tr => {
                const cells = [];
                tr.querySelectorAll('th, td').forEach(cell => {
                    cells.push(cell.innerText.trim().substring(0, 200));
                });
                if (cells.length > 0) rows.push(cells);
            });
            if (rows.length > 0) {
                tables.push({index: idx, rows: rows.slice(0, 50)});
            }
        });
        return JSON.stringify(tables);
    })()
    """

    # JavaScript to extract form fields with current values
    FORM_EXTRACT_JS = """
    (() => {
        const fields = [];
        document.querySelectorAll('input, select, textarea').forEach(el => {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return;
            const style = window.getComputedStyle(el);
            if (style.display === 'none') return;
            const label = el.labels && el.labels[0] ? el.labels[0].innerText.trim() : '';
            const placeholder = el.getAttribute('placeholder') || '';
            const ariaLabel = el.getAttribute('aria-label') || '';
            fields.push({
                type: el.type || el.tagName.toLowerCase(),
                name: el.name || '',
                label: label || ariaLabel || placeholder,
                value: el.value || '',
                required: el.required,
                x: Math.round(rect.left), y: Math.round(rect.top),
                w: Math.round(rect.width), h: Math.round(rect.height),
            });
        });
        return JSON.stringify(fields);
    })()
    """

    # JavaScript to extract page metadata
    META_EXTRACT_JS = """
    (() => {
        const meta = {};
        meta.title = document.title || '';
        meta.url = window.location.href;
        meta.description = '';
        const descEl = document.querySelector('meta[name="description"]');
        if (descEl) meta.description = descEl.getAttribute('content') || '';
        meta.h1 = [];
        document.querySelectorAll('h1').forEach(h => {
            const text = h.innerText.trim();
            if (text) meta.h1.push(text.substring(0, 200));
        });
        meta.h2 = [];
        document.querySelectorAll('h2').forEach(h => {
            const text = h.innerText.trim();
            if (text) meta.h2.push(text.substring(0, 200));
        });
        meta.canonical = '';
        const canonical = document.querySelector('link[rel="canonical"]');
        if (canonical) meta.canonical = canonical.getAttribute('href') || '';
        return JSON.stringify(meta);
    })()
    """

    def __init__(self, god: GodMode):
        self.god = god

    def extract_text(self, tab_id: str = None, max_chars: int = 5000) -> str:
        """Extract all visible text from the page in reading order."""
        tab_id = tab_id or self.god._get_active_tab()
        try:
            raw = self.god.cdp.eval(tab_id, self.TEXT_EXTRACT_JS)
            if raw and isinstance(raw, str):
                return raw[:max_chars]
            return str(raw or '')[:max_chars]
        except Exception as e:
            logger.error(f"Text extraction failed: {e}")
            return ''

    def extract_links(self, tab_id: str = None) -> List[Dict]:
        """Extract all visible links with text and coordinates."""
        tab_id = tab_id or self.god._get_active_tab()
        try:
            raw = self.god.cdp.eval(tab_id, self.LINKS_EXTRACT_JS)
            return json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception as e:
            logger.error(f"Link extraction failed: {e}")
            return []

    def extract_tables(self, tab_id: str = None) -> List[Dict]:
        """Extract all visible tables as structured data."""
        tab_id = tab_id or self.god._get_active_tab()
        try:
            raw = self.god.cdp.eval(tab_id, self.TABLE_EXTRACT_JS)
            return json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception as e:
            logger.error(f"Table extraction failed: {e}")
            return []

    def extract_forms(self, tab_id: str = None) -> List[Dict]:
        """Extract all form fields with labels and current values."""
        tab_id = tab_id or self.god._get_active_tab()
        try:
            raw = self.god.cdp.eval(tab_id, self.FORM_EXTRACT_JS)
            return json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception as e:
            logger.error(f"Form extraction failed: {e}")
            return []

    def extract_metadata(self, tab_id: str = None) -> Dict:
        """Extract page metadata (title, description, headings)."""
        tab_id = tab_id or self.god._get_active_tab()
        try:
            raw = self.god.cdp.eval(tab_id, self.META_EXTRACT_JS)
            return json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:
            logger.error(f"Metadata extraction failed: {e}")
            return {}

    def extract_all(self, tab_id: str = None) -> Dict:
        """Extract everything: text, links, tables, forms, metadata."""
        tab_id = tab_id or self.god._get_active_tab()
        return {
            'metadata': self.extract_metadata(tab_id),
            'text': self.extract_text(tab_id),
            'links': self.extract_links(tab_id),
            'tables': self.extract_tables(tab_id),
            'forms': self.extract_forms(tab_id),
        }

    def summarize_for_llm(self, tab_id: str = None, max_tokens: int = 800) -> str:
        """Generate a compact content summary for LLM consumption."""
        tab_id = tab_id or self.god._get_active_tab()
        meta = self.extract_metadata(tab_id)
        forms = self.extract_forms(tab_id)

        lines = []
        if meta.get('title'):
            lines.append(f"TITLE: {meta['title']}")
        if meta.get('url'):
            lines.append(f"URL: {meta['url']}")
        if meta.get('description'):
            lines.append(f"DESC: {meta['description'][:200]}")
        if meta.get('h1'):
            lines.append(f"H1: {', '.join(meta['h1'][:3])}")
        if meta.get('h2'):
            lines.append(f"H2: {', '.join(meta['h2'][:5])}")

        if forms:
            lines.append(f"\nFORM FIELDS ({len(forms)}):")
            for f in forms[:15]:
                val = f" = \"{f['value']}\"" if f.get('value') else ''
                req = ' *' if f.get('required') else ''
                lines.append(f"  [{f['type']}] {f['label'] or f['name']}{req}{val}")

        text = '\n'.join(lines)
        # Rough token estimate: 1 token ≈ 4 chars
        return text[:max_tokens * 4]


# ═══════════════════════════════════════════════════════════════════════
# MODULE 4: ERROR RECOVERY ENGINE
# ═══════════════════════════════════════════════════════════════════════

class ErrorRecovery:
    """
    Smart error recovery strategies.
    When an action fails, tries alternative approaches before giving up.

    Strategies:
    1. Retry — wait and try the same action again
    2. Scroll-to-find — element might be below the fold
    3. Dismiss — an overlay might be blocking
    4. Alternative target — find a similar element
    5. Navigate back — if we went to wrong page
    """

    def __init__(self, god: GodMode, max_retries: int = 3):
        self.god = god
        self.max_retries = max_retries
        self._retry_counts: Dict[str, int] = {}

    def _action_key(self, action: Action) -> str:
        """Generate a unique key for tracking retries per action."""
        return f"{action.action}:{action.target}:{action.value}"

    def should_retry(self, action: Action) -> bool:
        """Check if this action should be retried."""
        key = self._action_key(action)
        return self._retry_counts.get(key, 0) < self.max_retries

    def record_failure(self, action: Action):
        """Record a failure for retry tracking."""
        key = self._action_key(action)
        self._retry_counts[key] = self._retry_counts.get(key, 0) + 1

    def _recovery_strategy(self, action: Action, error: str,
                           attempt: int) -> Optional[Action]:
        """Select recovery action based on error type and attempt count."""
        error_lower = error.lower()

        if 'not found' in error_lower:
            if attempt <= 1:
                logger.info(f"Recovery: scrolling to find '{action.target}'")
                return Action(action='scroll', direction='down', amount=400)
            if attempt == 2:
                logger.info("Recovery: dismissing overlays")
                return Action(action='dismiss')
            if attempt == 3:
                logger.info("Recovery: scrolling up to find element")
                return Action(action='scroll', direction='up', amount=800)

        if 'cdp error' in error_lower or 'timeout' in error_lower:
            logger.info("Recovery: waiting 2s then retrying")
            time.sleep(2)
            return action

        if action.target and attempt <= 2:
            alt_targets = self._find_alternatives(action.target)
            if alt_targets:
                alt = alt_targets[attempt - 1] if attempt <= len(alt_targets) else None
                if alt:
                    logger.info(f"Recovery: trying alternative target '{alt}'")
                    return Action(
                        action=action.action, target=alt, value=action.value,
                    )

        return None

    def recover(self, action: Action, error: str,
                tab_id: str = None) -> Optional[Action]:
        """
        Attempt to recover from a failed action.

        Returns:
            A recovery Action to try, or None if unrecoverable.
        """
        self.record_failure(action)

        if not self.should_retry(action):
            return None

        attempt = self._retry_counts[self._action_key(action)]
        return self._recovery_strategy(action, error, attempt)

    def _find_alternatives(self, target: str) -> List[str]:
        """Generate alternative target texts for common UI patterns."""
        alternatives = {
            'submit': ['Send', 'OK', 'Confirm', 'Continue', 'Save'],
            'login': ['Sign In', 'Log In', 'Enter', 'Continue'],
            'sign in': ['Login', 'Log In', 'Sign in', 'Enter'],
            'search': ['Find', 'Go', 'Look up', 'Query'],
            'close': ['X', 'Cancel', 'Dismiss', 'Done'],
            'next': ['Continue', 'Forward', 'Proceed', '→'],
            'back': ['Previous', 'Return', '←', 'Go Back'],
            'accept': ['OK', 'Agree', 'Yes', 'Allow', 'Got it'],
            'cancel': ['Close', 'No', 'Dismiss', 'Never mind'],
            'register': ['Sign Up', 'Create Account', 'Join', 'Get Started'],
            'sign up': ['Register', 'Create Account', 'Join', 'Get Started'],
        }
        target_lower = target.lower()
        return alternatives.get(target_lower, [])

    def reset(self):
        """Reset retry counts for a new task."""
        self._retry_counts.clear()


# ═══════════════════════════════════════════════════════════════════════
# MODULE 5: HUMAN-LIKE BEHAVIOR ENGINE
# ═══════════════════════════════════════════════════════════════════════

class HumanBehavior:
    """
    Generates human-like interaction patterns to avoid bot detection.

    Features:
    - Variable delays between actions (gaussian distribution)
    - Random micro-movements before clicks
    - Realistic scroll patterns (not instant jumps)
    - Typing with variable speed and occasional pauses
    - Session-level behavior patterns (faster over time = familiarity)
    """

    def __init__(self, speed: float = 1.0):
        """
        Args:
            speed: Multiplier for all delays (0.5 = fast, 1.0 = normal, 2.0 = slow)
        """
        self.speed = speed
        self._action_count = 0

    def delay_before_action(self, action_type: str) -> float:
        """
        Calculate a human-like delay before executing an action.
        Returns seconds to sleep.
        """
        self._action_count += 1

        # Base delays per action type (seconds)
        base_delays = {
            'click': 0.3,
            'type': 0.5,
            'scroll': 0.2,
            'navigate': 0.1,
            'press': 0.15,
            'hover': 0.2,
        }
        base = base_delays.get(action_type, 0.3)

        # Add gaussian noise (humans are variable)
        noise = random.gauss(0, base * 0.3)
        delay = max(0.05, base + noise)

        # Familiarity effect: actions get slightly faster over time
        familiarity = max(0.5, 1.0 - self._action_count * 0.01)
        delay *= familiarity

        # Apply speed multiplier
        delay *= self.speed

        return round(delay, 3)

    def delay_after_navigation(self) -> float:
        """Delay after a page navigation (reading time)."""
        return max(0.5, random.gauss(1.5, 0.5)) * self.speed

    def typing_delay(self) -> float:
        """Delay between keystrokes (ms) — human-like variable speed."""
        # Average human typing: ~50-100ms between keys
        base = random.gauss(70, 20)
        # Occasional pause (thinking)
        if random.random() < 0.05:
            base += random.uniform(200, 500)
        return max(20, base) / 1000  # Convert to seconds

    def scroll_amount(self, requested: int) -> List[int]:
        """
        Break a large scroll into smaller human-like chunks.
        Returns list of scroll amounts.
        """
        if abs(requested) <= 150:
            return [requested]

        chunks = []
        remaining = abs(requested)
        sign = 1 if requested > 0 else -1

        while remaining > 0:
            chunk = min(remaining, random.randint(80, 200))
            chunks.append(chunk * sign)
            remaining -= chunk

        return chunks

    def should_pause(self) -> bool:
        """Occasionally pause to simulate reading/thinking."""
        return random.random() < 0.1  # 10% chance

    def pause_duration(self) -> float:
        """How long to pause for reading/thinking."""
        return random.uniform(0.5, 2.0) * self.speed

    def reset(self):
        self._action_count = 0


# ═══════════════════════════════════════════════════════════════════════
# MODULE 6: BLOCK DETECTOR
# ═══════════════════════════════════════════════════════════════════════

class BlockDetector:
    """
    Detects when the agent has been blocked, rate-limited, or presented
    with a CAPTCHA.

    Detection methods:
    - URL pattern matching (captcha, challenge, blocked pages)
    - Page content analysis (CAPTCHA keywords, error messages)
    - HTTP status code monitoring
    - Element detection (CAPTCHA iframes, reCAPTCHA widgets)
    """

    CAPTCHA_URL_PATTERNS = [
        'captcha', 'challenge', 'recaptcha', 'hcaptcha',
        'turnstile', 'arkose', 'funcaptcha', 'verify',
    ]

    CAPTCHA_TEXT_PATTERNS = [
        'verify you are human', 'are you a robot',
        'complete the captcha', 'security check',
        'prove you\'re not a robot', 'i\'m not a robot',
        'verify your identity', 'unusual traffic',
        'access denied', 'please verify', 'bot detection',
        'automated access', 'suspicious activity',
    ]

    BLOCK_TEXT_PATTERNS = [
        'access denied', '403 forbidden', 'blocked',
        'rate limit', 'too many requests', '429',
        'temporarily unavailable', 'please try again later',
        'your ip has been', 'banned', 'restricted',
    ]

    CAPTCHA_ELEMENT_JS = """
    (() => {
        const signals = [];
        // reCAPTCHA
        if (document.querySelector('.g-recaptcha, #recaptcha, [data-sitekey]'))
            signals.push('recaptcha');
        // hCaptcha
        if (document.querySelector('.h-captcha, [data-hcaptcha-sitekey]'))
            signals.push('hcaptcha');
        // Cloudflare Turnstile
        if (document.querySelector('.cf-turnstile, [data-cf-turnstile]'))
            signals.push('turnstile');
        // Generic CAPTCHA iframes
        document.querySelectorAll('iframe').forEach(f => {
            const src = (f.src || '').toLowerCase();
            if (src.includes('captcha') || src.includes('challenge') ||
                src.includes('recaptcha') || src.includes('hcaptcha'))
                signals.push('captcha_iframe:' + src.substring(0, 100));
        });
        // Cloudflare challenge
        if (document.querySelector('#challenge-form, #cf-challenge-running'))
            signals.push('cloudflare_challenge');
        return JSON.stringify(signals);
    })()
    """

    def __init__(self, god: GodMode):
        self.god = god

    def _scan_url_signals(self, tab_id: str) -> Tuple[List[str], bool]:
        """Check URL for captcha patterns. Returns (signals, captcha_found)."""
        signals = []
        url = self.god.cdp.eval(tab_id, 'window.location.href') or ''
        for pattern in self.CAPTCHA_URL_PATTERNS:
            if pattern in url.lower():
                signals.append(f'url_pattern:{pattern}')
        return signals, bool(signals)

    def _scan_title_signals(self, tab_id: str) -> Tuple[List[str], bool]:
        """Check page title for captcha patterns."""
        signals = []
        title = self.god.cdp.eval(tab_id, 'document.title') or ''
        for pattern in self.CAPTCHA_TEXT_PATTERNS:
            if pattern in title.lower():
                signals.append(f'title:{pattern}')
        return signals, bool(signals)

    def _scan_body_signals(self, tab_id: str) -> Tuple[List[str], bool, bool, bool]:
        """Check body text for captcha/block patterns.
        Returns (signals, captcha, blocked, rate_limited)."""
        signals = []
        captcha = blocked = rate_limited = False
        body_text = self.god.cdp.eval(
            tab_id,
            '(document.body && document.body.innerText || "").substring(0, 2000).toLowerCase()'
        ) or ''
        for pattern in self.CAPTCHA_TEXT_PATTERNS:
            if pattern in body_text:
                signals.append(f'text:{pattern}')
                captcha = True
        for pattern in self.BLOCK_TEXT_PATTERNS:
            if pattern in body_text:
                signals.append(f'block:{pattern}')
                blocked = True
                if 'rate limit' in pattern or '429' in pattern:
                    rate_limited = True
        return signals, captcha, blocked, rate_limited

    def _scan_captcha_elements(self, tab_id: str) -> Tuple[List[str], bool]:
        """Check for CAPTCHA DOM elements."""
        raw = self.god.cdp.eval(tab_id, self.CAPTCHA_ELEMENT_JS)
        if not raw:
            return [], False
        try:
            elements = json.loads(raw) if isinstance(raw, str) else raw
            return list(elements), bool(elements)
        except (json.JSONDecodeError, TypeError):
            return [], False

    @staticmethod
    def _determine_severity(captcha: bool, blocked: bool,
                            rate_limited: bool) -> Tuple[str, str]:
        """Return (severity, recommendation) based on detection results."""
        if captcha:
            return 'captcha', ('CAPTCHA detected. Options: wait and retry, '
                               'switch profile, use different IP, or request '
                               'human intervention.')
        if blocked:
            return 'blocked', ('Access blocked. Options: wait 30-60s, '
                               'switch to different profile, or try via '
                               'different URL.')
        if rate_limited:
            return 'rate_limited', ('Rate limited. Wait 30-60 seconds '
                                    'before retrying.')
        return 'none', 'No blocks detected.'

    def check(self, tab_id: str = None) -> Dict:
        """
        Run all detection checks.

        Returns dict with: blocked, captcha, rate_limited, signals,
        severity, recommendation.
        """
        tab_id = tab_id or self.god._get_active_tab()
        signals = []
        captcha = blocked = rate_limited = False

        try:
            url_sig, url_captcha = self._scan_url_signals(tab_id)
            signals.extend(url_sig)
            captcha |= url_captcha

            title_sig, title_captcha = self._scan_title_signals(tab_id)
            signals.extend(title_sig)
            captcha |= title_captcha

            body_sig, body_captcha, body_blocked, body_rl = self._scan_body_signals(tab_id)
            signals.extend(body_sig)
            captcha |= body_captcha
            blocked |= body_blocked
            rate_limited |= body_rl

            el_sig, el_captcha = self._scan_captcha_elements(tab_id)
            signals.extend(el_sig)
            captcha |= el_captcha

        except Exception as e:
            signals.append(f'check_error:{e}')

        severity, recommendation = self._determine_severity(
            captcha, blocked, rate_limited)

        return {
            'blocked': blocked or captcha,
            'captcha': captcha,
            'rate_limited': rate_limited,
            'signals': signals,
            'severity': severity,
            'recommendation': recommendation,
        }


# ═══════════════════════════════════════════════════════════════════════
# MODULE 7: MULTI-TAB ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

class MultiTabOrchestrator:
    """
    Coordinate actions across multiple browser tabs.

    Use cases:
    - Open a link in a new tab, extract data, close it, return to original
    - Compare content across two pages side by side
    - Keep a reference page open while filling a form on another
    - Chain: search on Google → open result in new tab → extract → close
    """

    def __init__(self, god: GodMode):
        self.god = god
        self._tab_stack: List[str] = []  # Tab navigation stack

    def open_in_new_tab(self, url: str) -> str:
        """Open URL in new tab, push current tab to stack. Returns new tab ID."""
        current = self.god._get_active_tab()
        if current:
            self._tab_stack.append(current)

        new_tab_id = self.god.new_tab(url)
        time.sleep(1.5)
        return new_tab_id

    def return_to_previous(self):
        """Close current tab and return to previous tab in stack."""
        current = self.god._get_active_tab()
        if current:
            self.god.close_tab(current)

        if self._tab_stack:
            prev = self._tab_stack.pop()
            self.god.activate_tab(prev)
            self.god._active_tab = prev

    def extract_and_return(self, url: str, extract_fn: Callable = None) -> Any:
        """
        Open URL in new tab, extract data, close tab, return to original.

        Args:
            url: URL to open
            extract_fn: Function(god, tab_id) → data. Defaults to scene().

        Returns:
            Whatever extract_fn returns
        """
        tab_id = self.open_in_new_tab(url)
        try:
            if extract_fn:
                result = extract_fn(self.god, tab_id)
            else:
                result = self.god.scene(tab_id=tab_id)
            return result
        finally:
            self.return_to_previous()

    def parallel_extract(self, urls: List[str],
                         extract_fn: Callable = None) -> List[Any]:
        """
        Open multiple URLs in tabs, extract data from each, close all.

        Note: Opens tabs sequentially (CDP is single-threaded).
        """
        results = []
        original_tab = self.god._get_active_tab()

        tab_ids = []
        for url in urls:
            tid = self.god.new_tab(url)
            tab_ids.append(tid)
            time.sleep(1)

        for tid in tab_ids:
            self.god.activate_tab(tid)
            time.sleep(0.5)
            try:
                if extract_fn:
                    results.append(extract_fn(self.god, tid))
                else:
                    results.append(self.god.scene(tab_id=tid))
            except Exception as e:
                results.append({'error': str(e)})

        # Close all new tabs
        for tid in tab_ids:
            try:
                self.god.close_tab(tid)
            except Exception:
                pass

        # Return to original
        if original_tab:
            self.god.activate_tab(original_tab)
            self.god._active_tab = original_tab

        return results

    @property
    def stack_depth(self) -> int:
        return len(self._tab_stack)


# ═══════════════════════════════════════════════════════════════════════
# MODULE 8: THE BRAIN — UNIFIED COGNITIVE CONTROLLER
# ═══════════════════════════════════════════════════════════════════════

class Brain:
    """
    The ultimate unified controller.
    Combines perception (GOD MODE) + cognition (LLM) + execution (Agent)
    + recovery + human-like behavior + block detection.

    This is the single entry point for all AI-driven web automation.

    Usage:
        # Quick: scripted automation
        brain = Brain()
        brain.execute_script("https://google.com", [
            {"action": "type", "target": "Search", "value": "AI"},
            {"action": "press", "value": "Enter"},
        ])

        # Full: LLM-driven autonomous agent (key from env or data/secrets.json)
        brain = Brain(llm_provider="openai")
        brain.execute_mission(
            "Go to Wikipedia and search for machine learning",
            start_url="https://en.wikipedia.org"
        )

        # Stealth: invisible Chrome with specific profile
        brain = Brain(
            llm_provider="ollama", llm_model="llama3.1",
            stealth_mode="hidden", chrome_profile="Profile 3"
        )
        brain.execute_mission("Check Facebook notifications")

        # Extract data from any page
        data = brain.extract("https://example.com")
        print(data['text'])
        print(data['tables'])
    """

    def _launch_stealth_chrome(self, cdp_port: int, stealth_mode: str,
                               chrome_profile: str) -> None:
        """Launch Chrome in stealth mode with the given profile."""
        try:
            resolved = StealthLauncher.resolve_profile(chrome_profile)
            profile_dir = resolved['directory']
            self._chrome_profile = resolved['name']
        except ValueError:
            profile_dir = chrome_profile
        self._launcher = StealthLauncher(port=cdp_port)
        mode_map = {
            'headless': StealthLauncher.Mode.HEADLESS,
            'hidden': StealthLauncher.Mode.HIDDEN,
            'offscreen': StealthLauncher.Mode.OFFSCREEN,
        }
        self._launcher.launch(
            mode=mode_map.get(stealth_mode, StealthLauncher.Mode.HEADLESS),
            profile=profile_dir,
        )

    def __init__(self, cdp_port: int = 9222,
                 llm_provider: str = "mock",
                 llm_api_key: str = None,
                 llm_model: str = None,
                 llm_base_url: str = None,
                 stealth_mode: str = None,
                 chrome_profile: str = None,
                 human_speed: float = 1.0):
        """
        Initialize the Brain.

        Args:
            cdp_port: Chrome DevTools Protocol port
            llm_provider: "openai", "claude", "gemini", "ollama", "mock"
            llm_api_key: API key (or use env vars)
            llm_model: Model name (auto-defaults per provider)
            llm_base_url: Custom API endpoint
            stealth_mode: None (use existing Chrome), "headless", "hidden", "offscreen"
            chrome_profile: Chrome profile directory or display name (e.g., "SOCIALS", "Mak", "Profile 3")
            human_speed: Behavior speed multiplier (0.5=fast, 1.0=normal, 2.0=slow)
        """
        self._port = cdp_port
        self._launcher = None
        self._stealth_mode = stealth_mode
        self._chrome_profile = chrome_profile

        if stealth_mode and chrome_profile:
            self._launch_stealth_chrome(cdp_port, stealth_mode, chrome_profile)

        # Core modules
        self.god = GodMode(cdp_port=cdp_port)
        self.agent = AutonomousAgent(god=self.god)
        self.llm = LLMConnector(
            provider=llm_provider, api_key=llm_api_key,
            model=llm_model, base_url=llm_base_url,
        )

        # Advanced modules
        self.extractor = ContentExtractor(self.god)
        self.recovery = ErrorRecovery(self.god)
        self.human = HumanBehavior(speed=human_speed)
        self.blocks = BlockDetector(self.god)
        self.tabs = MultiTabOrchestrator(self.god)
        self.differ = PageDiffer()

    def _make_enhanced_decide(self, check_blocks: bool) -> Callable:
        """Build a decide function that adds block-checking and human delays."""
        def enhanced_decide(system_prompt: str, user_prompt: str) -> str:
            enriched_prompt = user_prompt
            if check_blocks:
                block_check = self.blocks.check()
                if block_check['blocked']:
                    logger.warning(f"Block detected: {block_check['severity']}")
                    enriched_prompt += f"\n\nWARNING: {block_check['recommendation']}"
                    if block_check['captcha']:
                        return '{"action": "fail", "reason": "CAPTCHA detected -- requires human intervention"}'
            time.sleep(self.human.delay_before_action('think'))
            return self.llm.decide(system_prompt, enriched_prompt)
        return enhanced_decide

    def _make_step_callback(self, on_step: Callable = None) -> Callable:
        """Build a step callback with human-like delays."""
        def step_callback(step, action, observation):
            time.sleep(self.human.delay_before_action(action.action))
            if self.human.should_pause():
                time.sleep(self.human.pause_duration())
            if on_step:
                on_step(step, action, observation)
        return step_callback

    def execute_mission(self, objective: str, start_url: str = None,
                        max_steps: int = 30,
                        check_blocks: bool = True,
                        on_step: Callable = None) -> Dict:
        """
        Execute a mission using the LLM brain.

        Args:
            objective: Natural language task description
            start_url: Starting URL (optional, navigates first)
            max_steps: Maximum action steps
            check_blocks: Check for CAPTCHAs/blocks between actions
            on_step: Callback(step, action, observation) per step

        Returns:
            Complete session summary dict
        """
        if start_url:
            self.god.navigate(start_url)
            time.sleep(self.human.delay_after_navigation())

        result = self.agent.run(
            task=objective,
            decide_fn=self._make_enhanced_decide(check_blocks),
            max_steps=max_steps,
            on_step=self._make_step_callback(on_step),
        )
        result['llm_stats'] = self.llm.stats
        return result

    def execute_script(self, start_url: str, actions: List[Dict],
                       delay: float = 0.5) -> Dict:
        """
        Execute a pre-scripted action sequence (no LLM needed).

        Args:
            start_url: URL to start at
            actions: List of action dicts
            delay: Seconds between actions

        Returns:
            Session summary
        """
        self.god.navigate(start_url)
        time.sleep(1.5)
        return self.agent.run_script(actions, delay=delay)

    def extract(self, url: str = None, tab_id: str = None) -> Dict:
        """
        Extract all structured data from a page.

        Returns dict with: metadata, text, links, tables, forms
        """
        if url:
            self.god.navigate(url)
            time.sleep(1.5)
        return self.extractor.extract_all(tab_id)

    def see(self, depth: str = 'standard') -> Dict:
        """Shortcut to GOD MODE perception."""
        return self.god.see(depth=depth)

    def scene(self) -> str:
        """Shortcut to compressed scene for LLM."""
        return self.god.scene()

    def click(self, target) -> bool:
        """Shortcut to click."""
        return self.god.click(target)

    def type_text(self, text: str):
        """Shortcut to type."""
        self.god.type_text(text)

    def navigate(self, url: str):
        """Shortcut to navigate."""
        self.god.navigate(url)

    def check_blocks(self) -> Dict:
        """Check for CAPTCHAs and blocks."""
        return self.blocks.check()

    def open_in_tab(self, url: str) -> str:
        """Open URL in new tab, remember current tab."""
        return self.tabs.open_in_new_tab(url)

    def return_to_previous_tab(self):
        """Close current tab, go back to previous."""
        self.tabs.return_to_previous()

    def status(self) -> Dict:
        """Complete system status."""
        base = self.god.status()
        base['llm'] = self.llm.stats
        base['stealth'] = {
            'mode': self._stealth_mode,
            'profile': self._chrome_profile,
            'launcher_running': self._launcher.running if self._launcher else False,
        }
        base['human_behavior'] = {
            'speed': self.human.speed,
            'actions': self.human._action_count,
        }
        return base

    def shutdown(self):
        """Clean shutdown — close stealth Chrome if we launched it."""
        if self._launcher:
            self._launcher.stop()
            self._launcher = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()

    def __del__(self):
        self.shutdown()


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def _cmd_profiles():
    for p in StealthLauncher.list_profiles():
        print(f"  {p['directory']:20s} \u2192 {p['name']}")


def _cmd_mission(args):
    with Brain(
        cdp_port=args.port,
        llm_provider=args.provider,
        llm_model=args.model,
        llm_api_key=args.key,
        stealth_mode=args.stealth,
        chrome_profile=args.profile,
    ) as brain:
        result = brain.execute_mission(
            args.objective,
            start_url=args.url,
            max_steps=args.max_steps,
            on_step=lambda s, a, o: print(
                f"  [{s}] {'\u2713' if o.success else '\u2717'} {a.action}: "
                f"{o.action_result or o.error}"
            ),
        )
        print(json.dumps(result, indent=2, default=str))


def _cmd_extract(args):
    god = GodMode(cdp_port=args.port)
    extractor = ContentExtractor(god)
    god.navigate(args.url)
    time.sleep(2)

    handlers = {
        'all': lambda: print(json.dumps(
            extractor.extract_all(), indent=2, default=str, ensure_ascii=False)),
        'text': lambda: print(extractor.extract_text()),
        'links': lambda: [print(f"  {l.get('text', '')[:60]:60s} \u2192 {l.get('href', '')}")
                          for l in extractor.extract_links()],
        'tables': lambda: [_print_table(t) for t in extractor.extract_tables()],
        'forms': lambda: [print(f"  [{f['type']:10s}] {f['label'] or f['name']:30s} = {f.get('value', '')}")
                          for f in extractor.extract_forms()],
        'meta': lambda: print(json.dumps(extractor.extract_metadata(), indent=2)),
    }
    handlers[args.what]()


def _print_table(table: Dict):
    print(f"\n--- Table {table['index']} ---")
    for row in table['rows']:
        print(' | '.join(row))


def _cmd_check(args):
    god = GodMode(cdp_port=args.port)
    result = BlockDetector(god).check()
    print(json.dumps(result, indent=2))


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='AI Brain -- Cognitive layer for web automation')
    sub = parser.add_subparsers(dest='command')

    mission = sub.add_parser('mission', help='Execute an LLM-driven mission')
    mission.add_argument('objective', help='Mission objective')
    mission.add_argument('--url', help='Starting URL')
    mission.add_argument('--provider', default='mock', help='LLM provider')
    mission.add_argument('--model', default=None, help='LLM model')
    mission.add_argument('--key', default=None, help='API key')
    mission.add_argument('--profile', default=None,
                        help='Chrome profile directory or display name')
    mission.add_argument('--stealth', default=None, help='Stealth mode')
    mission.add_argument('--max-steps', type=int, default=30)
    mission.add_argument('--port', type=int, default=9222)

    extract = sub.add_parser('extract', help='Extract data from a page')
    extract.add_argument('url', help='URL to extract from')
    extract.add_argument('--what', default='all',
                         choices=['all', 'text', 'links', 'tables', 'forms', 'meta'])
    extract.add_argument('--port', type=int, default=9222)

    check = sub.add_parser('check', help='Check for blocks/CAPTCHAs')
    check.add_argument('--port', type=int, default=9222)

    sub.add_parser('profiles', help='List Chrome profiles')

    args = parser.parse_args()

    dispatch = {
        'profiles': lambda: _cmd_profiles(),
        'mission': lambda: _cmd_mission(args),
        'extract': lambda: _cmd_extract(args),
        'check': lambda: _cmd_check(args),
    }

    handler = dispatch.get(args.command)
    if handler:
        handler()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
