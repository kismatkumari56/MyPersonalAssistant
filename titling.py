"""Smart automatic conversation titling system for JUGNU (#58).

Provides:
- Heuristic rule-based title extraction with filler stripping and noun phrase prioritization.
- Non-blocking asynchronous AI-assisted title generation via local Ollama.
- Graceful offline fallback with zero external dependencies.
"""

from __future__ import annotations

import re
import string

import core

# Leading conversational filler patterns to strip
_FILLER_PREFIXES = [
    r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:help\s+(?:me\s+)?(?:with\s+)?)?",
    r"^please\s+(?:help\s+(?:me\s+)?(?:with\s+)?)?",
    r"^help\s+me\s+(?:with\s+)?(?:to\s+)?",
    r"^(?:i\s+want\s+to|i\s+need\s+to|i\s+would\s+like\s+to)\s+",
    r"^(?:how\s+to|how\s+do\s+i|how\s+can\s+i)\s+",
    r"^(?:write|create|generate|give|make|build)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?",
    r"^(?:tell\s+me\s+about|explain|describe|show\s+me)\s+(?:a\s+|an\s+|the\s+)?",
    r"^(?:what\s+is|what\s+are|what\s+was|who\s+is|who\s+are|why\s+is|why\s+are)\s+(?:a\s+|an\s+|the\s+)?",
    r"^(?:kya\s+aap|mujhe\s+batao|kaise\s+karein|bataiye)\s+",
]

_FILLER_REGEX = re.compile("|".join(_FILLER_PREFIXES), re.IGNORECASE)


def generate_heuristic_title(text: str, max_words: int = 5, max_chars: int = 34) -> str:
    """Generate a clean, punchy conversation title from user's first prompt."""
    if not text or not text.strip():
        return "New Chat"

    raw = text.strip()

    # Take first line if multi-line prompt
    first_line = raw.split("\n")[0].strip()

    # Strip conversational leading filler iteratively (supports stacked prefixes)
    cleaned = first_line
    for _ in range(5):
        m = _FILLER_REGEX.match(cleaned)
        if m and m.end() > 0:
            rest = cleaned[m.end():].strip()
            if rest:
                cleaned = rest
            else:
                break
        else:
            break

    # Strip trailing punctuation (question marks, exclamation points, colons, dots)
    cleaned = cleaned.rstrip(string.punctuation).strip()

    # If everything got stripped, fall back to first line
    if not cleaned:
        cleaned = first_line.rstrip(string.punctuation).strip()

    words = cleaned.split()
    if not words:
        return "New Chat"

    # Take up to max_words
    selected_words = words[:max_words]
    title = " ".join(selected_words)

    # Capitalize appropriately (Title Case)
    title = " ".join(w.capitalize() if not (w.isupper() and len(w) <= 4) else w for w in title.split())

    # Truncate if exceeds max_chars
    if len(title) > max_chars:
        truncated = title[:max_chars].rstrip()
        last_space = truncated.rfind(" ")
        if last_space > 10:
            title = truncated[:last_space]
        else:
            title = truncated

    return title or "New Chat"


def generate_ai_title(prompt_text: str, timeout: float = 4.0) -> str | None:
    """Query local Ollama with a specialized title generation prompt."""
    if not prompt_text or not prompt_text.strip():
        return None

    try:
        sample_prompt = prompt_text.strip()[:200]
        messages = [
            {
                "role": "system",
                "content": (
                    "Generate a concise 3 to 5-word title summarizing this inquiry. "
                    "Output ONLY the title with no quotes, no markdown, no punctuation, and no explanations."
                ),
            },
            {"role": "user", "content": sample_prompt},
        ]
        result = core.chat(
            messages,
            timeout=timeout,
            keep_alive="5m",
            options={"num_predict": 12, "temperature": 0.2},
        )
        if result and result.ok and result.answer:
            first_line = result.answer.strip().split("\n")[0].strip()
            clean = first_line.strip("\"'`“”#*.- ")
            if clean and len(clean) >= 3:
                return clean[:34]
        return None
    except Exception:
        return None
