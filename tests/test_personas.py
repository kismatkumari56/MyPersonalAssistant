"""Unit and integration tests for customizable assistant personas in JUGNU."""

import io
import json
import os
import sys
import tkinter as tk
from unittest.mock import Mock, patch

import pytest
import requests

import assistant
import desktop_app as desktop
from personas import (
    BUILTIN_PERSONAS,
    DEFAULT_TEMPERATURE,
    Persona,
    PersonaManager,
    get_persona,
    list_personas,
)
from strings import build_system_prompt, get_string


# ---------------- DATA MODEL & BUILT-IN PRESETS ----------------

def test_builtin_personas_present():
    ids = [p.id for p in BUILTIN_PERSONAS]
    assert "default" in ids
    assert "code" in ids
    assert "creative" in ids
    assert "tutor" in ids
    assert "concise" in ids

    code = next(p for p in BUILTIN_PERSONAS if p.id == "code")
    assert code.temperature == 0.2
    assert "software engineer" in code.system_prompt
    assert code.is_builtin is True

    creative = next(p for p in BUILTIN_PERSONAS if p.id == "creative")
    assert creative.temperature == 0.85
    assert creative.is_builtin is True

    concise = next(p for p in BUILTIN_PERSONAS if p.id == "concise")
    assert concise.temperature == 0.3
    assert concise.is_builtin is True


def test_persona_temperature_clamping():
    p_low = Persona(id="low", name="Low", description="", system_prompt="test", temperature=-0.5)
    assert p_low.temperature == 0.0

    p_high = Persona(id="high", name="High", description="", system_prompt="test", temperature=3.5)
    assert p_high.temperature == 1.5

    p_invalid = Persona(id="nan", name="NaN", description="", system_prompt="test", temperature="not_a_num")
    assert p_invalid.temperature == DEFAULT_TEMPERATURE


def test_persona_serialization():
    p = Persona(
        id="custom_1",
        name="Tester",
        description="Testing role",
        system_prompt="You are a test assistant.",
        temperature=0.45,
        icon="🧪",
        is_builtin=False,
    )
    d = p.to_dict()
    assert d["id"] == "custom_1"
    assert d["name"] == "Tester"
    assert d["temperature"] == 0.45
    assert d["icon"] == "🧪"
    assert d["is_builtin"] is False

    reconstructed = Persona.from_dict(d)
    assert reconstructed.id == p.id
    assert reconstructed.name == p.name
    assert reconstructed.system_prompt == p.system_prompt
    assert reconstructed.temperature == p.temperature


# ---------------- PERSONA MANAGER & CUSTOM CRUD ----------------

def test_persona_manager_crud(tmp_path):
    personas_file = tmp_path / "personas.json"
    mgr = PersonaManager(str(personas_file))

    # Initial state has built-ins only
    assert len(mgr.custom()) == 0
    assert len(mgr.all()) == len(BUILTIN_PERSONAS)

    # Add custom persona
    custom_p = mgr.add_custom(
        name="Python Coach",
        description="Helps with Python idioms and testing",
        system_prompt="You are a helpful Python coach.",
        temperature=0.35,
        icon="🐍",
    )
    assert custom_p.id.startswith("custom_")
    assert custom_p.name == "Python Coach"
    assert custom_p.temperature == 0.35
    assert custom_p.is_builtin is False

    # Check persistence
    assert personas_file.exists()
    mgr2 = PersonaManager(str(personas_file))
    assert len(mgr2.custom()) == 1
    loaded = mgr2.get(custom_p.id)
    assert loaded.name == "Python Coach"
    assert loaded.system_prompt == "You are a helpful Python coach."

    # Update custom persona
    updated = mgr.update_custom(
        custom_p.id,
        name="Senior Python Coach",
        temperature=0.25,
    )
    assert updated.name == "Senior Python Coach"
    assert updated.temperature == 0.25

    # Delete custom persona
    deleted = mgr.delete_custom(custom_p.id)
    assert deleted is True
    assert len(mgr.custom()) == 0
    assert mgr.get(custom_p.id).id == "default"


def test_persona_manager_validation(tmp_path):
    mgr = PersonaManager(str(tmp_path / "personas.json"))

    # Empty name
    with pytest.raises(ValueError, match="name cannot be empty"):
        mgr.add_custom(name="", description="desc", system_prompt="prompt")

    # Name too long
    with pytest.raises(ValueError, match="exceed 50 characters"):
        mgr.add_custom(name="x" * 51, description="desc", system_prompt="prompt")

    # Empty prompt
    with pytest.raises(ValueError, match="prompt cannot be empty"):
        mgr.add_custom(name="Valid Name", description="desc", system_prompt="  ")

    # Overwrite built-in ID
    with pytest.raises(ValueError, match="Cannot overwrite built-in"):
        mgr.add_custom(name="Code Fake", description="", system_prompt="p", persona_id="code")

    # Attempt to modify or delete built-in persona
    with pytest.raises(ValueError, match="Built-in personas cannot be modified"):
        mgr.update_custom("default", name="New Default")

    with pytest.raises(ValueError, match="Built-in personas cannot be deleted"):
        mgr.delete_custom("default")


def test_persona_manager_case_insensitive_and_fallback(tmp_path):
    mgr = PersonaManager(str(tmp_path / "personas.json"))

    # Match by ID uppercase/lowercase
    assert mgr.get("CODE").id == "code"
    assert mgr.get("CrEaTiVe").id == "creative"

    # Match by Name
    assert mgr.get("Code Expert").id == "code"

    # Fallback to default for unknown ID or None
    assert mgr.get(None).id == "default"
    assert mgr.get("non_existent_role_xyz").id == "default"


# ---------------- SYSTEM PROMPT INTEGRATION ----------------

def test_build_system_prompt_default():
    prompt = build_system_prompt("en", "2026-11-13 12:00:00")
    assert "You are JUGNU" in prompt
    assert "Current local date and time: 2026-11-13 12:00:00" in prompt
    assert "Always answer in clear and simple English" in prompt


def test_build_system_prompt_custom_persona():
    custom_intro = "You are a specialized mathematical physics solver."
    prompt = build_system_prompt("en", "2026-11-13 12:00:00", persona_prompt=custom_intro)
    assert "You are a specialized mathematical physics solver." in prompt
    assert "You are JUGNU" not in prompt
    assert "Current local date and time: 2026-11-13 12:00:00" in prompt
    assert "Always answer in clear and simple English" in prompt


def test_build_system_prompt_hinglish_with_persona():
    custom_intro = "You are a concise business advisor."
    prompt = build_system_prompt("hinglish", "2026-11-13 12:00:00", persona_prompt=custom_intro)
    assert "You are a concise business advisor." in prompt
    assert "Hinglish: Hindi written in the English (Roman) alphabet" in prompt


# ---------------- CLI ASSISTANT INTEGRATION ----------------

def test_cli_ask_ai_with_persona():
    captured_payloads = []

    def mock_post(url, json=None, **kwargs):
        captured_payloads.append(json)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"message":{"content":"Code reply"}}'
        return resp

    with patch("assistant.requests.post", side_effect=mock_post):
        res = assistant.ask_ai("Write quicksort", persona="code")

    assert res == "Code reply"
    assert len(captured_payloads) == 1
    payload = captured_payloads[0]
    assert payload["options"]["temperature"] == 0.2
    assert "software engineer" in payload["messages"][0]["content"]


# ---------------- DESKTOP APP INTEGRATION ----------------

@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tkinter not available or failed to initialize")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


@pytest.fixture
def desktop_app(tmp_path, monkeypatch, tk_root):
    monkeypatch.setattr(desktop, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(desktop, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(desktop, "PERSONAS_FILE", str(tmp_path / "personas.json"))
    monkeypatch.setattr(desktop.JugnuApp, "warm_up", lambda self: None)

    for child in tk_root.winfo_children():
        try:
            child.destroy()
        except Exception:
            pass

    with patch("desktop_app.TTSManager") as mock_tts:
        mock_tts.return_value.available = False
        app = desktop.JugnuApp(tk_root)
        yield app, tk_root
        for timer in tk_root.tk.splitlist(tk_root.tk.call("after", "info")):
            try:
                tk_root.after_cancel(timer)
            except Exception:
                pass
        if getattr(app, "persona_win", None) is not None:
            try:
                app.persona_win.destroy()
            except Exception:
                pass
        app.welcome_photo = None
        for child in tk_root.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass


def test_desktop_persona_selector_and_switching(desktop_app):
    app, root = desktop_app

    # Default persona initially
    assert app.current_persona().id == "default"
    assert "Default" in app.persona_btn.cget("text")

    # Switch to code persona
    app.set_chat_persona("code")
    assert app.current_persona().id == "code"
    assert "Code Expert" in app.persona_btn.cget("text")
    assert app.current_chat().get("persona") == "code"

    # New chat gets the saved default persona
    app.new_chat()
    assert app.current_chat().get("persona") == "code"


def test_desktop_ask_ollama_injects_persona_prompt_and_temp(desktop_app):
    app, root = desktop_app
    app.set_chat_persona("creative")
    chat = app.current_chat()
    if chat not in app.chats:
        app.chats.append(chat)

    captured_requests = []

    def mock_post(url, json=None, **kwargs):
        captured_requests.append(json)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"message":{"content":"Once upon a time..."}}'
        return resp

    with patch("desktop_app.requests.post", side_effect=mock_post):
        app.ask_ollama(chat["id"])

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req["options"]["temperature"] == 0.85
    assert "creative writer" in req["messages"][0]["content"].lower()


def test_desktop_persona_manager_dialog_and_custom_creation(desktop_app):
    app, root = desktop_app

    # Open dialog
    app.open_persona_manager()
    assert app.persona_win is not None
    assert app.pw["win"].winfo_exists()

    # Built-in persona cannot be deleted or saved
    assert app.pw["is_builtin"] is True
    assert str(app.pw["save_btn"]["state"]) == "disabled"
    assert str(app.pw["delete_btn"]["state"]) == "disabled"

    # Click New Persona
    app.on_persona_new()
    assert app.pw["selected_persona_id"] is None
    assert str(app.pw["save_btn"]["state"]) == "normal"

    # Fill form
    app.pw["name_entry"].insert(0, "Math Whiz")
    app.pw["desc_entry"].insert(0, "Answers advanced calculus")
    app.pw["temp_scale"].set(0.15)
    app.pw["prompt_text"].insert("1.0", "You are an elite mathematician.")

    # Save
    app.on_persona_save()
    custom_p = app.persona_manager.get("Math Whiz")
    assert custom_p.id != "default"
    assert custom_p.name == "Math Whiz"
    assert custom_p.temperature == 0.15

    # Check that custom persona can now be deleted
    assert app.pw["is_builtin"] is False
    assert str(app.pw["delete_btn"]["state"]) == "normal"

    with patch("tkinter.messagebox.askyesno", return_value=True):
        app.on_persona_delete()

    assert app.persona_manager.get("Math Whiz").id == "default"

    # Close dialog
    app.close_persona_manager()
    assert app.persona_win is None


def test_desktop_persona_theme_and_language_updates(desktop_app):
    app, _ = desktop_app
    app.open_persona_manager()

    # English initially
    assert app.t("persona_new_btn") == "➕  New Persona"
    assert app.pw["new_btn"].cget("text") == "➕  New Persona"

    # Switch language to Hinglish
    app.language = "hinglish"
    app.apply_language()
    assert app.t("persona_new_btn") == "➕  Naya Persona"
    assert app.pw["new_btn"].cget("text") == "➕  Naya Persona"
    assert app.persona_sidebar_btn.cget("text") == "⚙️  Personas Manage Karo…"

    # Toggle theme
    initial_bg = app.pw["body"].cget("bg")
    app.toggle_theme()
    assert app.pw["body"].cget("bg") != initial_bg

    app.close_persona_manager()


def test_persona_manager_corrupted_file_fallback(tmp_path):
    bad_file = tmp_path / "bad_personas.json"
    bad_file.write_text("{bad json syntax", encoding="utf-8")
    mgr = PersonaManager(str(bad_file))
    assert len(mgr.custom()) == 0
    assert len(mgr.all()) == len(BUILTIN_PERSONAS)

    bad_file.write_text(json.dumps("not a list"), encoding="utf-8")
    mgr2 = PersonaManager(str(bad_file))
    assert len(mgr2.custom()) == 0


def test_cli_persona_interactive_commands(capsys):
    inputs = iter([
        "/personas",
        "/persona code",
        "/persona",
        "bye",
    ])

    with patch("builtins.input", side_effect=lambda _: next(inputs)):
        with patch("assistant.greet"):
            assistant.run_assistant()

    captured = capsys.readouterr().out
    assert "Code Expert" in captured
    assert "Switched persona to" in captured
    assert "Current persona is" in captured
