"""JUGNU's desktop window, built with PySide6 and themed with Qt Style Sheets.

All the logic (history, settings, local answers, the Ollama client) lives in jugnu_core.py;
this file only draws it. The theme is in light.qss and dark.qss.
Run it with:  python desktop_app.py
"""
import math
import os
import sys

from PySide6.QtCore import QEvent, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QKeySequence, QPixmap, QShortcut, QTextOption
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QTextBrowser, QVBoxLayout,
                               QWidget)

import jugnu_core as core
from command_palette import Command, CommandPaletteDialog, CommandRegistry, ShortcutsHelpDialog
from export_manager import export_chat
from history_lock import HistoryInUseError, HistoryLock
from markdown_render import plain_text, render_html
from search_engine import SearchEngine
from strings import get_string, next_language
from tts import RATE_MAX, RATE_MIN, TTSManager

HERE = os.path.dirname(os.path.abspath(__file__))
BUBBLE_PT = 10        # chat text size, in points
COPY_TEXT = "⧉ Copy"
COPIED_TEXT = "✓ Copied!"


def load_stylesheet(dark_mode):
    """The whole theme: dark.qss or light.qss, from beside this file."""
    with open(os.path.join(HERE, "dark.qss" if dark_mode else "light.qss"), encoding="utf-8") as f:
        return f.read()


def max_bubble_width(viewport_width):
    """Widest a bubble may be, keeping it inside the chat area with room to show who sent it."""
    return max(40, min(720, viewport_width - 100, int(viewport_width * 0.72)))


def repolish(widget):
    """Re-apply the style sheet after a property used by a selector (variant, state) changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Task(QThread):
    """Runs one blocking call (an Ollama request) off the GUI thread.

    `done` is emitted from the worker thread; Qt queues it, so the connected slot runs on the
    GUI thread. The work itself never touches a widget.
    """
    done = Signal(object)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work

    def run(self):
        self.done.emit(self.work())


class InputBox(QPlainTextEdit):
    """The message box. Enter sends; Shift+Enter starts a new line."""
    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class BubbleText(QTextBrowser):
    """A message's text: read-only, selectable, as tall as its content and never scrolling itself."""
    copy_all = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("bubbleText")
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Long words and URLs wrap anywhere rather than running out of the bubble.
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def fit(self, max_width):
        """Size to the text: as narrow as it allows, at most `max_width`, and exactly as tall."""
        # Measure a copy: the widget lays out its own document at its current width, not ours.
        doc = self.document().clone(self)
        doc.setTextWidth(max_width)
        width = min(max_width, math.ceil(doc.idealWidth()) + 2)
        doc.setTextWidth(width)
        self.setFixedSize(width, math.ceil(doc.size().height()) + 2)
        doc.deleteLater()

    def wheelEvent(self, event):
        event.ignore()  # let the chat area scroll instead

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu(event.pos())
        menu.addAction("Copy All", self.copy_all.emit)
        menu.exec(event.globalPos())


class Bubble(QFrame):
    """One chat message: name, time, Copy (and Read aloud for replies) above the text."""

    def __init__(self, app, role, text, timestamp=None):
        super().__init__()
        self.app, self.text = app, text
        is_user = role == "user"
        self.setObjectName("bubble")
        self.setProperty("role", "user" if is_user else "assistant")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.codes = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.name_label = QLabel(app.t("you_label") if is_user else "🤖 JUGNU")
        self.name_label.setObjectName("bubbleName")
        header.addWidget(self.name_label)
        self.time_label = None
        if timestamp:
            self.time_label = QLabel(timestamp)
            self.time_label.setObjectName("bubbleTime")
            header.addWidget(self.time_label)
        header.addStretch(1)
        self.copy_btn = self.small_button(COPY_TEXT, self.copy_message)
        header.addWidget(self.copy_btn)
        self.read_btn = None
        if not is_user:
            self.read_btn = self.small_button("🔊", lambda: app.read_aloud(text))
            self.read_btn.setEnabled(app.tts.available)
            header.addWidget(self.read_btn)
        layout.addLayout(header)

        self.body = BubbleText(self)
        self.body.setFont(app.bubble_font())
        if is_user:
            self.body.setPlainText(text)  # what you type is always shown exactly as typed
        else:
            self.show_markdown(app.t("code_copy_btn"))
        self.body.anchorClicked.connect(self.on_link)
        self.body.copy_all.connect(self.copy_message)
        layout.addWidget(self.body)

    def small_button(self, text, slot):
        button = QPushButton(text)
        button.setProperty("variant", "ghost")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def show_markdown(self, copy_label):
        """Show the reply as styled Markdown. If anything goes wrong, show the plain message instead."""
        try:
            html, self.codes = render_html(self.text, self.app.colors, mono=self.app.mono_family(),
                                           base_pt=BUBBLE_PT, copy_label=copy_label)
            self.body.setHtml(html)
        except Exception:
            self.codes = []
            self.body.setPlainText(self.text)

    def fit(self, max_width):
        self.body.fit(max_width)

    def copy_message(self):
        QApplication.clipboard().setText(self.text)
        self.copy_btn.setText(COPIED_TEXT)
        self.copy_btn.setProperty("state", "copied")
        repolish(self.copy_btn)
        QTimer.singleShot(1500, self, self.reset_copy_button)

    def reset_copy_button(self):
        self.copy_btn.setText(COPY_TEXT)
        self.copy_btn.setProperty("state", "")
        repolish(self.copy_btn)

    def on_link(self, url):
        # Only the code blocks' own "Copy" links do anything; other links are just shown.
        target = url.toString()
        if target.startswith("copy:"):
            index = int(target[5:])
            if 0 <= index < len(self.codes):
                QApplication.clipboard().setText(self.codes[index])
                self.show_markdown(self.app.t("code_copied_btn"))
                QTimer.singleShot(1500, self, lambda: self.show_markdown(self.app.t("code_copy_btn")))


class VoiceSettingsDialog(QDialog):
    """Speed, volume and voice. Changes are heard from the next sentence and saved as you go."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setModal(False)
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 18)
        layout.setSpacing(6)

        self.rate_label = self.field_label()
        self.rate = QSlider(Qt.Horizontal)
        self.rate.setRange(RATE_MIN, RATE_MAX)
        self.rate.setSingleStep(5)
        self.rate.setPageStep(25)
        self.volume_label = self.field_label()
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setSingleStep(5)
        self.voice_label = self.field_label()
        self.voice = QComboBox()
        self.test_btn = QPushButton()
        self.close_btn = QPushButton()
        self.close_btn.setProperty("variant", "danger")
        for widget in (self.rate_label, self.rate, self.volume_label, self.volume,
                       self.voice_label, self.voice):
            layout.addWidget(widget)
        layout.addSpacing(12)
        layout.addWidget(self.test_btn)
        layout.addWidget(self.close_btn)

        # Set the starting positions first, then connect, so opening the window never rewrites the saved values.
        self.rate.setValue(app.voice_rate)
        self.volume.setValue(round(app.voice_volume * 100))
        self.rate.valueChanged.connect(self.on_rate)
        self.volume.valueChanged.connect(self.on_volume)
        self.rate.sliderReleased.connect(app.save_settings)
        self.volume.sliderReleased.connect(app.save_settings)
        self.voice.activated.connect(self.on_voice)
        self.test_btn.clicked.connect(app.test_voice)
        self.close_btn.clicked.connect(self.close)
        self.retranslate()

    @staticmethod
    def field_label():
        label = QLabel()
        label.setProperty("variant", "field")
        return label

    def voice_choices(self):
        """Dropdown entries: the default first, then every installed voice (names made unique)."""
        choices, seen = [(self.app.t("voice_default_name"), None)], {}
        for vid, name in self.app.tts.voices:
            seen[name] = seen.get(name, 0) + 1
            choices.append((name if seen[name] == 1 else f"{name} ({seen[name]})", vid))
        return choices

    def retranslate(self):
        app = self.app
        self.setWindowTitle(app.t("voice_settings_title"))
        self.update_value_labels()
        self.voice_label.setText(app.t("voice_choice_label"))
        self.test_btn.setText(app.t("voice_test_btn"))
        self.close_btn.setText(app.t("voice_close_btn"))
        # The default entry's name depends on the language, so rebuild the list.
        self.voice.clear()
        for label, vid in self.voice_choices():
            self.voice.addItem(label, vid)
        # A saved voice that is no longer installed shows (and plays) as the default.
        index = self.voice.findData(app.voice_id) if app.voice_id else 0
        self.voice.setCurrentIndex(max(index, 0))

    def update_value_labels(self):
        self.rate_label.setText(self.app.t("voice_rate_label", value=self.app.voice_rate))
        self.volume_label.setText(self.app.t("voice_volume_label", value=round(self.app.voice_volume * 100)))

    def on_rate(self, value):
        self.app.voice_rate = int(value)
        self.app.push_voice_settings()
        self.update_value_labels()

    def on_volume(self, value):
        self.app.voice_volume = int(value) / 100
        self.app.push_voice_settings()
        self.update_value_labels()

    def on_voice(self, index):
        self.app.voice_id = self.voice.itemData(index)
        self.app.push_voice_settings()
        self.app.save_settings()

    def closeEvent(self, event):
        self.app.save_settings()
        super().closeEvent(event)


class JugnuWindow(QMainWindow):
    def __init__(self, history_file=core.HISTORY_FILE, settings_file=core.SETTINGS_FILE):
        super().__init__()
        self.history_file, self.settings_file = history_file, settings_file
        self.setWindowTitle(core.APP_TITLE)
        self.resize(1200, 760)
        self.setMinimumSize(850, 570)

        self.chats = core.load_history(history_file)
        self.current_id = None
        self.draft_chat = None  # Unsaved empty chat; saved only after first message.
        self.busy = False
        self.dark_mode = core.load_dark_mode(settings_file)
        self.voice_enabled = core.load_voice_enabled(settings_file)
        self.language = core.load_language(settings_file)
        self.voice_rate, self.voice_volume, self.voice_id = core.load_voice_prefs(settings_file)
        self.tts = TTSManager(rate=self.voice_rate, volume=self.voice_volume,
                              voice_id=self.voice_id)  # Offline voice output; runs on its own thread.
        self.voice_dialog = None
        self.palette_dialog = None
        self.shortcuts_dialog = None
        self.commands = CommandRegistry()
        self._status = ("status_ready", {})  # Remembered so the status line can be re-translated.
        self._mono = None
        self.bubbles = []
        self._stick_to_bottom = False

        self.tasks = []  # background requests that are still running
        self.closed = False

        self.build_ui()
        if self.chats:
            self.current_id = self.chats[-1]["id"]  # Reopen most recent saved conversation.
        else:
            self.make_draft()
        self.apply_theme()
        self.apply_language()

        self.resize_timer = QTimer(self, singleShot=True, interval=60)
        self.resize_timer.timeout.connect(self.fit_bubbles)
        self.speech_timer = QTimer(self, interval=200)
        self.speech_timer.timeout.connect(self.poll_speech)
        self.speech_timer.start()
        QTimer.singleShot(250, self, self.warm_up)

    # ---------- Layout ----------
    def build_ui(self):
        # Visible text is filled in by apply_language(), so it can be switched at runtime.
        root = QWidget()
        self.setCentralWidget(root)
        main = QHBoxLayout(root)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(230)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 12)
        side.setSpacing(8)
        side.addWidget(QLabel("🤖  JUGNU", objectName="brand"))
        side.addWidget(QLabel("Just an Unusually Genius\nNeural Utility", objectName="tagline"))
        side.addSpacing(10)
        self.new_btn = self.sidebar_button(side, self.new_chat)
        self.palette_btn = self.sidebar_button(side, self.open_command_palette)
        side.addSpacing(10)
        self.history_label = QLabel(objectName="historyLabel")
        side.addWidget(self.history_label)
        self.search_entry = QLineEdit(objectName="searchEntry")
        self.search_entry.setClearButtonEnabled(True)
        self.search_entry.textChanged.connect(self.on_search_query_changed)
        side.addWidget(self.search_entry)
        self.history_list = QListWidget(objectName="historyList")
        self.history_list.itemClicked.connect(self.select_chat)
        self.history_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self.show_chat_menu)
        side.addWidget(self.history_list, 1)
        self.clear_btn = self.sidebar_button(side, self.clear_chat)
        self.clear_btn.setProperty("variant", "danger")
        self.voice_btn = self.sidebar_button(side, self.toggle_voice)
        self.voice_settings_btn = self.sidebar_button(side, self.open_voice_settings)
        self.language_btn = self.sidebar_button(side, self.toggle_language)
        self.theme_btn = self.sidebar_button(side, self.toggle_theme)
        if not self.tts.available:
            self.voice_btn.setEnabled(False)  # pyttsx3 not installed
            self.voice_settings_btn.setEnabled(False)
        main.addWidget(sidebar)

        content = QWidget(objectName="content")
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        header = QFrame(objectName="header")
        header.setFixedHeight(64)
        head = QHBoxLayout(header)
        head.setContentsMargins(22, 0, 18, 0)
        self.header_title = QLabel(objectName="headerTitle")
        head.addWidget(self.header_title)
        head.addStretch(1)
        # Shown only while speech is playing (see poll_speech).
        self.stop_btn = QPushButton("⏹  Stop")
        self.stop_btn.setProperty("variant", "stop")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self.stop_speaking)
        self.stop_btn.hide()
        head.addWidget(self.stop_btn)
        self.status = QLabel(objectName="status")
        head.addWidget(self.status)
        column.addWidget(header)

        self.scroll = QScrollArea(objectName="chatScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.messages = QWidget(objectName="messages")
        self.messages_layout = QVBoxLayout(self.messages)
        self.messages_layout.setContentsMargins(20, 16, 20, 16)
        self.messages_layout.setSpacing(14)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.verticalScrollBar().rangeChanged.connect(self.on_scroll_range)
        column.addWidget(self.scroll, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(12, 0, 12, 12)
        bottom.setSpacing(10)
        self.input_box = InputBox()
        self.input_box.setFixedHeight(64)
        self.input_box.submitted.connect(self.send_message)
        bottom.addWidget(self.input_box, 1)
        self.send_btn = QPushButton()
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setMinimumHeight(48)
        self.send_btn.clicked.connect(self.send_message)
        bottom.addWidget(self.send_btn)
        column.addLayout(bottom)
        main.addWidget(content, 1)

        QShortcut(QKeySequence("Ctrl+N"), self, self.new_chat)
        QShortcut(QKeySequence("Ctrl+F"), self, self.focus_search)
        QShortcut(QKeySequence("Ctrl+K"), self, self.open_command_palette)
        QShortcut(QKeySequence("Ctrl+D"), self, self.toggle_theme)
        QShortcut(QKeySequence("Ctrl+L"), self, self.toggle_language)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, self.toggle_voice)
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_voice_settings)
        QShortcut(QKeySequence("Ctrl+Up"), self, self.navigate_previous_chat)
        QShortcut(QKeySequence("Ctrl+Down"), self, self.navigate_next_chat)
        QShortcut(QKeySequence("F1"), self, self.open_shortcuts_help)
        QShortcut(QKeySequence("Ctrl+/"), self, self.open_shortcuts_help)
        QShortcut(QKeySequence("Escape"), self, self.handle_escape)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)

    def focus_search(self):
        self.search_entry.setFocus()
        self.search_entry.selectAll()

    def on_search_query_changed(self, _text):
        self.refresh_history_list()

    def open_command_palette(self):
        """Open the interactive command palette HUD modal."""
        self.register_commands()
        dialog = CommandPaletteDialog(self.commands, self)
        self.palette_dialog = dialog
        dialog.exec()

    def open_shortcuts_help(self):
        """Open the keyboard shortcuts cheat sheet dialog."""
        dialog = ShortcutsHelpDialog(self, is_dark=self.dark_mode)
        self.shortcuts_dialog = dialog
        dialog.exec()

    def register_commands(self):
        """Populate the command registry with localized application commands."""
        self.commands.clear()
        self.commands.register(Command(
            id="chat:new",
            title=self.t("cmd_new_chat"),
            category=self.t("cat_chat"),
            shortcut="Ctrl+N",
            callback=self.new_chat,
            keywords=["new", "create", "start", "blank", "chat", "nayi"],
            icon="➕",
        ))
        self.commands.register(Command(
            id="chat:clear",
            title=self.t("cmd_clear_chat"),
            category=self.t("cat_chat"),
            shortcut="Ctrl+Shift+Delete",
            callback=self.clear_chat,
            keywords=["clear", "delete", "empty", "remove", "saaf"],
            icon="🗑️",
        ))
        self.commands.register(Command(
            id="theme:toggle",
            title=self.t("cmd_toggle_theme"),
            category=self.t("cat_appearance"),
            shortcut="Ctrl+D",
            callback=self.toggle_theme,
            keywords=["theme", "dark", "light", "mode", "color", "appearance"],
            icon="🎨",
        ))
        self.commands.register(Command(
            id="lang:toggle",
            title=self.t("cmd_toggle_language"),
            category=self.t("cat_settings"),
            shortcut="Ctrl+L",
            callback=self.toggle_language,
            keywords=["language", "english", "hinglish", "switch", "translate", "bhasha"],
            icon="🌐",
        ))
        self.commands.register(Command(
            id="voice:toggle",
            title=self.t("cmd_toggle_voice"),
            category=self.t("cat_voice"),
            shortcut="Ctrl+Shift+S",
            callback=self.toggle_voice,
            keywords=["voice", "speech", "mute", "unmute", "sound", "awaaz"],
            icon="🔊",
        ))
        self.commands.register(Command(
            id="voice:settings",
            title=self.t("cmd_voice_settings"),
            category=self.t("cat_voice"),
            shortcut="Ctrl+,",
            callback=self.open_voice_settings,
            keywords=["voice", "settings", "rate", "speed", "volume"],
            icon="⚙️",
        ))
        self.commands.register(Command(
            id="nav:prev_chat",
            title=self.t("cmd_prev_chat"),
            category=self.t("cat_navigation"),
            shortcut="Ctrl+Up",
            callback=self.navigate_previous_chat,
            keywords=["previous", "up", "back", "earlier", "pichli"],
            icon="⬆️",
        ))
        self.commands.register(Command(
            id="nav:next_chat",
            title=self.t("cmd_next_chat"),
            category=self.t("cat_navigation"),
            shortcut="Ctrl+Down",
            callback=self.navigate_next_chat,
            keywords=["next", "down", "forward", "later", "agli"],
            icon="⬇️",
        ))
        self.commands.register(Command(
            id="help:shortcuts",
            title=self.t("cmd_shortcuts_help"),
            category=self.t("cat_help"),
            shortcut="F1",
            callback=self.open_shortcuts_help,
            keywords=["shortcuts", "hotkeys", "keys", "help", "cheat sheet"],
            icon="⌨️",
        ))
        self.commands.register(Command(
            id="app:quit",
            title=self.t("cmd_quit"),
            category=self.t("cat_app"),
            shortcut="Ctrl+Q",
            callback=self.close,
            keywords=["quit", "exit", "close", "bye", "band"],
            icon="🚪",
        ))

    def navigate_previous_chat(self):
        """Switch to previous conversation in the history list."""
        count = self.history_list.count()
        if count <= 1:
            return
        row = self.history_list.currentRow()
        new_row = row - 1 if row > 0 else count - 1
        item = self.history_list.item(new_row)
        if item:
            self.history_list.setCurrentItem(item)
            self.select_chat(item)

    def navigate_next_chat(self):
        """Switch to next conversation in the history list."""
        count = self.history_list.count()
        if count <= 1:
            return
        row = self.history_list.currentRow()
        new_row = row + 1 if row < count - 1 else 0
        item = self.history_list.item(new_row)
        if item:
            self.history_list.setCurrentItem(item)
            self.select_chat(item)

    def handle_escape(self):
        """Global escape key handler."""
        if getattr(self, "palette_dialog", None) and self.palette_dialog.isVisible():
            self.palette_dialog.reject()
            return
        if getattr(self, "shortcuts_dialog", None) and self.shortcuts_dialog.isVisible():
            self.shortcuts_dialog.reject()
            return
        if getattr(self, "voice_dialog", None) and self.voice_dialog.isVisible():
            self.voice_dialog.reject()
            return
        if self.tts.speaking:
            self.stop_speaking()
            return
        if getattr(self, "is_streaming", False):
            self.stop_generating()
            return

    def sidebar_button(self, layout, slot):
        button = QPushButton()
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        layout.addWidget(button)
        return button

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Resize:
            self.resize_timer.start()  # re-fit bubbles once resizing settles
        return super().eventFilter(watched, event)

    def on_scroll_range(self, _minimum, maximum):
        if self._stick_to_bottom:
            self.scroll.verticalScrollBar().setValue(maximum)

    def scroll_to_bottom(self):
        # The new size is only known after the layout runs, so keep following it for a moment.
        self._stick_to_bottom = True
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())
        QTimer.singleShot(300, self, self.stop_sticking)

    def stop_sticking(self):
        self._stick_to_bottom = False

    # ---------- Theme and language ----------
    def bubble_font(self):
        font = self.font()
        font.setPointSize(BUBBLE_PT)
        return font

    def mono_family(self):
        if self._mono is None:
            self._mono = QFontDatabase.systemFont(QFontDatabase.FixedFont).family()
        return self._mono

    @property
    def colors(self):
        return core.DARK if self.dark_mode else core.LIGHT

    def apply_theme(self):
        QApplication.instance().setStyleSheet(load_stylesheet(self.dark_mode))
        self.theme_btn.setText(self.theme_button_text())
        self.display_chat()  # reply bubbles carry theme colours in their rich text

    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.save_settings()
        self.apply_theme()

    def t(self, key, **kwargs):
        """Look up a user-facing string in the currently selected language."""
        return get_string(self.language, key, **kwargs)

    def voice_button_text(self):
        return self.t("voice_on" if self.voice_enabled else "voice_off")

    def theme_button_text(self):
        return self.t("theme_to_light" if self.dark_mode else "theme_to_dark")

    def toggle_language(self):
        self.language = next_language(self.language)
        self.save_settings()
        self.apply_language()

    def apply_language(self):
        # Set every piece of visible text from the strings dictionary.
        self.new_btn.setText(self.t("new_chat_btn"))
        if hasattr(self, "palette_btn"):
            self.palette_btn.setText(self.t("command_palette_btn"))
        self.history_label.setText(self.t("history_label"))
        self.search_entry.setPlaceholderText(self.t("search_placeholder"))
        self.clear_btn.setText(self.t("clear_chat_btn"))
        self.voice_btn.setText(self.voice_button_text())
        self.voice_settings_btn.setText(self.t("voice_settings_btn"))
        self.language_btn.setText(self.t("language_btn", name=self.t("language_name")))
        self.theme_btn.setText(self.theme_button_text())
        self.header_title.setText(self.t("header_title"))
        self.send_btn.setText(self.t("send_btn"))
        if hasattr(self, "commands"):
            self.register_commands()
        if self.voice_dialog is not None:
            self.voice_dialog.retranslate()
        key, kwargs = self._status
        self.set_status(key, **kwargs)
        self.display_chat()  # Redraw so the welcome screen and "You" labels switch too.

    def set_status(self, key, **kwargs):
        # Store the key (not the text) so a language switch can re-translate the status line.
        self._status = (key, kwargs)
        self.status.setText("• " + self.t(key, **kwargs))

    def save_settings(self):
        core.save_settings(self.settings_file, dark_mode=self.dark_mode,
                           voice_enabled=self.voice_enabled, language=self.language,
                           voice_rate=self.voice_rate, voice_volume=self.voice_volume,
                           voice_id=self.voice_id)

    def save_history(self):
        try:
            core.save_history(self.chats, self.history_file)
        except (OSError, TypeError, ValueError) as error:
            self.set_status("status_save_error", error=error)

    # ---------- Voice ----------
    def toggle_voice(self):
        self.voice_enabled = not self.voice_enabled
        if not self.voice_enabled:
            self.tts.stop()  # Muting silences the current reply immediately.
        self.voice_btn.setText(self.voice_button_text())
        self.save_settings()

    def stop_speaking(self):
        self.tts.stop()
        self.poll_speech()  # hide the Stop button right away

    def read_aloud(self, text):
        # Reads exactly this message. Works even when the sidebar Voice toggle is off,
        # because the user asked for it explicitly. Replaces anything already being read.
        if not self.tts.available:
            return
        self.tts.stop()
        self.tts.speak(plain_text(text))  # without ** # ` and other Markdown symbols
        self.poll_speech()

    def test_voice(self):
        # Plays a short sample with the current settings, even if the Voice toggle is off.
        if not self.tts.available:
            return
        self.tts.stop()
        self.tts.speak(self.t("voice_test_sample"))
        self.poll_speech()

    def poll_speech(self):
        # Keep the Stop button and the Read aloud buttons in step with the speech engine.
        self.stop_btn.setVisible(bool(self.tts.speaking))
        available = bool(self.tts.available)
        self.voice_settings_btn.setEnabled(available)
        for bubble in self.bubbles:
            if bubble.read_btn is not None:
                bubble.read_btn.setEnabled(available)

    def push_voice_settings(self):
        # Hand the current values to the speech thread. Cheap, so it is safe on every slider tick.
        self.tts.apply_settings(self.voice_rate, self.voice_volume, self.voice_id)

    def open_voice_settings(self):
        if not self.tts.available:
            return
        if self.voice_dialog is None:
            self.voice_dialog = VoiceSettingsDialog(self)
        self.voice_dialog.show()
        self.voice_dialog.raise_()
        self.voice_dialog.activateWindow()

    # ---------- Chats ----------
    def make_draft(self):
        self.draft_chat = core.new_chat()
        self.current_id = self.draft_chat["id"]

    def current_chat(self):
        if self.draft_chat and self.draft_chat.get("id") == self.current_id:
            return self.draft_chat
        return self.find_chat(self.current_id)

    def find_chat(self, chat_id):
        return next((c for c in self.chats if c.get("id") == chat_id), None)

    def refresh_history_list(self):
        self.history_list.clear()
        query = self.search_entry.text().strip() if hasattr(self, "search_entry") else ""
        if not query:
            chats_to_show = list(reversed(self.chats))
            self.history_label.setText(self.t("history_label"))
        else:
            engine = SearchEngine(self.chats)
            results = engine.search(query)
            seen_ids = set()
            ordered_chats = []
            for r in results:
                if r.chat_id not in seen_ids:
                    seen_ids.add(r.chat_id)
                    chat = self.find_chat(r.chat_id)
                    if chat:
                        ordered_chats.append(chat)
            chats_to_show = ordered_chats
            if chats_to_show:
                self.history_label.setText(self.t("search_matches_count", count=len(chats_to_show)))
            else:
                self.history_label.setText(self.t("search_no_matches"))

        for chat in chats_to_show:  # newest first or ranked by search
            item = QListWidgetItem(core.list_title(chat.get("title", core.NEW_CHAT_TITLE)))
            item.setData(Qt.UserRole, chat["id"])
            item.setToolTip(chat.get("title", ""))
            self.history_list.addItem(item)
            if chat["id"] == self.current_id:
                item.setSelected(True)
                self.history_list.setCurrentItem(item)

    def new_chat(self):
        # A blank chat is temporary. It must not create a history entry until a message is sent.
        self.tts.stop()
        self.make_draft()
        self.refresh_history_list()
        self.display_chat()
        self.input_box.setFocus()

    def select_chat(self, item):
        chat_id = item.data(Qt.UserRole)
        if self.find_chat(chat_id) is not None and chat_id != self.current_id:
            self.tts.stop()
            self.current_id = chat_id
            self.draft_chat = None
            self.display_chat()

    def show_chat_menu(self, pos):
        item = self.history_list.itemAt(pos)
        if item is None:
            return None
        chat_id = item.data(Qt.UserRole)  # the menu acts on the chat that was clicked, not on its row
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.addAction(self.t("chat_menu_export"), lambda: self.export_chat_dialog(chat_id))
        menu.addSeparator()
        menu.addAction(self.t("chat_menu_rename"), lambda: self.rename_chat(chat_id))
        menu.addAction(self.t("chat_menu_delete"), lambda: self.delete_chat(chat_id))
        menu.popup(self.history_list.viewport().mapToGlobal(pos))
        return menu

    def export_chat_dialog(self, chat_id):
        chat = self.find_chat(chat_id)
        if not chat:
            return
        default_title = chat.get("title", "chat")
        clean_title = "".join(c for c in default_title if c.isalnum() or c in (" ", "_", "-")).strip() or "chat"
        default_file = os.path.join(os.path.expanduser("~"), f"{clean_title}.md")
        filter_str = "Markdown (*.md);;HTML (*.html);;JSON (*.json);;Plain Text (*.txt)"
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            self.t("export_dialog_title"),
            default_file,
            filter_str
        )
        if not file_path:
            return
        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        if not ext:
            if "html" in selected_filter.lower():
                ext = "html"
            elif "json" in selected_filter.lower():
                ext = "json"
            elif "txt" in selected_filter.lower():
                ext = "txt"
            else:
                ext = "md"
            file_path = f"{file_path}.{ext}"

        try:
            export_chat(chat, ext, file_path, dark_mode=self.dark_mode)
            QMessageBox.information(
                self,
                self.t("export_success_title"),
                self.t("export_success_msg", path=file_path)
            )
        except Exception as error:
            QMessageBox.warning(
                self,
                "Export Error",
                self.t("export_error_msg", error=str(error))
            )

    def ask_chat_title(self, initial):
        """Returns the text entered, or None if it was cancelled."""
        dialog = QInputDialog(self)
        dialog.setWindowTitle(self.t("rename_dialog_title"))
        dialog.setLabelText(self.t("rename_dialog_prompt"))
        dialog.setTextValue(initial)
        dialog.setOkButtonText(self.t("rename_save_btn"))
        dialog.setCancelButtonText(self.t("rename_cancel_btn"))
        return dialog.textValue() if dialog.exec() == QDialog.Accepted else None

    def confirm(self, title, message):
        return QMessageBox.question(self, title, message) == QMessageBox.Yes

    def rename_chat(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat is None:
            return
        new_title = self.ask_chat_title(chat.get("title", ""))
        if new_title is None:
            return  # cancelled
        new_title = core.tidy_title(new_title)
        if not new_title or new_title == chat.get("title"):
            return  # an empty name is ignored: the old title stays
        chat["title"] = new_title
        self.save_history()
        self.refresh_history_list()

    def delete_chat(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat is None:
            return
        if not self.confirm(self.t("delete_chat_title"),
                            self.t("delete_chat_message", title=chat.get("title", ""))):
            return
        if getattr(self, "is_streaming", False) and getattr(self, "current_stream_chat_id", None) == chat_id:
            self.stop_generating()
        was_open = chat_id == self.current_id
        self.chats = [saved for saved in self.chats if saved.get("id") != chat_id]  # only this chat goes
        if was_open:
            self.tts.stop()
            self.make_draft()  # deleting the open chat leaves a fresh empty one
        self.save_history()
        self.refresh_history_list()
        if was_open:
            self.display_chat()

    def clear_chat(self):
        chat = self.current_chat()
        if not chat or not chat.get("messages"):
            return  # Nothing saved to clear; leave the empty draft alone.
        if not self.confirm(self.t("clear_dialog_title"), self.t("clear_dialog_message")):
            return
        if getattr(self, "is_streaming", False) and getattr(self, "current_stream_chat_id", None) == chat.get("id"):
            self.stop_generating()
        self.tts.stop()
        self.chats = [saved for saved in self.chats if saved.get("id") != chat.get("id")]
        self.make_draft()
        self.save_history()
        self.refresh_history_list()
        self.display_chat()

    # ---------- Messages ----------
    def clear_messages(self):
        self.bubbles = []
        while self.messages_layout.count() > 1:  # keep the stretch at the end
            widget = self.messages_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()

    def display_chat(self):
        self.clear_messages()
        self.refresh_history_list()
        chat = self.current_chat()
        messages = chat.get("messages", []) if chat else []
        if not messages:
            self.show_welcome()
        for msg in messages:
            self.add_bubble(msg.get("role"), msg.get("content", ""), timestamp=msg.get("time"))
        self.scroll_to_bottom()

    def show_welcome(self):
        # Welcome splash shown on launch and for each empty New Chat.
        welcome = QWidget()
        layout = QVBoxLayout(welcome)
        layout.setContentsMargins(0, 24, 0, 0)
        layout.setSpacing(6)
        pixmap = QPixmap(os.path.join(HERE, "jugnu_bot.png"))
        if pixmap.isNull():
            # Friendly fallback if the companion image hasn't been copied beside the script.
            image = QLabel("🤖", objectName="welcomeEmoji")
        else:
            image = QLabel()
            image.setPixmap(pixmap.scaled(360, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        for widget in (image, QLabel(self.t("welcome_title"), objectName="welcomeTitle"),
                       QLabel("Just an Unusually Genius Neural Utility", objectName="welcomeTagline"),
                       QLabel(self.t("welcome_prompt"), objectName="welcomePrompt")):
            layout.addWidget(widget, 0, Qt.AlignHCenter)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, welcome)

    def add_bubble(self, role, text, timestamp=None):
        if not self.bubbles and self.messages_layout.count() > 1:
            self.clear_messages()  # the first message replaces the welcome splash
        bubble = Bubble(self, role, text, timestamp)
        bubble.fit(max_bubble_width(self.scroll.viewport().width()))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(row)
        if role == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, holder)
        self.bubbles.append(bubble)
        return bubble

    def fit_bubbles(self):
        width = max_bubble_width(self.scroll.viewport().width())
        for bubble in self.bubbles:
            bubble.fit(width)

    # ---------- Sending ----------
    def start_task(self, work, slot):
        """Run `work()` on a QThread; `slot(result)` then runs on the GUI thread."""
        self.tasks = [task for task in self.tasks if task.isRunning()]
        task = Task(work)
        task.done.connect(slot)
        self.tasks.append(task)
        task.start()

    def wait_for_tasks(self, msecs):
        """Wait up to `msecs` for running requests. True when none is left."""
        return all(task.wait(msecs) for task in self.tasks)

    def warm_up(self):
        if self.closed:
            return  # the window was closed before the delayed warm-up started
        self.start_task(core.warm_up, self.on_warmed)

    def on_warmed(self, ok):
        self.set_status("status_ready" if ok else "status_offline")

    def send_message(self):
        if self.busy:
            return
        user_text = self.input_box.toPlainText().strip()
        if not user_text:
            return
        self.tts.stop()  # A new message interrupts any reply still being read aloud.
        chat = self.current_chat()
        if not chat:
            self.make_draft()
            chat = self.current_chat()
        if self.draft_chat and chat.get("id") == self.draft_chat.get("id"):
            self.chats.append(self.draft_chat)
            self.draft_chat = None
        if chat.get("title") == core.NEW_CHAT_TITLE:
            chat["title"] = user_text[:core.MAX_LIST_TITLE]
        time_now = core.message_time_text()
        chat["messages"].append({"role": "user", "content": user_text, "time": time_now})
        self.input_box.clear()
        self.add_bubble("user", user_text, timestamp=time_now)
        self.scroll_to_bottom()
        self.refresh_history_list()
        self.save_history()
        self.busy = True
        self.send_btn.setEnabled(False)
        self.set_status("status_thinking")

        # Clock/date and arithmetic questions are answered locally—no model call required.
        answer = core.local_answer(user_text, self.language)
        if answer is not None:
            self.finish_answer(chat["id"], answer, False)
            return
        self.ask_ollama(chat["id"])

    def create_streaming_bubble(self, chat_id, timestamp=None):
        if getattr(self, "current_id", None) != chat_id:
            self.streaming_bubble_info = None
            return None

        c = getattr(self, "colors", LIGHT)
        row = tk.Frame(self.messages_frame, bg=c["bg"])
        row.pack(fill="x", pady=7, padx=8)
        bubble = tk.Frame(row, bg=c["assistant"])
        bubble.pack(side="left", padx=(0, 40))
        header = tk.Frame(bubble, bg=bubble["bg"])
        header.pack(fill="x", padx=12, pady=(8, 2))

        name_lbl = tk.Label(header, text="🤖 JUGNU", bg=bubble["bg"], fg=c["accent"],
                            font=("Segoe UI", 9, "bold"), anchor="w")
        name_lbl.pack(side="left")

        time_lbl = None
        if timestamp:
            time_lbl = tk.Label(header, text=timestamp, bg=bubble["bg"], fg=c["muted"],
                                font=("Segoe UI", 8), anchor="w")
            time_lbl.pack(side="left", padx=(8, 0))

        read_btn = None
        if hasattr(self, "tts"):
            read_btn = tk.Button(header, text="🔊", command=lambda: None,
                                 relief="flat", bd=0, font=("Segoe UI Emoji", 10), cursor="hand2",
                                 bg=bubble["bg"], fg=c["accent"], activebackground=bubble["bg"],
                                 activeforeground=c["accent2"], disabledforeground=c["muted"],
                                 state="disabled")
            read_btn.pack(side="right", padx=(4, 0))
            if hasattr(self, "read_buttons"):
                self.read_buttons.append(read_btn)

        copy_btn = tk.Button(header, text="⧉ Copy", font=("Segoe UI", 8, "bold"),
                             relief="flat", bd=0, highlightthickness=0,
                             bg=bubble["bg"], fg=c["muted"],
                             highlightbackground=bubble["bg"],
                             activebackground=c["border"], activeforeground=c["accent"],
                             cursor="hand2", padx=6, pady=1, state="disabled")
        copy_btn.pack(side="right", padx=(8, 0))

        hover_bg = c["border"]

        def on_btn_enter(_event):
            try:
                if "Copied" not in copy_btn.cget("text"):
                    copy_btn.configure(fg=c["accent"], bg=hover_bg)
            except tk.TclError:
                pass

        def on_btn_leave(_event):
            try:
                if "Copied" not in copy_btn.cget("text"):
                    copy_btn.configure(fg=c["muted"], bg=bubble["bg"])
                else:
                    copy_btn.configure(bg=bubble["bg"])
            except tk.TclError:
                pass

        copy_btn.bind("<Enter>", on_btn_enter)
        copy_btn.bind("<Leave>", on_btn_leave)

        txt = tk.Text(bubble, font=self.bubble_font, wrap="word", width=18,
                      relief="flat", bd=0, highlightthickness=0,
                      bg=bubble["bg"], fg=c["text"],
                      selectbackground=c["accent"], selectforeground="white",
                      inactiveselectbackground=c["accent"], cursor="xterm", height=1)
        txt.pack(fill="x", padx=12, pady=(2, 10))

        widgets_to_bind = [row, bubble, header, name_lbl, copy_btn, txt]
        if read_btn:
            widgets_to_bind.append(read_btn)
        if time_lbl:
            widgets_to_bind.append(time_lbl)
        for w in widgets_to_bind:
            w.bind("<MouseWheel>", self.on_mousewheel)
            w.bind("<Button-4>", self.on_mousewheel)
            w.bind("<Button-5>", self.on_mousewheel)

        self.streaming_bubble_info = {
            "chat_id": chat_id,
            "row": row,
            "bubble": bubble,
            "header": header,
            "txt": txt,
            "copy_btn": copy_btn,
            "read_btn": read_btn,
            "timestamp": timestamp,
        }
        if hasattr(self, "canvas"):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self.canvas.yview_moveto(1.0)
        return self.streaming_bubble_info

    def append_stream_token(self, chat_id, token):
        if not token:
            return
        if getattr(self, "streaming_bubble_info", None) and self.streaming_bubble_info.get("chat_id") == chat_id:
            txt = self.streaming_bubble_info["txt"]
            try:
                txt.configure(state="normal")
                txt.insert("end", token)
                full_text = txt.get("1.0", "end - 1c")
                canvas_w = self.current_canvas_width(fallback=580)
                calc_cols = self.compute_bubble_cols(full_text, canvas_w)
                txt.configure(width=calc_cols)
                self.root.update_idletasks()
                h = self.fit_bubble_height(txt, full_text)
                txt.configure(height=h, state="disabled")
                if hasattr(self, "canvas"):
                    self.canvas.configure(scrollregion=self.canvas.bbox("all"))
                    self.canvas.yview_moveto(1.0)
            except tk.TclError:
                pass

    def finish_streaming(self, chat_id, accumulated_text, is_error=False, error_message=None):
        self.busy = False
        self.is_streaming = False
        self.current_stream_chat_id = None
        self.current_stream_response = None
        if hasattr(self, "stop_event") and self.stop_event is not None:
            self.stop_event.clear()

        # Hide Stop button and restore Send button
        if hasattr(self, "stop_gen_btn"):
            try:
                self.stop_gen_btn.pack_forget()
            except Exception:
                pass
        if hasattr(self, "send_btn"):
            try:
                self.send_btn.configure(state="normal")
            except Exception:
                pass
        if hasattr(self, "set_status"):
            self.set_status("status_ready")

        chat = next((ct for ct in self.chats if ct.get("id") == chat_id), None)
        timestamp = message_time_text()

        # Case 1: Error or no non-whitespace text generated
        if is_error or not accumulated_text.strip():
            if getattr(self, "streaming_bubble_info", None) and self.streaming_bubble_info.get("chat_id") == chat_id:
                try:
                    self.streaming_bubble_info["row"].destroy()
                except (tk.TclError, KeyError):
                    pass
                self.streaming_bubble_info = None

            notice = error_message or get_string(self.language, "error_empty_reply")
            if chat:
                chat["messages"].append({
                    "role": "assistant",
                    "content": notice,
                    "time": timestamp,
                    "is_error": True
                })
                self.save_history()

            if chat and chat.get("id") == self.current_id:
                self.add_bubble("assistant", notice, timestamp=timestamp)
                if hasattr(self, "canvas"):
                    self.canvas.yview_moveto(1.0)
            if hasattr(self, "refresh_history_list"):
                self.refresh_history_list()
            return

        # Case 2: Generated tokens present (completed normally, stopped, or partial before error)
        final_text = accumulated_text.strip()
        if final_text and chat:
            msg = {
                "role": "assistant",
                "content": final_text,
                "time": timestamp
            }
            if is_error:
                msg["is_error"] = True
            chat["messages"].append(msg)
            self.save_history()

        # Finalize the visual bubble on screen if still viewing this chat
        if getattr(self, "streaming_bubble_info", None) and self.streaming_bubble_info.get("chat_id") == chat_id:
            info = self.streaming_bubble_info
            self.streaming_bubble_info = None
            measure_text = final_text
            try:
                txt = info["txt"]
                txt.configure(state="normal")
                txt.delete("1.0", "end")
                segments = render_markdown(final_text)
                canvas_w = self.current_canvas_width(fallback=580)
                measure_text, _ = self.fill_bubble_text(txt, final_text, segments)
                calc_cols = self.compute_bubble_cols(measure_text, canvas_w)
                txt.configure(width=calc_cols)
                self.root.update_idletasks()
                h = self.fit_bubble_height(txt, measure_text)
                txt.configure(height=h, state="disabled")
                copy_btn = info["copy_btn"]
                copy_btn.configure(
                    state="normal",
                    command=lambda t=final_text, b=copy_btn: self.copy_to_clipboard(t, b)
                )
                read_btn = info["read_btn"]
                if read_btn and hasattr(self, "tts") and self.tts.available:
                    read_btn.configure(
                        state="normal",
                        command=lambda t=final_text: self.read_aloud(t)
                    )
                if hasattr(self, "bubble_items"):
                    self.bubble_items.append((info["bubble"], txt, measure_text))
            except (tk.TclError, KeyError):
                pass

            if getattr(self, "voice_enabled", False) and not is_error:
                if hasattr(self, "tts"):
                    self.tts.speak(plain_text(final_text))

        elif chat and chat.get("id") == self.current_id and final_text:
            self.add_bubble("assistant", final_text, timestamp=timestamp)
            if hasattr(self, "canvas"):
                self.canvas.yview_moveto(1.0)
            if getattr(self, "voice_enabled", False) and not is_error:
                if hasattr(self, "tts"):
                    self.tts.speak(plain_text(final_text))

        if hasattr(self, "refresh_history_list"):
            self.refresh_history_list()

    def ask_ollama(self, chat_id):
        # The worker gets a copy, so it never reads the list while the GUI changes it.
        chat = {"messages": [dict(m) for m in self.find_chat(chat_id)["messages"]]}
        language = self.language  # The prompt and any error text use the language chosen at send time.
        self.start_task(lambda: (chat_id, *core.ask_ollama(chat, language)), self.on_answer)

    def on_answer(self, result):
        self.finish_answer(*result)

    def finish_answer(self, chat_id, answer, is_error=False):
        chat = self.find_chat(chat_id)  # may be gone if it was deleted while the request ran
        time_now = core.message_time_text()
        if chat and isinstance(answer, str) and answer.strip():
            message = {"role": "assistant", "content": answer, "time": time_now}
            if is_error:
                message["is_error"] = True
            chat["messages"].append(message)
            self.save_history()
            # Only display if the user is still viewing the chat that received the answer.
            if chat_id == self.current_id:
                self.add_bubble("assistant", answer, timestamp=time_now)
                self.scroll_to_bottom()
                if self.voice_enabled:
                    self.tts.speak(plain_text(answer))
        self.busy = False
        self.send_btn.setEnabled(True)
        self.set_status("status_ready")
        self.refresh_history_list()

    def closeEvent(self, event):
        self.closed = True
        self.speech_timer.stop()
        self.tts.shutdown()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("JUGNU")
    lock = HistoryLock(core.HISTORY_FILE)
    try:
        lock.acquire()
    except HistoryInUseError:
        QMessageBox.critical(None, "Chat history in use",
                             "Another JUGNU window is using this chat history. "
                             "Close that window before opening another.")
        return 1
    except OSError as error:
        QMessageBox.critical(None, "Chat history unavailable", f"Could not lock chat history.\n\n{error}")
        return 1
    try:
        window = JugnuWindow()
        window.show()
        code = app.exec()
        finished = window.wait_for_tasks(2000)
    finally:
        lock.release()
    if not finished:
        # A request is still waiting on Ollama. A running QThread aborts the process when it is
        # destroyed, so leave right away; the history was already saved when the message was sent.
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
