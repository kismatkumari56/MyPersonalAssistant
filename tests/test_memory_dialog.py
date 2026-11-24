"""Tests for the PySide6 Memory Manager Dialog."""

from pathlib import Path
from PySide6.QtWidgets import QApplication

from memory import MemoryStore
from memory_dialog import MemoryManagerDialog

app = QApplication.instance() or QApplication([])


def test_memory_dialog_initialization(tmp_path: Path) -> None:
    store = MemoryStore(file_path=tmp_path / "test_mem.json")
    store.add_fact(category="preference", key="Format", value="Bullets")

    dialog = MemoryManagerDialog(store=store)
    assert dialog.table.rowCount() == 1
    assert dialog.master_toggle.isChecked() is True

    # Test adding fact via dialog
    dialog.key_input.setText("TestingKey")
    dialog.val_input.setText("TestingVal")
    dialog._on_add_fact()

    assert store.get_fact(store.get_all_facts()[0].id) is not None
    assert dialog.table.rowCount() == 2

    # Test search filter
    dialog.search_input.setText("Bullets")
    assert dialog.table.rowCount() == 1

    dialog.search_input.setText("NonExistent")
    assert dialog.table.rowCount() == 0

    dialog.close()
