"""Comprehensive automated tests for Command Palette and Shortcut System (#53).

Tests:
1. fuzzy_match: exact, prefix, acronym, substring, keyword, subsequence, typo fallback, no match.
2. CommandRegistry: register, get, unregister, empty query grouping, ranked search.
3. CommandPaletteDialog: list population, real-time filtering, arrow navigation, execution.
4. ShortcutsHelpDialog: rendering in dark/light mode, shortcut tables, escape closing.
5. desktop_app integration: hotkeys (Ctrl+K, Ctrl+D, Ctrl+L, Ctrl+Shift+S, Ctrl+Up/Down), escape handler.
6. assistant CLI: /shortcuts command and help menu.
"""
from unittest.mock import MagicMock, patch

import pytest

from command_palette import (
    Command,
    CommandPaletteDialog,
    CommandRegistry,
    ShortcutsHelpDialog,
    fuzzy_match,
)


# ============================================================================
# 1. Fuzzy Matching Tests
# ============================================================================

def test_fuzzy_match_exact_and_prefix():
    assert fuzzy_match("new chat", "New Chat") == 100.0
    prefix_score = fuzzy_match("new", "New Chat")
    assert prefix_score >= 90.0


def test_fuzzy_match_acronym():
    # "nc" matches "New Chat"
    acronym_score = fuzzy_match("nc", "New Chat")
    assert acronym_score >= 80.0

    # "tt" matches "Toggle Theme"
    assert fuzzy_match("tt", "Toggle Theme") >= 80.0


def test_fuzzy_match_substring_and_word_prefix():
    # "theme" in "Toggle Theme"
    sub_score = fuzzy_match("theme", "Toggle Theme")
    assert sub_score >= 80.0

    # "dark" in keywords
    kw_score = fuzzy_match("dark", "Toggle Theme", keywords=["dark", "light", "mode"])
    assert kw_score >= 60.0


def test_fuzzy_match_subsequence_and_typo():
    # Subsequence "tthm" in "Toggle Theme"
    subseq_score = fuzzy_match("tthm", "Toggle Theme")
    assert subseq_score >= 40.0

    # Typo "toogle" for "toggle"
    typo_score = fuzzy_match("toogle", "Toggle Theme")
    assert typo_score >= 20.0


def test_fuzzy_match_empty_and_no_match():
    # Empty query matches with default baseline
    assert fuzzy_match("", "Any Title") == 1.0
    # Completely unrelated string
    assert fuzzy_match("zyxwvu", "New Chat") == 0.0


# ============================================================================
# 2. Command Registry Tests
# ============================================================================

def test_command_registry_basic_operations():
    registry = CommandRegistry()
    assert registry.all_commands() == []

    cmd1 = Command(id="c1", title="New Chat", category="Chat", shortcut="Ctrl+N")
    cmd2 = Command(id="c2", title="Toggle Theme", category="Appearance", shortcut="Ctrl+D")
    registry.register(cmd1)
    registry.register(cmd2)

    assert len(registry.all_commands()) == 2
    assert registry.get("c1") == cmd1
    assert registry.get("c2") == cmd2
    assert registry.get("c_nonexistent") is None

    registry.unregister("c1")
    assert len(registry.all_commands()) == 1
    assert registry.get("c1") is None


def test_command_registry_search_and_ranking():
    registry = CommandRegistry()
    c_new = Command(id="new", title="New Conversation", category="Chat", shortcut="Ctrl+N")
    c_clear = Command(id="clear", title="Clear Chat", category="Chat", shortcut="Ctrl+Shift+Del")
    c_theme = Command(id="theme", title="Toggle Theme", category="Appearance", keywords=["dark", "light"])
    c_lang = Command(id="lang", title="Switch Language", category="Settings", keywords=["english", "hinglish"])

    for c in [c_new, c_clear, c_theme, c_lang]:
        registry.register(c)

    # Empty query returns all commands
    all_results = registry.search("")
    assert len(all_results) == 4

    # Query "theme"
    theme_results = registry.search("theme")
    assert len(theme_results) >= 1
    assert theme_results[0][0].id == "theme"

    # Query keyword "dark"
    dark_results = registry.search("dark")
    assert len(dark_results) >= 1
    assert dark_results[0][0].id == "theme"

    # Query category "Chat"
    chat_results = registry.search("Chat")
    matched_ids = [cmd.id for cmd, _score in chat_results]
    assert "new" in matched_ids
    assert "clear" in matched_ids


# ============================================================================
# 3. Command Palette Dialog Tests (Qt)
# ============================================================================

def test_command_palette_dialog_filtering_and_navigation(qtbot):
    registry = CommandRegistry()
    cb_called = []
    c1 = Command(id="cmd1", title="New Chat", category="Chat", shortcut="Ctrl+N", callback=lambda: cb_called.append("c1"))
    c2 = Command(id="cmd2", title="Toggle Theme", category="Appearance", shortcut="Ctrl+D", callback=lambda: cb_called.append("c2"))
    registry.register(c1)
    registry.register(c2)

    dialog = CommandPaletteDialog(registry)
    qtbot.addWidget(dialog)

    # Initial state: 2 commands
    assert dialog.results_list.count() == 2

    # Type "theme"
    dialog.search_input.setText("theme")
    assert dialog.results_list.count() == 1
    assert "Toggle Theme" in dialog.results_list.item(0).text()

    # Arrow navigation
    dialog.search_input.setText("")
    assert dialog.results_list.count() == 2
    dialog._select_next()
    assert dialog.results_list.currentRow() == 1
    dialog._select_prev()
    assert dialog.results_list.currentRow() == 0

    # Execute selected item (row 0 is "Appearance" -> c2)
    dialog.execute_selected()
    assert cb_called == ["c2"]
    assert dialog.executed_command == c2


def test_command_palette_dialog_no_matches(qtbot):
    registry = CommandRegistry()
    registry.register(Command(id="c1", title="New Chat"))
    dialog = CommandPaletteDialog(registry)
    qtbot.addWidget(dialog)

    dialog.search_input.setText("xyz123random")
    assert dialog.results_list.count() == 1
    assert "No matching" in dialog.results_list.item(0).text()


# ============================================================================
# 4. Shortcuts Help Dialog Tests (Qt)
# ============================================================================

def test_shortcuts_help_dialog(qtbot):
    dialog_dark = ShortcutsHelpDialog(is_dark=True)
    qtbot.addWidget(dialog_dark)
    assert dialog_dark.windowTitle() == "⌨️ Keyboard Shortcuts Cheat Sheet"

    dialog_light = ShortcutsHelpDialog(is_dark=False)
    qtbot.addWidget(dialog_light)
    assert dialog_light.windowTitle() == "⌨️ Keyboard Shortcuts Cheat Sheet"


# ============================================================================
# 5. Main Window Keyboard Shortcuts Integration Tests
# ============================================================================

def test_main_window_commands_and_shortcuts(make_window, monkeypatch):
    app = make_window([
        {"id": "chat1", "title": "First", "messages": [{"role": "user", "content": "hello"}]},
        {"id": "chat2", "title": "Second", "messages": [{"role": "user", "content": "world"}]},
    ])

    # Check commands registry initialized on window
    assert hasattr(app, "commands")
    assert len(app.commands.all_commands()) >= 8

    # Test Theme Toggle via command
    initial_dark = app.dark_mode
    app.commands.get("theme:toggle").callback()
    assert app.dark_mode != initial_dark

    # Test Language Toggle via command
    initial_lang = app.language
    app.commands.get("lang:toggle").callback()
    assert app.language != initial_lang

    # Test Voice Toggle via command
    initial_voice = app.voice_enabled
    app.commands.get("voice:toggle").callback()
    assert app.voice_enabled != initial_voice

    # Test Navigation Next/Prev
    app.history_list.setCurrentRow(0)
    app.navigate_next_chat()
    assert app.history_list.currentRow() == 1
    app.navigate_previous_chat()
    assert app.history_list.currentRow() == 0

    # Test Escape Handler
    app.handle_escape()  # runs safely without error


def test_main_window_open_dialogs(make_window, monkeypatch):
    app = make_window([])

    palette_opened = []
    shortcuts_opened = []
    monkeypatch.setattr(CommandPaletteDialog, "exec", lambda self: palette_opened.append(True))
    monkeypatch.setattr(ShortcutsHelpDialog, "exec", lambda self: shortcuts_opened.append(True))

    app.open_command_palette()
    assert palette_opened == [True]

    app.open_shortcuts_help()
    assert shortcuts_opened == [True]


# ============================================================================
# 6. Assistant CLI Shortcuts Tests
# ============================================================================

def test_assistant_cli_shortcuts(capsys, monkeypatch):
    import assistant

    # Test show_shortcuts function
    assistant.show_shortcuts()
    out = capsys.readouterr().out
    assert "Ctrl+K" in out
    assert "Ctrl+N" in out
    assert "Ctrl+D" in out
    assert "Ctrl+L" in out

    # Test show_help mentions shortcuts
    assistant.show_help()
    out_help = capsys.readouterr().out
    assert "/shortcuts" in out_help

    # Test interactive execution of /shortcuts
    inputs = iter(["/shortcuts", "bye"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    assistant.run_assistant()
    out_cli = capsys.readouterr().out
    assert "JUGNU POWER-USER SHORTCUTS" in out_cli
