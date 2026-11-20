"""Tests for Conversation Message Tree Branching & Checkpoint Forking (#62).

Verifies:
- MessageNode data structure, properties, and serialization.
- ConversationTree DAG operations:
  - Branch creation and sibling management.
  - Active path resolution from root to leaf.
  - Real-time branch switching and active pointer updates.
  - Checkpoint tree forking into clean independent trees.
  - Backward compatibility parsing flat linear message lists.
- Desktop PySide6 UI Integration:
  - Bubble branch pagination widgets (◀ 1 / 2 ▶).
  - Branch navigation via pagination controls.
  - Assistant response regeneration creating alternative branches.
  - User prompt editing creating branched discussions.
  - Checkpoint conversation forking into new sidebar chats.
"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from conversation_tree import ConversationTree, MessageNode
import jugnu_core as core


# =====================================================================
# 1. MessageNode Unit Tests
# =====================================================================

def test_message_node_defaults_and_properties():
    node = MessageNode(role="user", content="Hello world")
    assert node.id.startswith("node_")
    assert node.role == "user"
    assert node.content == "Hello world"
    assert node.parent_id is None
    assert node.children_ids == []
    assert node.active_child_index == 0
    assert node.is_leaf is True
    assert node.active_child_id is None

    # Add child
    node.children_ids.append("child_1")
    node.children_ids.append("child_2")
    assert node.is_leaf is False
    assert node.active_child_id == "child_1"

    # Switch active child
    node.active_child_index = 1
    assert node.active_child_id == "child_2"

    # Out of range index
    node.active_child_index = 5
    assert node.active_child_id is None


def test_message_node_serialization_roundtrip():
    original = MessageNode(
        id="custom_node_id",
        role="assistant",
        content="AI generated text",
        parent_id="parent_123",
        children_ids=["c1", "c2"],
        active_child_index=1,
        timestamp="12:45 PM",
        is_error=False,
        metrics={"tps": 32.5},
        metadata={"custom": "val"},
    )
    data = original.to_dict()
    assert data["id"] == "custom_node_id"
    assert data["role"] == "assistant"
    assert data["metrics"] == {"tps": 32.5}

    restored = MessageNode.from_dict(data)
    assert restored.id == original.id
    assert restored.role == original.role
    assert restored.content == original.content
    assert restored.parent_id == original.parent_id
    assert restored.children_ids == original.children_ids
    assert restored.active_child_index == original.active_child_index
    assert restored.timestamp == original.timestamp
    assert restored.metrics == original.metrics
    assert restored.metadata == original.metadata


# =====================================================================
# 2. ConversationTree DAG Operations Tests
# =====================================================================

def test_conversation_tree_linear_growth():
    tree = ConversationTree()
    assert tree.get_active_path() == []
    assert tree.get_active_messages() == []

    # First turn: user prompt
    n1 = tree.add_node(role="user", content="What is Python?", timestamp="10:00 AM")
    assert len(tree.root_ids) == 1
    assert tree.root_ids[0] == n1.id

    # Assistant reply
    n2 = tree.add_node(role="assistant", content="Python is a programming language.", parent_id=n1.id, timestamp="10:01 AM")
    assert n1.children_ids == [n2.id]
    assert n2.parent_id == n1.id

    # Active path
    path = tree.get_active_path()
    assert [n.content for n in path] == ["What is Python?", "Python is a programming language."]
    messages = tree.get_active_messages()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["time"] == "10:00 AM"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["node_id"] == n2.id


def test_conversation_tree_branching_and_pagination():
    tree = ConversationTree()
    u1 = tree.add_node("user", "Tell me a joke")
    # First assistant reply
    a1 = tree.add_node("assistant", "Why did the chicken cross the road?", parent_id=u1.id)

    # Second assistant reply (alternative branch)
    a2 = tree.add_node("assistant", "Knock knock. Who's there?", parent_id=u1.id)

    assert u1.children_ids == [a1.id, a2.id]
    assert u1.active_child_index == 1  # newly added branch becomes active

    # Active path now shows a2
    path = tree.get_active_path()
    assert [n.id for n in path] == [u1.id, a2.id]

    # Siblings check
    idx, total, siblings = tree.get_siblings(a2.id)
    assert idx == 1
    assert total == 2
    assert siblings == [a1.id, a2.id]

    idx1, total1, _ = tree.get_siblings(a1.id)
    assert idx1 == 0
    assert total1 == 2

    # Switch branch back to a1
    assert tree.switch_branch(a1.id, 0) is True
    assert u1.active_child_index == 0
    path = tree.get_active_path()
    assert [n.id for n in path] == [u1.id, a1.id]

    # Out of range switch
    assert tree.switch_branch(a1.id, 99) is False


def test_conversation_tree_fork():
    tree = ConversationTree()
    u1 = tree.add_node("user", "Hello")
    a1 = tree.add_node("assistant", "Hi there!", parent_id=u1.id)
    u2 = tree.add_node("user", "How are you?", parent_id=a1.id)
    a2 = tree.add_node("assistant", "I am doing well!", parent_id=u2.id)
    u3 = tree.add_node("user", "Unrelated follow-up", parent_id=a2.id)

    # Fork tree at checkpoint a2 (should copy u1 -> a1 -> u2 -> a2)
    forked = tree.fork_tree(a2.id)
    active_msgs = forked.get_active_messages()
    assert len(active_msgs) == 4
    assert [m["content"] for m in active_msgs] == [
        "Hello",
        "Hi there!",
        "How are you?",
        "I am doing well!",
    ]
    # Checkpoint u3 was not included
    assert "Unrelated follow-up" not in [m["content"] for m in active_msgs]

    # Forking an unknown node produces an empty tree
    assert tree.fork_tree("non_existent").get_active_path() == []


def test_conversation_tree_backward_compatibility():
    legacy_messages = [
        {"role": "user", "content": "Explain binary search", "time": "09:00 AM"},
        {"role": "assistant", "content": "Binary search divides the interval...", "time": "09:01 AM"},
        {"role": "user", "content": "What is the time complexity?", "time": "09:02 AM"},
        {"role": "assistant", "content": "O(log n)", "time": "09:03 AM", "is_error": False},
    ]

    tree = ConversationTree.from_linear_messages(legacy_messages)
    active = tree.get_active_messages()
    assert len(active) == 4
    assert active[0]["content"] == "Explain binary search"
    assert active[3]["content"] == "O(log n)"
    assert active[3]["time"] == "09:03 AM"

    # Serializing tree and deserializing
    tree_dict = tree.to_dict()
    reloaded = ConversationTree.from_dict(tree_dict)
    assert len(reloaded.get_active_path()) == 4
    assert reloaded.get_active_messages()[1]["content"] == "Binary search divides the interval..."


# =====================================================================
# 3. Desktop UI Bubble & Branching Integration Tests
# =====================================================================

def test_bubble_renders_pagination_for_siblings(qtbot, make_window):
    tree = ConversationTree()
    u1 = tree.add_node("user", "Prompt")
    a1 = tree.add_node("assistant", "Response 1", parent_id=u1.id)
    a2 = tree.add_node("assistant", "Response 2", parent_id=u1.id)
    tree.switch_branch(a1.id, 0)  # active is a1

    app = make_window([
        {
            "id": "c1",
            "title": "Branch Test",
            "messages": tree.get_active_messages(),
            "tree": tree.to_dict(),
        }
    ])
    assert len(app.bubbles) == 2
    assistant_bubble = app.bubbles[1]

    # Bubble must have pagination controls
    assert len(assistant_bubble.page_controls) == 3
    prev_btn, page_lbl, next_btn = assistant_bubble.page_controls
    assert page_lbl.text() == "1/2"
    assert not prev_btn.isEnabled()
    assert next_btn.isEnabled()

    # Click next_btn to switch to Response 2
    qtbot.mouseClick(next_btn, Qt.LeftButton)
    assert len(app.bubbles) == 2
    updated_assistant = app.bubbles[1]
    assert updated_assistant.text == "Response 2"
    _, new_page_lbl, _ = updated_assistant.page_controls
    assert new_page_lbl.text() == "2/2"


def test_regenerate_assistant_response_creates_sibling(qtbot, make_window):
    app = make_window([
        {
            "id": "c1",
            "title": "Regen Test",
            "messages": [
                {"role": "user", "content": "Tell me a fact"},
                {"role": "assistant", "content": "Honey never spoils."},
            ],
        }
    ])
    assert len(app.bubbles) == 2
    assistant_bubble = app.bubbles[1]

    # Initially 1/1 so no pagination controls
    assert len(assistant_bubble.page_controls) == 0
    assert hasattr(assistant_bubble, "regen_btn")

    # Mock Ollama response for regeneration
    with patch("jugnu_core.ask_ollama", return_value=("Bananas are berries.", False)):
        qtbot.mouseClick(assistant_bubble.regen_btn, Qt.LeftButton)
        qtbot.waitUntil(lambda: not app.busy, timeout=3000)

    # After regeneration, current bubble shows newly generated alternative
    assert len(app.bubbles) == 2
    new_bubble = app.bubbles[1]
    assert new_bubble.text == "Bananas are berries."
    assert len(new_bubble.page_controls) == 3
    _, page_lbl, _ = new_bubble.page_controls
    assert page_lbl.text() == "2/2"


def test_edit_user_message_branches_conversation(qtbot, make_window, monkeypatch):
    app = make_window([
        {
            "id": "c1",
            "title": "Edit Test",
            "messages": [
                {"role": "user", "content": "Hello there"},
                {"role": "assistant", "content": "General Kenobi!"},
            ],
        }
    ])
    assert len(app.bubbles) == 2
    user_bubble = app.bubbles[0]
    assert hasattr(user_bubble, "edit_btn")

    # Mock input dialog text and Ollama response
    monkeypatch.setattr(
        "desktop_app.QInputDialog.exec",
        lambda self: QDialog.Accepted,
    )
    monkeypatch.setattr(
        "desktop_app.QInputDialog.textValue",
        lambda self: "Greetings there",
    )

    with patch("jugnu_core.ask_ollama", return_value=("Greetings traveler!", False)):
        qtbot.mouseClick(user_bubble.edit_btn, Qt.LeftButton)
        qtbot.waitUntil(lambda: not app.busy, timeout=3000)

    # The user bubble now has a sibling branch
    assert len(app.bubbles) == 2
    new_user_bubble = app.bubbles[0]
    assert new_user_bubble.text == "Greetings there"
    assert len(new_user_bubble.page_controls) == 3
    _, page_lbl, _ = new_user_bubble.page_controls
    assert page_lbl.text() == "2/2"


def test_fork_chat_at_node_spawns_new_conversation(qtbot, make_window):
    app = make_window([
        {
            "id": "c1",
            "title": "Original Project",
            "messages": [
                {"role": "user", "content": "Step 1: Setup project"},
                {"role": "assistant", "content": "Project configured."},
                {"role": "user", "content": "Step 2: Add database"},
                {"role": "assistant", "content": "Database connected."},
            ],
        }
    ])
    assert len(app.chats) == 1
    # Fork from Step 1 assistant response (bubbles[1])
    assistant_bubble_step1 = app.bubbles[1]
    assert hasattr(assistant_bubble_step1, "fork_btn")

    qtbot.mouseClick(assistant_bubble_step1.fork_btn, Qt.LeftButton)

    # New chat should be added and selected
    assert len(app.chats) == 2
    forked_chat = app.current_chat()
    assert forked_chat["title"] == "[Fork] Original Project"
    # Forked chat contains only the messages up to step 1
    assert len(forked_chat["messages"]) == 2
    assert [m["content"] for m in forked_chat["messages"]] == [
        "Step 1: Setup project",
        "Project configured.",
    ]
