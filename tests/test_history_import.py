import json

import pytest

import history_import as importer
from history_lock import HistoryInUseError, HistoryLock


def chat(chat_id="a", content="नमस्ते"):
    return {"id": chat_id, "title": "Unicode chat", "messages": [
        {"role": "user", "content": content}]}


def write_import(path, chats, version=1):
    path.write_text(json.dumps({"version": version, "conversations": chats}), encoding="utf-8")


def test_preview_fork_and_reimport_are_lossless(tmp_path):
    history, source = tmp_path / "history.json", tmp_path / "import.json"
    original = [chat()]
    history.write_text(json.dumps(original), encoding="utf-8")
    write_import(source, [chat(), chat("a", "different"), chat("b"), {"id": "bad"}])
    preview = importer.preview_import(source, history)
    assert (preview.new, preview.duplicate, preview.conflicting, preview.rejected) == (0, 2, 1, 1)
    assert json.loads(history.read_text(encoding="utf-8")) == original
    importer.commit_import(source, history, conflict="fork", expected_fingerprint=preview.fingerprint)
    saved = json.loads(history.read_text(encoding="utf-8"))
    assert len(saved) == 2
    assert saved[0] == original[0]
    assert saved[1]["id"] != "a"
    assert importer.preview_import(source, history).conflicting == 0
    importer.commit_import(source, history, conflict="fork")
    assert json.loads(history.read_text(encoding="utf-8")) == saved


@pytest.mark.parametrize("payload", ["{", '{"version":2,"conversations":[]}',
                                    '{"version":1,"conversations":null}'])
def test_invalid_documents_preserve_history(tmp_path, payload):
    history, source = tmp_path / "history.json", tmp_path / "import.json"
    history.write_text("[]")
    source.write_text(payload)
    with pytest.raises(ValueError):
        importer.commit_import(source, history)
    assert history.read_text(encoding="utf-8") == "[]"


def test_size_limit_roles_and_changed_preview(tmp_path, monkeypatch):
    history, source = tmp_path / "history.json", tmp_path / "import.json"
    write_import(source, [chat()])
    preview = importer.preview_import(source, history)
    write_import(source, [chat("b", "changed")])
    with pytest.raises(ValueError, match="changed"):
        importer.commit_import(source, history, expected_fingerprint=preview.fingerprint)
    assert not history.exists()
    bad = chat()
    bad["messages"][0]["role"] = "system"
    assert importer.plan_import([], [bad])[0].rejected == 1
    monkeypatch.setattr(importer, "MAX_IMPORT_BYTES", 1)
    with pytest.raises(ValueError, match="limit"):
        importer.commit_import(source, history)


def test_failed_replace_and_lock_contention_preserve_history(tmp_path, monkeypatch):
    history, source = tmp_path / "history.json", tmp_path / "import.json"
    history.write_text("[]")
    write_import(source, [chat()])
    lock = HistoryLock(history)
    lock.acquire()
    try:
        with pytest.raises(HistoryInUseError):
            importer.commit_import(source, history)
    finally:
        lock.release()
    def fail_replace(*args):
        raise OSError("interrupted")
    monkeypatch.setattr(importer.core.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        importer.commit_import(source, history)
    assert history.read_text(encoding="utf-8") == "[]"
    assert not list(tmp_path.glob("*.tmp"))

