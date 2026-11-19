"""Local Encrypted Chat Vault with Master Passphrase & Automated Backup Scheduler.

Provides:
- Authenticated client-side encryption (PBKDF2-HMAC-SHA256 + Counter Stream Cipher)
  with zero external dependencies and zero plaintext persistence when enabled.
- Session auto-lock timer and password verification.
- Automated background backup scheduler (daily/weekly) with configurable retention.
- One-click database integrity check, vacuum/compaction, and backup restoration.
"""
from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


VAULT_MAGIC = b"JUGNU_VAULT_V1"
VAULT_FILE = "jugnu_history.enc"
BACKUP_DIR = "backups"
DEFAULT_RETENTION = 5


def derive_keys(passphrase: str, salt: bytes) -> Tuple[bytes, bytes]:
    """Derive 32-byte encryption key and 32-byte MAC key using PBKDF2-HMAC-SHA256."""
    derived = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 100_000, 64)
    return derived[:32], derived[32:]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Deterministic HMAC-SHA256 counter mode keystream generator."""
    blocks, counter = [], 0
    while sum(len(b) for b in blocks) < length:
        blocks.append(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(blocks)[:length]


def encrypt_payload(data: bytes, passphrase: str) -> bytes:
    """Encrypt byte payload into an authenticated binary envelope."""
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = derive_keys(passphrase, salt)
    stream = _keystream(enc_key, nonce, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, VAULT_MAGIC + salt + nonce + ciphertext, hashlib.sha256).digest()
    return VAULT_MAGIC + salt + nonce + tag + ciphertext


def decrypt_payload(payload: bytes, passphrase: str) -> bytes:
    """Decrypt authenticated binary envelope. Fails fast on wrong password or tampering."""
    if len(payload) < 78 or not payload.startswith(VAULT_MAGIC):
        raise ValueError("Invalid vault envelope: corrupted or unrecognized format.")
    salt, nonce, tag, ciphertext = payload[14:30], payload[30:46], payload[46:78], payload[78:]
    enc_key, mac_key = derive_keys(passphrase, salt)
    expected_tag = hmac.new(mac_key, VAULT_MAGIC + salt + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Incorrect passphrase or corrupted vault file.")
    stream = _keystream(enc_key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


class VaultManager:
    """Manages encryption lifecycle, session auto-lock, and automated backup rotation."""

    def __init__(self, history_file: str = "jugnu_history.json",
                 vault_file: str = VAULT_FILE, backup_dir: str = BACKUP_DIR):
        self.history_file = history_file
        self.vault_file = vault_file
        self.backup_dir = backup_dir
        self.current_passphrase: Optional[str] = None
        self.is_locked = self.is_vault_enabled()
        self.auto_lock_minutes: int = 15
        self.retention_count: int = DEFAULT_RETENTION
        self.backup_schedule: str = "daily"
        self.last_activity: float = time.time()
        self.last_backup_time: float = 0.0

    def is_vault_enabled(self) -> bool:
        return os.path.exists(self.vault_file)

    def record_activity(self):
        self.last_activity = time.time()

    def enable_vault(self, passphrase: str, existing_chats: Optional[List[Dict[str, Any]]] = None):
        """Enable encrypted vault mode, encrypting history and deleting plaintext."""
        if not passphrase or len(passphrase.strip()) < 4:
            raise ValueError("Master passphrase must be at least 4 characters long.")

        chats = existing_chats
        if chats is None and os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    chats = json.load(f)
            except Exception:
                chats = []
        chats = chats or []

        data = json.dumps(chats, ensure_ascii=False).encode("utf-8")
        encrypted = encrypt_payload(data, passphrase)
        with open(self.vault_file, "wb") as f:
            f.write(encrypted)

        # Zero plaintext persistence
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "wb") as f:
                    f.write(os.urandom(os.path.getsize(self.history_file)))
                os.remove(self.history_file)
            except OSError:
                pass

        self.current_passphrase = passphrase
        self.is_locked = False
        self.record_activity()

    def disable_vault(self, passphrase: str) -> List[Dict[str, Any]]:
        """Disable encrypted vault mode, restoring plaintext history and removing vault."""
        chats = self.unlock(passphrase)
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(chats, f, indent=2, ensure_ascii=False)
        if os.path.exists(self.vault_file):
            os.remove(self.vault_file)
        self.current_passphrase = None
        self.is_locked = False
        return chats

    def unlock(self, passphrase: str) -> List[Dict[str, Any]]:
        """Verify passphrase and return decrypted chats."""
        if not self.is_vault_enabled():
            return []
        with open(self.vault_file, "rb") as f:
            raw = f.read()
        decrypted = decrypt_payload(raw, passphrase)
        chats = json.loads(decrypted.decode("utf-8"))
        self.current_passphrase = passphrase
        self.is_locked = False
        self.record_activity()
        return chats if isinstance(chats, list) else []

    def lock(self):
        """Lock active session and wipe in-memory passphrase."""
        self.current_passphrase = None
        self.is_locked = self.is_vault_enabled()

    def check_auto_lock(self) -> bool:
        """Check if idle timeout has expired; lock session if exceeded."""
        if self.is_vault_enabled() and not self.is_locked and self.auto_lock_minutes > 0:
            if time.time() - self.last_activity >= self.auto_lock_minutes * 60:
                self.lock()
                return True
        return False

    def save_chats(self, chats: List[Dict[str, Any]]):
        """Save chats to disk respecting vault encryption mode."""
        if self.is_vault_enabled():
            if self.is_locked or not self.current_passphrase:
                raise PermissionError("Vault is locked; cannot save chats.")
            data = json.dumps(chats, ensure_ascii=False).encode("utf-8")
            encrypted = encrypt_payload(data, self.current_passphrase)
            tmp = f"{self.vault_file}.tmp"
            with open(tmp, "wb") as f:
                f.write(encrypted)
            os.replace(tmp, self.vault_file)
        else:
            tmp = f"{self.history_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(chats, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.history_file)
        self.record_activity()

    def load_chats(self) -> List[Dict[str, Any]]:
        """Load chats from disk according to current vault state."""
        if self.is_vault_enabled():
            if self.is_locked or not self.current_passphrase:
                return []
            return self.unlock(self.current_passphrase)
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def create_backup(self, prefix: str = "backup") -> str:
        """Create timestamped backup and prune to retention limit."""
        os.makedirs(self.backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.is_vault_enabled():
            dst = os.path.join(self.backup_dir, f"{prefix}_{ts}.enc")
            shutil.copy2(self.vault_file, dst)
        else:
            dst = os.path.join(self.backup_dir, f"{prefix}_{ts}.json")
            if os.path.exists(self.history_file):
                shutil.copy2(self.history_file, dst)
            else:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write("[]")
        self.last_backup_time = time.time()
        self.prune_backups()
        return dst

    def prune_backups(self):
        """Retain only the latest N backups in the backup directory."""
        if not os.path.exists(self.backup_dir):
            return
        files = sorted(
            glob.glob(os.path.join(self.backup_dir, "backup_*.*")),
            key=os.path.getmtime,
            reverse=True,
        )
        for f in files[self.retention_count:]:
            try:
                os.remove(f)
            except OSError:
                pass

    def check_scheduled_backup(self) -> Optional[str]:
        """Perform automated backup if schedule interval has elapsed."""
        if self.backup_schedule == "manual":
            return None
        now = time.time()
        interval = 86400 if self.backup_schedule == "daily" else 604800
        if now - self.last_backup_time >= interval:
            return self.create_backup()
        return None

    def restore_backup(self, backup_path: str, passphrase: Optional[str] = None) -> List[Dict[str, Any]]:
        """Restore conversation history from backup file."""
        if not os.path.exists(backup_path):
            raise FileNotFoundError(f"Backup file not found: {backup_path}")

        if backup_path.endswith(".enc"):
            if not passphrase:
                raise ValueError("Passphrase required to restore encrypted backup.")
            with open(backup_path, "rb") as f:
                decrypted = decrypt_payload(f.read(), passphrase)
            chats = json.loads(decrypted.decode("utf-8"))
            shutil.copy2(backup_path, self.vault_file)
            if os.path.exists(self.history_file):
                os.remove(self.history_file)
            self.current_passphrase = passphrase
            self.is_locked = False
        else:
            with open(backup_path, "r", encoding="utf-8") as f:
                chats = json.load(f)
            shutil.copy2(backup_path, self.history_file)
            if os.path.exists(self.vault_file):
                os.remove(self.vault_file)
            self.current_passphrase = None
            self.is_locked = False
        return chats if isinstance(chats, list) else []

    def verify_integrity(self, file_path: Optional[str] = None,
                         passphrase: Optional[str] = None) -> Tuple[bool, str]:
        """Verify database/vault structure, HMAC tag, and JSON integrity."""
        target = file_path or (self.vault_file if self.is_vault_enabled() else self.history_file)
        if not os.path.exists(target):
            return True, "No database file on disk yet (clean state)."
        try:
            if target.endswith(".enc") or self.is_vault_enabled():
                pw = passphrase or self.current_passphrase
                if not pw:
                    return False, "Passphrase required to verify encrypted vault integrity."
                with open(target, "rb") as f:
                    raw = f.read()
                data = decrypt_payload(raw, pw)
                parsed = json.loads(data.decode("utf-8"))
            else:
                with open(target, "r", encoding="utf-8") as f:
                    parsed = json.load(f)

            if not isinstance(parsed, list):
                return False, "Database root is not a valid list."
            total_msgs = sum(len(c.get("messages", [])) for c in parsed if isinstance(c, dict))
            return True, f"Integrity verified. {len(parsed)} conversations, {total_msgs} messages."
        except Exception as ex:
            return False, f"Integrity check failed: {ex}"

    def vacuum(self) -> Tuple[int, int]:
        """Compact database and purge unnecessary whitespace."""
        target = self.vault_file if self.is_vault_enabled() else self.history_file
        if not os.path.exists(target):
            return 0, 0
        old_size = os.path.getsize(target)
        chats = self.load_chats()
        self.save_chats(chats)
        new_size = os.path.getsize(target)
        return old_size, new_size
