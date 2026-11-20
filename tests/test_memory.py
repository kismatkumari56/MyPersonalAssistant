"""Comprehensive unit tests for the JUGNU Offline Persistent User Memory Engine (Issue #65)."""

import json
import tempfile
from pathlib import Path

import pytest

from memory import (
    DEFAULT_MEMORY_FILE,
    MemoryFact,
    MemoryStore,
    extract_facts,
)


@pytest.fixture
def temp_store(tmp_path: Path) -> MemoryStore:
    file_path = tmp_path / "test_jugnu_memory.json"
    return MemoryStore(file_path=file_path)


def test_memory_fact_serialization() -> None:
    fact = MemoryFact(
        id="fact-1",
        category="tech_stack",
        key="Language",
        value="Python 3.12",
        source_chat_id="chat-123",
        enabled=True,
    )
    d = fact.to_dict()
    assert d["id"] == "fact-1"
    assert d["key"] == "Language"
    assert d["value"] == "Python 3.12"

    restored = MemoryFact.from_dict(d)
    assert restored.id == fact.id
    assert restored.category == fact.category
    assert restored.key == fact.key
    assert restored.value == fact.value
    assert restored.enabled is True


def test_memory_store_crud(temp_store: MemoryStore) -> None:
    # Add
    f1 = temp_store.add_fact(category="preference", key="Tone", value="Concise code only")
    assert f1.key == "Tone"
    assert len(temp_store.get_all_facts()) == 1

    # Get
    retrieved = temp_store.get_fact(f1.id)
    assert retrieved is not None
    assert retrieved.value == "Concise code only"

    # Update
    updated = temp_store.update_fact(f1.id, value="Very concise, no prose")
    assert updated is not None
    assert updated.value == "Very concise, no prose"

    # Delete
    deleted = temp_store.delete_fact(f1.id)
    assert deleted is True
    assert len(temp_store.get_all_facts()) == 0


def test_memory_store_deduplication(temp_store: MemoryStore) -> None:
    # Adding same key & category updates existing fact instead of adding duplicate
    f1 = temp_store.add_fact(category="tech_stack", key="Language", value="Python 3.10")
    f2 = temp_store.add_fact(category="tech_stack", key="Language", value="Python 3.12")

    assert f1.id == f2.id
    assert f2.value == "Python 3.12"
    assert len(temp_store.get_all_facts()) == 1


def test_memory_store_persistence(tmp_path: Path) -> None:
    file_path = tmp_path / "persistent_memory.json"
    store1 = MemoryStore(file_path=file_path)
    store1.add_fact(category="personal", key="User Name", value="Alex")

    # Reload store from same file
    store2 = MemoryStore(file_path=file_path)
    facts = store2.get_all_facts()
    assert len(facts) == 1
    assert facts[0].key == "User Name"
    assert facts[0].value == "Alex"


def test_memory_store_search(temp_store: MemoryStore) -> None:
    temp_store.add_fact(category="tech_stack", key="Language", value="Python")
    temp_store.add_fact(category="tech_stack", key="Framework", value="FastAPI")
    temp_store.add_fact(category="personal", key="Location", value="Bengaluru")

    res_fastapi = temp_store.search_facts("fastapi")
    assert len(res_fastapi) == 1
    assert res_fastapi[0].key == "Framework"

    res_all = temp_store.search_facts("")
    assert len(res_all) == 3


def test_memory_store_export_import(temp_store: MemoryStore, tmp_path: Path) -> None:
    temp_store.add_fact(category="preference", key="Format", value="Markdown tables")
    exported = temp_store.export_memory()

    store2 = MemoryStore(file_path=tmp_path / "imported_memory.json")
    count = store2.import_memory(exported)
    assert count == 1
    assert len(store2.get_all_facts()) == 1
    assert store2.get_all_facts()[0].value == "Markdown tables"


def test_memory_context_injection_and_budget(temp_store: MemoryStore) -> None:
    temp_store.add_fact(category="personal", key="User Name", value="Alex")
    temp_store.add_fact(category="tech_stack", key="Stack", value="Python, FastAPI, PySide6")

    ctx = temp_store.build_memory_context(max_chars=500)
    assert "[Persistent User Memory & Guidelines]" in ctx
    assert "User Name: Alex" in ctx
    assert "Stack: Python, FastAPI, PySide6" in ctx

    # Test budget limit
    small_ctx = temp_store.build_memory_context(max_chars=60)
    # Should cap cleanly without exceeding limit
    assert len(small_ctx) <= 60


def test_memory_store_toggle_disabled(temp_store: MemoryStore) -> None:
    temp_store.add_fact(category="personal", key="Name", value="Alex")
    assert len(temp_store.get_active_facts()) == 1

    temp_store.set_enabled(False)
    assert temp_store.is_enabled() is False
    assert len(temp_store.get_active_facts()) == 0
    assert temp_store.build_memory_context() == ""


def test_extract_facts_explicit() -> None:
    # English explicit
    f1 = extract_facts("Please remember that my API token expires on Monday")
    assert len(f1) == 1
    assert "API token expires on Monday" in f1[0]["value"]

    # Hinglish explicit
    f2 = extract_facts("Yaad rakhna ki I prefer dark mode and high contrast")
    assert len(f2) == 1
    assert "dark mode" in f2[0]["value"]

    # Name extraction
    f3 = extract_facts("Hi there, my name is Alex Carter")
    assert len(f3) == 1
    assert f3[0]["key"] == "User Name"
    assert f3[0]["value"] == "Alex Carter"

    # Hinglish name
    f4 = extract_facts("Mera naam Rohit Sharma hai")
    assert len(f4) == 1
    assert f4[0]["key"] == "User Name"
    assert f4[0]["value"] == "Rohit Sharma"


def test_extract_facts_preferences_and_tech() -> None:
    f1 = extract_facts("I prefer concise answers without verbose explanations")
    assert len(f1) >= 1
    assert any("concise" in fact["value"] for fact in f1)

    f2 = extract_facts("We use Python and FastAPI in our tech stack")
    assert any(fact["key"] == "Tech Stack" and fact["value"] == "Python" for fact in f2)
    assert any(fact["key"] == "Framework" and fact["value"] == "FastAPI" for fact in f2)
