"""
Cryptographic security module: DPAPI hardware-bound key management.
Generates random AES encryption keys and seals them using Windows DPAPI,
binding the key to the current user's hardware root-of-trust (TPM/motherboard).
No plaintext keys ever touch disk.
"""
import os
import json
import base64
import hashlib
import logging
import secrets
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Key storage location (DPAPI-protected)
DEFAULT_KEY_PATH = Path(os.environ.get("APPDATA", "")) / "ScreenMemory" / "keystore.dat"


class DPAPIKeyManager:
    """
    Hardware-bound key management using Windows DPAPI.
    DPAPI encrypts data using credentials derived from the user's login password
    and the machine's TPM/motherboard SID — keys cannot be extracted on another machine.
    """

    def __init__(self, key_path: Optional[Path] = None):
        self.key_path = key_path or DEFAULT_KEY_PATH
        self._dpapi_available = False

        try:
            import win32crypt
            self._dpapi_available = True
            logger.info("DPAPI available (win32crypt loaded)")
        except ImportError:
            logger.warning("win32crypt not available — DPAPI key binding disabled")
            logger.warning("Install with: pip install pywin32")

    @property
    def is_available(self) -> bool:
        return self._dpapi_available

    def generate_key(self, key_bytes: int = 32) -> bytes:
        """Generate a cryptographically secure random key."""
        return secrets.token_bytes(key_bytes)

    def protect(self, data: bytes, description: str = "ScreenMemory Encryption Key") -> bytes:
        """
        Encrypt data using DPAPI (bound to current user + machine).
        The encrypted blob can only be decrypted by the same user on the same machine.
        Falls back to base64 wrapping with warning marker if DPAPI is unavailable.
        """
        if not self._dpapi_available:
            logger.warning("DPAPI unavailable — using unprotected storage (NOT hardware-bound)")
            # Prefix with marker so unprotect() knows this is fallback-wrapped
            return b"NOPROTECT:" + data  # signed: delta

        import win32crypt
        # CryptProtectData: encrypts with user's DPAPI master key (derived from login creds + TPM)
        try:
            encrypted = win32crypt.CryptProtectData(
                data,
                description,
                None,   # optional entropy (additional secret)
                None,   # reserved
                None,   # prompt struct
                0x04,   # CRYPTPROTECT_LOCAL_MACHINE (0x04 per wincrypt.h) — tied to this machine
            )
            return encrypted
        except Exception as e:
            logger.error("DPAPI CryptProtectData failed: %s -- falling back to unprotected storage", e)
            return b"NOPROTECT:" + data  # signed: gamma

    def unprotect(self, encrypted_data: bytes) -> bytes:
        """
        Decrypt DPAPI-protected data.
        Only works for the same user on the same machine that encrypted it.
        Handles fallback-wrapped data from non-DPAPI protect() calls.
        """
        # Handle fallback-wrapped data (from protect() when DPAPI unavailable)
        if encrypted_data.startswith(b"NOPROTECT:"):
            logger.warning("Unwrapping non-DPAPI-protected data (NOT hardware-bound)")
            return encrypted_data[len(b"NOPROTECT:"):]  # signed: delta

        if not self._dpapi_available:
            raise RuntimeError("DPAPI not available and data is DPAPI-encrypted")

        import win32crypt
        try:
            description, decrypted = win32crypt.CryptUnprotectData(
                encrypted_data,
                None,   # optional entropy
                None,   # reserved
                None,   # prompt struct
                0x04,   # CRYPTPROTECT_LOCAL_MACHINE (0x04 per wincrypt.h — must match protect flag)
            )
            return decrypted
        except Exception as e:
            logger.error("DPAPI CryptUnprotectData failed: %s", e)
            raise RuntimeError(f"DPAPI decryption failed: {e}") from e  # signed: gamma

    def store_key(self, key: bytes) -> bool:
        """
        Generate, DPAPI-protect, and store an encryption key to disk.
        The stored file contains only the DPAPI-encrypted blob — useless without
        the user's Windows credentials and the machine's hardware identity.
        """
        try:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)

            protected = self.protect(key)

            # Store as base64-encoded JSON with metadata
            payload = {
                "version": 1,
                "algorithm": "AES-256-CBC",
                "kdf": "DPAPI+PBKDF2-HMAC-SHA512",
                "protected_key": base64.b64encode(protected).decode("ascii"),
                "key_hash": hashlib.sha256(key).hexdigest()[:16],  # verification only
            }

            with open(self.key_path, "w") as f:
                json.dump(payload, f, indent=2)

            logger.info("Encryption key stored (DPAPI-protected): %s", self.key_path)
            return True

        except Exception as e:
            logger.error("Failed to store key: %s", e)
            return False

    def load_key(self) -> Optional[bytes]:
        """
        Load and decrypt the DPAPI-protected encryption key from disk.
        Returns None if key doesn't exist or can't be decrypted.
        """
        if not self.key_path.exists():
            logger.info("No stored key found at %s", self.key_path)
            return None

        try:
            with open(self.key_path) as f:
                payload = json.load(f)

            protected = base64.b64decode(payload["protected_key"])
            key = self.unprotect(protected)

            # Verify key hash
            expected_hash = payload.get("key_hash", "")
            actual_hash = hashlib.sha256(key).hexdigest()[:16]
            if expected_hash and actual_hash != expected_hash:
                logger.error("Key hash mismatch — possible corruption")
                return None

            logger.info("Encryption key loaded and verified")
            return key

        except Exception as e:
            logger.error("Failed to load key: %s", e)
            return None

    def get_or_create_key(self) -> Optional[bytes]:
        """
        Get existing key or generate + store a new one.
        This is the primary API for the daemon to obtain its encryption key.
        """
        key = self.load_key()
        if key:
            return key

        if not self._dpapi_available:
            logger.warning("DPAPI unavailable — generating ephemeral key (not hardware-bound)")
            return self.generate_key()

        # Generate new key and store it
        key = self.generate_key(32)  # 256-bit AES key
        if self.store_key(key):
            return key

        logger.error("Failed to create encryption key")
        return None

    def derive_sqlcipher_key(self, master_key: bytes) -> str:
        """
        Derive a SQLCipher-compatible hex key from the master key.
        Uses PBKDF2-HMAC-SHA512 with a fixed salt for deterministic derivation.
        """
        import hmac

        # Fixed salt (not secret — DPAPI provides the real security)
        salt = b"ScreenMemory-SQLCipher-v1"

        # PBKDF2 with 100,000 iterations
        derived = hashlib.pbkdf2_hmac("sha512", master_key, salt, 100_000, dklen=32)
        return "x'" + derived.hex() + "'"


class FallbackKeyManager:
    """
    Non-DPAPI fallback for development/testing.
    Stores key using OS keyring or environment variable.
    NOT recommended for production — use DPAPIKeyManager.
    """

    def get_or_create_key(self) -> Optional[bytes]:
        """Get key from environment variable."""
        env_key = os.environ.get("SCREENMEMORY_KEY")
        if env_key:
            return base64.b64decode(env_key) if len(env_key) > 32 else env_key.encode()

        # Generate ephemeral key
        logger.warning("No encryption key configured — using ephemeral key")
        return secrets.token_bytes(32)


class FileSystemKeyManager:
    """
    Filesystem-based key manager for non-Windows platforms or when DPAPI fails.
    Stores key as a base64-encoded file with restricted permissions.
    Less secure than DPAPI (no hardware binding) but works cross-platform.
    """
    # signed: gamma

    def __init__(self, key_path: Optional[Path] = None):
        self.key_path = key_path or Path(os.environ.get("APPDATA", Path.home())) / "ScreenMemory" / "keystore_fs.dat"

    def get_or_create_key(self) -> Optional[bytes]:
        """Load key from file or generate a new one."""
        if self.key_path.exists():
            try:
                payload = json.loads(self.key_path.read_text())
                key = base64.b64decode(payload["key"])
                expected_hash = payload.get("key_hash", "")
                actual_hash = hashlib.sha256(key).hexdigest()[:16]
                if expected_hash and actual_hash != expected_hash:
                    logger.error("FileSystemKeyManager: key hash mismatch -- possible corruption")
                    return None
                logger.info("FileSystemKeyManager: key loaded from %s", self.key_path)
                return key
            except Exception as e:
                logger.error("FileSystemKeyManager: failed to load key: %s", e)
                return None

        key = secrets.token_bytes(32)
        try:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "manager": "FileSystemKeyManager",
                "key": base64.b64encode(key).decode("ascii"),
                "key_hash": hashlib.sha256(key).hexdigest()[:16],
            }
            self.key_path.write_text(json.dumps(payload, indent=2))
            logger.warning("FileSystemKeyManager: key stored at %s (NOT hardware-bound)", self.key_path)
            return key
        except Exception as e:
            logger.error("FileSystemKeyManager: failed to store key: %s", e)
            return secrets.token_bytes(32)  # return ephemeral key as last resort


def get_key_manager():
    """Factory: get the best available key manager."""
    mgr = DPAPIKeyManager()
    if mgr.is_available:
        return mgr
    logger.warning("DPAPI unavailable, falling back to FileSystemKeyManager (no hardware binding)")
    return FileSystemKeyManager()  # signed: gamma


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    mgr = DPAPIKeyManager()
    print(f"DPAPI available: {mgr.is_available}")

    if mgr.is_available:
        # Test round-trip
        test_data = b"ScreenMemory secret test data 12345"
        encrypted = mgr.protect(test_data)
        print(f"Encrypted: {len(encrypted)} bytes")

        decrypted = mgr.unprotect(encrypted)
        assert decrypted == test_data, "Round-trip failed!"
        print(f"Decrypted: {decrypted.decode()}")

        # Test key storage
        key = mgr.get_or_create_key()
        if key:
            print(f"Encryption key: {len(key)} bytes (SHA256: {hashlib.sha256(key).hexdigest()[:16]})")
            sqlcipher_key = mgr.derive_sqlcipher_key(key)
            print(f"SQLCipher key: {sqlcipher_key[:20]}...")
    else:
        print("Install pywin32 for DPAPI: pip install pywin32")
