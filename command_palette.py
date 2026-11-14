"""Global Command Palette, Fuzzy Action Launcher, and Keyboard Shortcuts System (#53).

Provides:
- Command dataclass with metadata, category, shortcut, and callback.
- High-performance fuzzy matching algorithm with exact, prefix, acronym, substring,
  and subsequence scoring.
- CommandRegistry for registering and querying commands.
- CommandPaletteDialog: Floating modal HUD with live search and arrow navigation.
- ShortcutsHelpDialog: Interactive cheat sheet displaying all keyboard shortcuts.

100% offline, zero-cost, standard library + PySide6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import re
from typing import Any, Callable, Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


@dataclass
class Command:
    """Represents an actionable command within JUGNU."""

    id: str
    title: str
    category: str = "General"
    shortcut: str = ""
    callback: Callable[[], Any] | None = None
    keywords: list[str] = field(default_factory=list)
    icon: str = "⚡"
    description: str = ""


def fuzzy_match(query: str, target: str, keywords: Sequence[str] = ()) -> float:
    """Calculate a relevance score for query against target text and keywords.

    Returns:
        float score >= 0.0. Higher is more relevant. 0.0 means no match.
    """
    q = query.strip().lower()
    if not q:
        return 1.0  # Empty query matches everything with baseline score

    t = target.strip().lower()
    if not t:
        return 0.0

    # 1. Exact match (highest priority)
    if q == t:
        return 100.0

    # 2. Target starts with query (prefix match)
    if t.startswith(q):
        return 90.0 + (len(q) / len(t)) * 5.0

    # 3. Word boundary / acronym match (e.g. "nt" -> "New Chat", "tt" -> "Toggle Theme")
    words = [w for w in re.split(r"[\s\-_/]+", t) if w]
    initials = "".join(w[0] for w in words)
    if q == initials or initials.startswith(q):
        return 85.0

    # Check if any word starts with query
    if any(w.startswith(q) for w in words):
        return 80.0

    # 4. Substring match
    if q in t:
        pos = t.find(q)
        pos_penalty = max(0.0, 10.0 - (pos * 0.5))
        return 65.0 + pos_penalty + (len(q) / len(t)) * 10.0

    # 5. Check keywords
    kw_score = 0.0
    for kw in keywords:
        k = kw.lower().strip()
        if q == k:
            kw_score = max(kw_score, 75.0)
        elif k.startswith(q):
            kw_score = max(kw_score, 65.0)
        elif q in k:
            kw_score = max(kw_score, 55.0)
    if kw_score > 0.0:
        return kw_score

    # 6. Subsequence match (all chars of query appear in order in target)
    t_idx = 0
    q_idx = 0
    while q_idx < len(q) and t_idx < len(t):
        if q[q_idx] == t[t_idx]:
            q_idx += 1
        t_idx += 1
    if q_idx == len(q):
        coverage = len(q) / len(t)
        return 40.0 + coverage * 20.0

    # 7. Fuzzy string similarity fallback (typo tolerance against full text or words)
    for w in words:
        w_ratio = difflib.SequenceMatcher(None, q, w).ratio()
        if w_ratio >= 0.7:
            return 25.0 + w_ratio * 15.0

    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    if ratio >= 0.65:
        return 20.0 + ratio * 20.0

    return 0.0


class CommandRegistry:
    """Central registry holding all executable commands."""

    def __init__(self):
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        """Register a command."""
        self._commands[command.id] = command

    def unregister(self, command_id: str) -> None:
        """Unregister a command by ID."""
        self._commands.pop(command_id, None)

    def get(self, command_id: str) -> Command | None:
        """Look up command by ID."""
        return self._commands.get(command_id)

    def all_commands(self) -> list[Command]:
        """Return all registered commands."""
        return list(self._commands.values())

    def clear(self) -> None:
        """Clear all registered commands."""
        self._commands.clear()

    def search(self, query: str = "") -> list[tuple[Command, float]]:
        """Search commands using fuzzy matching, ordered by relevance score."""
        q = query.strip()
        if not q:
            # Group by category, then title
            sorted_cmds = sorted(
                self._commands.values(),
                key=lambda c: (c.category, c.title),
            )
            return [(cmd, 1.0) for cmd in sorted_cmds]

        scored: list[tuple[Command, float]] = []
        for cmd in self._commands.values():
            score = fuzzy_match(q, cmd.title, cmd.keywords)
            # Also check category match
            if score == 0.0:
                cat_score = fuzzy_match(q, cmd.category)
                if cat_score > 0.0:
                    score = cat_score * 0.7
            if score > 0.0:
                scored.append((cmd, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored


class PaletteSearchInput(QLineEdit):
    """Custom search input that emits arrow key signals to navigate the list."""

    up_pressed = Signal()
    down_pressed = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Up:
            self.up_pressed.emit()
            return
        if event.key() == Qt.Key_Down:
            self.down_pressed.emit()
            return
        super().keyPressEvent(event)


class CommandPaletteDialog(QDialog):
    """Interactive floating Command Palette (Ctrl+K) modal dialog."""

    def __init__(self, registry: CommandRegistry, parent: QWidget | None = None):
        super().__init__(parent)
        self.registry = registry
        self.executed_command: Command | None = None

        self.setObjectName("commandPalette")
        self.setWindowTitle("Command Palette")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.resize(600, 420)

        self._build_ui()
        self._populate_list("")

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Header Search Input
        self.search_input = PaletteSearchInput(self)
        self.search_input.setObjectName("paletteSearch")
        self.search_input.setPlaceholderText("> Type a command or search actions... (Esc to close)")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_text_changed)
        self.search_input.returnPressed.connect(self.execute_selected)
        self.search_input.up_pressed.connect(self._select_prev)
        self.search_input.down_pressed.connect(self._select_next)
        layout.addWidget(self.search_input)

        # Results List
        self.results_list = QListWidget(self)
        self.results_list.setObjectName("paletteList")
        self.results_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.results_list, 1)

        # Footer shortcut tips
        self.footer = QLabel("↑ / ↓ to navigate  •  Enter to run  •  Esc to close", self)
        self.footer.setObjectName("paletteFooter")
        layout.addWidget(self.footer)

        # Shortcut for escape
        QShortcut(QKeySequence("Escape"), self, self.reject)

    def _populate_list(self, query: str):
        self.results_list.clear()
        results = self.registry.search(query)

        if not results:
            item = QListWidgetItem("No matching commands found")
            item.setFlags(Qt.NoItemFlags)
            self.results_list.addItem(item)
            return

        for cmd, _score in results:
            shortcut_badge = f"    [{cmd.shortcut}]" if cmd.shortcut else ""
            display_text = f"{cmd.icon}  {cmd.title}  ({cmd.category}){shortcut_badge}"
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, cmd.id)
            if cmd.description:
                item.setToolTip(cmd.description)
            self.results_list.addItem(item)

        if self.results_list.count() > 0:
            self.results_list.setCurrentRow(0)

    def _on_text_changed(self, text: str):
        self._populate_list(text)

    def _select_prev(self):
        count = self.results_list.count()
        if count <= 0:
            return
        row = self.results_list.currentRow()
        new_row = row - 1 if row > 0 else count - 1
        self.results_list.setCurrentRow(new_row)

    def _select_next(self):
        count = self.results_list.count()
        if count <= 0:
            return
        row = self.results_list.currentRow()
        new_row = row + 1 if row < count - 1 else 0
        self.results_list.setCurrentRow(new_row)

    def _on_item_double_clicked(self, _item):
        self.execute_selected()

    def execute_selected(self):
        """Execute the highlighted command and close dialog."""
        current_item = self.results_list.currentItem()
        if not current_item:
            return

        cmd_id = current_item.data(Qt.UserRole)
        if not cmd_id:
            return

        cmd = self.registry.get(cmd_id)
        if cmd:
            self.executed_command = cmd
            self.accept()
            if cmd.callback:
                cmd.callback()


class ShortcutsHelpDialog(QDialog):
    """Interactive cheat sheet displaying all JUGNU keyboard shortcuts."""

    def __init__(self, parent: QWidget | None = None, is_dark: bool = True):
        super().__init__(parent)
        self.setObjectName("shortcutsHelp")
        self.setWindowTitle("⌨️ Keyboard Shortcuts Cheat Sheet")
        self.setModal(True)
        self.resize(580, 520)

        self._build_ui(is_dark)

    def _build_ui(self, is_dark: bool):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QLabel("⚡ JUGNU Power-User Keyboard Shortcuts", self)
        header.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(header)

        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(False)

        bg = "#211D35" if is_dark else "#FFFFFF"
        text_color = "#F4EEFF" if is_dark else "#27213B"
        border_color = "#463A66" if is_dark else "#D9CFF5"
        kbd_bg = "#2E2749" if is_dark else "#F0EAFB"
        kbd_border = "#6A53A8" if is_dark else "#8B5CF6"
        accent = "#8B6CF0" if is_dark else "#8B5CF6"

        html_content = f"""
        <html>
        <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background-color: {bg};
                color: {text_color};
                font-size: 13px;
                line-height: 1.5;
                margin: 0;
                padding: 10px;
            }}
            h3 {{
                color: {accent};
                margin-top: 14px;
                margin-bottom: 6px;
                font-size: 14px;
                border-bottom: 1px solid {border_color};
                padding-bottom: 4px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-bottom: 10px;
            }}
            td {{
                padding: 6px 8px;
                vertical-align: middle;
            }}
            td.shortcut {{
                width: 170px;
                text-align: right;
            }}
            kbd {{
                background-color: {kbd_bg};
                border: 1px solid {kbd_border};
                border-radius: 4px;
                box-shadow: 0 1px 2px rgba(0,0,0,0.1);
                color: {text_color};
                display: inline-block;
                font-family: Consolas, Monaco, monospace;
                font-size: 11px;
                padding: 2px 6px;
                font-weight: 600;
            }}
        </style>
        </head>
        <body>
            <h3>🧭 Navigation & Command Palette</h3>
            <table>
                <tr><td>Open Command Palette</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>K</kbd></td></tr>
                <tr><td>New Conversation</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>N</kbd></td></tr>
                <tr><td>Focus Search</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>F</kbd></td></tr>
                <tr><td>Previous Conversation</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>↑</kbd></td></tr>
                <tr><td>Next Conversation</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>↓</kbd></td></tr>
                <tr><td>Close Dialog / Cancel</td><td class="shortcut"><kbd>Esc</kbd></td></tr>
            </table>

            <h3>🎨 Appearance & Language</h3>
            <table>
                <tr><td>Toggle Dark / Light Theme</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>D</kbd></td></tr>
                <tr><td>Switch Language (En / Hinglish)</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>L</kbd></td></tr>
            </table>

            <h3>🔊 Voice & Accessibility</h3>
            <table>
                <tr><td>Toggle Voice Output</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>S</kbd></td></tr>
                <tr><td>Open Voice Settings</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>,</kbd></td></tr>
            </table>

            <h3>💬 Chat & Messaging</h3>
            <table>
                <tr><td>Send Message</td><td class="shortcut"><kbd>Enter</kbd></td></tr>
                <tr><td>Insert Newline</td><td class="shortcut"><kbd>Shift</kbd> + <kbd>Enter</kbd></td></tr>
            </table>

            <h3>❓ Help & System</h3>
            <table>
                <tr><td>Keyboard Shortcuts Cheat Sheet</td><td class="shortcut"><kbd>F1</kbd> or <kbd>Ctrl</kbd> + <kbd>/</kbd></td></tr>
                <tr><td>Quit Application</td><td class="shortcut"><kbd>Ctrl</kbd> + <kbd>Q</kbd></td></tr>
            </table>
        </body>
        </html>
        """
        browser.setHtml(html_content)
        layout.addWidget(browser, 1)

        close_btn = QPushButton("Close (Esc)", self)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        QShortcut(QKeySequence("Escape"), self, self.accept)
