"""
Centralized credential management for ScreenMemory operational tools.

All AWS, Cloudflare, SMTP, and other service credentials MUST be loaded
through this module — never hardcoded in script files.

Usage:
    from tools.credentials import aws_session, cf_headers, smtp_creds

    # AWS (boto3 session with credentials from env vars)
    session = aws_session(region='us-west-2')
    ses = session.client('sesv2')

    # Cloudflare
    headers = cf_headers()
    requests.get(url, headers=headers)

    # SMTP
    user, password = smtp_creds('mail@exzilcalanza.info')

Environment Variables (set in .env or system env):
    AWS_ACCESS_KEY_ID       — AWS access key
    AWS_SECRET_ACCESS_KEY   — AWS secret key
    AWS_DEFAULT_REGION      — Default AWS region (fallback: us-east-1)
    CF_API_KEY              — Cloudflare API key
    CF_EMAIL                — Cloudflare account email
    CF_ZONE_ID              — Cloudflare zone ID
    R53_ZONE_ID             — Route 53 hosted zone ID
    SMTP_PASSWORD           — SMTP password for email accounts
    SES_DOMAIN              — SES verified domain (default: exzilcalanza.info)
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Try loading .env file if python-dotenv is available
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
try:
    from dotenv import load_dotenv
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE)
        logger.info("Loaded credentials from .env file")
except ImportError:
    pass


def _require(var_name: str, fallback: str = None) -> str:
    """Get env var, raise if missing and no fallback."""
    val = os.environ.get(var_name, fallback)
    if not val:
        raise EnvironmentError(
            f"Missing required environment variable: {var_name}\n"
            f"Set it in your environment or in {_ENV_FILE}"
        )
    return val


def _get(var_name: str, fallback: str = "") -> str:
    """Get env var with optional fallback (no error)."""
    return os.environ.get(var_name, fallback)


# ── AWS ─────────────────────────────────────────────────────────────────────

def aws_session(region: str = None):
    """Create a boto3 Session with credentials from environment variables."""
    import boto3
    return boto3.Session(
        aws_access_key_id=_require("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_require("AWS_SECRET_ACCESS_KEY"),
        region_name=region or _get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def aws_client(service: str, region: str = None):
    """Shorthand: create a boto3 client for a given AWS service."""
    return aws_session(region).client(service, region_name=region)


def r53_zone_id() -> str:
    """Get Route 53 hosted zone ID from environment."""
    return _require("R53_ZONE_ID")


def ses_domain() -> str:
    """Get SES verified domain."""
    return _get("SES_DOMAIN", "exzilcalanza.info")


# ── Cloudflare ──────────────────────────────────────────────────────────────

def cf_headers() -> dict:
    """Get Cloudflare API request headers."""
    return {
        "X-Auth-Email": _require("CF_EMAIL"),
        "X-Auth-Key": _require("CF_API_KEY"),
        "Content-Type": "application/json",
    }


def cf_zone_id() -> str:
    """Get Cloudflare zone ID."""
    return _require("CF_ZONE_ID")


# ── SMTP ────────────────────────────────────────────────────────────────────

def smtp_creds(username: str = None) -> tuple:
    """Get SMTP credentials. Returns (username, password)."""
    user = username or _get("SMTP_USERNAME", "mail@exzilcalanza.info")
    password = _require("SMTP_PASSWORD")
    return user, password


# ── Validation ──────────────────────────────────────────────────────────────

def check_all() -> dict:
    """Check which credentials are configured. Returns status dict."""
    checks = {
        "AWS_ACCESS_KEY_ID": bool(os.environ.get("AWS_ACCESS_KEY_ID")),
        "AWS_SECRET_ACCESS_KEY": bool(os.environ.get("AWS_SECRET_ACCESS_KEY")),
        "CF_API_KEY": bool(os.environ.get("CF_API_KEY")),
        "CF_EMAIL": bool(os.environ.get("CF_EMAIL")),
        "R53_ZONE_ID": bool(os.environ.get("R53_ZONE_ID")),
        "CF_ZONE_ID": bool(os.environ.get("CF_ZONE_ID")),
        "SMTP_PASSWORD": bool(os.environ.get("SMTP_PASSWORD")),
        "SES_DOMAIN": bool(os.environ.get("SES_DOMAIN")),
    }
    return checks


if __name__ == "__main__":
    print("Credential Configuration Status:")
    print("=" * 45)
    status = check_all()
    for var, configured in status.items():
        icon = "✅" if configured else "❌"
        print(f"  {icon} {var}")

    missing = [k for k, v in status.items() if not v]
    if missing:
        print(f"\n⚠️  {len(missing)} credential(s) not configured.")
        print(f"   Set them in environment or create: {_ENV_FILE}")
    else:
        print("\n✅ All credentials configured.")
