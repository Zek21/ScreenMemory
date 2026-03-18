#!/usr/bin/env python3
"""
SKYNET RESEARCH ENGINE — Level 5 Internet Intelligence
=======================================================
Gives Skynet workers the ability to research the internet, discover
latest technologies, and apply cutting-edge knowledge to the system.

Workers have these internet tools available:
  1. web_fetch (Copilot CLI built-in) — fetch any URL as markdown
  2. Chrome CDP (tools/chrome_bridge/cdp.py) — full browser automation
  3. GodMode (tools/chrome_bridge/god_mode.py) — semantic web navigation
  4. browser_control (tools/browser/browser_control.py) — MCP browser

This module provides higher-level research capabilities:
  - Google search URL generation
  - Technology discovery queries
  - Research synthesis patterns
  - Knowledge capture from web sources

Usage (from workers):
  from tools.skynet_research import research_urls, tech_discovery_queries
  urls = research_urls("Python asyncio best practices 2026")
  queries = tech_discovery_queries("performance optimization")

CLI:
  python tools/skynet_research.py search "query"        # Generate search URLs
  python tools/skynet_research.py tech "topic"          # Tech discovery queries
  python tools/skynet_research.py sources               # List known tech sources
  python tools/skynet_research.py protocol              # Print research protocol

Version: 1.0.0 — Level 5 (2026-03-18)
# signed: orchestrator
"""

import sys
import json
from urllib.parse import quote_plus
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"


# ──────────────────────────────────────────────────────────────────────
# AUTHORITATIVE TECHNOLOGY SOURCES
# ──────────────────────────────────────────────────────────────────────

TECH_SOURCES = {
    "python": [
        "https://docs.python.org/3/whatsnew/",
        "https://peps.python.org/pep-0000/",
        "https://realpython.com/",
        "https://pypi.org/search/?q={query}",
        "https://github.com/trending/python?since=weekly",
    ],
    "javascript": [
        "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
        "https://github.com/trending/javascript?since=weekly",
        "https://www.npmjs.com/search?q={query}",
        "https://tc39.es/ecma262/",
    ],
    "ai_ml": [
        "https://arxiv.org/list/cs.AI/recent",
        "https://arxiv.org/list/cs.CL/recent",
        "https://huggingface.co/models?sort=trending",
        "https://github.com/trending?since=weekly",
        "https://paperswithcode.com/latest",
    ],
    "devops": [
        "https://github.com/trending?since=weekly",
        "https://www.cncf.io/blog/",
        "https://kubernetes.io/blog/",
        "https://docs.docker.com/engine/release-notes/",
    ],
    "security": [
        "https://nvd.nist.gov/vuln/search",
        "https://cve.mitre.org/cve/search_cve_list.html",
        "https://owasp.org/www-project-top-ten/",
        "https://github.com/advisories",
    ],
    "web": [
        "https://web.dev/blog/",
        "https://developer.chrome.com/blog/",
        "https://caniuse.com/",
        "https://www.w3.org/TR/",
    ],
    "go": [
        "https://go.dev/doc/",
        "https://go.dev/blog/",
        "https://github.com/trending/go?since=weekly",
        "https://pkg.go.dev/search?q={query}",
    ],
    "windows": [
        "https://learn.microsoft.com/en-us/windows/win32/",
        "https://learn.microsoft.com/en-us/windows/apps/",
        "https://devblogs.microsoft.com/directx/",
    ],
    "general": [
        "https://news.ycombinator.com/",
        "https://lobste.rs/",
        "https://github.com/trending?since=weekly",
        "https://stackoverflow.com/questions?tab=Votes&pagesize=15",
    ],
}


# ──────────────────────────────────────────────────────────────────────
# SEARCH URL GENERATORS
# ──────────────────────────────────────────────────────────────────────

def google_search_url(query, site=None, recent=True):
    """Generate a Google search URL."""
    q = query
    if site:
        q = f"site:{site} {query}"
    if recent:
        q += " 2025 OR 2026"
    return f"https://www.google.com/search?q={quote_plus(q)}"


def github_search_url(query, language=None, sort="stars"):
    """Generate a GitHub search URL."""
    q = query
    if language:
        q += f" language:{language}"
    return f"https://github.com/search?q={quote_plus(q)}&sort={sort}&type=repositories"


def stackoverflow_search_url(query, tag=None):
    """Generate a StackOverflow search URL."""
    q = query
    if tag:
        q = f"[{tag}] {query}"
    return f"https://stackoverflow.com/search?q={quote_plus(q)}&tab=votes"


def arxiv_search_url(query):
    """Generate an arXiv search URL for latest papers."""
    return f"https://arxiv.org/search/?searchtype=all&query={quote_plus(query)}&order=-announced_date_first"


def pypi_search_url(query):
    """Generate a PyPI package search URL."""
    return f"https://pypi.org/search/?q={quote_plus(query)}"


def npm_search_url(query):
    """Generate an npm package search URL."""
    return f"https://www.npmjs.com/search?q={quote_plus(query)}"


def research_urls(query, domains=None):
    """
    Generate a comprehensive set of research URLs for a query.
    
    Returns a dict of {source: url} that workers can fetch with web_fetch.
    """
    urls = {
        "google": google_search_url(query),
        "google_recent": google_search_url(query, recent=True),
        "github": github_search_url(query),
        "stackoverflow": stackoverflow_search_url(query),
    }
    
    # Add domain-specific sources
    if domains:
        for domain in domains:
            sources = TECH_SOURCES.get(domain, [])
            for i, src in enumerate(sources[:3]):
                key = f"{domain}_{i}"
                if "{query}" in src:
                    urls[key] = src.format(query=quote_plus(query))
                else:
                    urls[key] = src
    
    return urls


def tech_discovery_queries(topic):
    """
    Generate a set of research queries for discovering the latest
    technology related to a topic.
    
    Returns a list of (query, purpose) tuples.
    """
    return [
        (f"{topic} latest best practices 2026", "Current best practices"),
        (f"{topic} state of the art 2025 2026", "State of the art"),
        (f"{topic} new tools libraries 2026", "New tools and libraries"),
        (f"{topic} performance optimization techniques", "Performance optimization"),
        (f"{topic} security vulnerabilities fixes", "Security concerns"),
        (f"{topic} architecture patterns modern", "Modern architecture"),
        (f"awesome {topic} github", "Curated resource lists"),
        (f"{topic} benchmark comparison 2025 2026", "Benchmarks and comparisons"),
    ]


# ──────────────────────────────────────────────────────────────────────
# RESEARCH PROTOCOL
# ──────────────────────────────────────────────────────────────────────

RESEARCH_PROTOCOL = """
=== SKYNET RESEARCH PROTOCOL (Level 5) ===

When researching a topic, follow this structured approach:

PHASE 1 — SURVEY (breadth-first)
  Use web_fetch to scan multiple sources:
  - Google search for recent results (2025-2026)
  - GitHub trending repos in the relevant language
  - StackOverflow top-voted answers
  - Official documentation for the technology
  
  Example:
    web_fetch("https://www.google.com/search?q=python+asyncio+best+practices+2026")
    web_fetch("https://github.com/trending/python?since=weekly")

PHASE 2 — DEEP DIVE (depth-first)
  Pick the most promising 2-3 sources and read them fully:
  - Official docs: read the "What's New" or changelog
  - GitHub repos: read the README, check stars/activity
  - Blog posts: extract key techniques and patterns
  
  Example:
    web_fetch("https://docs.python.org/3/whatsnew/3.13.html")
    web_fetch("https://github.com/user/repo", max_length=10000)

PHASE 3 — SYNTHESIZE
  Combine findings into actionable insights:
  - What's the current state of the art?
  - What new tools/libraries should we adopt?
  - What patterns should we apply to our codebase?
  - What security issues should we address?

PHASE 4 — APPLY
  Implement findings directly:
  - Update relevant code with modern patterns
  - Add new dependencies if they solve real problems
  - Update documentation with new knowledge
  - Broadcast learnings to the Skynet bus

PHASE 5 — SHARE
  Post discoveries to the collective:
  from tools.skynet_knowledge import broadcast_learning
  broadcast_learning("worker_name", "discovery details", "technology", ["tags"])

=== KEY TOOLS FOR RESEARCH ===

1. web_fetch(url) — Built into Copilot CLI. Fetches ANY URL as markdown.
   web_fetch("https://example.com", max_length=10000)

2. Chrome CDP — For pages requiring JavaScript rendering:
   from tools.chrome_bridge.cdp import CDP
   cdp = CDP()
   await cdp.navigate(tab_id, "https://example.com")
   content = await cdp.evaluate(ws, "document.body.innerText")

3. GodMode — For complex web UIs requiring interaction:
   from tools.chrome_bridge.god_mode import GodMode
   gm = GodMode()
   gm.navigate("https://example.com")
   gm.click("Sign In")

4. Research URLs — Pre-built search URL generators:
   from tools.skynet_research import research_urls, tech_discovery_queries
   urls = research_urls("topic", domains=["python", "ai_ml"])
   queries = tech_discovery_queries("performance")

=== THINKING OUTSIDE THE BOX ===

Don't just solve the immediate problem. Ask:
- What would the best engineer in the world do here?
- Is there a library that already solves this perfectly?
- What's the cutting-edge approach to this problem?
- Are there arxiv papers with novel solutions?
- What are the top GitHub repos in this space doing?
- Is our architecture fundamentally right, or should it be redesigned?
- What technology from other domains could be applied here?
"""


# ──────────────────────────────────────────────────────────────────────
# WEB FETCH HELPER (for use within Python scripts)
# ──────────────────────────────────────────────────────────────────────

def fetch_url(url, max_length=5000):
    """
    Fetch a URL and return its content as text.
    This is a Python-native fallback for when web_fetch (Copilot CLI tool) 
    is not available (e.g., in standalone scripts).
    
    For workers in Copilot CLI, prefer using the built-in web_fetch tool directly.
    """
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Skynet/5.0"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            return content[:max_length]
    except Exception as e:
        return f"FETCH_ERROR: {e}"


def search_google(query, max_results=5):
    """
    Search Google and return result URLs.
    Uses web scraping of Google search results page.
    
    For workers in Copilot CLI, prefer using web_fetch on the Google search URL.
    """
    import re
    url = google_search_url(query, recent=True)
    content = fetch_url(url, max_length=15000)
    # Extract URLs from Google results
    urls = re.findall(r'https?://[^\s<>"\']+', content)
    # Filter out Google's own URLs
    external = [u for u in urls if "google.com" not in u and "gstatic" not in u]
    return list(dict.fromkeys(external))[:max_results]


# ──────────────────────────────────────────────────────────────────────
# KNOWLEDGE CAPTURE FROM RESEARCH
# ──────────────────────────────────────────────────────────────────────

def capture_research(worker_name, topic, findings, sources):
    """
    Capture research findings into the Skynet knowledge system.
    
    Args:
        worker_name: Who did the research
        topic: What was researched
        findings: Key findings (list of strings)
        sources: URLs consulted (list of strings)
    """
    try:
        from tools.skynet_knowledge import broadcast_learning
        summary = f"RESEARCH [{topic}]: " + "; ".join(findings[:5])
        broadcast_learning(worker_name, summary, "research", [topic, "level5"])
    except Exception:
        pass
    
    # Also save to local research log
    log_path = DATA / "research_log.json"
    entry = {
        "worker": worker_name,
        "topic": topic,
        "findings": findings,
        "sources": sources,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }
    
    log = []
    if log_path.exists():
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            log = []
    
    log.append(entry)
    # Keep last 100 entries
    log = log[-100:]
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    return entry


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python tools/skynet_research.py search 'query'")
        print("  python tools/skynet_research.py tech 'topic'")
        print("  python tools/skynet_research.py sources")
        print("  python tools/skynet_research.py protocol")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "skynet AI agent system"
        urls = research_urls(query, domains=["python", "ai_ml", "general"])
        print(f"=== RESEARCH URLs for: {query} ===\n")
        for name, url in urls.items():
            print(f"  {name:20s}: {url}")
        print(f"\nUse web_fetch on any URL above to read content.")
    
    elif cmd == "tech":
        topic = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "multi-agent AI systems"
        queries = tech_discovery_queries(topic)
        print(f"=== TECH DISCOVERY for: {topic} ===\n")
        for q, purpose in queries:
            url = google_search_url(q)
            print(f"  [{purpose}]")
            print(f"    Query: {q}")
            print(f"    URL:   {url}\n")
    
    elif cmd == "sources":
        print("=== AUTHORITATIVE TECHNOLOGY SOURCES ===\n")
        for domain, sources in TECH_SOURCES.items():
            print(f"  {domain}:")
            for src in sources:
                print(f"    - {src}")
            print()
    
    elif cmd == "protocol":
        print(RESEARCH_PROTOCOL)
    
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
