"""
JUGNU Offline Persistent User Memory & Preference Profile Engine (Issue #65).

Provides 100% offline, privacy-preserving persistent memory, heuristic and explicit
fact extraction, deduplication, atomic JSON persistence, and prompt context injection.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_MEMORY_FILE = "jugnu_memory.json"
MAX_MEMORY_CONTEXT_CHARS = 600


@dataclass
class MemoryFact:
    """Individual user fact, preference, or persistent instruction."""

    id: str
    category: str  # preference, tech_stack, personal, instruction, custom
    key: str
    value: str
    source_chat_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MemoryFact:
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            category=data.get("category", "custom"),
            key=data.get("key", "fact"),
            value=data.get("value", ""),
            source_chat_id=data.get("source_chat_id"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            enabled=data.get("enabled", True),
        )


class MemoryStore:
    """Thread-safe persistent store for user memory facts with atomic writes."""

    def __init__(self, file_path: Optional[str | Path] = None):
        if file_path is None:
            self.file_path = Path(os.environ.get("JUGNU_MEMORY_FILE", DEFAULT_MEMORY_FILE)).resolve()
        else:
            self.file_path = Path(file_path).resolve()

        self._lock = threading.RLock()
        self._enabled: bool = True
        self._facts: Dict[str, MemoryFact] = {}
        self.load()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            self.save()

    def load(self) -> None:
        """Load memory facts from persistent JSON file."""
        with self._lock:
            self._facts.clear()
            if not self.file_path.exists():
                return
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                self._enabled = data.get("system_enabled", True)
                facts_data = data.get("facts", [])
                for item in facts_data:
                    fact = MemoryFact.from_dict(item)
                    self._facts[fact.id] = fact
            except Exception:
                # Corrupted or unreadable memory starts clean without crashing
                self._facts.clear()

    def save(self) -> None:
        """Atomically persist memory store to disk using a temp file."""
        with self._lock:
            data = {
                "system_enabled": self._enabled,
                "version": "1.0",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "facts": [f.to_dict() for f in self._facts.values()],
            }
            content = json.dumps(data, indent=2, ensure_ascii=False)

            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file in the same directory, then atomic rename
            temp_file = tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.file_path.parent,
                delete=False,
                encoding="utf-8",
            )
            temp_file.write(content)
            temp_file.flush()
            temp_file.close()

            try:
                os.replace(temp_file.name, self.file_path)
            except Exception:
                if os.path.exists(temp_file.name):
                    try:
                        os.remove(temp_file.name)
                    except OSError:
                        pass
                raise

    def add_fact(
        self,
        category: str,
        key: str,
        value: str,
        source_chat_id: Optional[str] = None,
        enabled: bool = True,
    ) -> MemoryFact:
        """Add or update an existing fact by matching normalized key."""
        with self._lock:
            norm_key = key.strip().lower()
            # Deduplicate if an existing fact shares the exact normalized key & category
            for fact in self._facts.values():
                if fact.key.strip().lower() == norm_key and fact.category == category:
                    fact.value = value.strip()
                    fact.enabled = enabled
                    if source_chat_id:
                        fact.source_chat_id = source_chat_id
                    self.save()
                    return fact

            new_id = uuid.uuid4().hex[:12]
            fact = MemoryFact(
                id=new_id,
                category=category,
                key=key.strip(),
                value=value.strip(),
                source_chat_id=source_chat_id,
                enabled=enabled,
            )
            self._facts[new_id] = fact
            self.save()
            return fact

    def update_fact(self, fact_id: str, **kwargs: Any) -> Optional[MemoryFact]:
        with self._lock:
            fact = self._facts.get(fact_id)
            if not fact:
                return None
            for k, v in kwargs.items():
                if hasattr(fact, k):
                    setattr(fact, k, v)
            self.save()
            return fact

    def delete_fact(self, fact_id: str) -> bool:
        with self._lock:
            if fact_id in self._facts:
                del self._facts[fact_id]
                self.save()
                return True
            return False

    def get_fact(self, fact_id: str) -> Optional[MemoryFact]:
        with self._lock:
            return self._facts.get(fact_id)

    def get_all_facts(self) -> List[MemoryFact]:
        with self._lock:
            return sorted(self._facts.values(), key=lambda f: f.created_at, reverse=True)

    def get_active_facts(self) -> List[MemoryFact]:
        with self._lock:
            if not self._enabled:
                return []
            return [f for f in self.get_all_facts() if f.enabled]

    def search_facts(self, query: str) -> List[MemoryFact]:
        with self._lock:
            q = query.strip().lower()
            if not q:
                return self.get_all_facts()
            return [
                f
                for f in self.get_all_facts()
                if q in f.key.lower() or q in f.value.lower() or q in f.category.lower()
            ]

    def clear_all(self) -> None:
        with self._lock:
            self._facts.clear()
            self.save()

    def export_memory(self) -> str:
        with self._lock:
            return json.dumps(
                {
                    "system_enabled": self._enabled,
                    "facts": [f.to_dict() for f in self._facts.values()],
                },
                indent=2,
            )

    def import_memory(self, json_str: str) -> int:
        with self._lock:
            imported = 0
            try:
                data = json.loads(json_str)
                facts_data = data.get("facts", [])
                for item in facts_data:
                    fact = MemoryFact.from_dict(item)
                    self._facts[fact.id] = fact
                    imported += 1
                self.save()
            except Exception:
                pass
            return imported

    def build_memory_context(self, max_chars: int = MAX_MEMORY_CONTEXT_CHARS) -> str:
        """Format active memory facts into a concise system prompt injection block."""
        with self._lock:
            active = self.get_active_facts()
            if not active:
                return ""

            lines = ["[Persistent User Memory & Guidelines]"]
            current_len = len(lines[0])

            for fact in active:
                line = f"• {fact.key}: {fact.value}"
                if current_len + len(line) + 1 > max_chars:
                    break
                lines.append(line)
                current_len += len(line) + 1

            if len(lines) <= 1:
                return ""

            return "\n".join(lines)


# Singleton memory store
_global_store: Optional[MemoryStore] = None
_global_lock = threading.Lock()


def get_memory_store() -> MemoryStore:
    global _global_store
    with _global_lock:
        if _global_store is None:
            _global_store = MemoryStore()
        return _global_store


def extract_facts(user_message: str) -> List[Dict[str, str]]:
    """Extract explicit and implicit user facts and preferences from message text."""
    facts: List[Dict[str, str]] = []
    text = user_message.strip()

    # 1. Explicit triggers: "Remember that X", "Remember X", "Yaad rakhna ki X", "Yaad rakhna X"
    rem_patterns = [
        r"^(?:please\s+)?remember\s+(?:that\s+)?(.+)$",
        r"^(?:kripya\s+)?yaad\s+rakhna\s+(?:ki\s+)?(.+)$",
    ]
    for pattern in rem_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            fact_body = match.group(1).strip()
            # If fact is of format "key is value" or "I prefer value"
            key = "User Note"
            val = fact_body
            if " is " in fact_body:
                parts = fact_body.split(" is ", 1)
                key = parts[0].strip().title()
                val = parts[1].strip()
            elif " prefer " in fact_body:
                parts = fact_body.split(" prefer ", 1)
                key = "Preference"
                val = f"Prefers {parts[1].strip()}"
            facts.append({"category": "instruction", "key": key, "value": val})
            return facts

    # 2. Name triggers: "My name is X", "Mera naam X hai"
    name_patterns = [
        r"\bmy\s+name\s+is\s+([A-Z][a-zA-Z\s]{1,30})\b",
        r"\bmera\s+naam\s+([A-Z][a-zA-Z\s]{1,30})\s+hai\b",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            facts.append({"category": "personal", "key": "User Name", "value": name})

    # 3. Preference triggers: "I prefer X", "Mujhe X pasand hai"
    pref_match = re.search(r"\b(?:i\s+prefer|i\s+like)\s+([^.,;!\n]+)", text, re.IGNORECASE)
    if pref_match:
        pref = pref_match.group(1).strip()
        facts.append({"category": "preference", "key": "Preference", "value": pref})

    # 4. Tech Stack heuristic extraction
    tech_patterns = [
        (r"\b(python\s*3\.\d+|python)\b", "Tech Stack", "Python"),
        (r"\b(fastapi)\b", "Framework", "FastAPI"),
        (r"\b(django)\b", "Framework", "Django"),
        (r"\b(react(?:\.js)?)\b", "Frontend", "React"),
        (r"\b(pyside6|pyqt\d?)\b", "GUI Framework", "PySide6"),
        (r"\b(ollama)\b", "LLM Backend", "Ollama"),
        (r"\b(docker|kubernetes)\b", "DevOps", "Docker/K8s"),
    ]
    for pat, key, val in tech_patterns:
        if re.search(pat, text, re.IGNORECASE) and re.search(r"\b(i\s+use|we\s+use|working\s+with|stack|tech)\b", text, re.IGNORECASE):
            facts.append({"category": "tech_stack", "key": key, "value": val})

    return facts
