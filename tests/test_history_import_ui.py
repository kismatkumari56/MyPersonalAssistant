from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("outcome", ["cancel", "failure", "success"])
def test_import_refreshes_only_after_success(window, monkeypatch, outcome):
    import desktop_app
    from history_import import ImportPreview

    monkeypatch.setattr(desktop_app.QFileDialog, "getOpenFileName", lambda *args: ("import.json", ""))
    monkeypatch.setattr(desktop_app, "preview_import", lambda *args: ImportPreview(1, 0, 0, 0))
    commit = Mock(side_effect=OSError("disk full")) if outcome == "failure" else Mock(return_value=ImportPreview(1, 0, 0, 0))
    monkeypatch.setattr(desktop_app, "commit_import", commit)
    refresh = Mock()
    monkeypatch.setattr(window, "refresh_history_list", refresh)
    monkeypatch.setattr(window, "display_chat", Mock())
    monkeypatch.setattr(desktop_app.core, "load_history", lambda *args: [{"id": "new"}])
    original = window.chats

    class Dialog:
        AcceptRole = 0
        Cancel = 1
        def __init__(self, *args):
            self.buttons = []
        def setWindowTitle(self, *args):
            pass
        def setText(self, *args):
            pass
        def addButton(self, *args):
            button = object()
            self.buttons.append(button)
            return button
        def exec(self):
            pass
        def clickedButton(self):
            return self.buttons[0] if outcome != "cancel" else None
        @staticmethod
        def warning(*args):
            pass
        @staticmethod
        def information(*args):
            pass

    monkeypatch.setattr(desktop_app, "QMessageBox", Dialog)
    window.tasks = []
    window.busy = False
    window.open_history_import()
    assert commit.call_count == int(outcome != "cancel")
    assert refresh.call_count == int(outcome == "success")
    if outcome != "success":
        assert window.chats is original
