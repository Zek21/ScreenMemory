"""
SKYNET WORKER BOOT — EXACT PROVEN PROCEDURE
============================================
NOTICE: This file now redirects to the canonical boot script:
    tools/skynet_worker_boot.py (v3.0.0)

Both scripts use the same pyautogui-only proven procedure.
skynet_worker_boot.py is the authoritative version.

Usage:
    python tools/exact_boot.py --all --orch-hwnd 460966
    python tools/exact_boot.py --name alpha --orch-hwnd 460966
    python tools/exact_boot.py --close-all
"""

import sys
import os

# Redirect to canonical boot script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.skynet_worker_boot import main

if __name__ == "__main__":
    main()
