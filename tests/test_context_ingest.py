"""Unit and integration tests for local document attachment and context ingestion."""

from pathlib import Path
from unittest.mock import Mock, patch
import pytest

import assistant
import core
import desktop_app
from context_ingest import (
    AttachedDocument,
    AttachmentError,
    BinaryFileError,
    FileNotFoundErrorCustom,
    FileTooLargeError,
    assemble_prompt_with_attachments,
    budget_content,
    format_size,
    ingest_file,
    is_binary_sample,
    read_text_with_encoding,
)


# ---------------- UTILITY & FORMATTING TESTS ----------------

def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(2048) == "2.0 KB"
    assert format_size(1048576) == "1.0 MB"


def test_is_binary_sample():
    # Plain text samples
    assert not is_binary_sample(b"def hello(): return 'world'")
    assert not is_binary_sample("Namaste duniya 123! \n\t".encode("utf-8"))

    # Null byte detection
    assert is_binary_sample(b"hello\x00world")

    # High non-printable character ratio
    assert is_binary_sample(bytes(range(0, 31)))


# ---------------- ENCODING & INGESTION TESTS ----------------

def test_read_text_with_encoding_utf8(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("print('hello world')", encoding="utf-8")
    content, enc = read_text_with_encoding(f)
    assert content == "print('hello world')"
    assert enc == "utf-8"


def test_read_text_with_encoding_utf8_sig(tmp_path):
    f = tmp_path / "sample_bom.txt"
    f.write_text("bom content", encoding="utf-8-sig")
    content, enc = read_text_with_encoding(f)
    assert content == "bom content"
    assert enc in ("utf-8", "utf-8-sig")


def test_read_text_with_encoding_utf16(tmp_path):
    f = tmp_path / "sample_u16.txt"
    f.write_text("unicode utf-16 content", encoding="utf-16")
    content, enc = read_text_with_encoding(f)
    assert content == "unicode utf-16 content"
    assert enc == "utf-16"


def test_read_text_with_encoding_latin1(tmp_path):
    f = tmp_path / "sample_latin.txt"
    raw = "caf\xe9 cr\xe8me".encode("latin-1")
    f.write_bytes(raw)
    content, _ = read_text_with_encoding(f)
    assert "caf" in content


def test_read_text_file_not_found():
    with pytest.raises(FileNotFoundErrorCustom):
        read_text_with_encoding("non_existent_file_xyz_123.py")


def test_read_text_file_too_large(tmp_path):
    large_file = tmp_path / "large.txt"
    # Write 513 KB
    large_file.write_bytes(b"a" * (513 * 1024))
    with pytest.raises(FileTooLargeError):
        read_text_with_encoding(large_file)


def test_read_text_binary_rejected(tmp_path):
    bin_file = tmp_path / "image.png"
    bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00")
    with pytest.raises(BinaryFileError):
        read_text_with_encoding(bin_file)


# ---------------- CONTEXT BUDGETING TESTS ----------------

def test_budget_content_under_limit():
    text = "line 1\nline 2\nline 3"
    budgeted, is_trunc, total, kept = budget_content(text, max_chars=100)
    assert not is_trunc
    assert budgeted == text
    assert total == 3
    assert kept == 3


def test_budget_content_exceeds_limit():
    lines = [f"This is line number {i} with some extra padding text" for i in range(100)]
    text = "\n".join(lines)
    budgeted, is_trunc, total, kept = budget_content(text, max_chars=300)
    assert is_trunc
    assert total == 100
    assert kept < 100
    assert "[... " in budgeted
    assert "lines omitted to fit model context window" in budgeted
    # Head and tail lines should be present
    assert "This is line number 0" in budgeted
    assert "This is line number 99" in budgeted


# ---------------- ATTACHED DOCUMENT & PROMPT ASSEMBLY ----------------

def test_attached_document_prompt_formatting():
    doc = AttachedDocument(
        path="/tmp/math.py",
        filename="math.py",
        size_bytes=1024,
        content="def add(a, b):\n    return a + b",
        language="python",
        encoding="utf-8",
        is_truncated=False,
        total_lines=2,
        kept_lines=2,
    )
    formatted = doc.format_for_prompt()
    assert "[Attached File: math.py (1.0 KB)]" in formatted
    assert "```python\ndef add(a, b):\n    return a + b\n```" in formatted


def test_attached_document_prompt_formatting_truncated():
    doc = AttachedDocument(
        path="/tmp/large.py",
        filename="large.py",
        size_bytes=2048,
        content="line1\n[... 5 lines omitted ...]\nline10",
        language="python",
        encoding="utf-8",
        is_truncated=True,
        total_lines=10,
        kept_lines=5,
    )
    formatted = doc.format_for_prompt()
    assert "(truncated 5 lines)" in formatted


def test_ingest_file_auto_detects_language(tmp_path):
    py_file = tmp_path / "script.py"
    py_file.write_text("x = 42\n", encoding="utf-8")
    doc = ingest_file(py_file)
    assert doc.filename == "script.py"
    assert doc.language == "python"
    assert doc.size_bytes > 0
    assert not doc.is_truncated

    md_file = tmp_path / "README.md"
    md_file.write_text("# Title\n", encoding="utf-8")
    doc_md = ingest_file(md_file)
    assert doc_md.language == "markdown"


def test_ingest_file_directory_rejected(tmp_path):
    sub_dir = tmp_path / "some_dir"
    sub_dir.mkdir()
    with pytest.raises(AttachmentError):
        ingest_file(sub_dir)


def test_assemble_prompt_with_attachments_no_attachments():
    assert assemble_prompt_with_attachments("Hello assistant") == "Hello assistant"
    assert assemble_prompt_with_attachments("") == ""


def test_assemble_prompt_with_attachments_with_query():
    doc = AttachedDocument(
        path="/tmp/test.py",
        filename="test.py",
        size_bytes=100,
        content="print(1)",
        language="python",
    )
    assembled = assemble_prompt_with_attachments("Review this code", [doc])
    assert "[Attached File: test.py" in assembled
    assert "User Inquiry:\nReview this code" in assembled


def test_assemble_prompt_with_attachments_empty_query():
    doc = AttachedDocument(
        path="/tmp/test.py",
        filename="test.py",
        size_bytes=100,
        content="print(1)",
        language="python",
    )
    assembled = assemble_prompt_with_attachments("", [doc])
    assert "[Attached File: test.py" in assembled
    assert "Please review and explain the attached file(s) above." in assembled


# ---------------- CLI ASSISTANT INTEGRATION TESTS ----------------

def test_assistant_ask_ai_with_attachments():
    doc = AttachedDocument(
        path="/tmp/test.py",
        filename="test.py",
        size_bytes=100,
        content="print(1)",
        language="python",
    )
    with patch("assistant.chat") as mock_chat:
        mock_chat.return_value = core.ChatResult(answer="This prints 1.")
        answer = assistant.ask_ai("What does this do?", attachments=[doc])
        assert answer == "This prints 1."
        sent_messages = mock_chat.call_args[0][0]
        # The user message should include the attached document block
        assert "[Attached File: test.py" in sent_messages[-1]["content"]
        assert "What does this do?" in sent_messages[-1]["content"]


# ---------------- DESKTOP APP INTEGRATION TESTS ----------------

@pytest.fixture
def mock_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_app, "HISTORY_FILE", tmp_path / "history.json")
    app = desktop_app.JugnuApp.__new__(desktop_app.JugnuApp)
    app.language = "en"
    app.current_id = "test-chat"
    app.draft_chat = None
    app.voice_enabled = False
    app.colors = desktop_app.LIGHT
    app.chats = [{"id": "test-chat", "title": "New Chat", "messages": []}]
    app.attachments = []
    app.attachments_frame = Mock()
    app.attachments_frame.winfo_children.return_value = []
    app.messages_frame = Mock()
    app.bubble_items = []
    app.input_box = Mock()
    app.send_btn = Mock()
    app.root = Mock()
    app.set_status = Mock()
    app.save_history = Mock()
    app.refresh_history_list = Mock()
    app.add_bubble = Mock()
    app.finish_answer = Mock()
    app.ask_ollama = Mock()
    app.display_chat = Mock()
    app.tts = Mock()
    app.is_time_question = lambda text: False
    app.extract_calc_expression = lambda text: None
    return app


def test_desktop_pick_attachments(mock_desktop, tmp_path):
    f = tmp_path / "example.py"
    f.write_text("a = 10", encoding="utf-8")

    with patch("desktop_app.filedialog.askopenfilenames", return_value=[str(f)]):
        with patch.object(mock_desktop, "render_attachment_chips") as mock_render:
            mock_desktop.pick_attachments()
            assert len(mock_desktop.attachments) == 1
            assert mock_desktop.attachments[0].filename == "example.py"
            assert mock_render.called


def test_desktop_pick_attachments_binary_error(mock_desktop, tmp_path):
    bin_file = tmp_path / "sample.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03\x04")

    with patch("desktop_app.filedialog.askopenfilenames", return_value=[str(bin_file)]):
        with patch("desktop_app.messagebox.showerror") as mock_err:
            with patch.object(mock_desktop, "render_attachment_chips") as mock_render:
                mock_desktop.pick_attachments()
                assert len(mock_desktop.attachments) == 0
                assert mock_err.called
                assert mock_render.called


def test_desktop_remove_attachment(mock_desktop):
    doc1 = AttachedDocument("/tmp/a.py", "a.py", 100, "content a", "python")
    doc2 = AttachedDocument("/tmp/b.py", "b.py", 200, "content b", "python")
    mock_desktop.attachments = [doc1, doc2]

    with patch.object(mock_desktop, "render_attachment_chips") as mock_render:
        mock_desktop.remove_attachment(0)
        assert len(mock_desktop.attachments) == 1
        assert mock_desktop.attachments[0].filename == "b.py"
        assert mock_render.called


def test_desktop_send_message_assembles_attachments(mock_desktop):
    doc = AttachedDocument("/tmp/calc.py", "calc.py", 50, "def calc(): pass", "python")
    mock_desktop.attachments = [doc]
    mock_desktop.busy = False
    mock_desktop.input_box.get.return_value = "Explain this script."

    with patch.object(mock_desktop, "render_attachment_chips"):
        mock_desktop.send_message()

    chat = mock_desktop.chats[0]
    assert len(chat["messages"]) == 1
    content = chat["messages"][0]["content"]
    assert "[Attached File: calc.py" in content
    assert "Explain this script." in content
    # Attachments should be cleared after send
    assert len(mock_desktop.attachments) == 0


def test_desktop_new_and_clear_chat_resets_attachments(mock_desktop):
    mock_desktop.attachments = [AttachedDocument("/tmp/a.py", "a.py", 10, "x=1", "python")]
    with patch.object(mock_desktop, "render_attachment_chips"):
        mock_desktop.new_chat()
        assert len(mock_desktop.attachments) == 0
