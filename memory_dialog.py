"""
Memory Manager Dialog — PySide6 Desktop HUD (Issue #65).

Provides user-facing controls to inspect, filter, add, toggle, edit, and delete
persistent memory facts, as well as export/import and master enable/disable toggles.
"""

from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from memory import MemoryStore, get_memory_store


class MemoryManagerDialog(QDialog):
    """HUD Dialog for inspecting and managing persistent user memory."""

    memory_updated = Signal()

    def __init__(self, parent: Optional[QWidget] = None, store: Optional[MemoryStore] = None):
        super().__init__(parent)
        self.store = store or get_memory_store()
        self.setWindowTitle("🧠 User Memory & Preference Manager")
        self.resize(750, 520)

        self._setup_ui()
        self._refresh_facts()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Top Bar: Master Toggle + Search
        top_layout = QHBoxLayout()
        self.master_toggle = QCheckBox("Enable Persistent Memory")
        self.master_toggle.setChecked(self.store.is_enabled())
        self.master_toggle.toggled.connect(self._on_master_toggle)
        top_layout.addWidget(self.master_toggle)

        top_layout.addStretch()

        top_layout.addWidget(QLabel("🔍 Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter facts by key, category or text...")
        self.search_input.textChanged.connect(self._on_search_changed)
        top_layout.addWidget(self.search_input)

        layout.addLayout(top_layout)

        # Table: Facts list
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Status", "Category", "Key", "Value", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        # Add Fact GroupBox
        add_box = QGroupBox("➕ Add New Fact")
        add_layout = QHBoxLayout(add_box)

        self.cat_combo = QComboBox()
        self.cat_combo.addItems(["preference", "tech_stack", "personal", "instruction", "custom"])
        add_layout.addWidget(self.cat_combo)

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("Key (e.g. Language, Tone, Name)")
        add_layout.addWidget(self.key_input)

        self.val_input = QLineEdit()
        self.val_input.setPlaceholderText("Value (e.g. Python 3.12, Concise)")
        add_layout.addWidget(self.val_input)

        self.add_btn = QPushButton("Add Fact")
        self.add_btn.clicked.connect(self._on_add_fact)
        add_layout.addWidget(self.add_btn)

        layout.addWidget(add_box)

        # Bottom Bar: Bulk Actions & Close
        btn_layout = QHBoxLayout()

        self.clear_btn = QPushButton("🗑️ Clear All Memory")
        self.clear_btn.clicked.connect(self._on_clear_all)
        btn_layout.addWidget(self.clear_btn)

        self.export_btn = QPushButton("📤 Export JSON")
        self.export_btn.clicked.connect(self._on_export)
        btn_layout.addWidget(self.export_btn)

        self.import_btn = QPushButton("📥 Import JSON")
        self.import_btn.clicked.connect(self._on_import)
        btn_layout.addWidget(self.import_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def _refresh_facts(self) -> None:
        query = self.search_input.text()
        facts = self.store.search_facts(query)

        self.table.setRowCount(len(facts))
        for row, fact in enumerate(facts):
            # Checkbox item for enabled status
            chk = QCheckBox()
            chk.setChecked(fact.enabled)
            chk.toggled.connect(lambda checked, fid=fact.id: self._on_fact_toggled(fid, checked))
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, chk_widget)

            # Category
            self.table.setItem(row, 1, QTableWidgetItem(fact.category))

            # Key
            self.table.setItem(row, 2, QTableWidgetItem(fact.key))

            # Value
            self.table.setItem(row, 3, QTableWidgetItem(fact.value))

            # Delete button
            del_btn = QPushButton("❌")
            del_btn.setToolTip("Delete this fact")
            del_btn.clicked.connect(lambda _, fid=fact.id: self._on_delete_fact(fid))
            self.table.setCellWidget(row, 4, del_btn)

    def _on_master_toggle(self, enabled: bool) -> None:
        self.store.set_enabled(enabled)
        self.memory_updated.emit()

    def _on_search_changed(self, text: str) -> None:
        self._refresh_facts()

    def _on_fact_toggled(self, fact_id: str, enabled: bool) -> None:
        self.store.update_fact(fact_id, enabled=enabled)
        self.memory_updated.emit()

    def _on_add_fact(self) -> None:
        key = self.key_input.text().strip()
        val = self.val_input.text().strip()
        if not key or not val:
            return
        cat = self.cat_combo.currentText()
        self.store.add_fact(category=cat, key=key, value=val)
        self.key_input.clear()
        self.val_input.clear()
        self._refresh_facts()
        self.memory_updated.emit()

    def _on_delete_fact(self, fact_id: str) -> None:
        self.store.delete_fact(fact_id)
        self._refresh_facts()
        self.memory_updated.emit()

    def _on_clear_all(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear Memory",
            "Are you sure you want to permanently delete all stored memory facts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.store.clear_all()
            self._refresh_facts()
            self.memory_updated.emit()

    def _on_export(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Memory", "jugnu_memory_export.json", "JSON (*.json)")
        if file_path:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(self.store.export_memory())
            QMessageBox.information(self, "Exported", "Memory facts exported successfully.")

    def _on_import(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Memory", "", "JSON (*.json)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    count = self.store.import_memory(f.read())
                self._refresh_facts()
                self.memory_updated.emit()
                QMessageBox.information(self, "Imported", f"Successfully imported {count} facts.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to import memory: {e}")
