"""
Chrome Bridge CDP — Command-Line Interface & HTTP API
Standalone EXE entry point for direct Chrome automation.

Usage:
    # Interactive REPL
    chrome-cdp

    # Single command
    chrome-cdp tabs
    chrome-cdp eval <tab_id> "document.title"
    chrome-cdp screenshot <tab_id> output.png
    chrome-cdp navigate <tab_id> https://example.com

    # Launch Chrome with debugging
    chrome-cdp launch --headless
    chrome-cdp launch --url https://example.com

    # HTTP API server
    chrome-cdp serve --port 8420

    # Script execution
    chrome-cdp run script.py
"""

import sys
import os
import json
import time
import base64
import argparse
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Allow running from source or as frozen EXE
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, CDPError, connect, launch


VERSION = '1.0.0'
BANNER = f"""
╔══════════════════════════════════════════════════╗
║  Chrome Bridge CDP v{VERSION}                        ║
║  Direct Chrome DevTools Protocol Controller      ║
║  Zero mouse interference • Faster than extension ║
╚══════════════════════════════════════════════════╝
"""


# ─── CLI Commands ────────────────────────────────────────────

def cmd_tabs(chrome, args):
    tabs = chrome.tabs()
    if not tabs:
        print('No tabs open.')
        return
    for t in tabs:
        active = '→' if t.get('active') else ' '
        print(f"  {active} [{t['id'][:8]}] {t.get('title', '?')[:60]}")
        print(f"             {t.get('url', '?')[:70]}")


def cmd_eval(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    result = chrome.eval(tab_id, args.expression, await_promise=args.await_promise)
    if isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result)


def cmd_navigate(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    chrome.navigate(tab_id, args.url, wait=not args.no_wait)
    print(f'Navigated to {args.url}')


def cmd_screenshot(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    data = chrome.screenshot(tab_id, full_page=args.full, file_path=args.output)
    if not args.output:
        out = os.path.join('screenshots', f'screenshot-{int(time.time())}.png')
        os.makedirs('screenshots', exist_ok=True)
        with open(out, 'wb') as f:
            f.write(data)
        print(f'Saved: {out} ({len(data)} bytes)')
    else:
        print(f'Saved: {args.output} ({len(data)} bytes)')


def cmd_pdf(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    out = args.output or f'page-{int(time.time())}.pdf'
    data = chrome.pdf(tab_id, file_path=out)
    print(f'Saved: {out} ({len(data)} bytes)')


def cmd_click(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    if args.selector:
        chrome.click_selector(tab_id, args.selector)
        print(f'Clicked: {args.selector}')
    else:
        chrome.click(tab_id, args.x, args.y)
        print(f'Clicked at ({args.x}, {args.y})')


def cmd_type(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    chrome.type_text(tab_id, args.text)
    print(f'Typed: {args.text[:50]}')


def cmd_info(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    info = chrome.get_page_info(tab_id)
    print(json.dumps(info, indent=2, default=str))


def cmd_text(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    text = chrome.get_text(tab_id, args.selector)
    print(text)


def cmd_links(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    links = chrome.extract_links(tab_id, args.filter)
    for l in links:
        print(f"  {l.get('text', '?')[:40]:40s} → {l.get('href', '')[:60]}")
    print(f'\n  Total: {len(links)} links')


def cmd_cookies(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    cookies = chrome.get_cookies(tab_id)
    for c in cookies:
        print(f"  {c['name']:30s} = {c['value'][:40]}")
    print(f'\n  Total: {len(cookies)} cookies')


def cmd_perf(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    metrics = chrome.performance_metrics(tab_id)
    for k, v in sorted(metrics.items()):
        print(f'  {k:40s} {v}')


def cmd_emulate(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    chrome.emulate_device(tab_id, args.device)
    print(f'Emulating: {args.device}')


def cmd_version(chrome, args):
    info = chrome.version()
    for k, v in info.items():
        print(f'  {k}: {v}')


def cmd_new(chrome, args):
    tab = chrome.new_tab(args.url)
    print(f"Created tab: {tab['id'][:8]}")


def cmd_close(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    chrome.close_tab(tab_id)
    print('Tab closed.')


def cmd_raw(chrome, args):
    tab_id = resolve_tab(chrome, args.tab)
    params = json.loads(args.params) if args.params else None
    result = chrome.raw(tab_id, args.method, params)
    print(json.dumps(result, indent=2, default=str))


def resolve_tab(chrome, tab_ref):
    """Resolve tab reference to full tab ID."""
    if not tab_ref or tab_ref == 'active':
        tabs = chrome.tabs()
        if not tabs:
            raise CDPError('No tabs available')
        return tabs[0]['id']
    tabs = chrome.tabs()
    for t in tabs:
        if t['id'].startswith(tab_ref) or tab_ref in t.get('title', '') or tab_ref in t.get('url', ''):
            return t['id']
    return tab_ref


# ─── Interactive REPL ────────────────────────────────────────

def repl(chrome):
    print(BANNER)
    print('  Connected! Type commands or "help" for usage.\n')
    tab_ctx = None  # current tab context

    while True:
        try:
            prompt = f'cdp:{tab_ctx[:8] if tab_ctx else "?"}> ' if tab_ctx else 'cdp> '
            line = input(prompt).strip()
            if not line:
                continue
            if line in ('exit', 'quit', 'q'):
                break
            if line == 'help':
                print_repl_help()
                continue

            parts = line.split(None, 1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ''

            if cmd == 'tabs':
                tabs = chrome.tabs()
                for t in tabs:
                    marker = '→' if t['id'] == tab_ctx else ' '
                    print(f"  {marker} [{t['id'][:8]}] {t.get('title', '?')[:50]}")
                    print(f"             {t.get('url', '?')[:65]}")

            elif cmd == 'use':
                tab_ctx = resolve_tab(chrome, rest or 'active')
                tabs = chrome.tabs()
                for t in tabs:
                    if t['id'] == tab_ctx:
                        print(f"  Using: [{tab_ctx[:8]}] {t.get('title', '?')[:50]}")
                        break

            elif cmd in ('eval', 'js'):
                tid = tab_ctx or resolve_tab(chrome, 'active')
                result = chrome.eval(tid, rest)
                if isinstance(result, (dict, list)):
                    print(json.dumps(result, indent=2, default=str))
                else:
                    print(result)

            elif cmd in ('nav', 'go', 'navigate'):
                tid = tab_ctx or resolve_tab(chrome, 'active')
                chrome.navigate(tid, rest)
                print(f'  → {rest}')

            elif cmd in ('shot', 'screenshot'):
                tid = tab_ctx or resolve_tab(chrome, 'active')
                if rest:
                    fname = rest
                else:
                    fname = os.path.join('screenshots', f'shot-{int(time.time())}.png')
                    os.makedirs('screenshots', exist_ok=True)
                data = chrome.screenshot(tid, file_path=fname)
                print(f'  Saved: {fname} ({len(data)} bytes)')

            elif cmd == 'pdf':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                fname = rest or f'page-{int(time.time())}.pdf'
                data = chrome.pdf(tid, file_path=fname)
                print(f'  Saved: {fname} ({len(data)} bytes)')

            elif cmd == 'click':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                if rest.startswith('#') or rest.startswith('.') or rest.startswith('['):
                    chrome.click_selector(tid, rest)
                else:
                    coords = rest.split()
                    chrome.click(tid, float(coords[0]), float(coords[1]))
                print('  Clicked.')

            elif cmd == 'type':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                chrome.type_text(tid, rest)
                print(f'  Typed: {rest[:40]}')

            elif cmd == 'key':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                chrome.press_key(tid, rest)

            elif cmd == 'text':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                sel = rest if rest else None
                print(chrome.get_text(tid, sel))

            elif cmd == 'info':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                print(json.dumps(chrome.get_page_info(tid), indent=2, default=str))

            elif cmd == 'links':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                links = chrome.extract_links(tid, rest if rest else None)
                for l in links[:20]:
                    print(f"  {l.get('text','')[:35]:35s} → {l.get('href','')[:55]}")
                print(f'  Total: {len(links)}')

            elif cmd == 'cookies':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                cookies = chrome.get_cookies(tid)
                for c in cookies:
                    print(f"  {c['name']:25s} = {c['value'][:35]}")

            elif cmd == 'meta':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                meta = chrome.extract_meta(tid)
                for m in meta:
                    if m.get('content'):
                        print(f"  {m.get('name',''):25s} = {m['content'][:50]}")

            elif cmd == 'new':
                t = chrome.new_tab(rest or 'about:blank')
                tab_ctx = t['id']
                print(f"  New tab: [{tab_ctx[:8]}]")

            elif cmd == 'close':
                if tab_ctx:
                    chrome.close_tab(tab_ctx)
                    tab_ctx = None
                    print('  Tab closed.')

            elif cmd == 'perf':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                m = chrome.performance_metrics(tid)
                for k, v in sorted(m.items()):
                    if v > 0:
                        print(f'  {k:35s} {v:.2f}')

            elif cmd == 'emulate':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                chrome.emulate_device(tid, rest)
                print(f'  Emulating: {rest}')

            elif cmd == 'version':
                v = chrome.version()
                for k, val in v.items():
                    print(f'  {k}: {val}')

            elif cmd == 'scroll':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                amount = int(rest) if rest else -300
                chrome.scroll(tid, delta_y=amount)

            elif cmd == 'fill':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                # format: fill selector=value selector2=value2
                pairs = {}
                for pair in rest.split():
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        pairs[k] = v
                chrome.fill_form(tid, pairs)
                print(f'  Filled {len(pairs)} fields.')

            elif cmd == 'raw':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                rparts = rest.split(None, 1)
                method = rparts[0]
                params = json.loads(rparts[1]) if len(rparts) > 1 else None
                r = chrome.raw(tid, method, params)
                print(json.dumps(r, indent=2, default=str))

            elif cmd == 'wait':
                time.sleep(float(rest) if rest else 1)

            elif cmd == 'html':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                sel = rest if rest else 'html'
                print(chrome.outer_html(tid, sel))

            elif cmd == 'title':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                print(chrome.eval(tid, 'document.title'))

            elif cmd == 'url':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                print(chrome.get_url(tid))

            elif cmd == 'block':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                urls = rest.split()
                chrome.block_urls(tid, urls)
                print(f'  Blocked {len(urls)} URL patterns.')

            elif cmd == 'ua':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                chrome.set_user_agent(tid, rest)
                print(f'  UA set.')

            elif cmd == 'dark':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                chrome.dark_mode(tid, rest != 'off')
                print(f'  Dark mode: {"on" if rest != "off" else "off"}')

            elif cmd == 'storage':
                tid = tab_ctx or resolve_tab(chrome, 'active')
                s = chrome.get_local_storage(tid)
                for k, v in (s or {}).items():
                    print(f'  {k}: {str(v)[:60]}')

            else:
                print(f'  Unknown command: {cmd}. Type "help" for usage.')

        except CDPError as e:
            print(f'  Error: {e}')
        except KeyboardInterrupt:
            break
        except EOFError:
            break
        except Exception as e:
            print(f'  Error: {e}')

    print('Bye!')


def print_repl_help():
    print("""
  Navigation:
    tabs                      List all tabs
    use <tab>                 Switch to tab (id prefix, title, or url match)
    nav <url>                 Navigate to URL
    new [url]                 Open new tab
    close                     Close current tab

  Content:
    eval <js>                 Execute JavaScript
    text [selector]           Get text content
    html [selector]           Get HTML
    title                     Get page title
    url                       Get current URL
    info                      Get page info
    links [filter]            Extract links
    meta                      Extract meta tags
    cookies                   List cookies
    storage                   List localStorage

  Input (NO mouse interference):
    click <selector|x y>      Click element or coordinates
    type <text>               Type text
    key <key>                 Press key (Enter, Tab, Escape, etc.)
    scroll <amount>           Scroll (negative=down, positive=up)
    fill sel=val sel2=val2    Fill form fields

  Visual:
    shot [filename]           Take screenshot
    pdf [filename]            Generate PDF

  Emulation:
    emulate <device>          Emulate device (iphone 12, pixel 7, etc.)
    dark [on|off]             Toggle dark mode
    ua <user-agent>           Set user agent
    block <url patterns>      Block URLs

  Debug:
    raw <method> [params]     Send raw CDP command
    perf                      Performance metrics
    version                   Chrome version info

  General:
    wait <seconds>            Wait
    help                      This help
    exit                      Quit
""")


# ─── HTTP API Server ─────────────────────────────────────────

class CDPAPIHandler(BaseHTTPRequestHandler):
    chrome = None

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        parsed = urlparse(self.path)
        path = parsed.path.strip('/')
        params = parse_qs(parsed.query)

        # Read POST body
        body = {}
        if self.command == 'POST':
            length = int(self.headers.get('Content-Length', 0))
            if length:
                body = json.loads(self.rfile.read(length))

        try:
            result = self._dispatch(path, params, body)
            self._respond(200, result)
        except CDPError as e:
            self._respond(400, {'error': str(e)})
        except Exception as e:
            self._respond(500, {'error': str(e)})

    def _dispatch(self, path, params, body):
        chrome = self.chrome

        if path == 'tabs':
            return chrome.tabs()
        elif path == 'version':
            return chrome.version()
        elif path == 'eval':
            tab = body.get('tab') or params.get('tab', ['active'])[0]
            expr = body.get('expression') or params.get('expression', [''])[0]
            tab_id = resolve_tab(chrome, tab)
            return {'result': chrome.eval(tab_id, expr)}
        elif path == 'navigate':
            tab = body.get('tab') or params.get('tab', ['active'])[0]
            url = body.get('url') or params.get('url', [''])[0]
            tab_id = resolve_tab(chrome, tab)
            chrome.navigate(tab_id, url)
            return {'ok': True}
        elif path == 'screenshot':
            tab = body.get('tab') or params.get('tab', ['active'])[0]
            tab_id = resolve_tab(chrome, tab)
            data = chrome.screenshot(tab_id)
            return {'data': base64.b64encode(data).decode()}
        elif path == 'click':
            tab = body.get('tab') or params.get('tab', ['active'])[0]
            tab_id = resolve_tab(chrome, tab)
            if 'selector' in body:
                chrome.click_selector(tab_id, body['selector'])
            else:
                chrome.click(tab_id, body.get('x', 0), body.get('y', 0))
            return {'ok': True}
        elif path == 'type':
            tab = body.get('tab') or params.get('tab', ['active'])[0]
            tab_id = resolve_tab(chrome, tab)
            chrome.type_text(tab_id, body.get('text', ''))
            return {'ok': True}
        elif path == 'info':
            tab = body.get('tab') or params.get('tab', ['active'])[0]
            tab_id = resolve_tab(chrome, tab)
            return chrome.get_page_info(tab_id)
        elif path == 'new':
            url = body.get('url') or params.get('url', ['about:blank'])[0]
            return chrome.new_tab(url)
        elif path == 'close':
            tab = body.get('tab') or params.get('tab', [''])[0]
            tab_id = resolve_tab(chrome, tab)
            chrome.close_tab(tab_id)
            return {'ok': True}
        elif path == 'raw':
            tab = body.get('tab') or params.get('tab', ['active'])[0]
            tab_id = resolve_tab(chrome, tab)
            return chrome.raw(tab_id, body.get('method', ''), body.get('params'))
        elif path == 'healthz':
            return {'status': 'ok', 'version': VERSION}
        else:
            return {'error': f'Unknown endpoint: {path}'}

    def _respond(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress default logging


def serve(chrome, port=8420):
    CDPAPIHandler.chrome = chrome
    server = HTTPServer(('127.0.0.1', port), CDPAPIHandler)
    print(f'CDP API server on http://127.0.0.1:{port}')
    print(f'Endpoints: /tabs /eval /navigate /screenshot /click /type /info /new /close /raw /healthz')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# ─── Main Entry Point ───────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='chrome-cdp',
        description='Chrome Bridge CDP — Direct Chrome DevTools Protocol Controller'
    )
    parser.add_argument('--port', '-p', type=int, default=None,
                        help='Chrome debug port (auto-detect if not specified)')
    parser.add_argument('--version', '-V', action='version', version=f'chrome-cdp {VERSION}')

    sub = parser.add_subparsers(dest='command')

    # launch
    p_launch = sub.add_parser('launch', help='Launch Chrome with debugging')
    p_launch.add_argument('--url', default=None)
    p_launch.add_argument('--headless', action='store_true')
    p_launch.add_argument('--debug-port', type=int, default=9222)
    p_launch.add_argument('--chrome-path', default=None)

    # serve
    p_serve = sub.add_parser('serve', help='Start HTTP API server')
    p_serve.add_argument('--api-port', type=int, default=8420)

    # tabs
    sub.add_parser('tabs', help='List all tabs')

    # eval
    p_eval = sub.add_parser('eval', help='Execute JavaScript')
    p_eval.add_argument('tab', nargs='?', default='active')
    p_eval.add_argument('expression')
    p_eval.add_argument('--await', dest='await_promise', action='store_true')

    # navigate
    p_nav = sub.add_parser('navigate', help='Navigate to URL')
    p_nav.add_argument('tab', nargs='?', default='active')
    p_nav.add_argument('url')
    p_nav.add_argument('--no-wait', action='store_true')

    # screenshot
    p_shot = sub.add_parser('screenshot', help='Take screenshot')
    p_shot.add_argument('tab', nargs='?', default='active')
    p_shot.add_argument('--output', '-o', default=None)
    p_shot.add_argument('--full', action='store_true')

    # pdf
    p_pdf = sub.add_parser('pdf', help='Generate PDF')
    p_pdf.add_argument('tab', nargs='?', default='active')
    p_pdf.add_argument('--output', '-o', default=None)

    # click
    p_click = sub.add_parser('click', help='Click element')
    p_click.add_argument('tab', nargs='?', default='active')
    p_click.add_argument('--selector', '-s', default=None)
    p_click.add_argument('--x', type=float, default=0)
    p_click.add_argument('--y', type=float, default=0)

    # type
    p_type = sub.add_parser('type', help='Type text')
    p_type.add_argument('tab', nargs='?', default='active')
    p_type.add_argument('text')

    # info
    p_info = sub.add_parser('info', help='Get page info')
    p_info.add_argument('tab', nargs='?', default='active')

    # text
    p_text = sub.add_parser('text', help='Get page text')
    p_text.add_argument('tab', nargs='?', default='active')
    p_text.add_argument('--selector', '-s', default=None)

    # links
    p_links = sub.add_parser('links', help='Extract links')
    p_links.add_argument('tab', nargs='?', default='active')
    p_links.add_argument('--filter', default=None)

    # cookies
    p_cookies = sub.add_parser('cookies', help='List cookies')
    p_cookies.add_argument('tab', nargs='?', default='active')

    # perf
    p_perf = sub.add_parser('perf', help='Performance metrics')
    p_perf.add_argument('tab', nargs='?', default='active')

    # emulate
    p_emu = sub.add_parser('emulate', help='Emulate device')
    p_emu.add_argument('tab', nargs='?', default='active')
    p_emu.add_argument('device')

    # version
    sub.add_parser('version', help='Chrome version')

    # new
    p_new = sub.add_parser('new', help='Open new tab')
    p_new.add_argument('url', nargs='?', default='about:blank')

    # close
    p_close = sub.add_parser('close', help='Close tab')
    p_close.add_argument('tab', nargs='?', default='active')

    # raw
    p_raw = sub.add_parser('raw', help='Send raw CDP command')
    p_raw.add_argument('tab', nargs='?', default='active')
    p_raw.add_argument('method')
    p_raw.add_argument('--params', default=None)

    # run
    p_run = sub.add_parser('run', help='Run a script')
    p_run.add_argument('script')

    args = parser.parse_args()

    # Connect to Chrome
    chrome = None
    try:
        if args.command == 'launch':
            chrome = CDP.launch(
                chrome_path=args.chrome_path,
                port=args.debug_port,
                headless=args.headless,
            )
            if args.url:
                tabs = chrome.tabs()
                if tabs:
                    chrome.navigate(tabs[0]['id'], args.url)
            print(f'Chrome launched on port {args.debug_port}')
            # Drop into REPL after launch
            repl(chrome)
            return

        # Auto-connect
        if args.port:
            chrome = CDP(port=args.port)
        else:
            chrome = CDP.attach()

        if args.command == 'serve':
            serve(chrome, args.api_port)
        elif args.command == 'tabs':
            cmd_tabs(chrome, args)
        elif args.command == 'eval':
            cmd_eval(chrome, args)
        elif args.command == 'navigate':
            cmd_navigate(chrome, args)
        elif args.command == 'screenshot':
            cmd_screenshot(chrome, args)
        elif args.command == 'pdf':
            cmd_pdf(chrome, args)
        elif args.command == 'click':
            cmd_click(chrome, args)
        elif args.command == 'type':
            cmd_type(chrome, args)
        elif args.command == 'info':
            cmd_info(chrome, args)
        elif args.command == 'text':
            cmd_text(chrome, args)
        elif args.command == 'links':
            cmd_links(chrome, args)
        elif args.command == 'cookies':
            cmd_cookies(chrome, args)
        elif args.command == 'perf':
            cmd_perf(chrome, args)
        elif args.command == 'emulate':
            cmd_emulate(chrome, args)
        elif args.command == 'version':
            cmd_version(chrome, args)
        elif args.command == 'new':
            cmd_new(chrome, args)
        elif args.command == 'close':
            cmd_close(chrome, args)
        elif args.command == 'raw':
            cmd_raw(chrome, args)
        elif args.command == 'run':
            with open(args.script) as f:
                code = f.read()
            exec(code, {'chrome': chrome, 'CDP': CDP, '__name__': '__main__'})
        else:
            # No command = interactive REPL
            repl(chrome)

    except CDPError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        if chrome:
            chrome.close()


if __name__ == '__main__':
    main()
