"""UI tests for Encrypted Chat Vault and Backup Scheduler dialogs."""
import os
import shutil
import tempfile
import tkinter as tk
import types
from unittest.mock import patch
import pytest

from desktop_app import JugnuApp
from vault import VaultManager


_shared_root = None
_shared_app = None


@pytest.fixture(autouse=True)
def setup_app_mock(monkeypatch):
    """Mock heavy subsystems (Ollama warmup, TTS, STT)."""
    monkeypatch.setattr(JugnuApp, "warm_up", lambda self: None)
    monkeypatch.setattr("desktop_app.TTSManager", lambda *a, **k: types.SimpleNamespace(
        available=False,
        speaking=False,
        voices=[],
        stop=lambda: None,
        speak=lambda _t: None,
        apply_settings=lambda *_a: None,
        shutdown=lambda: None,
    ))


@pytest.fixture
def tk_app(tmp_path):
    """Provide a reliable headless JugnuApp instance with custom vault and history."""
    global _shared_root, _shared_app
    if _shared_root is None:
        try:
            _shared_root = tk.Tk()
            _shared_root.withdraw()
        except tk.TclError:
            pytest.skip("No display available for Tk")
        _shared_app = JugnuApp(_shared_root)

    app = _shared_app
    history_file = str(tmp_path / "jugnu_history.json")
    vault_file = str(tmp_path / "jugnu_history.enc")
    backup_dir = str(tmp_path / "backups")

    app.vault = VaultManager(history_file=history_file, vault_file=vault_file, backup_dir=backup_dir)

    yield app, _shared_root

    # Clean up any open child dialogs
    if getattr(app, "vault_win", None) and app.vault_win.winfo_exists():
        try:
            app.vault_win.destroy()
        except Exception:
            pass
    if getattr(app, "unlock_win", None) and app.unlock_win.winfo_exists():
        try:
            app.unlock_win.destroy()
        except Exception:
            pass


def test_vault_button_presence(tk_app):
    """Verify that the Vault button exists in the sidebar."""
    app, root = tk_app
    assert hasattr(app, "vault_btn")
    assert app.vault_btn.winfo_exists()
    assert "Vault" in app.vault_btn["text"] or "🔒" in app.vault_btn["text"]


def test_open_vault_dialog_unencrypted(tk_app):
    """Verify opening vault dialog when unencrypted."""
    app, root = tk_app
    win = app.open_vault_dialog()
    assert win is not None
    assert win.winfo_exists()
    assert hasattr(win, "p1_entry")
    assert hasattr(win, "p2_entry")
    assert hasattr(win, "do_enable")


def test_enable_vault_via_dialog_flow(tk_app):
    """Test enabling encryption and master passphrase flow."""
    app, root = tk_app
    win = app.open_vault_dialog()

    win.p1_entry.insert(0, "SecurePass789")
    win.p2_entry.insert(0, "SecurePass789")

    with patch("tkinter.messagebox.showinfo"):
        win.do_enable()

    assert app.vault.is_vault_enabled() is True
    assert app.vault.is_locked is False


def test_lock_and_unlock_dialog_flow(tk_app):
    """Test locking session and unlocking via modal dialog."""
    app, root = tk_app
    app.vault.enable_vault("LockPass123", [{"id": "c1", "title": "Test", "messages": []}])
    app.vault.lock()
    assert app.vault.is_locked is True

    # Opening vault dialog when locked should trigger unlock dialog
    unlock_win = app.open_vault_dialog()
    assert unlock_win is not None
    assert hasattr(unlock_win, "pass_entry")
    assert hasattr(unlock_win, "do_unlock")

    # Enter correct passphrase and unlock
    unlock_win.pass_entry.insert(0, "LockPass123")
    unlock_win.do_unlock()

    assert app.vault.is_locked is False
    assert app.vault.current_passphrase == "LockPass123"


def test_backup_and_maintenance_actions(tk_app):
    """Verify backup now, integrity check, and vacuum compaction handlers."""
    app, root = tk_app
    app.vault.enable_vault("PassMaint", [{"id": "c1", "title": "Test", "messages": []}])
    win = app.open_vault_dialog()

    with patch("tkinter.messagebox.showinfo"):
        win.do_backup_now()
        backups = [f for f in os.listdir(app.vault.backup_dir) if f.startswith("backup_")]
        assert len(backups) == 1

        win.do_verify()
        win.do_vacuum()
