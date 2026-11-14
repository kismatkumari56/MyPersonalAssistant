"""JUGNU's logic, with no GUI code: chat history, settings, local answers and the Ollama client.

The desktop window (desktop_app.py) builds on these functions, and the tests can call them
without opening a window.
"""
import ast
import json
import os
import re
import tempfile
import uuid
from datetime import datetime

import requests

from analytics import parse_ollama_metrics
from strings import build_system_prompt, get_string, normalize_language
from tts import DEFAULT_RATE, DEFAULT_VOLUME, RATE_MIN, RATE_MAX, clamp
from assistant import calculate, is_time_question

APP_TITLE = "JUGNU | Just an Unusually Genius Neural Utility"
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "llama3.2:3b"
HISTORY_FILE = "jugnu_history.json"
SETTINGS_FILE = "jugnu_settings.json"
MAX_LIST_TITLE = 34    # longer chat titles are shortened with "…" in the sidebar list
MAX_TITLE_LENGTH = 60  # longest title a user can give a chat
NEW_CHAT_TITLE = "New Chat"

LIGHT = {
    "bg": "#F7F4FF", "sidebar": "#EEE8FF", "panel": "#FFFFFF",
    "text": "#27213B", "muted": "#756B91", "accent": "#8B5CF6",
    "accent2": "#EC4899", "user": "#E3DCFF", "assistant": "#F0EAFB",
    "entry": "#FFFFFF", "border": "#D9CFF5", "danger": "#FCE7F3",
    "code": "#FFFFFF", "inline_code": "#E2D9F7"
}
DARK = {
    "bg": "#191728", "sidebar": "#211D35", "panel": "#28223D",
    "text": "#F4EEFF", "muted": "#B7A9D8", "accent": "#8B6CF0",
    "accent2": "#D946A6", "user": "#34365A", "assistant": "#2A2440",
    "entry": "#2A2440", "border": "#463A66", "danger": "#4B2A40",
    "code": "#17142A", "inline_code": "#3A3157"
}


class InvalidOllamaResponse(ValueError):
    """The decoded response does not contain a usable chat message."""


def ollama_answer(data):
    if not isinstance(data, dict):
        raise InvalidOllamaResponse()
    message = data.get("message")
    if not isinstance(message, dict):
        raise InvalidOllamaResponse()
    content = message.get("content")
    if not isinstance(content, str):
        raise InvalidOllamaResponse()
    return content.strip()


def local_datetime_text():
    # Uses the system's configured local timezone.
    now = datetime.now().astimezone()
    return now.strftime("%A, %d %B %Y, %I:%M:%S %p %Z")


def message_time_text():
    return datetime.now().astimezone().strftime("%I:%M %p")


# ---------- Chat history ----------
def new_chat():
    """An empty, unsaved chat. It only goes into the history once a message is sent."""
    return {"id": str(uuid.uuid4()), "title": NEW_CHAT_TITLE, "messages": []}


def load_history(path=HISTORY_FILE):
    """Load valid, non-empty conversations from the history file."""
    try:
        with open(path, "r", encoding="utf-8") as history_file:
            data = json.load(history_file)

        if not isinstance(data, list):
            return []

        valid_chats = []
        seen_ids = set()

        for chat in data:
            if not isinstance(chat, dict):
                continue

            chat_id = chat.get("id")
            messages = chat.get("messages")

            if not isinstance(chat_id, str) or not chat_id.strip():
                continue
            if chat_id in seen_ids:
                continue
            if not isinstance(messages, list):
                continue

            valid_messages = []
            for message in messages:
                if not isinstance(message, dict):
                    continue

                role = message.get("role")
                content = message.get("content")

                if role not in ("user", "assistant"):
                    continue
                if not isinstance(content, str) or not content.strip():
                    continue

                msg_dict = {
                    "role": role,
                    "content": content
                }
                time_val = message.get("time")
                if isinstance(time_val, str) and time_val.strip():
                    msg_dict["time"] = time_val.strip()
                if role == "assistant" and message.get("is_error") is True:
                    msg_dict["is_error"] = True
                if role == "assistant" and isinstance(message.get("metrics"), dict):
                    msg_dict["metrics"] = message["metrics"]

                valid_messages.append(msg_dict)

            # Empty conversations should never appear in saved history.
            if not valid_messages:
                continue

            chat["messages"] = valid_messages
            if chat.get("is_custom_title"):
                chat["is_custom_title"] = True
            title = chat.get("title")
            chat["title"] = title.strip() if isinstance(title, str) and title.strip() else NEW_CHAT_TITLE

            valid_chats.append(chat)
            seen_ids.add(chat_id)

        return valid_chats

    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_history(chats, path=HISTORY_FILE):
    """Save non-empty conversations atomically to avoid partial JSON files.

    Raises OSError, TypeError or ValueError when the file cannot be written.
    """
    saved_chats = [
        chat for chat in chats
        if isinstance(chat, dict)
        and isinstance(chat.get("messages"), list)
        and any(
            isinstance(message, dict)
            and message.get("role") in ("user", "assistant")
            and isinstance(message.get("content"), str)
            and message["content"].strip()
            for message in chat["messages"]
        )
    ]

    history_path = os.path.abspath(path)
    history_dir = os.path.dirname(history_path)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=history_dir,
            suffix=".tmp",
            delete=False
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(saved_chats, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        # The temporary file is in the same directory, so replacement is atomic.
        os.replace(temp_path, history_path)
        temp_path = None

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def tidy_title(title):
    """A title the user typed, with spaces tidied and the length capped. Empty means "keep the old one"."""
    return " ".join(title.split())[:MAX_TITLE_LENGTH]


def list_title(title):
    """The title as shown in the sidebar list."""
    if len(title) > MAX_LIST_TITLE:
        return title[:MAX_LIST_TITLE - 1] + "…"
    return title


# ---------- Settings ----------
def _read_settings(path):
    """The settings file as a dict, or {} when it is missing or damaged."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_dark_mode(path=SETTINGS_FILE):
    return bool(_read_settings(path).get("dark_mode", True))


def load_voice_enabled(path=SETTINGS_FILE):
    # Voice output is off by default; the choice is remembered between launches.
    return bool(_read_settings(path).get("voice_enabled", False))


def load_voice_prefs(path=SETTINGS_FILE):
    """Speed, volume and voice choice. Anything missing or damaged falls back to the defaults."""
    data = _read_settings(path)
    rate, volume, voice_id = DEFAULT_RATE, DEFAULT_VOLUME, None
    saved_rate = data.get("voice_rate")
    saved_volume = data.get("voice_volume")
    saved_voice = data.get("voice_id")
    if isinstance(saved_rate, (int, float)) and not isinstance(saved_rate, bool):
        rate = int(clamp(saved_rate, RATE_MIN, RATE_MAX))
    if isinstance(saved_volume, (int, float)) and not isinstance(saved_volume, bool):
        volume = float(clamp(saved_volume, 0.0, 1.0))
    if isinstance(saved_voice, str) and saved_voice:
        voice_id = saved_voice
    return rate, volume, voice_id


def load_language(path=SETTINGS_FILE):
    # English by default; an unknown or damaged value also falls back to English.
    return normalize_language(_read_settings(path).get("language"))


def load_performance_hud(path=SETTINGS_FILE):
    return bool(_read_settings(path).get("show_performance_hud", True))


def save_settings(path=SETTINGS_FILE, **settings):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        pass


# ---------- Answers ----------
def extract_calc_expression(text):
    """
    Extracts an arithmetic expression from user input if present.
    Supports explicit commands (e.g. 'calculate 12*7', 'solve 0/0'),
    conversational math requests (e.g. 'the expression is 5/3+3', 'what is 12*7?'),
    and plain arithmetic expressions (e.g. '12*7', '5/3+3').
    Returns the expression string if detected, or None for general chat.
    """
    q = text.strip()
    if not q:
        return None

    def is_math_ast(expr_str):
        if re.search(r"\d{1,4}[-/]\d{1,2}[-/]\d{2,4}", expr_str):
            return False
        if re.fullmatch(r"[\d\.\s\+\-\*\/\%\(\)]+", expr_str):
            if any(op in expr_str for op in ["+", "*", "/", "%"]) or re.search(r"\d\s*-\s*\d", expr_str):
                try:
                    tree = ast.parse(expr_str, mode="eval")
                    valid_nodes = (
                        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                        ast.Mod, ast.Pow, ast.USub, ast.UAdd
                    )
                    if all(isinstance(node, valid_nodes) for node in ast.walk(tree)):
                        if any(isinstance(node, ast.BinOp) for node in ast.walk(tree)):
                            return True
                except Exception:
                    pass
        return False

    # 1. Direct command prefixes: 'calculate', 'calc', 'solve', 'evaluate'
    match = re.match(r"^(?:calculate|calc|solve|evaluate)(?:[:\s]+(.*)|$)", q, flags=re.IGNORECASE)
    if match:
        remainder = match.group(1).strip() if match.group(1) else ""
        # Strip optional 'the expression' / 'the value of' from remainder, e.g. 'calculate the expression 5/3+3'
        remainder = re.sub(r"^(?:the\s+)?(?:expression|value\s+of)[:\s]*", "", remainder, flags=re.IGNORECASE).strip()
        return remainder

    # 2. Conversational math prefixes: 'the expression is ...', 'expression: ...', 'what is ...', 'value of ...'
    m_conv = re.match(
        r"^(?:(?:what\s+is|what\'s)\s+(?:the\s+)?(?:value\s+of|expression\s+is|expression)?|(?:the\s+)?(?:expression|value)\s+(?:is|of)|expression)[:\s]*(.*)$",
        q, flags=re.IGNORECASE
    )
    if m_conv and m_conv.group(1).strip():
        candidate = m_conv.group(1).strip().rstrip("?. ")
        if is_math_ast(candidate):
            return candidate

    # 3. Plain arithmetic expression (e.g. '12*7', '5/3+3', '0/0')
    if is_math_ast(q):
        return q

    return None


def local_answer(text, language):
    """Answer clock/date and arithmetic questions without the model, or None for everything else."""
    if is_time_question(text):
        return get_string(language, "time_answer", when=local_datetime_text())
    calc_expr = extract_calc_expression(text)
    if calc_expr is not None:
        return calculate(calc_expr)
    return None


def model_messages(chat, language):
    """The messages sent to Ollama: the system prompt plus recent turns, without failure notices."""
    # Keep recent context only, to reduce prompt size and improve latency.
    recent = [
        m for m in chat.get("messages", [])
        if not (m["role"] == "assistant" and m.get("is_error") is True)
    ][-10:]
    messages = [{
        "role": "system",
        "content": build_system_prompt(language, local_datetime_text())
    }]
    messages.extend({"role": m["role"], "content": m["content"]} for m in recent)
    return messages


class OllamaReply(tuple):
    """Tuple (answer, is_error) that also carries optional metrics for telemetry."""

    def __new__(cls, values, metrics=None):
        instance = super().__new__(cls, values)
        instance.metrics = metrics
        return instance


def ask_ollama(chat, language):
    """Ask the model for the next reply in `chat`. Blocks; returns (answer, is_error) and never raises."""
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": MODEL,
            "messages": model_messages(chat, language),
            "stream": False,
            "keep_alive": "10m",
            "options": {"num_predict": 256, "temperature": 0.6}
        }, timeout=180)
        response.raise_for_status()
        data = response.json()
        answer = ollama_answer(data)
        if not answer:
            return OllamaReply((get_string(language, "error_empty_reply"), True))
        metrics = parse_ollama_metrics(data)
        return OllamaReply((answer, False), metrics=metrics)
    except InvalidOllamaResponse:
        return OllamaReply((get_string(language, "error_invalid_response"), True))
    except requests.Timeout:
        return OllamaReply((get_string(language, "error_timeout"), True))
    except requests.ConnectionError:
        return OllamaReply((get_string(language, "error_connection"), True))
    except (requests.RequestException, ValueError) as e:
        return OllamaReply((get_string(language, "error_generic", error=e), True))


def warm_up():
    """Load the model into memory so the first real reply is fast. Blocks; True if Ollama answered."""
    try:
        requests.post(OLLAMA_URL, json={
            "model": MODEL, "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": False, "keep_alive": "10m",
            "options": {"num_predict": 3}
        }, timeout=90)
        return True
    except requests.RequestException:
        return False
