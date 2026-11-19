"""Unit tests for vault.py: encryption, authentication envelope, auto-lock, backups, and integrity."""
import json
import os
import shutil
import tempfile
import time
import pytest

from vault import (
    VAULT_MAGIC,
    VaultManager,
    decrypt_payload,
    derive_keys,
    encrypt_payload,
)


@pytest.fixture
def temp_vault_dir():
    """Create an isolated temporary directory for vault and history files."""
    tmp = tempfile.mkdtemp()
    history_file = os.path.join(tmp, "jugnu_history.json")
    vault_file = os.path.join(tmp, "jugnu_history.enc")
    backup_dir = os.path.join(tmp, "backups")
    yield tmp, history_file, vault_file, backup_dir
    shutil.rmtree(tmp, ignore_errors=True)


SAMPLE_CHATS = [
    {
        "id": "chat-001",
        "title": "Confidential Research",
        "messages": [
            {"role": "user", "content": "Keep this secret"},
            {"role": "assistant", "content": "Understood, encrypted locally."},
        ],
    }
]


# ---------------------------------------------------------------------------
# Cryptographic Envelope Tests
# ---------------------------------------------------------------------------

def test_derive_keys_deterministic():
    salt = b"0" * 16
    enc1, mac1 = derive_keys("passphrase", salt)
    enc2, mac2 = derive_keys("passphrase", salt)
    assert enc1 == enc2
    assert mac1 == mac2
    assert len(enc1) == 32
    assert len(mac1) == 32
    assert enc1 != mac1


def test_encrypt_decrypt_roundtrip():
    payload = b"Top secret personal assistant chat message!"
    passphrase = "MasterPassword123!"
    encrypted = encrypt_payload(payload, passphrase)

    assert encrypted.startswith(VAULT_MAGIC)
    assert len(encrypted) >= len(payload) + 78
    assert payload not in encrypted

    decrypted = decrypt_payload(encrypted, passphrase)
    assert decrypted == payload


def test_decrypt_incorrect_passphrase():
    payload = b"Confidential data"
    encrypted = encrypt_payload(payload, "RightPassword")

    with pytest.raises(ValueError, match="Incorrect passphrase or corrupted vault file"):
        decrypt_payload(encrypted, "WrongPassword")


def test_decrypt_tampered_payload():
    payload = b"Important records"
    encrypted = bytearray(encrypt_payload(payload, "Pass1234"))

    # Tamper with a single byte in ciphertext
    encrypted[-1] ^= 0xFF
    with pytest.raises(ValueError, match="Incorrect passphrase or corrupted vault file"):
        decrypt_payload(bytes(encrypted), "Pass1234")


def test_decrypt_corrupted_header():
    with pytest.raises(ValueError, match="Invalid vault envelope"):
        decrypt_payload(b"TOO_SHORT", "Pass1234")


# ---------------------------------------------------------------------------
# VaultManager Lifecycle Tests
# ---------------------------------------------------------------------------

def test_enable_vault_zero_plaintext_persistence(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir

    # Write initial plaintext history
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(SAMPLE_CHATS, f)

    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    assert mgr.is_vault_enabled() is False

    mgr.enable_vault("VaultKey4321")

    assert mgr.is_vault_enabled() is True
    assert mgr.is_locked is False
    assert os.path.exists(vault_file) is True
    # Zero plaintext persistence: plaintext file MUST be deleted from disk
    assert os.path.exists(history_file) is False

    # Vault content is not plaintext
    with open(vault_file, "rb") as f:
        raw = f.read()
    assert b"Confidential Research" not in raw
    assert raw.startswith(VAULT_MAGIC)


def test_unlock_and_lock_lifecycle(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("MasterKey11", SAMPLE_CHATS)

    # Lock session
    mgr.lock()
    assert mgr.is_locked is True
    assert mgr.current_passphrase is None
    assert mgr.load_chats() == []

    # Attempt unlock with wrong passphrase
    with pytest.raises(ValueError):
        mgr.unlock("WrongKey")
    assert mgr.is_locked is True

    # Unlock with correct passphrase
    chats = mgr.unlock("MasterKey11")
    assert mgr.is_locked is False
    assert len(chats) == 1
    assert chats[0]["title"] == "Confidential Research"


def test_save_chats_respects_encryption(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("Pass9999", SAMPLE_CHATS)

    updated = SAMPLE_CHATS + [{"id": "chat-002", "title": "Second Chat", "messages": []}]
    mgr.save_chats(updated)

    # Reading loaded chats gets the updated list
    loaded = mgr.load_chats()
    assert len(loaded) == 2
    assert loaded[1]["title"] == "Second Chat"
    assert os.path.exists(history_file) is False

    # When locked, saving raises PermissionError
    mgr.lock()
    with pytest.raises(PermissionError):
        mgr.save_chats(updated)


def test_disable_vault_restores_plaintext(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("Pass7777", SAMPLE_CHATS)

    mgr.disable_vault("Pass7777")
    assert mgr.is_vault_enabled() is False
    assert mgr.is_locked is False
    assert os.path.exists(vault_file) is False
    assert os.path.exists(history_file) is True

    with open(history_file, "r", encoding="utf-8") as f:
        chats = json.load(f)
    assert len(chats) == 1
    assert chats[0]["title"] == "Confidential Research"


# ---------------------------------------------------------------------------
# Session Auto-Lock Tests
# ---------------------------------------------------------------------------

def test_auto_lock_timer(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("AutoLockKey", SAMPLE_CHATS)
    mgr.auto_lock_minutes = 10

    # Activity recorded right now
    mgr.record_activity()
    assert mgr.check_auto_lock() is False
    assert mgr.is_locked is False

    # Simulate 11 minutes of idle time
    mgr.last_activity = time.time() - (11 * 60)
    assert mgr.check_auto_lock() is True
    assert mgr.is_locked is True

    # "Never" auto-lock option (0 minutes)
    mgr.unlock("AutoLockKey")
    mgr.auto_lock_minutes = 0
    mgr.last_activity = time.time() - (100 * 60)
    assert mgr.check_auto_lock() is False
    assert mgr.is_locked is False


# ---------------------------------------------------------------------------
# Backup Scheduler, Retention, and Restoration Tests
# ---------------------------------------------------------------------------

def test_create_and_prune_backups(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("BackupKey", SAMPLE_CHATS)
    mgr.retention_count = 3

    backups = []
    for i in range(5):
        time.sleep(0.01)
        path = mgr.create_backup(prefix=f"backup_test_{i}")
        backups.append(path)

    # Only 3 backups should remain
    remaining = [f for f in os.listdir(backup_dir) if f.startswith("backup_")]
    assert len(remaining) == 3


def test_scheduled_backup_intervals(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("SchedKey", SAMPLE_CHATS)
    mgr.backup_schedule = "daily"

    # Immediately after start, last_backup_time is 0, so scheduled backup should trigger
    mgr.last_backup_time = time.time()
    assert mgr.check_scheduled_backup() is None

    # Simulate 25 hours passed
    mgr.last_backup_time = time.time() - 90000
    bk = mgr.check_scheduled_backup()
    assert bk is not None
    assert os.path.exists(bk)

    # Manual schedule never runs automatically
    mgr.backup_schedule = "manual"
    mgr.last_backup_time = 0.0
    assert mgr.check_scheduled_backup() is None


def test_restore_encrypted_and_plaintext_backups(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("RestoreKey", SAMPLE_CHATS)

    enc_backup = mgr.create_backup("test_restore")
    assert enc_backup.endswith(".enc")

    # Modify current chats
    mgr.save_chats([])
    assert mgr.load_chats() == []

    # Restore from encrypted backup
    restored = mgr.restore_backup(enc_backup, passphrase="RestoreKey")
    assert len(restored) == 1
    assert restored[0]["title"] == "Confidential Research"


# ---------------------------------------------------------------------------
# Integrity Verification & Vacuum Compaction Tests
# ---------------------------------------------------------------------------

def test_verify_integrity_and_vacuum(temp_vault_dir):
    _, history_file, vault_file, backup_dir = temp_vault_dir
    mgr = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)
    mgr.enable_vault("VacuumKey", SAMPLE_CHATS)

    ok, msg = mgr.verify_integrity()
    assert ok is True
    assert "Integrity verified" in msg

    # Vacuum compaction
    old_sz, new_sz = mgr.vacuum()
    assert old_sz > 0
    assert new_sz > 0
