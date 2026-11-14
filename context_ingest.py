"""Local document attachment, code file ingestion, and context budgeting for JUGNU."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Maximum file size allowed for a single attachment (512 KB)
MAX_FILE_SIZE_BYTES = 512 * 1024

# Default maximum character budget per single attachment (~3,000 tokens)
DEFAULT_MAX_ATTACHMENT_CHARS = 12000

# Mapping of common file extensions to markdown language syntax tags
EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".rs": "rust",
    ".go": "go",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".bat": "batch",
    ".cmd": "batch",
    ".ps1": "powershell",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".xml": "xml",
    ".csv": "csv",
    ".log": "log",
    ".ini": "ini",
    ".toml": "toml",
    ".env": "text",
}


class AttachmentError(Exception):
    """Base exception for file attachment errors."""


class FileNotFoundErrorCustom(AttachmentError):
    """Raised when an attachment file does not exist."""


class BinaryFileError(AttachmentError):
    """Raised when attempting to attach a binary file."""


class FileTooLargeError(AttachmentError):
    """Raised when an attachment exceeds the maximum file size."""


def format_size(size_bytes: int | float | None) -> str:
    """Format byte count into a human-readable string (e.g. '800 B', '2.4 KB')."""
    if size_bytes is None or size_bytes < 0:
        return "0 B"
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def is_binary_sample(sample: bytes) -> bool:
    """Check if byte sample contains null bytes or excessive non-text characters."""
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Count printable ASCII and standard text whitespace
    text_chars = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F})
    non_text = sum(byte not in text_chars for byte in sample)
    return (non_text / len(sample)) > 0.30


def read_text_with_encoding(file_path: str | Path) -> tuple[str, str]:
    """Read a local file using encoding detection and fallback chain."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundErrorCustom(f"File not found: {file_path}")

    size = path.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        raise FileTooLargeError(
            f"File '{path.name}' ({format_size(size)}) exceeds maximum limit of {format_size(MAX_FILE_SIZE_BYTES)}."
        )

    with open(path, "rb") as f:
        raw_bytes = f.read()

    # 1. Check for UTF-16 BOM first (since UTF-16 contains null bytes for ASCII range)
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw_bytes.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass

    # 2. Check for binary content (null bytes or high non-text byte ratio)
    sample = raw_bytes[:8192]
    if is_binary_sample(sample):
        raise BinaryFileError(f"'{path.name}' is a binary file and cannot be attached as text.")

    # 3. Try UTF-8 (with or without BOM)
    try:
        return raw_bytes.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass

    # 4. Try legacy single-byte encodings
    for enc in ["cp1252", "latin-1"]:
        try:
            return raw_bytes.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue

    # Fallback with replacement characters
    return raw_bytes.decode("utf-8", errors="replace"), "utf-8-fallback"


def budget_content(
    content: str,
    max_chars: int = DEFAULT_MAX_ATTACHMENT_CHARS,
) -> tuple[str, bool, int, int]:
    """
    Budget document content to fit within max_chars using a head-and-tail sliding window.

    Returns:
        (budgeted_content, is_truncated, total_lines, kept_lines)
    """
    lines = content.splitlines()
    total_lines = len(lines)

    if len(content) <= max_chars or total_lines <= 4:
        return content, False, total_lines, total_lines

    # Budget allocation: 60% head, 40% tail
    head_char_budget = int(max_chars * 0.60)
    tail_char_budget = int(max_chars * 0.40)

    head_lines: list[str] = []
    head_len = 0
    for line in lines:
        if head_len + len(line) + 1 > head_char_budget and head_lines:
            break
        head_lines.append(line)
        head_len += len(line) + 1

    tail_lines: list[str] = []
    tail_len = 0
    for line in reversed(lines[len(head_lines):]):
        if tail_len + len(line) + 1 > tail_char_budget and tail_lines:
            break
        tail_lines.append(line)
        tail_len += len(line) + 1
    tail_lines.reverse()

    kept_lines = len(head_lines) + len(tail_lines)
    omitted_lines = total_lines - kept_lines

    divider = f"\n[... {omitted_lines} lines omitted to fit model context window ...]\n"
    budgeted = "\n".join(head_lines) + divider + "\n".join(tail_lines)

    return budgeted, True, total_lines, kept_lines


@dataclass
class AttachedDocument:
    """Represents a validated, budgeted local file attached to the conversation."""

    path: str
    filename: str
    size_bytes: int
    content: str
    language: str
    encoding: str = "utf-8"
    is_truncated: bool = False
    total_lines: int = 0
    kept_lines: int = 0

    @property
    def formatted_size(self) -> str:
        return format_size(self.size_bytes)

    def format_for_prompt(self) -> str:
        """Format the attached document with clean markdown code fences."""
        tag = self.language if self.language else "text"
        note = f" (truncated {self.total_lines - self.kept_lines} lines)" if self.is_truncated else ""
        header = f"[Attached File: {self.filename} ({self.formatted_size}){note}]"
        return f"{header}\n```{tag}\n{self.content}\n```"


def ingest_file(
    file_path: str | Path,
    max_chars: int = DEFAULT_MAX_ATTACHMENT_CHARS,
) -> AttachedDocument:
    """
    Ingest a local document file, validating size, detecting encoding,
    and applying context budgeting.
    """
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundErrorCustom(f"File not found: {file_path}")
    if not path.is_file():
        raise AttachmentError(f"Target is a directory, not a file: {file_path}")

    size_bytes = path.stat().st_size
    raw_text, encoding = read_text_with_encoding(path)

    budgeted_text, is_truncated, total_lines, kept_lines = budget_content(
        raw_text, max_chars=max_chars
    )

    ext = path.suffix.lower()
    language = EXTENSION_LANGUAGE_MAP.get(ext, "text")

    return AttachedDocument(
        path=str(path),
        filename=path.name,
        size_bytes=size_bytes,
        content=budgeted_text,
        language=language,
        encoding=encoding,
        is_truncated=is_truncated,
        total_lines=total_lines,
        kept_lines=kept_lines,
    )


def assemble_prompt_with_attachments(
    user_text: str,
    attachments: Sequence[AttachedDocument] | None = None,
) -> str:
    """
    Assemble the final message string combining attached file contexts
    and the user's inquiry.
    """
    clean_text = user_text.strip() if user_text else ""
    if not attachments:
        return clean_text

    blocks = [doc.format_for_prompt() for doc in attachments]
    context_block = "\n\n".join(blocks)

    if clean_text:
        return f"{context_block}\n\nUser Inquiry:\n{clean_text}"
    return f"{context_block}\n\nPlease review and explain the attached file(s) above."
