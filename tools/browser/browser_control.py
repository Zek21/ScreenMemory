"""
Browser control utility for Antigravity's Chrome via the Chrome DevTools MCP server.
The MCP server runs inside Antigravity's extension host at http://127.0.0.1:<port>/mcp.
"""
import json
import os
import time
import urllib.request
import base64


MCP_URL = "http://127.0.0.1:61103/mcp"
_msg_id = 0


def _next_id():
    global _msg_id
    _msg_id += 1
    return _msg_id


def mcp_call(method, params=None):
    """Send a JSON-RPC request to the Antigravity MCP server and return the result."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params or {}
    }).encode()
    req = urllib.request.Request(
        MCP_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read().decode()

    # Parse SSE response
    for line in content.split("\n"):
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            if "result" in data:
                return data["result"]
            if "error" in data:
                raise Exception(f'MCP error: {data["error"]}')
    return None


def call_tool(tool_name, arguments=None):
    """Call an MCP tool and return its result."""
    result = mcp_call("tools/call", {
        "name": tool_name,
        "arguments": arguments or {}
    })
    if result and "content" in result:
        parts = []
        for c in result["content"]:
            if c.get("type") == "text":
                parts.append(c["text"])
            elif c.get("type") == "image":
                parts.append(f'[image: {c.get("mimeType", "image/png")}]')
        return "\n".join(parts)
    return result


# Convenience wrappers

def list_pages():
    return call_tool("list_pages")

def select_page(index):
    return call_tool("select_page", {"index": index})

def navigate(url):
    return call_tool("navigate_page", {"url": url})

def snapshot():
    return call_tool("take_snapshot")

def screenshot(filename=None):
    if filename is None:
        filename = os.path.join("screenshots", "screenshot.png")
    os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
    result = mcp_call("tools/call", {
        "name": "take_screenshot",
        "arguments": {}
    })
    if result and "content" in result:
        for c in result["content"]:
            if c.get("type") == "image":
                with open(filename, "wb") as f:
                    f.write(base64.b64decode(c["data"]))
                print(f"Screenshot saved: {filename}")
                return filename
            elif c.get("type") == "text":
                print(c["text"])
    return None

def click(element):
    return call_tool("click", {"element": element})

def fill(element, value):
    return call_tool("fill", {"element": element, "value": value})

def fill_form(fields):
    """fields: list of dicts with 'element' and 'value' keys"""
    return call_tool("fill_form", {"fields": fields})

def hover(element):
    return call_tool("hover", {"element": element})

def press_key(key):
    return call_tool("press_key", {"key": key})

def evaluate(expression):
    return call_tool("evaluate_script", {"expression": expression})

def wait_for(text, timeout=10):
    return call_tool("wait_for", {"text": text, "timeout": timeout})

def new_page(url=None):
    args = {}
    if url:
        args["url"] = url
    return call_tool("new_page", args)

def close_page(index):
    return call_tool("close_page", {"index": index})

def handle_dialog(accept=True, text=None):
    args = {"accept": accept}
    if text:
        args["text"] = text
    return call_tool("handle_dialog", args)


if __name__ == "__main__":
    print("=== Antigravity Browser Control (MCP) ===\n")

    print("Pages:")
    print(list_pages())

    print("\nTaking snapshot of current page...")
    print(snapshot()[:2000] if snapshot() else "No snapshot")

    print("\nBrowser control working!")
