import os
import glob
import ctypes
import sys
import json
import time
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

DATA_DIR = ROOT / "data"

# Critical files to NEVER delete
CRITICAL_FILES = {
    "workers.json", "orchestrator.json", "brain_config.json", 
    "agent_profiles.json", "worker_scores.json", "todos.json", 
    "incidents.json"
}

def _is_alive(pid: int) -> bool:
    """Check if a process is still running using ctypes (Windows)."""
    try:
        PROCESS_QUERY_LIMITED = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == STILL_ACTIVE
    except Exception:
        return False

def clean_pids():
    print(f"Scanning PID files in {DATA_DIR}...")
    pid_files = list(DATA_DIR.glob("*.pid"))
    cleaned = []
    alive = []
    
    for p_file in pid_files:
        if p_file.name in CRITICAL_FILES:
            continue
            
        try:
            content = p_file.read_text().strip()
            if not content:
                print(f"Empty PID file: {p_file.name} -> Removing")
                p_file.unlink()
                cleaned.append(p_file.name)
                continue
                
            pid = int(content)
            if _is_alive(pid):
                alive.append(f"{p_file.name} ({pid})")
            else:
                print(f"Dead PID file: {p_file.name} ({pid}) -> Removing")
                p_file.unlink()
                cleaned.append(p_file.name)
        except Exception as e:
            print(f"Error checking {p_file.name}: {e}")
            
    return cleaned, alive

def clean_temps():
    print(f"Scanning temp files in {DATA_DIR}...")
    cleaned = []
    
    # data/.dispatch_tmp_*
    for tmp in DATA_DIR.glob(".dispatch_tmp_*"):
        if tmp.name not in CRITICAL_FILES:
            try:
                tmp.unlink()
                cleaned.append(tmp.name)
            except Exception as e:
                print(f"Error removing {tmp.name}: {e}")

    # data/.gamma_dispatch.py
    gamma = DATA_DIR / ".gamma_dispatch.py"
    if gamma.exists() and gamma.name not in CRITICAL_FILES:
        try:
            gamma.unlink()
            cleaned.append(gamma.name)
        except Exception as e:
            print(f"Error removing {gamma.name}: {e}")
            
    return cleaned

def main():
    cleaned_pids, alive_pids = clean_pids()
    cleaned_temps = clean_temps()
    
    print("\n--- Summary ---")
    print(f"Alive PIDs: {len(alive_pids)}")
    for p in alive_pids:
        print(f"  {p}")
        
    print(f"Removed PIDs: {len(cleaned_pids)}")
    for p in cleaned_pids:
        print(f"  {p}")
        
    print(f"Removed Temps: {len(cleaned_temps)}")
    for t in cleaned_temps:
        print(f"  {t}")
        
    # Post result
    try:
        from tools.skynet_spam_guard import guarded_publish
        
        msg = f"WAVE1: [cleaned] Removed {len(cleaned_pids)} dead PIDs, {len(cleaned_temps)} temp files. Alive: {len(alive_pids)}"
        
        guarded_publish({
            'sender': 'beta',
            'topic': 'orchestrator',
            'type': 'result',
            'content': msg
        })
        print("\nResult posted to bus.")
    except Exception as e:
        print(f"\nFailed to post to bus: {e}")

if __name__ == "__main__":
    main()
