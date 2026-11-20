"""Comprehensive test suite for JUGNU extensions engine, slash commands, and hook lifecycle."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from extensions import (
    DigestExtension,
    Extension,
    ExtensionManager,
    ScratchpadExtension,
    TodoExtension,
    TodoItem,
    get_extension_manager,
)


@pytest.fixture
def temp_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def ext_manager(temp_dir):
    return ExtensionManager(data_dir=temp_dir)


# ---------------- TODO EXTENSION TESTS ----------------

def test_todo_add_and_list(temp_dir):
    todo_ext = TodoExtension(data_dir=temp_dir)
    # Add a normal todo
    res = todo_ext.handle_todo("add Write documentation")
    assert "Added todo #1" in res
    assert "Write documentation" in res

    # Add a high priority todo
    res_high = todo_ext.handle_todo("add Fix critical bug --priority high")
    assert "Added todo #2" in res_high
    assert "[HIGH]" in res_high

    # List todos
    listing = todo_ext.handle_todo("list")
    assert "#1" in listing
    assert "Write documentation" in listing
    assert "#2" in listing
    assert "Fix critical bug" in listing


def test_todo_done_and_delete(temp_dir):
    todo_ext = TodoExtension(data_dir=temp_dir)
    todo_ext.handle_todo("add Task 1")
    todo_ext.handle_todo("add Task 2")

    # Complete Task 1
    done_res = todo_ext.handle_todo("done 1")
    assert "Marked todo #1 as completed" in done_res

    # Listing reflects completion
    listing = todo_ext.handle_todo("list")
    assert "- [x] ~~#1 Task 1~~" in listing
    assert "- [ ] **#2** Task 2" in listing

    # Delete Task 2
    del_res = todo_ext.handle_todo("del 2")
    assert "Deleted todo #2" in del_res

    listing_after = todo_ext.handle_todo("list")
    assert "#2" not in listing_after


def test_todo_clear_completed(temp_dir):
    todo_ext = TodoExtension(data_dir=temp_dir)
    todo_ext.handle_todo("add Task 1")
    todo_ext.handle_todo("add Task 2")
    todo_ext.handle_todo("done 1")

    res = todo_ext.handle_todo("clear")
    assert "Cleared 1 completed todo(s)" in res
    assert len(todo_ext.todos) == 1
    assert todo_ext.todos[0].id == 2


def test_todo_persistence_roundtrip(temp_dir):
    ext1 = TodoExtension(data_dir=temp_dir)
    ext1.handle_todo("add Persistent Task")

    # Reload from disk in a fresh instance
    ext2 = TodoExtension(data_dir=temp_dir)
    assert len(ext2.todos) == 1
    assert ext2.todos[0].text == "Persistent Task"


# ---------------- SCRATCHPAD EXTENSION TESTS ----------------

def test_scratchpad_crud(temp_dir):
    scratch = ScratchpadExtension(data_dir=temp_dir)
    assert "Scratchpad is empty" in scratch.handle_scratch("get")

    # Set content
    set_res = scratch.handle_scratch("set function test() { return 42; }")
    assert "Scratchpad updated" in set_res
    assert "function test()" in scratch.handle_scratch("get")

    # Append content
    app_res = scratch.handle_scratch("append console.log(test());")
    assert "Appended to scratchpad" in app_res

    content = scratch.handle_scratch("get")
    assert "function test()" in content
    assert "console.log(test())" in content

    # Clear
    clear_res = scratch.handle_scratch("clear")
    assert "Scratchpad cleared" in clear_res
    assert "Scratchpad is empty" in scratch.handle_scratch("get")


def test_scratchpad_persistence(temp_dir):
    s1 = ScratchpadExtension(data_dir=temp_dir)
    s1.handle_scratch("set Important API Token: ABC-123")

    s2 = ScratchpadExtension(data_dir=temp_dir)
    assert "ABC-123" in s2.handle_scratch("get")


# ---------------- DIGEST EXTENSION TESTS ----------------

def test_digest_metrics():
    digest = DigestExtension()
    ctx = {
        "title": "Architecture Discussion",
        "messages": [
            {"role": "user", "content": "How do we scale our microservices backend?"},
            {
                "role": "assistant",
                "content": "You can scale using horizontal pod autoscaling, caching via Redis, and database read replicas.",
            },
            {"role": "user", "content": "Tell me more about Redis caching."},
            {
                "role": "assistant",
                "content": "Redis provides in-memory key-value caching with low sub-millisecond read latency.",
            },
        ],
    }

    res = digest.handle_digest("", context=ctx)
    assert "Conversation Digest: Architecture Discussion" in res
    assert "**Total Turns**: 4" in res
    assert "**User Word Count**:" in res
    assert "**Assistant Word Count**:" in res
    assert "**Estimated Token Footprint**:" in res


def test_digest_empty_messages():
    digest = DigestExtension()
    res = digest.handle_digest("", context={"messages": []})
    assert "No messages in active chat" in res


# ---------------- EXTENSION MANAGER TESTS ----------------

def test_manager_builtin_discovery(ext_manager):
    exts = ext_manager.list_extensions()
    names = [e["name"] for e in exts]
    assert "todo" in names
    assert "scratchpad" in names
    assert "digest" in names


def test_manager_slash_command_dispatch(ext_manager):
    assert ext_manager.is_slash_command("/todo add Buy Milk")
    assert ext_manager.is_slash_command("/scratch set hello")
    assert ext_manager.is_slash_command("/digest")
    assert not ext_manager.is_slash_command("What is the weather?")

    res = ext_manager.handle_command("/todo add Buy Milk")
    assert "Added todo #1" in res
    assert "Buy Milk" in res


def test_manager_enable_disable(ext_manager):
    assert ext_manager.is_enabled("todo")
    ext_manager.disable_extension("todo")
    assert not ext_manager.is_enabled("todo")

    # Command should not be recognized when disabled
    assert not ext_manager.is_slash_command("/todo list")
    assert ext_manager.handle_command("/todo list") is None

    # Re-enable
    ext_manager.enable_extension("todo")
    assert ext_manager.is_enabled("todo")
    assert ext_manager.is_slash_command("/todo list")


def test_manager_fault_tolerance(ext_manager):
    class CrashingExtension(Extension):
        name = "crasher"
        title = "Crash Tester"

        def register_commands(self):
            self.register_command("/crash", self.crash_handler)

        def crash_handler(self, args, ctx):
            raise RuntimeError("Fatal simulated crash")

        def before_message_sent(self, msg):
            raise ValueError("Hook crash")

    crashing_ext = CrashingExtension()
    ext_manager.register_extension(crashing_ext)

    # Command should catch error and return error message without bubbling exception
    res = ext_manager.handle_command("/crash")
    assert "❌ Error executing `/crash`" in res
    assert "Fatal simulated crash" in res

    # Hook crash should not prevent message pass-through
    orig_msg = {"role": "user", "content": "Hello"}
    dispatched = ext_manager.dispatch_before_message(orig_msg)
    assert dispatched == orig_msg


def test_manager_available_commands(ext_manager):
    cmds = ext_manager.get_available_slash_commands()
    triggers = [c["command"] for c in cmds]
    assert "/todo" in triggers
    assert "/scratch" in triggers
    assert "/digest" in triggers


def test_extensions_manager_dialog(qapp, temp_dir):
    from extensions_dialog import ExtensionsManagerDialog
    manager = ExtensionManager(data_dir=temp_dir)
    dialog = ExtensionsManagerDialog(manager=manager)
    assert dialog.card_layout.count() > 0

    # Find todo card and toggle it
    assert manager.is_enabled("todo")
    dialog.on_toggle("todo", False)
    assert not manager.is_enabled("todo")
    dialog.on_toggle("todo", True)
    assert manager.is_enabled("todo")
    dialog.close()

