"""
Skynet Secrets Loader — centralized credential management.

Loads API keys and secrets from (in priority order):
  1. Environment variables (highest priority)
  2. data/secrets.json (gitignored, local-only)
  3. Returns None if neither source has the key

Usage:
    from tools.skynet_secrets import get_secret
    key = get_secret("OPENAI_API_KEY")

CLI:
    python tools/skynet_secrets.py --list          # Show available keys (masked)
    python tools/skynet_secrets.py --check KEY     # Check if a specific key exists

# signed: beta
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = ROOT / "data" / "secrets.json"

# Well-known secret key names
KNOWN_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "SMTP_PASSWORD",
]

_secrets_cache: dict = None  # type: ignore


def _load_secrets_file() -> dict:
    """Load secrets from data/secrets.json. Returns empty dict on failure."""
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache
    if not SECRETS_FILE.exists():
        _secrets_cache = {}
        return _secrets_cache
    try:
        _secrets_cache = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        if not isinstance(_secrets_cache, dict):
            _secrets_cache = {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[SECRETS] Warning: could not read {SECRETS_FILE}: {e}", file=sys.stderr)
        _secrets_cache = {}
    return _secrets_cache


def get_secret(key: str, default: str = None) -> str:
    """Get a secret value. Env vars take priority over secrets.json.

    Args:
        key: Secret name (e.g. 'OPENAI_API_KEY')
        default: Fallback if not found anywhere

    Returns:
        The secret value, or default if not found.
    """
    # Priority 1: environment variable
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    # Priority 2: secrets.json
    secrets = _load_secrets_file()
    file_val = secrets.get(key)
    if file_val:
        return file_val
    return default


def has_secret(key: str) -> bool:
    """Check if a secret is available from any source."""
    return get_secret(key) is not None


def reload():
    """Force reload of secrets.json (clears cache)."""
    global _secrets_cache
    _secrets_cache = None


def _mask(value: str) -> str:
    """Mask a secret value for display. Shows first 4 and last 2 chars."""
    if not value or len(value) < 8:
        return "****"
    return value[:4] + "..." + value[-2:]


def main():
    """CLI interface for secrets management."""
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Secrets Loader")
    parser.add_argument("--list", action="store_true", help="List available keys (masked)")
    parser.add_argument("--check", type=str, help="Check if a specific key exists")
    args = parser.parse_args()

    if args.check:
        val = get_secret(args.check)
        if val:
            source = "env" if os.environ.get(args.check) else "secrets.json"
            print(f"[OK] {args.check} = {_mask(val)} (source: {source})")
        else:
            print(f"[MISSING] {args.check} not found in env or secrets.json")
        return

    if args.list:
        print("Skynet Secrets Status:")
        print(f"  secrets.json: {'EXISTS' if SECRETS_FILE.exists() else 'NOT FOUND'}")
        print()
        for key in KNOWN_KEYS:
            val = get_secret(key)
            if val:
                source = "env" if os.environ.get(key) else "file"
                print(f"  [OK]      {key:30s} = {_mask(val):15s}  ({source})")
            else:
                print(f"  [MISSING] {key}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
