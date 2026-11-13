"""Customizable assistant personas and prompt engineering module for JUGNU.

Provides built-in role presets (Default, Code Expert, Creative Writer, Study Tutor,
Concise Analyst) and a local manager for user-created custom personas with tuned
system prompts and sampling temperatures.
"""

import json
import os
import re
import uuid

DEFAULT_PERSONAS_FILE = "jugnu_personas.json"
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 1.5
DEFAULT_TEMPERATURE = 0.6


class Persona:
    """Represents an assistant persona with instructions, parameters, and metadata."""

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        system_prompt: str,
        temperature: float = DEFAULT_TEMPERATURE,
        icon: str = "🤖",
        is_builtin: bool = False,
    ):
        self.id = str(id).strip()
        self.name = str(name).strip()
        self.description = str(description).strip()
        self.system_prompt = str(system_prompt).strip()
        try:
            temp = float(temperature)
        except (ValueError, TypeError):
            temp = DEFAULT_TEMPERATURE
        self.temperature = max(TEMPERATURE_MIN, min(TEMPERATURE_MAX, temp))
        self.icon = str(icon).strip() or "🤖"
        self.is_builtin = bool(is_builtin)

    @property
    def display_label(self) -> str:
        """Formatted label with icon and name for menus and dropdowns."""
        return f"{self.icon}  {self.name}"

    def to_dict(self) -> dict:
        """Serialize persona to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "temperature": round(self.temperature, 2),
            "icon": self.icon,
            "is_builtin": self.is_builtin,
        }

    @classmethod
    def from_dict(cls, data: dict, is_builtin: bool = False):
        """Construct a Persona instance safely from a dictionary."""
        if not isinstance(data, dict):
            raise ValueError("Persona data must be a dictionary")
        return cls(
            id=data.get("id", f"custom_{uuid.uuid4().hex[:8]}"),
            name=data.get("name", "Custom Persona"),
            description=data.get("description", ""),
            system_prompt=data.get("system_prompt", ""),
            temperature=data.get("temperature", DEFAULT_TEMPERATURE),
            icon=data.get("icon", "✨"),
            is_builtin=is_builtin or data.get("is_builtin", False),
        )

    def __repr__(self) -> str:
        return f"<Persona id={self.id!r} name={self.name!r} temp={self.temperature}>"


BUILTIN_PERSONAS = [
    Persona(
        id="default",
        name="Default JUGNU",
        description="Friendly, balanced assistant for general questions and daily help.",
        system_prompt=(
            "You are JUGNU, a helpful, friendly personal AI assistant. "
            "Give clear, concise answers by default (usually 2-5 sentences). "
            "Use easy language. If more detail is needed, provide it."
        ),
        temperature=0.6,
        icon="🤖",
        is_builtin=True,
    ),
    Persona(
        id="code",
        name="Code Expert",
        description="Specialized software engineer for clean code, debugging, and best practices.",
        system_prompt=(
            "You are an expert software engineer and programming mentor. "
            "Write clean, production-ready, well-commented code following idiomatic best practices. "
            "Explain your implementation concisely, prioritize security and performance, and suggest modern idioms."
        ),
        temperature=0.2,
        icon="💻",
        is_builtin=True,
    ),
    Persona(
        id="creative",
        name="Creative Writer",
        description="Imaginative storyteller, poet, and creative brainstorming partner.",
        system_prompt=(
            "You are an imaginative creative writer, storyteller, and brainstorming partner. "
            "Use vivid sensory language, expressive vocabulary, and engaging narrative style. "
            "Embrace bold ideas, emotional resonance, and creative exploration."
        ),
        temperature=0.85,
        icon="✍️",
        is_builtin=True,
    ),
    Persona(
        id="tutor",
        name="Study Tutor",
        description="Patient academic tutor explaining complex concepts step by step.",
        system_prompt=(
            "You are a patient and supportive academic tutor. "
            "Break down complex concepts into simple, intuitive steps using clear analogies and real-world examples. "
            "Ask thought-provoking guiding questions to encourage learning and verify understanding."
        ),
        temperature=0.5,
        icon="🎓",
        is_builtin=True,
    ),
    Persona(
        id="concise",
        name="Concise Analyst",
        description="Direct, factual analyst delivering bullet points with zero filler.",
        system_prompt=(
            "You are a high-efficiency research and business analyst. "
            "Deliver direct, structured, bullet-point answers. "
            "Eliminate conversational filler, pleasantries, and unnecessary preambles. "
            "Focus strictly on facts, key insights, and actionable summaries."
        ),
        temperature=0.3,
        icon="⚡",
        is_builtin=True,
    ),
]

_BUILTIN_MAP = {p.id: p for p in BUILTIN_PERSONAS}


class PersonaManager:
    """Manages built-in and persistent custom personas."""

    def __init__(self, file_path: str = DEFAULT_PERSONAS_FILE):
        self.file_path = file_path
        self._custom_personas = []
        self.load()

    def load(self):
        """Load custom personas from local JSON file."""
        self._custom_personas = []
        if not os.path.exists(self.file_path):
            return

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("id") not in _BUILTIN_MAP:
                        try:
                            self._custom_personas.append(Persona.from_dict(item, is_builtin=False))
                        except Exception:
                            continue
        except Exception:
            # Fall back safely on corrupted or unreadable files
            self._custom_personas = []

    def save(self):
        """Save custom personas to local JSON file."""
        try:
            data = [p.to_dict() for p in self._custom_personas]
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def get(self, persona_id: str) -> Persona:
        """Find persona by ID or name; falls back to default JUGNU persona."""
        if not persona_id:
            return _BUILTIN_MAP["default"]

        norm_id = str(persona_id).strip().lower()
        if norm_id in _BUILTIN_MAP:
            return _BUILTIN_MAP[norm_id]

        for p in self._custom_personas:
            if p.id.lower() == norm_id:
                return p

        # Check by name (case-insensitive)
        for p in self.all():
            if p.name.lower() == norm_id:
                return p

        return _BUILTIN_MAP["default"]

    def all(self) -> list:
        """Return list of all built-in and custom personas."""
        return list(BUILTIN_PERSONAS) + list(self._custom_personas)

    def custom(self) -> list:
        """Return list of user-created custom personas."""
        return list(self._custom_personas)

    def add_custom(
        self,
        name: str,
        description: str,
        system_prompt: str,
        temperature: float = DEFAULT_TEMPERATURE,
        icon: str = "✨",
        persona_id: str = None,
    ) -> Persona:
        """Create and persist a new custom persona."""
        name = str(name).strip()
        if not name:
            raise ValueError("Persona name cannot be empty.")
        if len(name) > 50:
            raise ValueError("Persona name cannot exceed 50 characters.")

        description = str(description or "").strip()
        if len(description) > 150:
            raise ValueError("Persona description cannot exceed 150 characters.")

        system_prompt = str(system_prompt).strip()
        if not system_prompt:
            raise ValueError("System prompt cannot be empty.")

        if not persona_id:
            # Create a slug-like identifier with random suffix
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")
            persona_id = f"custom_{slug[:16]}_{uuid.uuid4().hex[:4]}"
        else:
            persona_id = str(persona_id).strip().lower()

        if persona_id in _BUILTIN_MAP:
            raise ValueError(f"Cannot overwrite built-in persona ID: {persona_id}")

        # Check for existing custom with same id
        existing = next((p for p in self._custom_personas if p.id == persona_id), None)
        if existing:
            raise ValueError(f"A custom persona with ID '{persona_id}' already exists.")

        persona = Persona(
            id=persona_id,
            name=name,
            description=description,
            system_prompt=system_prompt,
            temperature=temperature,
            icon=icon or "✨",
            is_builtin=False,
        )
        self._custom_personas.append(persona)
        self.save()
        return persona

    def update_custom(
        self,
        persona_id: str,
        name: str = None,
        description: str = None,
        system_prompt: str = None,
        temperature: float = None,
        icon: str = None,
    ) -> Persona:
        """Update an existing custom persona."""
        persona = next((p for p in self._custom_personas if p.id == persona_id), None)
        if not persona:
            if persona_id in _BUILTIN_MAP:
                raise ValueError("Built-in personas cannot be modified.")
            raise KeyError(f"Persona '{persona_id}' not found.")

        if name is not None:
            name_str = str(name).strip()
            if not name_str:
                raise ValueError("Persona name cannot be empty.")
            persona.name = name_str

        if description is not None:
            persona.description = str(description).strip()

        if system_prompt is not None:
            prompt_str = str(system_prompt).strip()
            if not prompt_str:
                raise ValueError("System prompt cannot be empty.")
            persona.system_prompt = prompt_str

        if temperature is not None:
            try:
                temp = float(temperature)
            except (ValueError, TypeError):
                temp = DEFAULT_TEMPERATURE
            persona.temperature = max(TEMPERATURE_MIN, min(TEMPERATURE_MAX, temp))

        if icon is not None:
            persona.icon = str(icon).strip() or "✨"

        self.save()
        return persona

    def delete_custom(self, persona_id: str) -> bool:
        """Delete an existing custom persona."""
        if persona_id in _BUILTIN_MAP:
            raise ValueError("Built-in personas cannot be deleted.")

        idx = next((i for i, p in enumerate(self._custom_personas) if p.id == persona_id), None)
        if idx is not None:
            self._custom_personas.pop(idx)
            self.save()
            return True
        return False


# Shared default manager instance
_GLOBAL_MANAGER = None


def get_default_manager() -> PersonaManager:
    """Get or create the singleton PersonaManager."""
    global _GLOBAL_MANAGER
    if _GLOBAL_MANAGER is None:
        _GLOBAL_MANAGER = PersonaManager()
    return _GLOBAL_MANAGER


def get_persona(persona_id: str) -> Persona:
    """Convenience lookup for persona by ID or name."""
    return get_default_manager().get(persona_id)


def list_personas() -> list:
    """Convenience list of all available personas."""
    return get_default_manager().all()
