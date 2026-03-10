"""
Agent Worker — Visible CLI dashboard for multi-agent operations.
Each instance runs in its own terminal window showing live status.

Usage: python agent_worker.py --role alpha --color cyan
"""
import os
import sys
import time
import json
import threading
import argparse
from datetime import datetime
from pathlib import Path

# Agent role configurations
ROLES = {
    "alpha": {
        "name": "ALPHA",
        "title": "Research & Intelligence",
        "color": "\033[96m",  # Cyan
        "cwd": r"D:\Prospects\ScreenMemory",
        "tasks": ["Web research", "News fetch", "Data gathering", "Topic analysis"],
    },
    "beta": {
        "name": "BETA",
        "title": "Code & Build",
        "color": "\033[93m",  # Yellow
        "cwd": r"D:\Prospects\ScreenMemory",
        "tasks": ["Module building", "Code generation", "File editing", "Refactoring"],
    },
    "gamma": {
        "name": "GAMMA",
        "title": "Test & Deploy",
        "color": "\033[95m",  # Magenta
        "cwd": r"D:\Prospects\ScreenMemory",
        "tasks": ["Test runner", "Blog deploy", "WordPress publish", "Cache flush"],
    },
    "delta": {
        "name": "DELTA",
        "title": "Monitor & Guardian",
        "color": "\033[91m",  # Red
        "cwd": r"D:\Prospects\ScreenMemory",
        "tasks": ["Process guardian", "Orphan detection", "Health checks", "Log watch"],
    },
}

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[92m"
WHITE = "\033[97m"

# Shared command queue file for receiving tasks from orchestrator
QUEUE_DIR = Path(r"D:\Prospects\ScreenMemory\data\agent_queues")


class AgentWorker:
    def __init__(self, role_id: str):
        self.role_id = role_id
        self.config = ROLES[role_id]
        self.color = self.config["color"]
        self.name = self.config["name"]
        self.status = "IDLE"
        self.current_task = None
        self.tasks_completed = 0
        self.start_time = time.time()
        self.log_lines = []
        self.max_log_lines = 25
        self._running = True
        self._new_lines = []  # buffer for dashboard

        # Create queue directory
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        self.queue_file = QUEUE_DIR / f"{role_id}_queue.json"
        self.result_file = QUEUE_DIR / f"{role_id}_result.json"

        # Clear old queue
        if self.queue_file.exists():
            self.queue_file.unlink()

        os.chdir(self.config["cwd"])

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        # Strip ANSI codes for dashboard feed
        clean = message
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', clean)
        entry = f"{DIM}[{timestamp}]{RESET} {message}"
        self.log_lines.append(entry)
        self._new_lines.append(clean)
        if len(self.log_lines) > self.max_log_lines:
            self.log_lines = self.log_lines[-self.max_log_lines:]

    def render(self):
        """Render compact dashboard — fits in quarter-screen split panes."""
        c = self.color
        uptime = int(time.time() - self.start_time)
        mins, secs = divmod(uptime, 60)
        status_color = GREEN if self.status == "IDLE" else "\033[93m" if self.status == "WORKING" else "\033[91m"
        cur = f"{c}{self.current_task[:40]}{RESET}" if self.current_task else f"{DIM}—{RESET}"
        caps = " │ ".join(self.config["tasks"])

        lines = []
        lines.append(f"{c}{'═' * 55}{RESET}")
        lines.append(f"{c}{BOLD} ⬡ AGENT {self.name}{RESET} — {self.config['title']}  {status_color}{BOLD}{self.status:<8}{RESET} {DIM}{mins:02d}:{secs:02d}{RESET}  ✓{self.tasks_completed}")
        lines.append(f"{c}{'═' * 55}{RESET}")
        lines.append(f" {WHITE}Current:{RESET} {cur}")
        lines.append(f" {DIM}{caps}{RESET}")
        lines.append(f"{c}{'─' * 55}{RESET}")

        # Activity log — show as many lines as terminal fits
        log_show = self.log_lines[-20:] if self.log_lines else [f"  {DIM}Waiting for tasks...{RESET}"]
        for line in log_show:
            lines.append(f"  {line}")
        pad_needed = 20 - len(log_show)
        for _ in range(pad_needed):
            lines.append(f"{'':55}")

        lines.append(f"{c}{'─' * 55}{RESET}")
        queue_icon = "📥 LISTENING" if self._running else "⏹ STOPPED"
        lines.append(f" {WHITE}Queue:{RESET} {queue_icon}  {DIM}({self.queue_file.name}){RESET}")
        lines.append(f"{c}{'═' * 55}{RESET}")

        sys.stdout.write("\033[H")
        sys.stdout.write("\n".join(lines))
        sys.stdout.write("\033[J")
        sys.stdout.flush()

        # Write live status for dashboard
        self._write_live_status()

    def _write_live_status(self):
        """Write status JSON for dashboard server to pick up."""
        if not self._new_lines:
            return
        try:
            live_file = QUEUE_DIR / f"{self.role_id}_live.json"
            data = {
                "status": self.status,
                "tasks_completed": self.tasks_completed,
                "current_task": self.current_task,
                "new_lines": self._new_lines[:20],
            }
            with open(live_file, 'w') as f:
                json.dump(data, f)
            self._new_lines = []
        except OSError:
            pass

    def check_queue(self):
        """Check for incoming tasks from the orchestrator."""
        if not self.queue_file.exists():
            return None

        try:
            with open(self.queue_file, 'r') as f:
                task = json.load(f)
            # Remove the queue file after reading
            self.queue_file.unlink()
            return task
        except (json.JSONDecodeError, OSError):
            return None

    def execute_task(self, task: dict):
        """Execute a task — streams EVERY line of output live to the dashboard."""
        self.status = "WORKING"
        self.current_task = task.get("description", "Unknown task")
        self.log(f"{GREEN}▶ Task: {self.current_task}{RESET}")
        self.render()

        command = task.get("command", "")
        task_type = task.get("type", "shell")
        all_output = []

        result = {"status": "success", "output": "", "error": ""}

        try:
            if task_type == "shell" and command:
                self.log(f"  {WHITE}$ {command[:48]}{RESET}")
                self.render()

                import subprocess
                proc = subprocess.Popen(
                    command, shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                # Stream every line live
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        # Truncate long lines for display
                        display = line[:52] if len(line) > 52 else line
                        self.log(f"  {DIM}│{RESET} {display}")
                        all_output.append(line)
                        self.render()

                proc.wait(timeout=120)
                result["output"] = "\n".join(all_output[-50:])
                result["returncode"] = proc.returncode

                if proc.returncode == 0:
                    self.log(f"  {GREEN}✓ Done (exit 0) — {len(all_output)} lines{RESET}")
                else:
                    self.log(f"  \033[91m✗ Exit code {proc.returncode}{RESET}")
                    result["status"] = "failed"
                self.render()

            elif task_type == "python" and command:
                self.log(f"  {WHITE}Running Python code...{RESET}")
                self.render()

                import subprocess
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-c", command],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        display = line[:52] if len(line) > 52 else line
                        self.log(f"  {DIM}│{RESET} {display}")
                        all_output.append(line)
                        self.render()

                proc.wait(timeout=120)
                result["output"] = "\n".join(all_output[-50:])
                result["returncode"] = proc.returncode

                if proc.returncode == 0:
                    self.log(f"  {GREEN}✓ Python done — {len(all_output)} lines{RESET}")
                else:
                    self.log(f"  \033[91m✗ Python exit {proc.returncode}{RESET}")
                    result["status"] = "failed"
                self.render()

            elif task_type == "message":
                self.log(f"  📨 {command[:50]}")
                self.render()

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            self.log(f"  \033[91m✗ Error: {str(e)[:50]}{RESET}")
            self.render()

        # Write result
        try:
            with open(self.result_file, 'w') as f:
                json.dump(result, f)
        except OSError:
            pass

        self.tasks_completed += 1
        self.status = "IDLE"
        self.current_task = None

    def run(self):
        """Main event loop — render dashboard and poll for tasks."""
        # Set terminal title, clear once, hide cursor
        if os.name == 'nt':
            os.system(f'title Agent {self.name} - {self.config["title"]}')
        os.system('cls' if os.name == 'nt' else 'clear')
        sys.stdout.write("\033[?25l")  # hide cursor
        sys.stdout.flush()

        self.log(f"{GREEN}Agent {self.name} online{RESET}")
        self.log(f"Listening on {self.queue_file.name}")

        try:
            while self._running:
                self.render()

                # Check for tasks
                task = self.check_queue()
                if task:
                    self.execute_task(task)
                else:
                    # Poll every 2 seconds
                    time.sleep(2)

        except KeyboardInterrupt:
            self.status = "SHUTDOWN"
            self.log("Shutdown signal received")
            self.render()
            sys.stdout.write("\033[?25h")  # restore cursor
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Agent Worker Dashboard")
    parser.add_argument("--role", choices=ROLES.keys(), required=True)
    args = parser.parse_args()

    worker = AgentWorker(args.role)
    worker.run()


if __name__ == "__main__":
    main()
