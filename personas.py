"""Multi-Persona System Prompt Studio & Custom AI Persona Switcher for JUGNU.

Provides:
- Built-in library of specialized presets (Senior Code Reviewer, Python Architect,
  Academic Writing Tutor, Socratic Teacher, Debugging Specialist, General Assistant).
- Custom persona creation, editing, cloning, deletion, and local persistence.
- Schema validation for custom personas and import/export bundles (.json).
- Per-conversation persona binding so changing personas never alters past chats.
- Dynamic system prompt construction with model temperature & context window overrides.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union


PERSONAS_FILE = "jugnu_personas.json"
SCHEMA_VERSION = "1.0"

VALID_CATEGORIES = ["General", "Coding", "Writing", "Analysis", "Education", "Custom"]


@dataclass
class Persona:
    """Represents an AI behavioral persona with custom instructions and model options."""

    @property
    def icon(self) -> str:
        return self.avatar

    @property
    def display_label(self) -> str:
        return f"{self.avatar}  {self.name}"

    id: str
    name: str
    avatar: str = "🤖"
    category: str = "General"
    description: str = ""
    system_prompt: str = ""
    temperature: float = 0.6
    num_ctx: Optional[int] = None
    is_builtin: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert persona instance to a serializable dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "avatar": self.avatar,
            "category": self.category,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "temperature": round(float(self.temperature), 2),
            "num_ctx": int(self.num_ctx) if self.num_ctx else None,
            "is_builtin": bool(self.is_builtin),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Persona":
        """Instantiate a Persona from dictionary, filling defaults where necessary."""
        return cls(
            id=str(data.get("id", str(uuid.uuid4()))),
            name=str(data.get("name", "Custom Persona")).strip(),
            avatar=str(data.get("avatar", "🤖")).strip() or "🤖",
            category=str(data.get("category", "General")).strip() or "General",
            description=str(data.get("description", "")).strip(),
            system_prompt=str(data.get("system_prompt", "")).strip(),
            temperature=float(data.get("temperature", 0.6)),
            num_ctx=int(data["num_ctx"]) if data.get("num_ctx") else None,
            is_builtin=bool(data.get("is_builtin", False)),
            created_at=str(data.get("created_at", datetime.now().isoformat())),
            updated_at=str(data.get("updated_at", datetime.now().isoformat())),
        )

    def validate(self) -> Tuple[bool, Optional[str]]:
        """Validate persona attributes against schema constraints."""
        if not self.id or not isinstance(self.id, str):
            return False, "Persona 'id' must be a non-empty string."
        if not self.name or not isinstance(self.name, str) or len(self.name.strip()) == 0:
            return False, "Persona 'name' cannot be empty."
        if len(self.name) > 60:
            return False, "Persona 'name' cannot exceed 60 characters."
        if not self.avatar or not isinstance(self.avatar, str) or len(self.avatar.strip()) == 0:
            return False, "Persona 'avatar' cannot be empty."
        if not self.system_prompt or not isinstance(self.system_prompt, str) or len(self.system_prompt.strip()) == 0:
            return False, "Persona 'system_prompt' cannot be empty."
        try:
            temp = float(self.temperature)
            if not (0.0 <= temp <= 2.0):
                return False, "Persona 'temperature' must be between 0.0 and 2.0."
        except (ValueError, TypeError):
            return False, "Persona 'temperature' must be a valid number."
        if self.num_ctx is not None:
            try:
                ctx = int(self.num_ctx)
                if ctx < 512 or ctx > 131072:
                    return False, "Persona 'num_ctx' must be between 512 and 131072 tokens."
            except (ValueError, TypeError):
                return False, "Persona 'num_ctx' must be an integer."
        return True, None


# ---------------------------------------------------------------------------
# Built-in Specialized Preset Personas Library
# ---------------------------------------------------------------------------

BUILTIN_PERSONAS: List[Persona] = [
    Persona(
        id="default",
        name="JUGNU General Assistant",
        avatar="🤖",
        category="General",
        description="Friendly, balanced, everyday desktop assistant for general questions and tasks.",
        system_prompt=(
            "You are JUGNU, a helpful, friendly personal AI assistant. "
            "Give clear, concise answers by default (usually 2-5 sentences). "
            "Use easy language. If more detail is needed, provide it."
        ),
        temperature=0.6,
        num_ctx=None,
        is_builtin=True,
    ),
    Persona(
        id="senior_reviewer",
        name="Senior Code Reviewer",
        avatar="🧐",
        category="Coding",
        description="Critically reviews code for security vulnerabilities, edge cases, scalability, and clean architecture.",
        system_prompt=(
            "You are a Staff / Principal Software Engineer conducting rigorous code reviews. "
            "When analyzing or writing code: "
            "1. Scrutinize architecture, design patterns, algorithmic efficiency, and edge cases. "
            "2. Identify potential memory leaks, race conditions, concurrency bugs, and security risks. "
            "3. Provide actionable, concise recommendations with before-and-after code snippets. "
            "4. Maintain a constructive, pragmatic, and high-standard engineering tone."
        ),
        temperature=0.2,
        num_ctx=4096,
        is_builtin=True,
    ),
    Persona(
        id="python_architect",
        name="Python Architect",
        avatar="🐍",
        category="Coding",
        description="Designs modern, idiomatic, PEP 8 compliant, type-annotated Python software systems.",
        system_prompt=(
            "You are an expert Python Software Architect. You craft idiomatic, robust, and clean Python code. "
            "Adhere strictly to PEP 8, modern type annotations, dataclasses, context managers, and modular architecture. "
            "Favor the standard library where practical. Write clean docstrings and include unit test examples "
            "using pytest for all proposed solutions."
        ),
        temperature=0.25,
        num_ctx=4096,
        is_builtin=True,
    ),
    Persona(
        id="writing_tutor",
        name="Academic Writing Tutor",
        avatar="📚",
        category="Writing",
        description="Polishes scholarly prose, sharpens thesis statements, and strengthens argumentative rigor.",
        system_prompt=(
            "You are an experienced University Academic Writing Tutor and Editor. "
            "Help the writer express complex ideas with clarity, academic precision, and logical cohesion. "
            "Strengthen thesis statements, eliminate passive fluff, enhance paragraph transitions, "
            "and suggest authoritative vocabulary while preserving the original voice."
        ),
        temperature=0.5,
        num_ctx=4096,
        is_builtin=True,
    ),
    Persona(
        id="socratic_teacher",
        name="Socratic Teacher",
        avatar="🏛️",
        category="Education",
        description="Guides learning through step-by-step inquiry, probing questions, and conceptual self-discovery.",
        system_prompt=(
            "You are a master Socratic Educator. Instead of merely handing over final answers, "
            "you guide the student through purposeful, probing questions that lead them to discover "
            "the core concepts on their own. Break complex topics into intuitive steps, acknowledge correct insights, "
            "and gently illuminate logical contradictions."
        ),
        temperature=0.7,
        num_ctx=4096,
        is_builtin=True,
    ),
    Persona(
        id="debugger",
        name="Debugging Specialist",
        avatar="🔍",
        category="Analysis",
        description="Systematically dissects stack traces, isolates root causes, and proposes defensive fixes.",
        system_prompt=(
            "You are an elite Software Debugging Specialist. When encountering an error or unexpected behavior: "
            "1. Carefully dissect the stack trace, error logs, and system environment. "
            "2. Formulate testable hypotheses regarding the true root cause (state mismatch, race condition, data type). "
            "3. Offer targeted print/logging statements or breakpoints to isolate the defect. "
            "4. Deliver minimal, defensive, and thoroughly verified bug fixes."
        ),
        temperature=0.2,
        num_ctx=4096,
        is_builtin=True,
    ),
]


# ---------------------------------------------------------------------------
# Persona Manager & Local Persistence
# ---------------------------------------------------------------------------

class PersonaManager:
    """Manages built-in and user-defined custom personas, storage, and import/export."""

    def __init__(self, storage_path: str = PERSONAS_FILE):
        self.storage_path = storage_path
        self._builtins: Dict[str, Persona] = {p.id: p for p in BUILTIN_PERSONAS}
        self._custom: Dict[str, Persona] = {}
        self.load_from_file()

    def get(self, persona_id: str) -> Persona:
        """Alias for get_persona for backward compatibility."""
        return self.get_persona(persona_id)

    def custom(self) -> List[Persona]:
        """Return list of custom user-created personas."""
        return [p for p in self.list_personas() if not p.is_builtin]

    def add_custom(self, name: str, description: str, system_prompt: str,
                   temperature: float = 0.6, icon: str = "🤖", **kwargs) -> Persona:
        p = Persona(
            id=str(uuid.uuid4())[:8],
            name=name,
            avatar=icon,
            description=description,
            system_prompt=system_prompt,
            temperature=temperature,
            is_builtin=False,
            **kwargs
        )
        self.save_persona(p)
        return p

    def update_custom(self, persona_id: str, **kwargs) -> Optional[Persona]:
        p = self.get_persona(persona_id)
        if not p or p.is_builtin:
            return None
        for k, v in kwargs.items():
            if k == "icon":
                p.avatar = v
            elif hasattr(p, k):
                setattr(p, k, v)
        self.save_persona(p)
        return p

    def delete_custom(self, persona_id: str) -> bool:
        return self.delete_persona(persona_id)

    def get_persona(self, persona_id: Optional[str]) -> Persona:
        """Retrieve a persona by ID, falling back to the default preset if not found."""
        if not persona_id:
            return self._builtins["default"]
        if persona_id in self._custom:
            return self._custom[persona_id]
        if persona_id in self._builtins:
            return self._builtins[persona_id]
        return self._builtins["default"]

    def list_personas(self, category: Optional[str] = None) -> List[Persona]:
        """List all personas (built-ins first, then custom), optionally filtered by category."""
        all_personas = list(self._builtins.values()) + list(self._custom.values())
        if category and category != "All":
            return [p for p in all_personas if p.category.lower() == category.lower()]
        return all_personas

    def list_categories(self) -> List[str]:
        """Return a sorted list of unique categories across all personas."""
        cats = set(VALID_CATEGORIES)
        for p in self._custom.values():
            if p.category:
                cats.add(p.category)
        return sorted(cats, key=lambda c: (c != "General", c != "Coding", c != "Writing", c))

    def add_persona(self, persona: Persona) -> Persona:
        """Add and persist a new custom persona."""
        persona.is_builtin = False
        valid, err = persona.validate()
        if not valid:
            raise ValueError(f"Invalid persona: {err}")

        # Ensure ID uniqueness against built-ins and custom
        if persona.id in self._builtins:
            persona.id = f"{persona.id}_{uuid.uuid4().hex[:6]}"

        persona.created_at = datetime.now().isoformat()
        persona.updated_at = datetime.now().isoformat()
        self._custom[persona.id] = persona
        self.save_to_file()
        return persona

    def update_persona(self, persona_id: str, updates: Dict[str, Any]) -> Persona:
        """Update an existing custom persona. Built-in presets cannot be overwritten."""
        if persona_id in self._builtins:
            raise ValueError("Built-in personas cannot be modified. Duplicate as a custom persona instead.")

        if persona_id not in self._custom:
            raise KeyError(f"Persona '{persona_id}' not found.")

        current = self._custom[persona_id]
        data = current.to_dict()
        for k, v in updates.items():
            if k not in ("id", "is_builtin", "created_at"):
                data[k] = v
        data["updated_at"] = datetime.now().isoformat()

        updated = Persona.from_dict(data)
        valid, err = updated.validate()
        if not valid:
            raise ValueError(f"Invalid persona update: {err}")

        self._custom[persona_id] = updated
        self.save_to_file()
        return updated

    def delete_persona(self, persona_id: str) -> bool:
        """Delete a custom persona. Built-in presets cannot be deleted."""
        if persona_id in self._builtins:
            raise ValueError("Built-in preset personas cannot be deleted.")
        if persona_id in self._custom:
            del self._custom[persona_id]
            self.save_to_file()
            return True
        return False

    def duplicate_persona(self, source_id: str, new_name: Optional[str] = None) -> Persona:
        """Clone an existing built-in or custom persona into a new custom editable persona."""
        source = self.get_persona(source_id)
        clone_id = f"custom_{re.sub(r'[^a-zA-Z0-9_]', '', source.id)}_{uuid.uuid4().hex[:6]}"
        name = new_name or f"{source.name} (Copy)"
        clone = Persona(
            id=clone_id,
            name=name,
            avatar=source.avatar,
            category=source.category if source.category != "General" else "Custom",
            description=source.description,
            system_prompt=source.system_prompt,
            temperature=source.temperature,
            num_ctx=source.num_ctx,
            is_builtin=False,
        )
        return self.add_persona(clone)

    def load_from_file(self, path: Optional[str] = None):
        """Load custom personas from local JSON storage."""
        file_path = path or self.storage_path
        self._custom.clear()
        if not os.path.exists(file_path):
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            personas_list = []
            if isinstance(data, dict) and "personas" in data:
                personas_list = data["personas"]
            elif isinstance(data, list):
                personas_list = data

            for item in personas_list:
                if isinstance(item, dict):
                    p = Persona.from_dict(item)
                    p.is_builtin = False
                    valid, _ = p.validate()
                    if valid and p.id not in self._builtins:
                        self._custom[p.id] = p
        except (json.JSONDecodeError, OSError):
            # Corrupted file is kept safely; does not crash app
            pass

    def save_to_file(self, path: Optional[str] = None):
        """Save custom personas to local JSON storage."""
        file_path = path or self.storage_path
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": datetime.now().isoformat(),
            "personas": [p.to_dict() for p in self._custom.values()],
        }
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

        tmp_path = f"{file_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            if os.path.exists(file_path):
                os.replace(tmp_path, file_path)
            else:
                os.rename(tmp_path, file_path)
        except OSError:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # -----------------------------------------------------------------------
    # Import / Export Capabilities
    # -----------------------------------------------------------------------

    def export_personas(self, persona_ids: Optional[List[str]] = None, indent: int = 2) -> str:
        """Export specified personas (or all available personas) as a JSON bundle string."""
        if persona_ids:
            selected = [self.get_persona(pid) for pid in persona_ids]
        else:
            selected = self.list_personas()

        bundle = {
            "version": SCHEMA_VERSION,
            "exported_at": datetime.now().isoformat(),
            "exported_by": "JUGNU Persona Studio",
            "count": len(selected),
            "personas": [p.to_dict() for p in selected],
        }
        return json.dumps(bundle, indent=indent, ensure_ascii=False)

    def export_to_file(self, file_path: str, persona_ids: Optional[List[str]] = None) -> str:
        """Export persona bundle to a .json file path."""
        content = self.export_personas(persona_ids=persona_ids)
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return file_path

    def import_personas(self, json_data_or_str: Union[str, Dict[str, Any], List[Any]]) -> Tuple[int, List[str]]:
        """Import personas from JSON data with rigorous schema validation.

        Returns (imported_count, error_messages).
        """
        if isinstance(json_data_or_str, str):
            try:
                data = json.loads(json_data_or_str)
            except json.JSONDecodeError as e:
                return 0, [f"Invalid JSON format: {e}"]
        else:
            data = json_data_or_str

        raw_list = []
        if isinstance(data, dict):
            if "personas" in data and isinstance(data["personas"], list):
                raw_list = data["personas"]
            elif "name" in data and "system_prompt" in data:
                raw_list = [data]  # single persona object
            else:
                return 0, ["JSON object does not contain a 'personas' list or valid persona definition."]
        elif isinstance(data, list):
            raw_list = data
        else:
            return 0, ["Expected a JSON object or array of personas."]

        imported_count = 0
        errors = []

        for idx, item in enumerate(raw_list):
            if not isinstance(item, dict):
                errors.append(f"Item #{idx + 1} is not a valid dictionary object.")
                continue

            try:
                persona = Persona.from_dict(item)
                persona.is_builtin = False

                # Generate fresh ID if collision occurs with built-ins or existing custom
                if persona.id in self._builtins or persona.id in self._custom:
                    persona.id = f"imported_{re.sub(r'[^a-zA-Z0-9_]', '', persona.id)}_{uuid.uuid4().hex[:6]}"

                valid, err = persona.validate()
                if not valid:
                    errors.append(f"Persona '{persona.name}': {err}")
                    continue

                self._custom[persona.id] = persona
                imported_count += 1
            except Exception as ex:
                errors.append(f"Item #{idx + 1}: {ex}")

        if imported_count > 0:
            self.save_to_file()

        return imported_count, errors

    def import_from_file(self, file_path: str) -> Tuple[int, List[str]]:
        """Import personas from a .json file path."""
        if not os.path.exists(file_path):
            return 0, [f"File not found: {file_path}"]
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return self.import_personas(content)


# ---------------------------------------------------------------------------
# Backward Compatibility Helpers
# ---------------------------------------------------------------------------
DEFAULT_PERSONAS_FILE = PERSONAS_FILE
DEFAULT_TEMPERATURE = 0.6
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 1.5

_DEFAULT_MANAGER: Optional[PersonaManager] = None

def get_default_manager() -> PersonaManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = PersonaManager()
    return _DEFAULT_MANAGER

def get_persona(persona_id: str) -> Persona:
    return get_default_manager().get_persona(persona_id)

def list_personas() -> List[Persona]:
    return get_default_manager().list_personas()
