"""Unit tests for personas.py: schema validation, built-in presets, manager, persistence, and import/export."""
import json
import os
import tempfile
import pytest

from personas import (
    BUILTIN_PERSONAS,
    Persona,
    PersonaManager,
    VALID_CATEGORIES,
)
"""Comprehensive automated tests for Customizable Assistant Personas (#47)."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

import assistant
import jugnu_core as core
from personas import (
    BUILTIN_PERSONAS,
    DEFAULT_TEMPERATURE,
    Persona,
    PersonaManager,
    get_persona,
    list_personas,
)
from strings import build_system_prompt, get_string


# ---------------------------------------------------------------------------
# Persona Model & Validation Tests
# ---------------------------------------------------------------------------

def test_builtin_personas_integrity():
    """Verify built-in presets exist, are marked builtin, and satisfy schema validation."""
    assert len(BUILTIN_PERSONAS) >= 6
    ids = {p.id for p in BUILTIN_PERSONAS}
    expected = {"default", "senior_reviewer", "python_architect", "writing_tutor", "socratic_teacher", "debugger"}
    assert expected.issubset(ids)

    for p in BUILTIN_PERSONAS:
        assert p.is_builtin is True
        valid, err = p.validate()
        assert valid is True, f"Built-in persona {p.id} failed validation: {err}"
        assert len(p.name) > 0
        assert len(p.avatar) > 0
        assert len(p.system_prompt) > 20
        assert 0.0 <= p.temperature <= 2.0
        assert p.category in VALID_CATEGORIES


def test_persona_serialization():
    """Test to_dict and from_dict roundtrip."""
    original = Persona(
        id="sql_wizard",
        name="SQL Query Optimizer",
        avatar="💾",
        category="Analysis",
        description="Optimizes SQL queries, explains execution plans, and designs indexes.",
        system_prompt="You are an expert database administrator and SQL performance tuning specialist.",
        temperature=0.3,
        num_ctx=8192,
        is_builtin=False,
    )
    data = original.to_dict()
    assert data["id"] == "sql_wizard"
    assert data["name"] == "SQL Query Optimizer"
    assert data["avatar"] == "💾"
    assert data["temperature"] == 0.3
    assert data["num_ctx"] == 8192

    restored = Persona.from_dict(data)
    assert restored.id == original.id
    assert restored.name == original.name
    assert restored.avatar == original.avatar
    assert restored.category == original.category
    assert restored.temperature == original.temperature
    assert restored.num_ctx == original.num_ctx


def test_persona_validation_errors():
    """Test various validation failure scenarios."""
    # Empty name
    p_bad_name = Persona(id="test1", name="", system_prompt="Valid prompt")
    valid, err = p_bad_name.validate()
    assert valid is False
    assert "name" in err

    # Name too long
    p_long_name = Persona(id="test2", name="X" * 70, system_prompt="Valid prompt")
    valid, err = p_long_name.validate()
    assert valid is False
    assert "60 characters" in err

    # Empty system prompt
    p_empty_prompt = Persona(id="test3", name="Valid Name", system_prompt="")
    valid, err = p_empty_prompt.validate()
    assert valid is False
    assert "system_prompt" in err

    # Temperature out of bounds
    p_bad_temp = Persona(id="test4", name="Valid", system_prompt="Valid prompt", temperature=3.5)
    valid, err = p_bad_temp.validate()
    assert valid is False
    assert "temperature" in err

    # Invalid context window
    p_bad_ctx = Persona(id="test5", name="Valid", system_prompt="Valid prompt", num_ctx=100)
    valid, err = p_bad_ctx.validate()
    assert valid is False
    assert "num_ctx" in err


# ---------------------------------------------------------------------------
# PersonaManager Tests
# ---------------------------------------------------------------------------

def test_manager_get_persona_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        # Built-in lookup
        p = mgr.get_persona("python_architect")
        assert p.name == "Python Architect"
        assert p.avatar == "🐍"

        # Missing lookup falls back to default
        p_missing = mgr.get_persona("non_existent_id")
        assert p_missing.id == "default"
        assert p_missing.name == "JUGNU General Assistant"

        # None falls back to default
        p_none = mgr.get_persona(None)
        assert p_none.id == "default"


def test_manager_add_and_list_custom_persona():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        initial_count = len(mgr.list_personas())

        new_p = Persona(
            id="rust_dev",
            name="Rust Guru",
            avatar="🦀",
            category="Coding",
            description="Specialist in Rust memory safety and concurrency.",
            system_prompt="You are a senior Rust systems programmer. Enforce borrow checker semantics.",
            temperature=0.2,
        )
        added = mgr.add_persona(new_p)
        assert added.id == "rust_dev"
        assert added.is_builtin is False

        # List all personas
        all_p = mgr.list_personas()
        assert len(all_p) == initial_count + 1
        assert any(p.id == "rust_dev" for p in all_p)

        # Filter by category
        coding_personas = mgr.list_personas(category="Coding")
        assert any(p.id == "rust_dev" for p in coding_personas)
        assert all(p.category.lower() == "coding" for p in coding_personas)


def test_manager_cannot_modify_or_delete_builtin():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        with pytest.raises(ValueError, match="Built-in personas cannot be modified"):
            mgr.update_persona("senior_reviewer", {"name": "Hacked Reviewer"})

        with pytest.raises(ValueError, match="Built-in preset personas cannot be deleted"):
            mgr.delete_persona("senior_reviewer")


def test_manager_update_and_delete_custom_persona():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        custom = Persona(
            id="temp_agent",
            name="Temp Agent",
            avatar="⏳",
            system_prompt="Temporary instructions.",
        )
        mgr.add_persona(custom)

        # Update
        updated = mgr.update_persona("temp_agent", {"name": "Updated Agent", "temperature": 0.85})
        assert updated.name == "Updated Agent"
        assert updated.temperature == 0.85
        assert mgr.get_persona("temp_agent").name == "Updated Agent"

        # Delete
        success = mgr.delete_persona("temp_agent")
        assert success is True
        assert mgr.get_persona("temp_agent").id == "default"  # fallback


def test_manager_duplicate_persona():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        cloned = mgr.duplicate_persona("python_architect", new_name="My Custom Python Architect")
        assert cloned.id != "python_architect"
        assert cloned.is_builtin is False
        assert cloned.name == "My Custom Python Architect"
        assert cloned.avatar == "🐍"
        assert "idiomatic" in cloned.system_prompt.lower()

        # Should be editable
        mgr.update_persona(cloned.id, {"avatar": "🔥"})
        assert mgr.get_persona(cloned.id).avatar == "🔥"


# ---------------------------------------------------------------------------
# Persistence & Reload Tests
# ---------------------------------------------------------------------------

def test_manager_persistence_across_instances():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "jugnu_personas.json")

        # Instance 1: add persona and save
        mgr1 = PersonaManager(storage_path=storage)
        custom_p = Persona(
            id="cyber_sec",
            name="Cybersecurity Auditor",
            avatar="🛡️",
            category="Analysis",
            system_prompt="Inspect for OWASP Top 10 vulnerabilities.",
            temperature=0.1,
            num_ctx=4096,
        )
        mgr1.add_persona(custom_p)
        assert os.path.exists(storage)

        # Instance 2: reload from file
        mgr2 = PersonaManager(storage_path=storage)
        loaded = mgr2.get_persona("cyber_sec")
        assert loaded.name == "Cybersecurity Auditor"
        assert loaded.avatar == "🛡️"
        assert loaded.temperature == 0.1
        assert loaded.num_ctx == 4096
        assert loaded.is_builtin is False


def test_manager_corrupted_json_handling():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "corrupt_personas.json")
        with open(storage, "w", encoding="utf-8") as f:
            f.write("{ invalid json text ###")

        # Must not raise an unhandled exception; built-ins should still be available
        mgr = PersonaManager(storage_path=storage)
        assert len(mgr.list_personas()) >= 6
        assert mgr.get_persona("default").name == "JUGNU General Assistant"


# ---------------------------------------------------------------------------
# Import / Export Tests
# ---------------------------------------------------------------------------

def test_export_personas_to_json_bundle():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        json_str = mgr.export_personas()
        data = json.loads(json_str)

        assert "version" in data
        assert "exported_by" in data
        assert "personas" in data
        assert len(data["personas"]) >= 6


def test_import_personas_from_bundle():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        bundle = {
            "version": "1.0",
            "personas": [
                {
                    "name": "DevOps Engineer",
                    "avatar": "🐳",
                    "category": "Coding",
                    "description": "Docker, Kubernetes, and CI/CD automation pipelines.",
                    "system_prompt": "You are a Cloud Infrastructure and DevOps Engineer. Focus on Docker and GitHub Actions.",
                    "temperature": 0.25,
                },
                {
                    "name": "Creative Storyteller",
                    "avatar": "🎭",
                    "category": "Writing",
                    "description": "Engaging worldbuilding, dialogue, and fictional narrative crafting.",
                    "system_prompt": "You are an imaginative creative fiction author and narrative designer.",
                    "temperature": 0.85,
                },
            ]
        }

        imported_count, errors = mgr.import_personas(bundle)
        assert imported_count == 2
        assert len(errors) == 0

        # Check imported personas exist in manager
        all_names = [p.name for p in mgr.list_personas()]
        assert "DevOps Engineer" in all_names
        assert "Creative Storyteller" in all_names


def test_import_malformed_personas_reports_errors():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = os.path.join(tmpdir, "personas.json")
        mgr = PersonaManager(storage_path=storage)

        bad_bundle = {
            "version": "1.0",
            "personas": [
                {
                    "name": "",  # invalid empty name
                    "avatar": "❌",
                    "system_prompt": "Valid prompt",
                },
                {
                    "name": "Valid Persona",
                    "avatar": "✅",
                    "system_prompt": "Valid prompt",
                    "temperature": 5.0,  # invalid temperature
                },
            ]
        }

        imported_count, errors = mgr.import_personas(bad_bundle)
        assert imported_count == 0
        assert len(errors) == 2


# ---------------------------------------------------------------------------
# Prompt Injection Tests
# ---------------------------------------------------------------------------

def test_build_system_prompt_with_persona_instructions():
    custom_instructions = "You are a Senior Code Reviewer. Strictly spot edge cases and security vulnerabilities."
    now_text = "2026-11-19 01:50 PM"

    # Default prompt when persona_prompt is None
    default_prompt = build_system_prompt("en", now_text)
    assert "You are JUGNU, a helpful, friendly personal AI assistant" in default_prompt
    assert "Current local date and time: 2026-11-19 01:50 PM" in default_prompt

    # Persona prompt injection
    injected_prompt = build_system_prompt("en", now_text, persona_prompt=custom_instructions)
    assert custom_instructions in injected_prompt
    assert "You are a Senior Code Reviewer" in injected_prompt
    assert "Current local date and time: 2026-11-19 01:50 PM" in injected_prompt
    assert "Always answer in clear and simple English only" in injected_prompt


def test_build_system_prompt_hinglish_with_persona():
    custom_instructions = "You are a Python Architect."
    now_text = "2026-11-19 01:50 PM"

    injected = build_system_prompt("hinglish", now_text, persona_prompt=custom_instructions)
    assert "You are a Python Architect" in injected
    assert "Always answer in Hinglish" in injected
# ============================================================================
# 1. Data Model & Built-in Presets
# ============================================================================

def test_builtin_personas_present():
    ids = [p.id for p in BUILTIN_PERSONAS]
    assert "default" in ids
    assert "code" in ids
    assert "creative" in ids
    assert "tutor" in ids
    assert "concise" in ids

    code = next(p for p in BUILTIN_PERSONAS if p.id == "code")
    assert code.temperature == 0.2
    assert "software engineer" in code.system_prompt.lower()
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
    assert reconstructed.display_label == "🧪  Tester"


# ============================================================================
# 2. Persona Manager & Custom CRUD
# ============================================================================

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
    assert len(mgr.custom()) == 1
    assert len(mgr.all()) == len(BUILTIN_PERSONAS) + 1
    assert os.path.exists(personas_file)

    # Persistence verification
    reloaded_mgr = PersonaManager(str(personas_file))
    assert len(reloaded_mgr.custom()) == 1
    persisted = reloaded_mgr.get(custom_p.id)
    assert persisted.name == "Python Coach"
    assert persisted.temperature == 0.35
    assert persisted.icon == "🐍"

    # Update custom persona
    updated = mgr.update_custom(custom_p.id, name="Advanced Python Coach", temperature=0.25)
    assert updated.name == "Advanced Python Coach"
    assert updated.temperature == 0.25

    # Delete custom persona
    assert mgr.delete_custom(custom_p.id) is True
    assert len(mgr.custom()) == 0
    assert len(mgr.all()) == len(BUILTIN_PERSONAS)


def test_persona_manager_validation(tmp_path):
    mgr = PersonaManager(str(tmp_path / "personas.json"))

    with pytest.raises(ValueError, match="cannot be empty"):
        mgr.add_custom(name="", description="", system_prompt="Valid prompt")

    with pytest.raises(ValueError, match="System prompt cannot be empty"):
        mgr.add_custom(name="Valid Name", description="", system_prompt="")

    with pytest.raises(ValueError, match="Built-in personas cannot be modified"):
        mgr.update_custom("default", name="Hacked")

    with pytest.raises(ValueError, match="Built-in personas cannot be deleted"):
        mgr.delete_custom("code")


def test_persona_manager_get_fallback():
    mgr = PersonaManager("non_existent_file.json")
    # Exact lookup
    assert mgr.get("code").id == "code"
    # Case-insensitive lookup
    assert mgr.get("CREATIVE").id == "creative"
    # By name
    assert mgr.get("Code Expert").id == "code"
    # Fallback to default
    assert mgr.get("unknown_xyz").id == "default"
    assert mgr.get(None).id == "default"


# ============================================================================
# 3. System Prompt & Core Integration
# ============================================================================

def test_build_system_prompt_with_persona():
    # Without persona
    default_prompt = build_system_prompt("en", "12:00 PM")
    assert "JUGNU" in default_prompt

    # With code persona
    code_p = get_persona("code")
    code_prompt = build_system_prompt("en", "12:00 PM", persona=code_p)
    assert "software engineer" in code_prompt
    assert "12:00 PM" in code_prompt


def test_model_messages_injects_persona():
    chat = {
        "persona": "creative",
        "messages": [{"role": "user", "content": "Write a poem"}]
    }
    msgs = core.model_messages(chat, "en")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert "creative writer" in msgs[0]["content"].lower()
    assert msgs[1]["content"] == "Write a poem"


def test_ask_ollama_injects_persona_temperature():
    captured_payloads = []

    def mock_post(url, json=None, **kwargs):
        captured_payloads.append(json)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"message":{"content":"Hello from Ollama"}}'
        return resp

    chat = {
        "persona": "code",
        "messages": [{"role": "user", "content": "Hello"}]
    }
    with patch("requests.post", side_effect=mock_post):
        ans, is_error = core.ask_ollama(chat, "en")

    assert is_error is False
    assert ans == "Hello from Ollama"
    assert len(captured_payloads) == 1
    assert captured_payloads[0]["options"]["temperature"] == 0.2


# ============================================================================
# 4. CLI Assistant Integration
# ============================================================================

def test_cli_ask_ai_with_persona():
    captured_payloads = []

    def mock_post(url, json=None, **kwargs):
        captured_payloads.append(json)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"message":{"content":"Code reply"}}'
        return resp

    with patch("requests.post", side_effect=mock_post):
        res = assistant.ask_ai("Write quicksort", persona="code")

    assert res == "Code reply"
    assert len(captured_payloads) == 1
    payload = captured_payloads[0]
    assert payload["options"]["temperature"] == 0.2
    assert "software engineer" in payload["messages"][0]["content"]


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


# ============================================================================
# 5. Desktop PySide6 UI Integration
# ============================================================================

def test_desktop_persona_selector_and_switching(window):
    # Default persona initially
    assert window.current_persona().id == "default"
    assert window.persona_combo.currentData() == "default"

    # Switch to code persona
    window.set_chat_persona("code")
    assert window.current_persona().id == "code"
    assert window.persona_combo.currentData() == "code"
    assert window.current_chat().get("persona") == "code"

    # New chat inherits the active persona
    window.new_chat()
    assert window.current_chat().get("persona") == "code"


def test_desktop_ask_ollama_injects_persona(window, monkeypatch):
    window.set_chat_persona("creative")
    captured = []

    def mock_ask_ollama(chat, language, persona=None):
        captured.append(dict(chat))
        return "creative answer", False

    monkeypatch.setattr(core, "ask_ollama", mock_ask_ollama)
    window.ask_ollama(window.current_id)

    # Let task process
    window.wait_for_tasks(1000)
    assert len(captured) == 1
    assert captured[0].get("persona") == "creative"


def test_desktop_persona_manager_dialog(window):
    window.open_persona_manager()
    assert window.persona_dialog is not None
    dlg = window.persona_dialog

    # Builtin persona cannot be modified or deleted
    assert dlg.save_btn.isEnabled() is False
    assert dlg.delete_btn.isEnabled() is False

    # New persona creation
    dlg.on_new()
    assert dlg.save_btn.isEnabled() is True
    dlg.name_edit.setText("Science Whiz")
    dlg.desc_edit.setText("Physics & Chemistry")
    dlg.temp_slider.setValue(45)
    dlg.prompt_edit.setPlainText("You are a science mentor.")
    dlg.on_save()

    p = window.persona_manager.get("Science Whiz")
    assert p.name == "Science Whiz"
    assert p.temperature == 0.45

    # Check that it appeared in window's persona_combo
    idx = window.persona_combo.findData(p.id)
    assert idx >= 0

    # Delete custom persona
    with patch("PySide6.QtWidgets.QMessageBox.question", return_value=16384):  # QMessageBox.Yes = 0x4000 = 16384
        dlg.on_delete()

    assert window.persona_manager.get("Science Whiz").id == "default"

    dlg.close()
    assert window.persona_dialog is None
