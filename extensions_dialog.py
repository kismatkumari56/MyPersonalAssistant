"""Extensions Manager Dialog for PySide6 GUI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from extensions import ExtensionManager, get_extension_manager
from strings import get_string


class ExtensionsManagerDialog(QDialog):
    """Visual dialog to inspect, enable, and disable local offline extensions."""

    extensions_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, manager: Optional[ExtensionManager] = None):
        super().__init__(parent)
        self.manager = manager or get_extension_manager()
        self.setWindowTitle(self.t("extensions_dialog_title", "Extensions Manager"))
        self.setMinimumSize(540, 420)
        self.resize(580, 480)
        self.build_ui()

    def t(self, key: str, fallback: str = "") -> str:
        parent = self.parent()
        lang = getattr(parent, "current_language", "en") if parent else "en"
        val = get_string(lang, key)
        return val if val != key else fallback

    def build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        header = QLabel(self.t("extensions_header", "⚡ Local Extensions & Slash Commands"))
        font = header.font()
        font.setPointSize(14)
        font.setBold(True)
        header.setFont(font)
        layout.addWidget(header)

        subhead = QLabel(
            self.t(
                "extensions_subhead",
                "Extend JUGNU with safe, offline productivity utilities and custom slash commands.",
            )
        )
        subhead.setStyleSheet("color: #718096; font-size: 12px;")
        subhead.setWordWrap(True)
        layout.addWidget(subhead)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        self.card_layout = QVBoxLayout(container)
        self.card_layout.setContentsMargins(0, 5, 0, 5)
        self.card_layout.setSpacing(12)

        self.populate_cards()

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        close_btn = QPushButton(self.t("close_btn", "Close"))
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def populate_cards(self) -> None:
        # Clear existing cards
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        exts = self.manager.list_extensions()
        for ext in exts:
            card = QFrame()
            card.setObjectName("extensionCard")
            card.setStyleSheet(
                "QFrame#extensionCard { border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px; background: palette(base); }"
            )
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(12, 10, 12, 10)
            c_layout.setSpacing(6)

            top_row = QHBoxLayout()
            title_lbl = QLabel(f"<b>{ext['title']}</b> <span style='color: #A0AEC0;'>v{ext['version']}</span>")
            top_row.addWidget(title_lbl)
            top_row.addStretch()

            toggle = QCheckBox(self.t("enabled", "Enabled"))
            toggle.setChecked(ext["enabled"])
            toggle.toggled.connect(lambda checked, name=ext["name"]: self.on_toggle(name, checked))
            top_row.addWidget(toggle)
            c_layout.addLayout(top_row)

            desc = QLabel(ext["description"])
            desc.setStyleSheet("color: #4A5568; font-size: 12px;")
            desc.setWordWrap(True)
            c_layout.addWidget(desc)

            if ext["commands"]:
                cmds_text = "  ".join(f"<code>{cmd}</code>" for cmd in ext["commands"])
                cmds_lbl = QLabel(f"<small>Commands: {cmds_text}</small>")
                cmds_lbl.setStyleSheet("color: #4A5568;")
                c_layout.addWidget(cmds_lbl)

            self.card_layout.addWidget(card)

        self.card_layout.addStretch()

    def on_toggle(self, ext_name: str, enabled: bool) -> None:
        if enabled:
            self.manager.enable_extension(ext_name)
        else:
            self.manager.disable_extension(ext_name)
        self.extensions_changed.emit()
