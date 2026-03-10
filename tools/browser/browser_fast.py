"""Fast browser control via Chrome DevTools Protocol (CDP) WebSocket on port 9222.
Antigravity launches Chrome with --remote-debugging-port=9222.
This bypasses the slow MCP HTTP+SSE layer for direct, instant interaction.

All input is VIRTUAL — CDP dispatches events directly into the browser engine.
Your real mouse/keyboard are never touched. You can work simultaneously.

System screenshots (non-browser) use mss for dual-monitor support.
"""
import asyncio, json, base64, os, time
try:
    import websockets
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

import requests

CDP_PORT = 9222
_msg_id = 0

# --- System-level utilities (dual monitor aware) ---

def system_screenshot(filepath=None, monitor=0):
    """Capture system screenshot via mss (works across dual monitors).
    monitor=0: all monitors combined, 1: primary, 2: secondary, etc.
    """
    import mss
    with mss.mss() as sct:
        img = sct.grab(sct.monitors[monitor])
        if filepath:
            from PIL import Image
            Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX").save(filepath)
            return filepath
        return img

def monitor_info():
    """Return info about all monitors."""
    import mss
    with mss.mss() as sct:
        return sct.monitors  # [0]=virtual, [1]=primary, [2]=secondary...

def _next_id():
    global _msg_id
    _msg_id += 1
    return _msg_id

# --- Sync helpers (for quick calls) ---

def list_pages():
    """List all open browser tabs."""
    r = requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5)
    return [p for p in r.json() if p.get("type") == "page"]

def find_page(url_contains):
    """Find first page whose URL contains the given string."""
    for p in list_pages():
        if url_contains.lower() in p.get("url", "").lower():
            return p
    return None

# --- Async CDP core ---

async def _send(ws, method, params=None):
    mid = _next_id()
    msg = {"id": mid, "method": method}
    if params:
        msg["params"] = params
    await ws.send(json.dumps(msg))
    while True:
        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if r.get("id") == mid:
            return r
        # Skip events

async def connect(ws_url):
    """Connect to a page's CDP WebSocket."""
    return await websockets.connect(ws_url, max_size=50*1024*1024)

async def evaluate(ws, expression):
    """Run JS and return the result value."""
    r = await _send(ws, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True
    })
    return r.get("result", {}).get("result", {}).get("value")

async def navigate(ws, url):
    """Navigate to a URL."""
    r = await _send(ws, "Page.navigate", {"url": url})
    await asyncio.sleep(2)
    return r

async def screenshot(ws, filepath=None, quality=80):
    """Take a screenshot. Returns base64 data or saves to file."""
    r = await _send(ws, "Page.captureScreenshot", {
        "format": "jpeg", "quality": quality
    })
    data = r.get("result", {}).get("data", "")
    if filepath and data:
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(data))
        return filepath
    return data

async def mouse_click(ws, x, y, button="left", click_count=1):
    """Click at (x, y) with real mouse events."""
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y
    })
    await asyncio.sleep(0.05)
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": button, "clickCount": click_count
    })
    await asyncio.sleep(0.05)
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": button, "clickCount": click_count
    })

async def mouse_move(ws, x, y):
    """Move mouse to (x, y)."""
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y
    })

async def type_text(ws, text):
    """Type text character by character via keyboard events."""
    for char in text:
        await _send(ws, "Input.dispatchKeyEvent", {
            "type": "keyDown", "text": char, "key": char,
            "unmodifiedText": char
        })
        await _send(ws, "Input.dispatchKeyEvent", {
            "type": "keyUp", "key": char
        })

async def insert_text(ws, text):
    """Insert text instantly via IME (fast, like paste)."""
    await _send(ws, "Input.insertText", {"text": text})

async def press_key(ws, key, modifiers=0):
    """Press a special key (Enter, Tab, Backspace, etc).
    modifiers: 1=Alt, 2=Ctrl, 4=Meta, 8=Shift
    """
    key_map = {
        "Enter": (13, "\r"), "Tab": (9, ""), "Backspace": (8, ""),
        "Escape": (27, ""), "ArrowUp": (38, ""), "ArrowDown": (40, ""),
        "ArrowLeft": (37, ""), "ArrowRight": (39, ""),
        "Delete": (46, ""), "Home": (36, ""), "End": (35, ""),
    }
    code, text = key_map.get(key, (0, ""))
    params = {
        "type": "rawKeyDown", "key": key,
        "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code,
        "modifiers": modifiers
    }
    if text:
        params["text"] = text
    await _send(ws, "Input.dispatchKeyEvent", params)
    params["type"] = "keyUp"
    await _send(ws, "Input.dispatchKeyEvent", params)

async def select_all(ws):
    """Ctrl+A."""
    await press_key(ws, "a", modifiers=2)

async def set_file_input(ws, selector, filepaths):
    """Set files on a file input element (for uploads).
    filepaths: list of absolute file paths.
    """
    if isinstance(filepaths, str):
        filepaths = [filepaths]
    # Get the node for the file input
    doc = await _send(ws, "DOM.getDocument", {"depth": 0})
    root_id = doc["result"]["root"]["nodeId"]
    node = await _send(ws, "DOM.querySelector", {
        "nodeId": root_id, "selector": selector
    })
    node_id = node["result"]["nodeId"]
    await _send(ws, "DOM.setFileInputFiles", {
        "nodeId": node_id, "files": filepaths
    })

async def intercept_file_chooser(ws, filepaths):
    """Enable file chooser interception, click something that opens it,
    then this will auto-fill the file(s). Call BEFORE triggering the chooser.
    """
    if isinstance(filepaths, str):
        filepaths = [filepaths]
    await _send(ws, "Page.setInterceptFileChooserDialog", {"enabled": True})
    return filepaths  # Store for handle_file_chooser

async def handle_file_chooser_event(ws, filepaths):
    """After file chooser opens, accept it with the given files."""
    # Listen for the event and handle it
    await _send(ws, "Page.handleFileChooser", {
        "action": "accept", "files": filepaths
    })

async def scroll(ws, x, y, delta_x=0, delta_y=0):
    """Scroll at position."""
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": x, "y": y,
        "deltaX": delta_x, "deltaY": delta_y
    })

async def get_element_center(ws, selector):
    """Get center coordinates of an element by CSS selector."""
    expr = f"""(() => {{
        const el = document.querySelector('{selector}');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {{x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height}};
    }})()"""
    return await evaluate(ws, expr)

async def click_element(ws, selector):
    """Find element by selector and click its center."""
    pos = await get_element_center(ws, selector)
    if pos:
        await mouse_click(ws, pos["x"], pos["y"])
        return True
    return False

async def click_text(ws, text, tag="*"):
    """Click element containing specific text."""
    expr = f"""(() => {{
        const els = document.querySelectorAll('{tag}');
        for (const el of els) {{
            if (el.innerText.trim() === '{text}' && el.offsetParent !== null) {{
                const r = el.getBoundingClientRect();
                return {{x: r.left + r.width/2, y: r.top + r.height/2}};
            }}
        }}
        return null;
    }})()"""
    pos = await evaluate(ws, expr)
    if pos:
        await mouse_click(ws, pos["x"], pos["y"])
        return True
    return False

# --- High-level convenience (sync wrappers) ---

def run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)

def quick_eval(ws_url, expression):
    """One-shot: connect, evaluate, disconnect."""
    async def _go():
        ws = await connect(ws_url)
        try:
            return await evaluate(ws, expression)
        finally:
            await ws.close()
    return run(_go())

def quick_screenshot(ws_url, filepath):
    """One-shot: connect, screenshot, disconnect."""
    async def _go():
        ws = await connect(ws_url)
        try:
            return await screenshot(ws, filepath)
        finally:
            await ws.close()
    return run(_go())


if __name__ == "__main__":
    pages = list_pages()
    for p in pages:
        print(f"{p['title'][:60]:60s} {p['url'][:80]}")
