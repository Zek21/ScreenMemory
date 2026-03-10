"""Chrome Bridge smoke tests.

Usage:
    python test_bridge.py              # real-agent smoke test
    python test_bridge.py --synthetic  # hub/client protocol self-test
"""

import argparse
import asyncio
import json
import threading
import time

import websockets

from bridge import Hub


async def _fake_agent(stop_flag):
    async with websockets.connect("ws://127.0.0.1:7777") as ws:
        await ws.send(json.dumps({
            "type": "register",
            "profileId": "test-agent-001",
            "email": "test@example.com",
            "tabs": [{"id": 11, "title": "Synthetic Tab", "url": "https://example.com", "active": True}],
            "windowCount": 1,
            "version": "synthetic",
            "transport": "fake-agent",
        }))

        while not stop_flag.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            msg = json.loads(raw)
            if msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong", "timestamp": msg.get("timestamp")}))
                continue

            cmd = msg.get("command")
            if cmd == "tabs.list":
                await ws.send(json.dumps({
                    "id": msg["id"],
                    "result": [{"id": 11, "title": "Synthetic Tab", "url": "https://example.com", "active": True}],
                }))
            elif cmd == "bridge.capabilities":
                await ws.send(json.dumps({
                    "id": msg["id"],
                    "result": {"version": "synthetic", "transport": "fake-agent", "chromeMinimum": 116},
                }))
            elif cmd == "bridge.status":
                await ws.send(json.dumps({
                    "id": msg["id"],
                    "result": {"connected": True, "profileId": "test-agent-001", "transport": "fake-agent"},
                }))
            elif cmd == "leak.__error":
                await ws.send(json.dumps({"id": msg["id"], "result": {"__error": "synthetic content failure"}}))
            elif cmd == "leak.error":
                await ws.send(json.dumps({"id": msg["id"], "result": {"error": "synthetic bare failure"}}))
            elif cmd == "leak.rich":
                await ws.send(json.dumps({
                    "id": msg["id"],
                    "result": {"ok": False, "error": "rich failure", "screenshot": "abc"},
                }))
            else:
                await ws.send(json.dumps({"id": msg["id"], "error": f"unknown command: {cmd}"}))


def _start_fake_agent():
    stop_flag = threading.Event()

    def runner():
        asyncio.run(_fake_agent(stop_flag))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    time.sleep(0.75)
    return stop_flag, thread


def run_synthetic_test():
    stop_flag, thread = _start_fake_agent()
    try:
        with Hub(retry=1) as hub:
            profiles = hub.wait_for_agent(timeout=5)
            chrome = hub.chrome("test-agent-001")

            print("Synthetic agent connected:")
            print(profiles)
            print("tabs.list:", chrome.tabs())
            print("bridge.capabilities:", chrome.capabilities())
            print("bridge.status:", chrome.status())

            try:
                chrome._cmd("leak.__error")
            except Exception as exc:
                print("leak.__error raised:", type(exc).__name__, str(exc))

            try:
                chrome._cmd("leak.error")
            except Exception as exc:
                print("leak.error raised:", type(exc).__name__, str(exc))

            print("leak.rich:", chrome._cmd("leak.rich"))
    finally:
        stop_flag.set()
        thread.join(timeout=2)


async def run_real_agent_test():
    async with websockets.connect("ws://127.0.0.1:7777") as ws:
        await ws.send(json.dumps({"type": "client"}))

        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        data = json.loads(msg)
        agents = data.get("agents", [])
        print(f"Agents connected: {len(agents)}")

        for agent in agents:
            pid = agent.get("profileId", "?")
            tabs = len(agent.get("tabs", []))
            version = agent.get("version", "?")
            transport = agent.get("transport", "?")
            print(f"  profile={pid[:16]}... tabs={tabs} version={version} transport={transport}")

        if not agents:
            print("No agents connected.")
            print("Load the unpacked extension from chrome-bridge/extension and keep Chrome running.")
            print("For a browser-independent protocol test, run: python test_bridge.py --synthetic")
            return

        target = agents[0]["profileId"]
        await ws.send(json.dumps({"id": 1, "command": "bridge.status", "target": target}))
        status_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        print("bridge.status:", status_resp.get("result") or status_resp.get("error"))

        await ws.send(json.dumps({"id": 2, "command": "tabs.list", "target": target}))
        tabs_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if "result" not in tabs_resp:
            print("tabs.list failed:", tabs_resp.get("error", "unknown"))
            return

        tabs = tabs_resp["result"]
        print(f"tabs.list returned {len(tabs)} tab(s)")
        for tab in tabs[:8]:
            print(f"  [{tab['id']:>4}] {tab.get('title', '')[:55]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic", action="store_true", help="Run a hub/client protocol self-test with a fake agent")
    args = parser.parse_args()

    if args.synthetic:
        run_synthetic_test()
    else:
        asyncio.run(run_real_agent_test())


if __name__ == "__main__":
    main()
