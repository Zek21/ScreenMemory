"""
Chrome Bridge v3.1 — Hub Server
The definitive WebSocket hub with command logging, health monitoring,
agent reconnect support, batch routing, event streaming, and performance tracking.

Usage:
    python server.py              # Start on default port 7777
    python server.py --port 8888  # Custom port
    python server.py --host 0.0.0.0 --port 7777  # LAN / remote hub binding
"""
import asyncio
import json
import sys
import time
import traceback
from collections import defaultdict, deque

try:
    import websockets
    from websockets import Response
    from websockets.datastructures import Headers
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets
    from websockets import Response
    from websockets.datastructures import Headers

PORT = 7777
HOST = "127.0.0.1"

# Connected agents (Chrome extensions) keyed by profileId
agents = {}  # profileId -> {"ws": ws, "info": {...}}

# Connected clients (Python scripts)
clients = set()

# Event subscriptions: client ws -> set of event types
client_events = defaultdict(set)

# Pending requests: id -> {"client_ws": ws, "future": asyncio.Future}
pending = {}
_next_id = 0

# Command log (ring buffer)
command_log = deque(maxlen=500)

# Metrics
metrics = {
    "started_at": time.time(),
    "commands_routed": 0,
    "commands_failed": 0,
    "batches_routed": 0,
    "total_agents_seen": 0,
    "total_clients_seen": 0,
    "avg_latency_ms": 0,
    "peak_latency_ms": 0,
    "agent_reconnects": 0,
}
latency_samples = []


async def process_request(connection, request):
    if request.path != "/healthz":
        return None

    body = json.dumps({
        "status": "ok",
        "version": "3.1.0",
        "uptime": round(time.time() - metrics["started_at"], 3),
        "agents": len(agents),
        "clients": len(clients),
        "host": connection.local_address[0] if connection.local_address else None,
        "port": connection.local_address[1] if connection.local_address else None,
    }).encode("utf-8")
    headers = Headers({
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
        "Content-Length": str(len(body)),
    })
    return Response(200, "OK", headers, body)


def next_id():
    global _next_id
    _next_id += 1
    return _next_id


def record_latency(ms):
    latency_samples.append(ms)
    if len(latency_samples) > 100:
        latency_samples.pop(0)
    metrics["avg_latency_ms"] = round(sum(latency_samples) / len(latency_samples), 1)
    if ms > metrics["peak_latency_ms"]:
        metrics["peak_latency_ms"] = round(ms, 1)


def log_command(command, target, success, latency_ms=None):
    """Log command to ring buffer for diagnostics."""
    command_log.append({
        "time": time.time(),
        "command": command,
        "target": (target or "")[:16],
        "success": success,
        "latency_ms": round(latency_ms, 1) if latency_ms else None,
    })


async def handle_connection(ws):
    """Handle incoming WebSocket connection (could be agent or client)."""
    conn_type = None
    profile_id = None

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ── Heartbeat ping/pong ──
            if msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong", "timestamp": msg.get("timestamp")}))
                continue

            # ── Agent registration ──
            if msg.get("type") == "register":
                conn_type = "agent"
                profile_id = msg.get("profileId", "unknown")
                is_new = profile_id not in agents
                was_reconnect = not is_new and agents[profile_id]["ws"] != ws
                agents[profile_id] = {
                    "ws": ws,
                    "info": msg,
                    "connected_at": time.time()
                }
                if is_new:
                    metrics["total_agents_seen"] += 1
                if was_reconnect:
                    metrics["agent_reconnects"] += 1
                version = msg.get("version", "1.x")
                status = "reconnected" if was_reconnect else "registered"
                print(f"[+] Agent {status}: {profile_id[:16]}... "
                      f"(v{version}, {len(msg.get('tabs', []))} tabs)")
                await broadcast_agents()
                continue

            # ── Agent tab update ──
            if conn_type == "agent" and "tabs" in msg:
                if profile_id and profile_id in agents:
                    agents[profile_id]["info"] = msg
                    await broadcast_agents()
                continue

            # ── Agent event → forward to subscribed clients ──
            if conn_type == "agent" and msg.get("type") == "event":
                event_type = msg.get("event", "")
                for client_ws, subscribed in list(client_events.items()):
                    if event_type in subscribed or "*" in subscribed:
                        try:
                            await client_ws.send(json.dumps(msg))
                        except Exception:
                            client_events.pop(client_ws, None)
                continue

            # ── Client identification ──
            if msg.get("type") == "client":
                conn_type = "client"
                clients.add(ws)
                metrics["total_clients_seen"] += 1
                print(f"[+] Client connected (total: {len(clients)})")
                await ws.send(json.dumps({
                    "type": "agents",
                    "agents": get_agents_info()
                }))
                continue

            # ── Client metrics request ──
            if conn_type == "client" and msg.get("command") == "bridge.hub.metrics":
                await ws.send(json.dumps({
                    "id": msg.get("id", 0),
                    "result": {
                        **metrics,
                        "uptime": time.time() - metrics["started_at"],
                        "active_agents": len(agents),
                        "active_clients": len(clients),
                        "pending_commands": len(pending),
                    }
                }))
                continue

            # ── Hub command log ──
            if conn_type == "client" and msg.get("command") == "bridge.hub.log":
                limit = msg.get("params", {}).get("limit", 50)
                await ws.send(json.dumps({
                    "id": msg.get("id", 0),
                    "result": list(command_log)[-limit:]
                }))
                continue

            # ── Hub health check ──
            if conn_type == "client" and msg.get("command") == "bridge.hub.health":
                agent_health = {}
                for pid, a in agents.items():
                    agent_health[pid[:16]] = {
                        "connected": time.time() - a["connected_at"],
                        "tabs": len(a["info"].get("tabs", [])),
                        "version": a["info"].get("version", "?"),
                    }
                await ws.send(json.dumps({
                    "id": msg.get("id", 0),
                    "result": {
                        "status": "healthy",
                        "uptime": time.time() - metrics["started_at"],
                        "agents": agent_health,
                        "clients": len(clients),
                        "pending": len(pending),
                        "commands_total": metrics["commands_routed"],
                        "avg_latency": metrics["avg_latency_ms"],
                        "peak_latency": metrics["peak_latency_ms"],
                    }
                }))
                continue

            # ── Client event subscription ──
            if conn_type == "client" and msg.get("command") == "bridge.events.subscribe":
                types = msg.get("params", {}).get("events", [])
                if isinstance(types, str):
                    types = [types]
                client_events[ws].update(types)
                await ws.send(json.dumps({
                    "id": msg.get("id", 0),
                    "result": {"ok": True, "subscriptions": list(client_events[ws])}
                }))
                continue

            if conn_type == "client" and msg.get("command") == "bridge.events.unsubscribe":
                types = msg.get("params", {}).get("events", [])
                if isinstance(types, str):
                    types = [types]
                client_events[ws].difference_update(types)
                await ws.send(json.dumps({
                    "id": msg.get("id", 0),
                    "result": {"ok": True, "subscriptions": list(client_events[ws])}
                }))
                continue

            # ── Client batch command ──
            if conn_type == "client" and msg.get("type") == "batch":
                target = msg.get("target")
                agent = find_agent(target)
                if not agent:
                    await ws.send(json.dumps({
                        "id": msg.get("id", 0),
                        "error": "No agent connected" + (f" matching '{target}'" if target else "")
                    }))
                    continue

                hub_id = next_id()
                pending[hub_id] = {
                    "client_ws": ws,
                    "client_id": msg.get("id", 0),
                    "sent_at": time.time(),
                    "is_batch": True,
                    "command": "batch",
                    "target": target,
                }
                metrics["batches_routed"] += 1

                try:
                    await agent["ws"].send(json.dumps({
                        "type": "batch", "id": hub_id,
                        "commands": msg.get("commands", [])
                    }))
                except Exception:
                    del pending[hub_id]
                    metrics["commands_failed"] += 1
                    log_command("batch", target, False)
                    await ws.send(json.dumps({"id": msg.get("id", 0), "error": "Agent disconnected"}))
                continue

            # ── Client command → route to agent ──
            if conn_type == "client" and "command" in msg:
                target = msg.get("target")
                cmd_id = msg.get("id", next_id())
                agent = find_agent(target)

                if not agent:
                    await ws.send(json.dumps({
                        "id": cmd_id,
                        "error": "No agent connected" + (f" matching '{target}'" if target else "")
                    }))
                    continue

                hub_id = next_id()
                forward = {
                    "id": hub_id,
                    "command": msg["command"],
                    "params": msg.get("params", {})
                }

                pending[hub_id] = {
                    "client_ws": ws,
                    "client_id": cmd_id,
                    "sent_at": time.time(),
                    "command": msg["command"],
                    "target": target,
                }

                try:
                    await agent["ws"].send(json.dumps(forward))
                    metrics["commands_routed"] += 1
                except Exception:
                    del pending[hub_id]
                    metrics["commands_failed"] += 1
                    log_command(msg["command"], target, False)
                    await ws.send(json.dumps({"id": cmd_id, "error": "Agent disconnected"}))
                continue

            # ── Agent response → route to client ──
            if conn_type == "agent" and "id" in msg and ("result" in msg or "error" in msg or "results" in msg):
                hub_id = msg["id"]
                if hub_id in pending:
                    p = pending.pop(hub_id)
                    elapsed_ms = (time.time() - p["sent_at"]) * 1000
                    record_latency(elapsed_ms)

                    response = {"id": p["client_id"]}
                    if "error" in msg:
                        response["error"] = msg["error"]
                        metrics["commands_failed"] += 1
                        log_command(p.get("command", "unknown"), p.get("target"), False, elapsed_ms)
                    elif p.get("is_batch"):
                        response["type"] = "batchResult"
                        response["results"] = msg.get("results", [])
                        log_command("batch", p.get("target"), True, elapsed_ms)
                    else:
                        response["result"] = msg["result"]
                        log_command(p.get("command", "unknown"), p.get("target"), True, elapsed_ms)

                    if msg.get("_ms"):
                        response["_ms"] = msg["_ms"]
                    response["_hub_ms"] = round(elapsed_ms, 1)

                    try:
                        await p["client_ws"].send(json.dumps(response))
                    except Exception:
                        pass
                continue

    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[!] Error: {e}")
        traceback.print_exc()
    finally:
        if conn_type == "agent" and profile_id:
            if profile_id in agents and agents[profile_id]["ws"] == ws:
                del agents[profile_id]
                print(f"[-] Agent disconnected: {profile_id[:16]}...")
                await broadcast_agents()
        elif conn_type == "client":
            clients.discard(ws)
            client_events.pop(ws, None)
            print(f"[-] Client disconnected (total: {len(clients)})")
            to_remove = [k for k, v in pending.items() if v["client_ws"] == ws]
            for k in to_remove:
                del pending[k]


def find_agent(target):
    """Find agent by profileId prefix or email substring."""
    if target:
        for pid, a in agents.items():
            if pid.startswith(target) or target in (a["info"].get("email", "")):
                return a
    elif agents:
        return next(iter(agents.values()))
    return None


def get_agents_info():
    """Get serializable agent info for clients."""
    result = []
    for pid, a in agents.items():
        info = a["info"]
        result.append({
            "profileId": pid,
            "email": info.get("email", ""),
            "tabs": info.get("tabs", []),
            "windowCount": info.get("windowCount", 0),
            "version": info.get("version", "1.x"),
            "connectedAt": a["connected_at"]
        })
    return result


async def broadcast_agents():
    """Send updated agent list to all clients."""
    msg = json.dumps({"type": "agents", "agents": get_agents_info()})
    for c in list(clients):
        try:
            await c.send(msg)
        except Exception:
            clients.discard(c)


async def cleanup_stale():
    """Periodically clean up stale pending requests."""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        stale = [k for k, v in pending.items() if now - v["sent_at"] > 60]
        for k in stale:
            p = pending.pop(k)
            metrics["commands_failed"] += 1
            try:
                await p["client_ws"].send(json.dumps({
                    "id": p["client_id"],
                    "error": "Request timed out"
                }))
            except Exception:
                pass


async def print_stats():
    """Print periodic stats."""
    while True:
        await asyncio.sleep(60)
        uptime = time.time() - metrics["started_at"]
        h, m = divmod(int(uptime), 3600)
        mins = m // 60
        print(f"[stats] uptime={h}h{mins}m agents={len(agents)} clients={len(clients)} "
              f"cmds={metrics['commands_routed']} batches={metrics['batches_routed']} "
              f"fails={metrics['commands_failed']} avg_lat={metrics['avg_latency_ms']}ms")


async def main():
    port = PORT
    host = HOST
    if "--host" in sys.argv:
        host = sys.argv[sys.argv.index("--host") + 1]
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    print(f"Chrome Bridge Hub v3.1 starting on ws://{host}:{port}")
    print(f"Waiting for agents (Chrome extensions) and clients (Python scripts)...")
    print(f"Features: command logging, health checks, reconnect support, compression")
    print()

    async with websockets.serve(
        handle_connection, host, port,
        max_size=50 * 1024 * 1024,
        ping_interval=30,
        ping_timeout=10,
        compression="deflate",
        process_request=process_request
    ):
        asyncio.create_task(cleanup_stale())
        asyncio.create_task(print_stats())
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nHub stopped.")
