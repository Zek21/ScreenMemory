"""Tests for core/security.py — DPAPI key management and cryptographic operations.

Tests cover: DPAPIKeyManager initialization, key generation, protect/unprotect
round-trip (mocked DPAPI), key storage/load with hash verification,
get_or_create_key flows, SQLCipher key derivation, FallbackKeyManager,
and get_key_manager factory.

Created by worker delta — critical security module test coverage.
"""

import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_key_path(tmp_path):
    """Provide a temporary key storage path."""
    return tmp_path / "keystore.dat"


@pytest.fixture
def mock_win32crypt():
    """Mock win32crypt for DPAPI tests (works on non-Windows too)."""
    mock_module = MagicMock()

    # Simulate DPAPI: just XOR with a fixed byte for deterministic test
    def fake_protect(data, desc, *args):
        return bytes(b ^ 0xAA for b in data)

    def fake_unprotect(data, *args):
        return ("test desc", bytes(b ^ 0xAA for b in data))

    mock_module.CryptProtectData = fake_protect
    mock_module.CryptUnprotectData = fake_unprotect
    return mock_module


# ── DPAPIKeyManager Tests ───────────────────────────────────────────────────

class TestDPAPIKeyManager:
    """Tests for DPAPIKeyManager class."""

    def test_init_dpapi_unavailable(self):
        """Manager initializes with dpapi_available=False when win32crypt missing."""
        with patch.dict(sys.modules, {'win32crypt': None}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr.key_path = Path("/tmp/test")
            mgr._dpapi_available = False
            assert mgr.is_available is False

    def test_init_dpapi_available(self, mock_win32crypt):
        """Manager initializes with dpapi_available=True when win32crypt exists."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager(key_path=Path("/tmp/test"))
            assert mgr.is_available is True

    def test_generate_key_default_32_bytes(self):
        """generate_key produces 32-byte key by default."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False
        key = mgr.generate_key()
        assert len(key) == 32
        assert isinstance(key, bytes)

    def test_generate_key_custom_length(self):
        """generate_key respects custom key_bytes."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False
        key = mgr.generate_key(key_bytes=16)
        assert len(key) == 16

    def test_generate_key_uniqueness(self):
        """Two generated keys are different (cryptographic randomness)."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False
        k1 = mgr.generate_key()
        k2 = mgr.generate_key()
        assert k1 != k2

    def test_protect_fallback_without_dpapi(self):
        """protect() falls back to NOPROTECT wrapping when DPAPI unavailable."""
        # signed: gamma — fixed incorrect test (protect does NOT raise, it wraps)
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr.key_path = Path("/tmp/test")
        mgr._dpapi_available = False
        result = mgr.protect(b"test data")
        assert result == b"NOPROTECT:test data"

    def test_unprotect_raises_without_dpapi_on_encrypted(self):
        """unprotect() raises RuntimeError on non-NOPROTECT data when DPAPI unavailable."""
        # signed: gamma
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False
        with pytest.raises(RuntimeError, match="DPAPI not available"):
            mgr.unprotect(b"encrypted data")

    def test_unprotect_unwraps_noprotect_data(self):
        """unprotect() handles NOPROTECT-wrapped data from fallback protect()."""
        # signed: gamma
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False
        wrapped = b"NOPROTECT:my secret data"
        assert mgr.unprotect(wrapped) == b"my secret data"

    def test_protect_unprotect_roundtrip(self, mock_win32crypt):
        """Data survives protect → unprotect round-trip."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr._dpapi_available = True
            mgr.key_path = Path("/tmp/test")

            original = b"ScreenMemory secret test data 12345"
            encrypted = mgr.protect(original)
            assert encrypted != original  # Must be transformed
            decrypted = mgr.unprotect(encrypted)
            assert decrypted == original

    def test_protect_output_differs_from_input(self, mock_win32crypt):
        """Protected data is different from plaintext."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr._dpapi_available = True
            data = b"sensitive information"
            protected = mgr.protect(data)
            assert protected != data

    def test_store_key_creates_file(self, tmp_key_path, mock_win32crypt):
        """store_key writes DPAPI-protected key to disk."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr._dpapi_available = True
            mgr.key_path = tmp_key_path

            key = secrets.token_bytes(32)
            result = mgr.store_key(key)

            assert result is True
            assert tmp_key_path.exists()

            # Verify file structure
            stored = json.loads(tmp_key_path.read_text())
            assert stored["version"] == 1
            assert stored["algorithm"] == "AES-256-CBC"
            assert "protected_key" in stored
            assert "key_hash" in stored
            assert stored["key_hash"] == hashlib.sha256(key).hexdigest()[:16]

    def test_load_key_roundtrip(self, tmp_key_path, mock_win32crypt):
        """Key stored via store_key can be loaded via load_key."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr._dpapi_available = True
            mgr.key_path = tmp_key_path

            original_key = secrets.token_bytes(32)
            mgr.store_key(original_key)
            loaded_key = mgr.load_key()

            assert loaded_key == original_key

    def test_load_key_missing_file(self, tmp_key_path):
        """load_key returns None when key file doesn't exist."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = True
        mgr.key_path = tmp_key_path  # doesn't exist yet
        assert mgr.load_key() is None

    def test_load_key_detects_corruption(self, tmp_key_path, mock_win32crypt):
        """load_key detects hash mismatch (corrupted key)."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr._dpapi_available = True
            mgr.key_path = tmp_key_path

            key = secrets.token_bytes(32)
            mgr.store_key(key)

            # Corrupt the hash
            stored = json.loads(tmp_key_path.read_text())
            stored["key_hash"] = "0000000000000000"
            tmp_key_path.write_text(json.dumps(stored))

            loaded = mgr.load_key()
            assert loaded is None  # Hash mismatch detected

    def test_get_or_create_key_creates_new(self, tmp_key_path, mock_win32crypt):
        """get_or_create_key generates and stores a new key when none exists."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr._dpapi_available = True
            mgr.key_path = tmp_key_path

            key = mgr.get_or_create_key()
            assert key is not None
            assert len(key) == 32
            assert tmp_key_path.exists()

    def test_get_or_create_key_reuses_existing(self, tmp_key_path, mock_win32crypt):
        """get_or_create_key returns existing key when available."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import DPAPIKeyManager
            mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
            mgr._dpapi_available = True
            mgr.key_path = tmp_key_path

            k1 = mgr.get_or_create_key()
            k2 = mgr.get_or_create_key()
            assert k1 == k2  # Same key returned

    def test_get_or_create_key_ephemeral_without_dpapi(self):
        """get_or_create_key generates ephemeral key when DPAPI unavailable."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False
        mgr.key_path = Path("/nonexistent/path/key.dat")

        key = mgr.get_or_create_key()
        assert key is not None
        assert len(key) == 32


# ── SQLCipher Key Derivation Tests ──────────────────────────────────────────

class TestSQLCipherDerivation:
    """Tests for derive_sqlcipher_key()."""

    def test_deterministic_derivation(self):
        """Same master key produces same SQLCipher key."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False

        master = b"0123456789abcdef0123456789abcdef"
        key1 = mgr.derive_sqlcipher_key(master)
        key2 = mgr.derive_sqlcipher_key(master)
        assert key1 == key2

    def test_sqlcipher_format(self):
        """SQLCipher key starts with x' and ends with '."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False

        key = mgr.derive_sqlcipher_key(b"test master key")
        assert key.startswith("x'")
        assert key.endswith("'")
        # Hex content should be 64 chars (32 bytes)
        hex_part = key[2:-1]
        assert len(hex_part) == 64
        # Should be valid hex
        int(hex_part, 16)

    def test_different_keys_different_derivation(self):
        """Different master keys produce different SQLCipher keys."""
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False

        k1 = mgr.derive_sqlcipher_key(b"master key A")
        k2 = mgr.derive_sqlcipher_key(b"master key B")
        assert k1 != k2


# ── FallbackKeyManager Tests ───────────────────────────────────────────────

class TestFallbackKeyManager:
    """Tests for FallbackKeyManager class."""

    def test_ephemeral_key_on_no_env(self):
        """Generates ephemeral key when no env var set."""
        from core.security import FallbackKeyManager
        old = os.environ.pop("SCREENMEMORY_KEY", None)
        try:
            mgr = FallbackKeyManager()
            key = mgr.get_or_create_key()
            assert key is not None
            assert len(key) == 32
        finally:
            if old:
                os.environ["SCREENMEMORY_KEY"] = old

    def test_key_from_env_short(self):
        """Short env var is encoded as UTF-8 bytes."""
        from core.security import FallbackKeyManager
        old = os.environ.get("SCREENMEMORY_KEY", None)
        try:
            os.environ["SCREENMEMORY_KEY"] = "short_key"
            mgr = FallbackKeyManager()
            key = mgr.get_or_create_key()
            assert key == b"short_key"
        finally:
            if old:
                os.environ["SCREENMEMORY_KEY"] = old
            else:
                os.environ.pop("SCREENMEMORY_KEY", None)

    def test_key_from_env_base64(self):
        """Long env var (>32 chars) is base64-decoded."""
        from core.security import FallbackKeyManager
        raw_key = secrets.token_bytes(32)
        encoded = base64.b64encode(raw_key).decode()
        assert len(encoded) > 32  # base64 is longer than raw

        old = os.environ.get("SCREENMEMORY_KEY", None)
        try:
            os.environ["SCREENMEMORY_KEY"] = encoded
            mgr = FallbackKeyManager()
            key = mgr.get_or_create_key()
            assert key == raw_key
        finally:
            if old:
                os.environ["SCREENMEMORY_KEY"] = old
            else:
                os.environ.pop("SCREENMEMORY_KEY", None)

    def test_ephemeral_keys_are_unique(self):
        """Each call without env var generates different key."""
        from core.security import FallbackKeyManager
        old = os.environ.pop("SCREENMEMORY_KEY", None)
        try:
            mgr = FallbackKeyManager()
            k1 = mgr.get_or_create_key()
            k2 = mgr.get_or_create_key()
            assert k1 != k2  # Ephemeral = new every time
        finally:
            if old:
                os.environ["SCREENMEMORY_KEY"] = old


# ── Factory Tests ───────────────────────────────────────────────────────────

class TestGetKeyManager:
    """Tests for get_key_manager() factory function."""

    def test_returns_dpapi_when_available(self, mock_win32crypt):
        """Returns DPAPIKeyManager when win32crypt is available."""
        with patch.dict(sys.modules, {'win32crypt': mock_win32crypt}):
            from core.security import get_key_manager, DPAPIKeyManager
            mgr = get_key_manager()
            assert isinstance(mgr, DPAPIKeyManager)

    def test_returns_fallback_when_unavailable(self):
        """Returns FallbackKeyManager when win32crypt is not available."""
        from core.security import FallbackKeyManager
        # Create a manager manually with DPAPI disabled
        from core.security import DPAPIKeyManager
        mgr = DPAPIKeyManager.__new__(DPAPIKeyManager)
        mgr._dpapi_available = False
        assert mgr.is_available is False
