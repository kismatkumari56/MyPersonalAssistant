"""Model discovery, formatting, selection, and streaming pull management for Ollama models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generator, Sequence

import requests

DEFAULT_MODEL = "llama3.2:3b"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_TAGS_ENDPOINT = f"{DEFAULT_OLLAMA_HOST}/api/tags"
DEFAULT_PULL_ENDPOINT = f"{DEFAULT_OLLAMA_HOST}/api/pull"
DEFAULT_CHAT_ENDPOINT = f"{DEFAULT_OLLAMA_HOST}/api/chat"

RECOMMENDED_MODELS = [
    {"name": "llama3.2:1b", "desc": "1.3 GB - Ultra-fast & lightweight (ideal for laptops/CPUs)"},
    {"name": "llama3.2:3b", "desc": "2.0 GB - Balanced default, smart and responsive"},
    {"name": "qwen2.5:1.5b", "desc": "1.0 GB - High-quality reasoning and multilingual"},
    {"name": "qwen2.5-coder:1.5b", "desc": "1.0 GB - Specialized lightweight coding assistant"},
    {"name": "deepseek-r1:1.5b", "desc": "1.1 GB - Powerful reasoning & chain-of-thought"},
    {"name": "mistral:7b", "desc": "4.1 GB - High capability 7B general assistant"},
]


def format_bytes(size_bytes: int | float | None) -> str:
    """Format a byte count into a human-readable string (e.g., '777.6 MB', '2.0 GB')."""
    if size_bytes is None or size_bytes < 0:
        return "0 B"
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


@dataclass
class ModelInfo:
    """Structured metadata for an installed local Ollama model."""

    name: str
    size: int = 0
    parameter_size: str = ""
    family: str = ""
    quantization_level: str = ""
    modified_at: str = ""

    @property
    def display_name(self) -> str:
        """Return a user-friendly label for dropdowns and menus."""
        tags = []
        if self.parameter_size:
            tags.append(self.parameter_size)
        if self.size > 0:
            tags.append(format_bytes(self.size))
        if tags:
            return f"{self.name} ({', '.join(tags)})"
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "parameter_size": self.parameter_size,
            "family": self.family,
            "quantization_level": self.quantization_level,
            "modified_at": self.modified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelInfo:
        details = data.get("details") or {}
        name = data.get("name") or data.get("model") or ""
        size = data.get("size") or 0
        parameter_size = details.get("parameter_size") or data.get("parameter_size") or ""
        family = details.get("family") or data.get("family") or ""
        quantization_level = details.get("quantization_level") or data.get("quantization_level") or ""
        modified_at = data.get("modified_at") or ""
        return cls(
            name=name,
            size=int(size) if isinstance(size, (int, float, str)) and str(size).isdigit() else 0,
            parameter_size=str(parameter_size),
            family=str(family),
            quantization_level=str(quantization_level),
            modified_at=str(modified_at),
        )


def fetch_local_models(
    base_url: str = DEFAULT_OLLAMA_HOST,
    timeout: float = 3.0,
) -> list[ModelInfo]:
    """
    Query the local Ollama daemon for installed models via GET /api/tags.

    Returns a list of ModelInfo objects. If Ollama is offline or returns an error,
    returns an empty list without raising an unhandled exception.
    """
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code != 200:
            return []
        data = response.json()
        raw_models = data.get("models")
        if not isinstance(raw_models, list):
            return []
        models: list[ModelInfo] = []
        for item in raw_models:
            if isinstance(item, dict):
                info = ModelInfo.from_dict(item)
                if info.name:
                    models.append(info)
        # Sort by name alphabetically
        models.sort(key=lambda m: m.name.lower())
        return models
    except (requests.RequestException, ValueError, TypeError):
        return []


def get_available_model_names(
    base_url: str = DEFAULT_OLLAMA_HOST,
    timeout: float = 3.0,
) -> list[str]:
    """Return a list of model names found locally, or fallback to [DEFAULT_MODEL] if none found."""
    models = fetch_local_models(base_url=base_url, timeout=timeout)
    if models:
        return [m.name for m in models]
    return [DEFAULT_MODEL]


def resolve_active_model(
    preferred_model: str | None,
    available_models: Sequence[str] | Sequence[ModelInfo],
    default_fallback: str = DEFAULT_MODEL,
) -> str:
    """
    Resolve which model to activate given a user preference and available models.

    - If preferred_model is found in available_models, returns it.
    - If preferred_model is missing its tag or ends with :latest, checks fuzzy matching.
    - If preferred_model is not installed and available_models has items, falls back to:
        1. default_fallback if present in available_models
        2. the first available model
    - If available_models is empty, returns preferred_model or default_fallback.
    """
    model_names = [m.name if isinstance(m, ModelInfo) else str(m) for m in available_models]

    if preferred_model:
        pref = preferred_model.strip()
        if pref in model_names:
            return pref
        # Try matching with or without :latest tag
        for name in model_names:
            if pref == name.split(":")[0] or name == f"{pref}:latest" or pref == f"{name}:latest":
                return name

    if model_names:
        if default_fallback in model_names:
            return default_fallback
        return model_names[0]

    return preferred_model.strip() if preferred_model else default_fallback


def pull_model_stream(
    model_name: str,
    base_url: str = DEFAULT_OLLAMA_HOST,
    timeout: float = 300.0,
) -> Generator[dict[str, Any], None, None]:
    """
    Stream the download progress for pulling an Ollama model via POST /api/pull.

    Yields JSON progress chunks from the Ollama streaming API.
    Raises requests.RequestException or ValueError on network/API failure.
    """
    name = model_name.strip()
    if not name:
        raise ValueError("Model name cannot be empty")

    url = f"{base_url.rstrip('/')}/api/pull"
    with requests.post(url, json={"name": name}, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line:
                try:
                    payload = json.loads(line.decode("utf-8"))
                    yield payload
                except json.JSONDecodeError:
                    continue

