#!/usr/bin/env python3
"""
Skynet Encryption at Rest — AES-256-GCM file encryption.

Provides transparent encrypt/decrypt for sensitive data files using
AES-256-GCM (authenticated encryption). Keys derived from a master
password via PBKDF2-HMAC-SHA256 with 100K iterations.

Encrypted file format:
  [16 bytes salt][12 bytes nonce][ciphertext][16 bytes GCM tag]

The tag is appended by AESGCM automatically. Salt is stored so the
same password always derives the same key for a given salt.

Usage:
  python tools/skynet_encryption.py encrypt PATH [--password PWD]
  python tools/skynet_encryption.py decrypt PATH [--password PWD]
  python tools/skynet_encryption.py rotate-key [--old-password OLD] [--new-password NEW]
  python tools/skynet_encryption.py status
  python tools/skynet_encryption.py verify PATH

signed: delta
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Crypto imports (require `pip install cryptography`)
# ---------------------------------------------------------------------------
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
KEY_FILE = DATA_DIR / "encryption_key.bin"      # salt + derived-key fingerprint
MANIFEST_FILE = DATA_DIR / "encryption_manifest.json"
ENCRYPTED_EXT = ".enc"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PBKDF2_ITERATIONS = 100_000
SALT_SIZE = 16      # bytes
NONCE_SIZE = 12     # bytes — standard for AES-GCM
KEY_SIZE = 32       # bytes — AES-256
TAG_SIZE = 16       # bytes — GCM auth tag (appended by AESGCM internally)
HEADER_MAGIC = b"SKENC1"  # 6-byte magic header for identification

# signed: delta


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class EncryptedFileRecord:
    """Metadata about an encrypted file."""
    original_path: str
    encrypted_path: str
    original_size: int
    encrypted_size: int
    sha256_original: str       # hash of original plaintext
    encrypted_at: float
    key_fingerprint: str       # first 8 hex chars of derived key hash
    # signed: delta


@dataclass
class EncryptionManifest:
    """Tracks all encrypted files."""
    version: int = 1
    files: Dict[str, EncryptedFileRecord] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    total_encrypted: int = 0
    total_decrypted: int = 0
    key_rotations: int = 0
    # signed: delta

    def save(self) -> None:
        self.last_updated = time.time()
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "files": {k: asdict(v) for k, v in self.files.items()},
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "total_encrypted": self.total_encrypted,
            "total_decrypted": self.total_decrypted,
            "key_rotations": self.key_rotations,
        }
        MANIFEST_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "EncryptionManifest":
        if not MANIFEST_FILE.exists():
            return cls()
        try:
            raw = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
            m = cls(
                version=raw.get("version", 1),
                created_at=raw.get("created_at", time.time()),
                last_updated=raw.get("last_updated", time.time()),
                total_encrypted=raw.get("total_encrypted", 0),
                total_decrypted=raw.get("total_decrypted", 0),
                key_rotations=raw.get("key_rotations", 0),
            )
            for k, v in raw.get("files", {}).items():
                m.files[k] = EncryptedFileRecord(**v)
            return m
        except Exception:
            return cls()


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------
def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit key from password + salt via PBKDF2-HMAC-SHA256."""
    if not CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library not installed: pip install cryptography")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))
    # signed: delta


def _key_fingerprint(key: bytes) -> str:
    """First 8 hex chars of SHA-256 of the derived key — for manifest tracking."""
    return hashlib.sha256(key).hexdigest()[:8]


def _file_sha256(path: Path) -> str:
    """SHA-256 hex digest of file contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def init_key(password: str) -> Tuple[bytes, bytes]:
    """
    Initialize or load encryption key material.

    Returns (derived_key, salt).
    If KEY_FILE exists, reads salt from it and re-derives.
    Otherwise creates new salt, derives key, and saves.
    """
    if KEY_FILE.exists():
        blob = KEY_FILE.read_bytes()
        if len(blob) < SALT_SIZE:
            raise ValueError("Corrupted key file — too short")
        salt = blob[:SALT_SIZE]
        stored_fp = blob[SALT_SIZE:].decode("utf-8", errors="ignore").strip()
        key = _derive_key(password, salt)
        fp = _key_fingerprint(key)
        if stored_fp and stored_fp != fp:
            raise ValueError("Wrong password — key fingerprint mismatch")
        return key, salt

    # New key
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)
    fp = _key_fingerprint(key)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(salt + fp.encode("utf-8"))
    return key, salt
    # signed: delta


# ---------------------------------------------------------------------------
# EncryptionManager
# ---------------------------------------------------------------------------
class EncryptionManager:
    """
    AES-256-GCM file encryption manager.

    Encrypted format per file:
      SKENC1 (6B magic) | salt (16B) | nonce (12B) | ciphertext+tag (var)

    The GCM tag (16 bytes) is appended to ciphertext by AESGCM internally.
    """

    def __init__(self, password: Optional[str] = None):
        self.password = password
        self._key: Optional[bytes] = None
        self._salt: Optional[bytes] = None
        self.manifest = EncryptionManifest.load()
        # signed: delta

    def _ensure_key(self) -> bytes:
        """Lazy key initialization."""
        if self._key is None:
            if self.password is None:
                raise ValueError("No password provided. Use --password or set SKYNET_ENC_PASSWORD env var.")
            self._key, self._salt = init_key(self.password)
        return self._key

    # -----------------------------------------------------------------------
    # Encrypt
    # -----------------------------------------------------------------------
    def encrypt_file(self, path: str | Path, output: Optional[str | Path] = None,
                     delete_original: bool = False) -> str:
        """
        Encrypt a file with AES-256-GCM.

        Args:
            path: File to encrypt.
            output: Output path (default: path + '.enc').
            delete_original: Remove plaintext after encryption.

        Returns:
            Path to encrypted file.
        """
        src = Path(path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"File not found: {src}")
        if src.suffix == ENCRYPTED_EXT:
            raise ValueError(f"File already has {ENCRYPTED_EXT} extension — appears encrypted")

        key = self._ensure_key()
        nonce = os.urandom(NONCE_SIZE)
        plaintext = src.read_bytes()
        original_hash = hashlib.sha256(plaintext).hexdigest()

        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
        # ciphertext includes the 16-byte GCM tag appended

        dst = Path(output) if output else src.with_suffix(src.suffix + ENCRYPTED_EXT)
        # Write: magic + salt + nonce + ciphertext (includes tag)
        with open(dst, "wb") as f:
            f.write(HEADER_MAGIC)
            f.write(self._salt)
            f.write(nonce)
            f.write(ciphertext)

        # Update manifest
        rel_key = str(src.relative_to(BASE_DIR)) if src.is_relative_to(BASE_DIR) else str(src)
        self.manifest.files[rel_key] = EncryptedFileRecord(
            original_path=str(src),
            encrypted_path=str(dst),
            original_size=len(plaintext),
            encrypted_size=dst.stat().st_size,
            sha256_original=original_hash,
            encrypted_at=time.time(),
            key_fingerprint=_key_fingerprint(key),
        )
        self.manifest.total_encrypted += 1
        self.manifest.save()

        if delete_original:
            src.unlink()

        return str(dst)
        # signed: delta

    # -----------------------------------------------------------------------
    # Decrypt
    # -----------------------------------------------------------------------
    def decrypt_file(self, path: str | Path, output: Optional[str | Path] = None,
                     delete_encrypted: bool = False) -> str:
        """
        Decrypt an AES-256-GCM encrypted file.

        Args:
            path: Encrypted file (.enc).
            output: Output path (default: strip .enc extension).
            delete_encrypted: Remove encrypted file after decryption.

        Returns:
            Path to decrypted file.
        """
        src = Path(path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"File not found: {src}")

        blob = src.read_bytes()
        magic_len = len(HEADER_MAGIC)
        min_size = magic_len + SALT_SIZE + NONCE_SIZE + TAG_SIZE + 1
        if len(blob) < min_size:
            raise ValueError("File too small to be a valid encrypted file")

        # Parse header
        magic = blob[:magic_len]
        if magic != HEADER_MAGIC:
            raise ValueError(f"Invalid file header — not a Skynet encrypted file (got {magic!r})")
        salt = blob[magic_len:magic_len + SALT_SIZE]
        nonce = blob[magic_len + SALT_SIZE:magic_len + SALT_SIZE + NONCE_SIZE]
        ciphertext_with_tag = blob[magic_len + SALT_SIZE + NONCE_SIZE:]

        # Derive key from stored salt
        if self.password is None:
            raise ValueError("No password provided.")
        key = _derive_key(self.password, salt)

        aesgcm = AESGCM(key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag, associated_data=None)
        except Exception as e:
            raise ValueError(f"Decryption failed — wrong password or corrupted file: {e}")

        # Output path
        if output:
            dst = Path(output)
        elif src.suffix == ENCRYPTED_EXT:
            dst = src.with_suffix("")  # strip .enc
        else:
            dst = src.with_suffix(".dec")

        dst.write_bytes(plaintext)
        self.manifest.total_decrypted += 1
        self.manifest.save()

        if delete_encrypted:
            src.unlink()

        return str(dst)
        # signed: delta

    # -----------------------------------------------------------------------
    # Key rotation
    # -----------------------------------------------------------------------
    def rotate_key(self, old_password: str, new_password: str) -> Dict:
        """
        Re-encrypt all tracked files with a new key derived from new_password.

        Returns dict with rotation results.
        """
        results = {"rotated": [], "failed": [], "total": 0}

        # Verify old password
        try:
            old_key, old_salt = init_key(old_password)
        except ValueError:
            raise ValueError("Old password incorrect — cannot rotate")

        # Create new salt + key
        new_salt = os.urandom(SALT_SIZE)
        new_key = _derive_key(new_password, new_salt)
        new_fp = _key_fingerprint(new_key)

        old_aesgcm = AESGCM(old_key)
        new_aesgcm = AESGCM(new_key)

        for rel_path, record in list(self.manifest.files.items()):
            enc_path = Path(record.encrypted_path)
            if not enc_path.exists():
                results["failed"].append({"path": str(enc_path), "reason": "file not found"})
                continue

            results["total"] += 1
            try:
                blob = enc_path.read_bytes()
                magic_len = len(HEADER_MAGIC)
                stored_salt = blob[magic_len:magic_len + SALT_SIZE]
                nonce = blob[magic_len + SALT_SIZE:magic_len + SALT_SIZE + NONCE_SIZE]
                ct = blob[magic_len + SALT_SIZE + NONCE_SIZE:]

                # Decrypt with old key (re-derive from stored salt)
                old_file_key = _derive_key(old_password, stored_salt)
                old_file_aesgcm = AESGCM(old_file_key)
                plaintext = old_file_aesgcm.decrypt(nonce, ct, associated_data=None)

                # Re-encrypt with new key
                new_nonce = os.urandom(NONCE_SIZE)
                new_ct = new_aesgcm.encrypt(new_nonce, plaintext, associated_data=None)

                with open(enc_path, "wb") as f:
                    f.write(HEADER_MAGIC)
                    f.write(new_salt)
                    f.write(new_nonce)
                    f.write(new_ct)

                record.encrypted_size = enc_path.stat().st_size
                record.encrypted_at = time.time()
                record.key_fingerprint = new_fp
                results["rotated"].append(str(enc_path))

            except Exception as e:
                results["failed"].append({"path": str(enc_path), "reason": str(e)})

        # Update key file with new salt + fingerprint
        KEY_FILE.write_bytes(new_salt + new_fp.encode("utf-8"))
        self._key = new_key
        self._salt = new_salt

        self.manifest.key_rotations += 1
        self.manifest.save()

        return results
        # signed: delta

    # -----------------------------------------------------------------------
    # Verify
    # -----------------------------------------------------------------------
    def verify_file(self, path: str | Path) -> Dict:
        """
        Verify an encrypted file is valid without fully decrypting.

        Checks: magic header, salt presence, nonce size, and
        attempts decryption to verify integrity.
        """
        src = Path(path).resolve()
        if not src.exists():
            return {"valid": False, "error": "file not found"}

        blob = src.read_bytes()
        magic_len = len(HEADER_MAGIC)
        min_size = magic_len + SALT_SIZE + NONCE_SIZE + TAG_SIZE + 1

        checks = {
            "file_exists": True,
            "min_size": len(blob) >= min_size,
            "magic_header": blob[:magic_len] == HEADER_MAGIC if len(blob) >= magic_len else False,
            "decrypts_ok": False,
            "file_size": len(blob),
        }

        if not checks["magic_header"] or not checks["min_size"]:
            checks["valid"] = False
            return checks

        # Try decryption
        try:
            salt = blob[magic_len:magic_len + SALT_SIZE]
            nonce = blob[magic_len + SALT_SIZE:magic_len + SALT_SIZE + NONCE_SIZE]
            ct = blob[magic_len + SALT_SIZE + NONCE_SIZE:]

            key = self._ensure_key()
            # Re-derive with file's salt
            file_key = _derive_key(self.password, salt)
            aesgcm = AESGCM(file_key)
            aesgcm.decrypt(nonce, ct, associated_data=None)
            checks["decrypts_ok"] = True
        except Exception as e:
            checks["decrypt_error"] = str(e)

        checks["valid"] = checks["decrypts_ok"]
        return checks
        # signed: delta

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------
    def status(self) -> Dict:
        """Return encryption system status."""
        return {
            "key_file_exists": KEY_FILE.exists(),
            "key_fingerprint": _key_fingerprint(self._key) if self._key else None,
            "manifest_file": str(MANIFEST_FILE),
            "total_encrypted": self.manifest.total_encrypted,
            "total_decrypted": self.manifest.total_decrypted,
            "key_rotations": self.manifest.key_rotations,
            "tracked_files": len(self.manifest.files),
            "crypto_available": CRYPTO_AVAILABLE,
        }
        # signed: delta


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------
def encrypt_file(path: str, password: Optional[str] = None, **kwargs) -> str:
    """Encrypt a file. Password from arg or SKYNET_ENC_PASSWORD env var."""
    pwd = password or os.environ.get("SKYNET_ENC_PASSWORD")
    if not pwd:
        raise ValueError("Password required: pass directly or set SKYNET_ENC_PASSWORD")
    mgr = EncryptionManager(pwd)
    return mgr.encrypt_file(path, **kwargs)
    # signed: delta


def decrypt_file(path: str, password: Optional[str] = None, **kwargs) -> str:
    """Decrypt a file. Password from arg or SKYNET_ENC_PASSWORD env var."""
    pwd = password or os.environ.get("SKYNET_ENC_PASSWORD")
    if not pwd:
        raise ValueError("Password required: pass directly or set SKYNET_ENC_PASSWORD")
    mgr = EncryptionManager(pwd)
    return mgr.decrypt_file(path, **kwargs)
    # signed: delta


def is_encrypted(path: str | Path) -> bool:
    """Quick check if a file looks like a Skynet encrypted file."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        with open(p, "rb") as f:
            magic = f.read(len(HEADER_MAGIC))
        return magic == HEADER_MAGIC
    except Exception:
        return False
    # signed: delta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _get_password(args, prompt: str = "Password: ") -> str:
    """Get password from args, env, or interactive prompt."""
    if hasattr(args, "password") and args.password:
        return args.password
    env = os.environ.get("SKYNET_ENC_PASSWORD")
    if env:
        return env
    return getpass.getpass(prompt)


def main():
    parser = argparse.ArgumentParser(
        description="Skynet Encryption at Rest — AES-256-GCM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # encrypt
    p_enc = sub.add_parser("encrypt", help="Encrypt a file")
    p_enc.add_argument("path", help="File to encrypt")
    p_enc.add_argument("--output", "-o", help="Output path (default: PATH.enc)")
    p_enc.add_argument("--password", "-p", help="Encryption password")
    p_enc.add_argument("--delete-original", action="store_true",
                       help="Delete plaintext after encryption")

    # decrypt
    p_dec = sub.add_parser("decrypt", help="Decrypt a file")
    p_dec.add_argument("path", help="Encrypted file to decrypt")
    p_dec.add_argument("--output", "-o", help="Output path (default: strip .enc)")
    p_dec.add_argument("--password", "-p", help="Decryption password")
    p_dec.add_argument("--delete-encrypted", action="store_true",
                       help="Delete encrypted file after decryption")

    # rotate-key
    p_rot = sub.add_parser("rotate-key", help="Rotate encryption key")
    p_rot.add_argument("--old-password", help="Current password")
    p_rot.add_argument("--new-password", help="New password")

    # verify
    p_ver = sub.add_parser("verify", help="Verify encrypted file integrity")
    p_ver.add_argument("path", help="Encrypted file to verify")
    p_ver.add_argument("--password", "-p", help="Password for verification")

    # status
    sub.add_parser("status", help="Show encryption system status")

    args = parser.parse_args()

    if not CRYPTO_AVAILABLE:
        print("ERROR: cryptography library not installed. Run: pip install cryptography")
        sys.exit(1)

    if args.command == "encrypt":
        pwd = _get_password(args)
        mgr = EncryptionManager(pwd)
        result = mgr.encrypt_file(args.path, output=args.output,
                                  delete_original=args.delete_original)
        print(f"Encrypted: {result}")

    elif args.command == "decrypt":
        pwd = _get_password(args)
        mgr = EncryptionManager(pwd)
        result = mgr.decrypt_file(args.path, output=args.output,
                                  delete_encrypted=args.delete_encrypted)
        print(f"Decrypted: {result}")

    elif args.command == "rotate-key":
        old = args.old_password or _get_password(args, "Current password: ")
        new = args.new_password or _get_password(args, "New password: ")
        mgr = EncryptionManager(old)
        results = mgr.rotate_key(old, new)
        print(f"Key rotated. {len(results['rotated'])} files re-encrypted, "
              f"{len(results['failed'])} failed.")
        if results["failed"]:
            for f in results["failed"]:
                print(f"  FAILED: {f['path']} — {f['reason']}")

    elif args.command == "verify":
        pwd = _get_password(args)
        mgr = EncryptionManager(pwd)
        result = mgr.verify_file(args.path)
        if result.get("valid"):
            print(f"VALID: {args.path} (size={result['file_size']})")
        else:
            print(f"INVALID: {args.path}")
            for k, v in result.items():
                if k != "valid":
                    print(f"  {k}: {v}")

    elif args.command == "status":
        mgr = EncryptionManager()
        status = mgr.status()
        print("Skynet Encryption Status:")
        for k, v in status.items():
            print(f"  {k}: {v}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
# signed: delta
