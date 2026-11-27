"""Offline, versioned conversation import with a preview before any write."""

import json
import hashlib
import os
import uuid
from dataclasses import dataclass

import jugnu_core as core
from history_lock import HistoryLock


MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_CONVERSATIONS = 10000
MAX_MESSAGES = 10000


@dataclass(frozen=True)
class ImportPreview:
    new: int
    duplicate: int
    conflicting: int
    rejected: int
    fingerprint: str = ""


def read_import(path):
    """Read one portable v1 file, rejecting oversized or unsupported documents."""
    if os.path.getsize(path) > MAX_IMPORT_BYTES:
        raise ValueError("Import file exceeds the 8 MiB limit")
    with open(path, "rb") as source:
        raw = source.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError("Import file exceeds the 8 MiB limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Import file is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported conversation import version")
    conversations = payload.get("conversations")
    if not isinstance(conversations, list) or len(conversations) > MAX_CONVERSATIONS:
        raise ValueError("Invalid conversation list")
    return conversations, hashlib.sha256(raw).hexdigest()


def _validate_chat(value):
    if not isinstance(value, dict):
        return None
    chat_id, title, messages = value.get("id"), value.get("title"), value.get("messages")
    if not isinstance(chat_id, str) or not chat_id.strip() or len(chat_id) > 128:
        return None
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        return None
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        return None
    clean_messages = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            return None
        content = message.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 1_000_000:
            return None
        clean = {"role": message["role"], "content": content}
        if "time" in message:
            if not isinstance(message["time"], str) or len(message["time"]) > 100:
                return None
            clean["time"] = message["time"]
        clean_messages.append(clean)
    return {"id": chat_id, "title": title, "messages": clean_messages}


def _signature(chat):
    return json.dumps((chat["title"], chat["messages"]), ensure_ascii=False, sort_keys=True)


def plan_import(existing, imported, *, conflict="skip"):
    """Return a preview and merged chats; conflict may be skip or fork."""
    if conflict not in ("skip", "fork"):
        raise ValueError("Conflict choice must be skip or fork")
    merged = list(existing)
    known_ids = {chat.get("id") for chat in existing if isinstance(chat, dict)}
    signatures = {
        _signature(clean) for chat in existing
        if (clean := _validate_chat(chat)) is not None
    }
    new = duplicate = conflicting = rejected = 0
    for value in imported:
        chat = _validate_chat(value)
        if chat is None:
            rejected += 1
            continue
        signature = _signature(chat)
        if signature in signatures:
            duplicate += 1
            continue
        if chat["id"] in known_ids:
            conflicting += 1
            if conflict == "skip":
                continue
            chat["id"] = str(uuid.uuid4())
            while chat["id"] in known_ids:
                chat["id"] = str(uuid.uuid4())
        else:
            new += 1
        merged.append(chat)
        known_ids.add(chat["id"])
        signatures.add(signature)
    return ImportPreview(new, duplicate, conflicting, rejected), merged


def _read_existing(path):
    try:
        with open(path, encoding="utf-8") as source:
            chats = json.load(source)
    except FileNotFoundError:
        return []
    if not isinstance(chats, list):
        raise ValueError("Existing history is not a conversation list")
    return chats


def preview_import(import_path, history_path):
    imported, fingerprint = read_import(import_path)
    preview = plan_import(_read_existing(history_path), imported)[0]
    return ImportPreview(preview.new, preview.duplicate, preview.conflicting,
                         preview.rejected, fingerprint)


def commit_import(import_path, history_path, *, conflict="skip", held_lock=None,
                  expected_fingerprint=None):
    """Recheck the import under the history lock and persist it in one replace."""
    imported, fingerprint = read_import(import_path)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise ValueError("Import file changed after preview; preview it again")
    lock = held_lock or HistoryLock(history_path)
    if held_lock is not None and (held_lock._file is None or
                                 held_lock.path != HistoryLock(history_path).path):
        raise RuntimeError("The matching history lock must already be held")
    if held_lock is None:
        lock.acquire()
    try:
        preview, merged = plan_import(_read_existing(history_path), imported, conflict=conflict)
        if len(merged) > 0 and (preview.new > 0 or (conflict == "fork" and preview.conflicting > 0)):
            core.save_history(merged, history_path)
        return preview
    finally:
        if held_lock is None:
            lock.release()
