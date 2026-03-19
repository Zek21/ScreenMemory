#!/usr/bin/env python3
"""CDP-based LinkedIn post publisher.

Connects to Chrome via CDP (port 9222), finds or opens a LinkedIn tab,
verifies login status, opens the post modal, types content, and clicks Post.

Usage:
    python tools/linkedin_cdp_poster.py --content "Your post text here"
    python tools/linkedin_cdp_poster.py --content "Post text" --dry-run
    python tools/linkedin_cdp_poster.py --file path/to/post.txt

Requirements:
    - Chrome running with --remote-debugging-port=9222
    - websocket-client package (pip install websocket-client)
    - Logged into LinkedIn in Chrome
"""
# signed: alpha

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import requests
    import websocket
except ImportError as e:
    print(f"Missing dependency: {e}. Install: pip install websocket-client requests")
    sys.exit(1)

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
LINKEDIN_FEED = "https://www.linkedin.com/feed/"
CONNECT_TIMEOUT = 10
NAV_WAIT = 5
MODAL_WAIT = 3
POST_CONFIRM_WAIT = 3


class CDPConnection:
    """Manages a CDP WebSocket connection to a Chrome tab."""
    # signed: alpha

    def __init__(self, ws_url: str, timeout: int = 15):
        self._ws = websocket.create_connection(ws_url, timeout=timeout)
        self._msg_id = 1

    def send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and wait for the matching response."""
        msg_id = self._msg_id
        self._msg_id += 1
        cmd = {"id": msg_id, "method": method}
        if params:
            cmd["params"] = params
        self._ws.send(json.dumps(cmd))
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") == msg_id:
                return resp

    def evaluate(self, expression: str) -> str | None:
        """Evaluate JS expression and return the string result."""
        r = self.send("Runtime.evaluate", {"expression": expression})
        return r.get("result", {}).get("result", {}).get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def get_tabs() -> list[dict]:
    """Fetch all Chrome tabs via CDP HTTP endpoint."""  # signed: alpha
    try:
        resp = requests.get(f"{CDP_URL}/json", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        print(f"ERROR: Cannot connect to Chrome CDP on port {CDP_PORT}.")
        print(f"Start Chrome with: chrome --remote-debugging-port={CDP_PORT}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: CDP tab list failed: {e}")
        sys.exit(1)


def activate_tab(tab_id: str):
    """Bring a Chrome tab to the foreground."""  # signed: alpha
    try:
        requests.get(f"{CDP_URL}/json/activate/{tab_id}", timeout=5)
    except Exception:
        pass


def find_linkedin_tab(tabs: list[dict]) -> dict | None:
    """Find an existing LinkedIn tab, preferring the feed page."""  # signed: alpha
    feed_tab = None
    any_linkedin = None
    for t in tabs:
        url = t.get("url", "").lower()
        if t.get("type") != "page":
            continue
        if "linkedin.com/feed" in url:
            feed_tab = t
            break
        if "linkedin.com" in url and not any_linkedin:
            any_linkedin = t
    return feed_tab or any_linkedin


def open_linkedin_tab(tabs: list[dict]) -> dict:
    """Navigate the first available page tab to LinkedIn feed."""  # signed: alpha
    for t in tabs:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            activate_tab(t["id"])
            time.sleep(0.5)
            with CDPConnection(t["webSocketDebuggerUrl"]) as cdp:
                cdp.send("Page.navigate", {"url": LINKEDIN_FEED})
            time.sleep(NAV_WAIT)
            # Re-fetch tabs to get updated URL
            refreshed = get_tabs()
            for rt in refreshed:
                if rt.get("id") == t["id"]:
                    return rt
            return t
    print("ERROR: No usable Chrome tab found.")
    sys.exit(1)


def check_login(cdp: CDPConnection) -> bool:
    """Return True if user is logged into LinkedIn."""  # signed: alpha
    url = cdp.evaluate("window.location.href") or ""
    if "login" in url.lower() or "uas/login" in url.lower() or "checkpoint" in url.lower():
        return False
    # Secondary: check for feed-specific elements that only appear when logged in
    has_feed = cdp.evaluate(
        "!!document.querySelector('.feed-shared-update-v2, .share-box-feed-entry__trigger, "
        "[data-control-name=\"share.main_trigger\"], button[aria-label*=\"Start a post\"]')"
    )
    return has_feed == True or has_feed == "true"  # noqa: E712


def click_start_post(cdp: CDPConnection) -> bool:
    """Click the 'Start a post' button to open the post modal. Returns True on success."""
    # signed: alpha
    result = cdp.evaluate("""
    (function() {
        // Strategy 1: dedicated share-box trigger
        var btn = document.querySelector('.share-box-feed-entry__trigger');
        // Strategy 2: data-control-name attribute
        if (!btn) btn = document.querySelector('[data-control-name="share.main_trigger"]');
        // Strategy 3: aria-label
        if (!btn) btn = document.querySelector('button[aria-label*="Start a post"]');
        // Strategy 4: text content scan
        if (!btn) {
            var buttons = document.querySelectorAll('button, div[role="button"]');
            for (var i = 0; i < buttons.length; i++) {
                var text = (buttons[i].textContent || '').trim();
                if (text.includes('Start a post') || text.includes('start a post')) {
                    btn = buttons[i];
                    break;
                }
            }
        }
        if (btn) {
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return 'OK';
        }
        return 'NOT_FOUND';
    })()
    """)
    return result == "OK"


def wait_for_editor(cdp: CDPConnection, timeout: int = 8) -> bool:
    """Poll until the post editor (contenteditable / ql-editor) appears."""
    # signed: alpha
    for _ in range(timeout * 2):
        found = cdp.evaluate(
            "!!(document.querySelector('.ql-editor') || "
            "document.querySelector('[contenteditable=\"true\"][role=\"textbox\"]') || "
            "document.querySelector('[role=\"textbox\"][contenteditable=\"true\"]'))"
        )
        if found == True or found == "true":  # noqa: E712
            return True
        time.sleep(0.5)
    return False


def set_post_content(cdp: CDPConnection, text: str) -> bool:
    """Type post content into the LinkedIn post modal editor."""
    # signed: alpha
    # Escape text for JS string literal
    js_text = (text
               .replace("\\", "\\\\")
               .replace("'", "\\'")
               .replace("\n", "\\n")
               .replace("\r", ""))

    result = cdp.evaluate(f"""
    (function() {{
        var editor = document.querySelector('.ql-editor');
        if (!editor) editor = document.querySelector('[contenteditable="true"][role="textbox"]');
        if (!editor) editor = document.querySelector('[role="textbox"][contenteditable="true"]');
        if (!editor) return 'NO_EDITOR';

        editor.focus();
        // Build paragraph HTML from text lines
        var text = '{js_text}';
        var lines = text.split('\\n');
        var html = lines.map(function(line) {{
            return line.trim() ? '<p>' + line + '</p>' : '<p><br></p>';
        }}).join('');
        editor.innerHTML = html;
        // Dispatch input event so LinkedIn registers the content change
        editor.dispatchEvent(new Event('input', {{bubbles: true}}));
        editor.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'OK';
    }})()
    """)
    return result == "OK"


def verify_post_content(cdp: CDPConnection, expected_snippet: str) -> bool:
    """Verify the editor contains at least the first 50 chars of expected content."""
    # signed: alpha
    snippet = expected_snippet[:50].replace("'", "\\'").replace("\n", " ")
    result = cdp.evaluate(f"""
    (function() {{
        var editor = document.querySelector('.ql-editor');
        if (!editor) editor = document.querySelector('[contenteditable="true"][role="textbox"]');
        if (!editor) return 'NO_EDITOR';
        var text = editor.innerText || editor.textContent || '';
        return text.includes('{snippet}') ? 'VERIFIED' : 'MISMATCH';
    }})()
    """)
    return result == "VERIFIED"


def click_post_button(cdp: CDPConnection) -> bool:
    """Click the 'Post' submit button in the modal."""
    # signed: alpha
    result = cdp.evaluate("""
    (function() {
        // LinkedIn post modal has a Post button -- find it precisely
        var buttons = document.querySelectorAll('button');
        var postBtn = null;
        for (var i = 0; i < buttons.length; i++) {
            var text = (buttons[i].innerText || buttons[i].textContent || '').trim();
            // Exact match "Post" -- avoid "Repost", "Start a post", etc.
            if (text === 'Post') {
                // Verify it's not disabled
                if (!buttons[i].disabled && !buttons[i].getAttribute('aria-disabled')) {
                    postBtn = buttons[i];
                    break;
                }
            }
        }
        if (!postBtn) {
            // Fallback: aria-label search
            postBtn = document.querySelector('button[aria-label="Post"]');
            if (postBtn && (postBtn.disabled || postBtn.getAttribute('aria-disabled') === 'true')) {
                postBtn = null;
            }
        }
        if (postBtn) {
            postBtn.click();
            return 'OK';
        }
        // Check if button exists but is disabled
        for (var j = 0; j < buttons.length; j++) {
            var t = (buttons[j].innerText || '').trim();
            if (t === 'Post' && (buttons[j].disabled || buttons[j].getAttribute('aria-disabled') === 'true')) {
                return 'DISABLED';
            }
        }
        return 'NOT_FOUND';
    })()
    """)
    return result == "OK"


def post_to_linkedin(content: str, dry_run: bool = False) -> dict:
    """Full LinkedIn posting pipeline. Returns status dict.

    Steps:
        1. Connect to CDP, find/open LinkedIn tab
        2. Check login status
        3. Click 'Start a post'
        4. Wait for editor modal
        5. Set post content
        6. Verify content was set
        7. Click Post (unless dry_run)
    """
    # signed: alpha
    result = {"status": "FAILED", "step": "init", "detail": ""}

    # Step 1: Find LinkedIn tab
    print("[1/7] Connecting to Chrome CDP...")
    tabs = get_tabs()
    tab = find_linkedin_tab(tabs)
    if tab:
        print(f"  Found LinkedIn tab: {tab['title'][:60]}")
        activate_tab(tab["id"])
        time.sleep(1)
    else:
        print("  No LinkedIn tab found -- opening one...")
        tab = open_linkedin_tab(tabs)

    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        result["detail"] = "No WebSocket URL for tab (tab may be inspected by DevTools)"
        print(f"  ERROR: {result['detail']}")
        return result

    with CDPConnection(ws_url) as cdp:
        # Navigate to feed if not already there
        current_url = cdp.evaluate("window.location.href") or ""
        if "linkedin.com/feed" not in current_url.lower():
            print("  Navigating to LinkedIn feed...")
            cdp.send("Page.navigate", {"url": LINKEDIN_FEED})
            time.sleep(NAV_WAIT)

        # Step 2: Check login
        print("[2/7] Checking login status...")
        if not check_login(cdp):
            result["step"] = "login_check"
            result["detail"] = "Not logged in. Please log into LinkedIn in Chrome first."
            print(f"  ERROR: {result['detail']}")
            return result
        print("  Logged in OK")

        # Step 3: Click Start a post
        print("[3/7] Clicking 'Start a post'...")
        if not click_start_post(cdp):
            result["step"] = "start_post"
            result["detail"] = "Could not find 'Start a post' button on feed page"
            print(f"  ERROR: {result['detail']}")
            return result
        print("  Post dialog opening...")

        # Step 4: Wait for editor
        print("[4/7] Waiting for editor modal...")
        if not wait_for_editor(cdp):
            result["step"] = "wait_editor"
            result["detail"] = "Post editor did not appear within timeout"
            print(f"  ERROR: {result['detail']}")
            return result
        print("  Editor ready")
        time.sleep(0.5)

        # Step 5: Set content
        print("[5/7] Setting post content...")
        if not set_post_content(cdp, content):
            result["step"] = "set_content"
            result["detail"] = "Could not find or write to editor element"
            print(f"  ERROR: {result['detail']}")
            return result
        print(f"  Content set ({len(content)} chars)")
        time.sleep(1)

        # Step 6: Verify content
        print("[6/7] Verifying content...")
        if not verify_post_content(cdp, content):
            result["step"] = "verify_content"
            result["detail"] = "Content verification failed -- editor text doesn't match"
            print(f"  WARNING: {result['detail']}")
            # Non-fatal: proceed anyway, LinkedIn may have reformatted

        if dry_run:
            result["status"] = "DRY_RUN_OK"
            result["step"] = "complete"
            result["detail"] = "Content set in editor. Post button NOT clicked (dry run)."
            print(f"[7/7] DRY RUN -- skipping Post click. Content is in the editor.")
            return result

        # Step 7: Click Post
        print("[7/7] Clicking Post button...")
        time.sleep(1)
        if not click_post_button(cdp):
            result["step"] = "post_click"
            result["detail"] = "Post button not found or disabled"
            print(f"  ERROR: {result['detail']}")
            return result

        time.sleep(POST_CONFIRM_WAIT)
        print("  Post submitted!")
        result["status"] = "POSTED"
        result["step"] = "complete"
        result["detail"] = f"Successfully posted {len(content)} chars to LinkedIn"

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Post to LinkedIn via Chrome CDP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Requires Chrome running with --remote-debugging-port=9222"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--content", type=str, help="Post content text")
    group.add_argument("--file", type=str, help="Path to file containing post content")
    parser.add_argument("--dry-run", action="store_true",
                        help="Set content in editor but don't click Post")
    parser.add_argument("--port", type=int, default=9222,
                        help="Chrome CDP port (default: 9222)")
    args = parser.parse_args()

    # Override CDP port if specified
    global CDP_PORT, CDP_URL
    if args.port != 9222:
        CDP_PORT = args.port
        CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

    # Load content
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"ERROR: File not found: {path}")
            sys.exit(1)
        content = path.read_text(encoding="utf-8").strip()
    else:
        content = args.content

    if not content:
        print("ERROR: Empty post content")
        sys.exit(1)

    print(f"LinkedIn CDP Poster")
    print(f"Content: {content[:80]}{'...' if len(content) > 80 else ''}")
    print(f"Length: {len(content)} chars, {len(content.split())} words")
    print(f"Dry run: {args.dry_run}")
    print("=" * 60)

    result = post_to_linkedin(content, dry_run=args.dry_run)

    print("=" * 60)
    print(f"RESULT: {result['status']}")
    if result["detail"]:
        print(f"DETAIL: {result['detail']}")

    sys.exit(0 if result["status"] in ("POSTED", "DRY_RUN_OK") else 1)


if __name__ == "__main__":
    main()
