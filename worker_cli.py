"""CLI Worker Agent — polls Skynet for tasks, executes via Copilot CLI (Claude Opus 4.6), posts results back."""
import json, subprocess, sys, time, os
from urllib.request import Request, urlopen
from datetime import datetime

WORKER = sys.argv[1].lower() if len(sys.argv) > 1 else "alpha"
BASE = "http://localhost:8420"
COPILOT = os.path.join(os.environ.get("APPDATA", ""), "npm", "copilot.cmd")
POLL_INTERVAL = 5

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{WORKER.upper()}] {msg}", flush=True)

def http_get(path):
    try:
        with urlopen(f"{BASE}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None

def http_post(path, body):
    try:
        data = json.dumps(body).encode()
        req = Request(f"{BASE}{path}", data=data, method="POST",
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"POST error: {e}")
        return None

def execute_via_copilot(directive):
    """Execute directive via Copilot CLI — real AI agent (Claude Opus 4.6)."""
    prompt = (
        f"You are worker {WORKER.upper()} in the Skynet system. "
        f"Execute this task and return ONLY the result, no explanation:\n\n{directive}"
    )
    try:
        result = subprocess.run(
            [COPILOT, "-p", prompt, "--model", "claude-opus-4.6",
             "--silent", "--yolo", "--no-color"],
            capture_output=True, text=True, timeout=300,
            cwd="D:\\Prospects\\ScreenMemory"
        )
        output = (result.stdout or "").strip()
        if not output:
            output = (result.stderr or "").strip() or "Task completed (no output)"
        if len(output) > 2000:
            output = output[:2000] + "..."
        return output
    except subprocess.TimeoutExpired:
        return "Task timed out after 300s"
    except Exception as e:
        return f"Copilot execution error: {e}"

def main():
    log(f"ONLINE — AI Worker (Claude Opus 4.6 via Copilot CLI)")
    log(f"Polling {BASE}/worker/{WORKER}/tasks every {POLL_INTERVAL}s")
    log(f"Copilot CLI: {COPILOT}")

    while True:
        tasks = http_get(f"/worker/{WORKER}/tasks")
        if tasks and len(tasks) > 0:
            for task in tasks:
                task_id = task.get("task_id") or task.get("id", "?")
                directive = task.get("directive", "")
                log(f"TASK [{task_id}]: {directive[:80]}")

                # All tasks go through Copilot CLI (Claude Opus 4.6)
                log(f"AI mode (Copilot CLI)")
                result = execute_via_copilot(directive)

                log(f"DONE [{task_id}]: {result[:120]}")

                resp = http_post(f"/worker/{WORKER}/result", {
                    "task_id": task_id,
                    "result": result,
                    "status": "completed"
                })
                if resp:
                    log(f"POSTED [{task_id}]")
                else:
                    log(f"FAILED to post [{task_id}]")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
