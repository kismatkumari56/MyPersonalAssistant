"""PySide6 Tool Execution Card Widget for Chat Bubbles."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ToolExecutionWidget(QFrame):
    """Collapsible UI card rendering tool execution telemetry, input arguments, and stdout."""

    def __init__(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_result = tool_result
        self.expanded = False
        self.setObjectName("toolExecutionCard")
        self.build_ui()

    def build_ui(self) -> None:
        self.setStyleSheet(
            """
            QFrame#toolExecutionCard {
                border: 1px solid #CBD5E0;
                border-radius: 8px;
                background-color: #F7FAFC;
                margin-top: 4px;
                margin-bottom: 4px;
            }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # Header row
        header = QHBoxLayout()
        header.setSpacing(8)

        icon_lbl = QLabel(f"<b>⚡ Tool:</b> <code>{self.tool_name}</code>")
        header.addWidget(icon_lbl)

        # Duration & Status
        duration = self.tool_result.get("duration_ms")
        dur_str = f" ({duration:.1f}ms)" if duration is not None else ""
        success = self.tool_result.get("success", False)

        status_lbl = QLabel(f"<span style='color: {'#2F855A' if success else '#C53030'}; font-weight: bold;'>{'✓ Success' if success else '⚠ Error'}{dur_str}</span>")
        header.addWidget(status_lbl)
        header.addStretch()

        self.toggle_btn = QPushButton("▼ Details")
        self.toggle_btn.setFixedWidth(80)
        self.toggle_btn.clicked.connect(self.toggle_details)
        header.addWidget(self.toggle_btn)

        layout.addLayout(header)

        # Collapsible details container
        self.details_widget = QWidget()
        details_layout = QVBoxLayout(self.details_widget)
        details_layout.setContentsMargins(0, 4, 0, 0)
        details_layout.setSpacing(6)

        # Arguments preview
        if self.tool_args:
            args_lbl = QLabel("<b>Arguments:</b>")
            details_layout.addWidget(args_lbl)
            args_view = QPlainTextEdit()
            args_view.setReadOnly(True)
            args_view.setMaximumHeight(80)
            args_view.setPlainText(json.dumps(self.tool_args, indent=2))
            details_layout.addWidget(args_view)

        # Output preview
        out_lbl = QLabel("<b>Output:</b>")
        details_layout.addWidget(out_lbl)
        out_view = QPlainTextEdit()
        out_view.setReadOnly(True)
        out_view.setMaximumHeight(120)
        out_text = self.tool_result.get("output") or self.tool_result.get("error") or "(no output)"
        out_view.setPlainText(out_text)
        details_layout.addWidget(out_view)

        self.details_widget.hide()
        layout.addWidget(self.details_widget)

    def toggle_details(self) -> None:
        self.expanded = not self.expanded
        if self.expanded:
            self.details_widget.show()
            self.toggle_btn.setText("▲ Hide")
        else:
            self.details_widget.hide()
            self.toggle_btn.setText("▼ Details")
