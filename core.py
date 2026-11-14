"""Shared core for JUGNU, used by both the command-line assistant (assistant.py)
and the desktop app (desktop_app.py).

Everything that must behave the same in both lives here, in one place:
  * the model name and Ollama address,
  * the clock helpers,
  * the safe calculator and the "what time is it?" detector,
  * build_messages(): turns a conversation into the list Ollama expects,
  * chat(): sends it to Ollama and returns a ChatResult (an answer or a clear error).

Each front end keeps its own screen code and its own wording. chat() reports
errors as a kind (ERROR_CONNECTION, ERROR_TIMEOUT, ...), never as text, so the
CLI and the GUI can each word the message their own way (the GUI translates it).
"""
import ast
import operator
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

# ---------------- CONFIGURATION ----------------

MODEL = "llama3.2:3b"
OLLAMA_URL = "http://localhost:11434/api/chat"
HISTORY_LIMIT = 10  # how many recent messages are sent to the model

# ---------------- CLOCK ----------------


def local_now():
    """The current moment in the computer's own time zone."""
    return datetime.now().astimezone()


def local_datetime_text():
    """Full local date, time and zone, e.g. 'Saturday, 14 November 2026, 08:23:00 AM IST'."""
    return local_now().strftime("%A, %d %B %Y, %I:%M:%S %p %Z")


# ---------------- "WHAT TIME IS IT?" DETECTOR ----------------

# Optional conversational openers/closers so polite phrasing still triggers
# local time/date replies without allowing unrelated sentences to match.
_GREETING_PREFIX = (
    r"(?:(?:hey|hi|hello|jugnu|ok|okay|please|pls|plz|sorry|wait|"
    r"excuse\s+me|bhai|yaar|ji)[,:\s]+)*"
)
_ASK_PREFIX = (
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:(?:tell|show|give)\s+me[,:\s]+|do\s+you\s+(?:know|have)[,:\s]+)?"
)
_POLITE_SUFFIX = (
    r"(?:[,\s]+(?:please|pls|plz|right\s+now|now|today|abhi|bhai|yaar|ji|jugnu))*"
)
_TIME_OR_DATE_NOUN = (
    r"(?:date\s+(?:and|&)\s+time|time\s+(?:and|&)\s+date|"
    r"day\s+(?:and|&)\s+date|date\s+(?:and|&)\s+day|time|date)"
)

_TIME_QUESTION_PATTERNS = [
    # Standalone short queries: "time", "date", "time please", "date?"
    re.compile(
        rf"^{_GREETING_PREFIX}\b(?:time|date)\b{_POLITE_SUFFIX}$",
        re.IGNORECASE,
    ),
    # English current time, date, and day questions
    re.compile(
        rf"^{_GREETING_PREFIX}{_ASK_PREFIX}"
        rf"(?:"
        rf"\bwhat(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+|local\s+|today(?:'s|s)?\s+)?{_TIME_OR_DATE_NOUN}\b|"
        rf"\bwhat(?:'s|\s+is)\s+(?:the\s+)?day\s+today\b|"
        rf"\bwhat\s+(?:the\s+)?(?:current\s+|local\s+|today(?:'s|s)?\s+)?{_TIME_OR_DATE_NOUN}\s+is\b|"
        rf"\bwhat\s+(?:time|date)\s+(?:is\s+it|it\s+is)\b|"
        rf"\bwhat\s+date\s+is\s+today\b|"
        rf"\b(?:what|which)\s+day(?:\s+of\s+(?:the\s+)?week)?\s+"
        rf"(?:is\s+(?:it|today)|(?:it|today)\s+is)\b|"
        rf"\b(?:the\s+(?:current\s+|local\s+)?|current\s+|local\s+|today(?:'s|s)?\s+){_TIME_OR_DATE_NOUN}\b|"
        rf"\b(?:the\s+)?(?:date\s+today|date\s+(?:and|&)\s+time|time\s+(?:and|&)\s+date)\b"
        rf")"
        rf"{_POLITE_SUFFIX}$",
        re.IGNORECASE,
    ),
    # Hindi / Hinglish current time, date, and day questions
    re.compile(
        rf"^{_GREETING_PREFIX}"
        rf"(?:"
        rf"\babhi\s+(?:time|samay)\b|"
        rf"\b(?:abhi\s+)?(?:time|samay)\s+"
        rf"(?:kya(?:\s+(?:hai|hua(?:\s+hai)?|chal\s+raha(?:\s+hai)?))?|"
        rf"kitna\s+(?:hua(?:\s+hai)?|baja(?:\s+hai)?)|hai\s+kya|bata(?:o|\s+do|na)?|bol(?:o)?)\b|"
        rf"\b(?:abhi\s+)?kya\s+(?:time|samay)(?:\s+(?:hai|hua(?:\s+hai)?|ho\s+raha(?:\s+hai)?))?\b|"
        rf"\b(?:abhi\s+)?kitna\s+(?:time|samay)\s+(?:hai|hua(?:\s+hai)?|ho\s+raha(?:\s+hai)?)\b|"
        rf"\b(?:abhi\s+)?(?:kitne\s+baje(?:\s+(?:hain?|hai|hue(?:\s+hain?)?))?|kitna\s+baja(?:\s+hai)?)\b|"
        rf"\baaj\s+(?:ki|ka)\s+(?:date|tareekh|tarikh)\b|"
        rf"\b(?:aaj\s+(?:ki|ka)\s+|aaj\s+)?(?:date|tareekh|tarikh)\s+"
        rf"(?:kya(?:\s+(?:hai|chal\s+rahi(?:\s+hai)?))?|hai\s+kya|bata(?:o|\s+do|na)?|bol(?:o)?)\b|"
        rf"\b(?:aaj\s+(?:ki|ka)\s+|aaj\s+)?(?:kya|kaun\s*sa|konsa|kaun\s*si|konsi|kitni|kitna)\s+"
        rf"(?:date|tareekh|tarikh)(?:\s+hai)?\b|"
        rf"\baaj\s+(?:(?:ka\s+)?(?:din|day)\s+(?:kya\s+hai|kaun\s*sa\s+hai|konsa\s+hai|hai\s+kya)|"
        rf"(?:kya|kaun\s*sa|konsa)\s+(?:din|day)(?:\s+hai)?)\b|"
        rf"\b(?:kaun\s*sa|konsa)\s+(?:din|day)\s+hai\b|"
        rf"\b(?:aaj\s+(?:ki|ka)\s+|abhi\s+)?(?:(?:date|tareekh|tarikh)\s+(?:aur|and|&)\s+(?:time|samay)|"
        rf"(?:time|samay)\s+(?:aur|and|&)\s+(?:date|tareekh|tarikh))"
        rf"(?:\s+(?:kya\s+(?:hai|hain)|bata(?:o|\s+do|na)?))?\b"
        rf")"
        rf"{_POLITE_SUFFIX}$",
        re.IGNORECASE,
    ),
]


def is_time_question(text):
    """Return True if *text* asks for the current local time, date, or day."""
    if not isinstance(text, str):
        return False

    q = re.sub(r"\s+", " ", text.strip().lower()).replace("\u2019", "'")
    q = q.rstrip("?!.। ")
    if not q:
        return False

    return any(pattern.fullmatch(q) for pattern in _TIME_QUESTION_PATTERNS)


# ---------------- CALCULATOR ----------------

def calculate(expression):
    """
    Safely evaluates basic arithmetic expressions.
    Supports +, -, *, /, //, %, and **.
    """

    allowed_operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos
    }

    def evaluate(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value

        if isinstance(node, ast.BinOp):
            left = evaluate(node.left)
            right = evaluate(node.right)

            op_type = type(node.op)

            if op_type not in allowed_operators:
                raise ValueError("Unsupported operator")

            if op_type == ast.Pow and abs(right) > 100:
                raise ValueError("Exponent is too large")

            return allowed_operators[op_type](left, right)

        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)

            if op_type not in allowed_operators:
                raise ValueError("Unsupported operator")

            return allowed_operators[op_type](evaluate(node.operand))

        raise ValueError("Invalid mathematical expression")

    try:
        parsed_expression = ast.parse(expression, mode="eval")
        result = evaluate(parsed_expression.body)
        return f"The answer is {result}."

    except ZeroDivisionError:
        return "Error: Division by zero is not allowed."

    except Exception:
        return "Sorry, I could not calculate that expression."


# ---------------- TALKING TO OLLAMA ----------------

ERROR_CONNECTION = "connection"  # Ollama is not running / cannot be reached
ERROR_TIMEOUT = "timeout"        # Ollama did not answer in time
ERROR_INVALID = "invalid"        # Ollama answered, but not with a usable chat message
ERROR_OTHER = "other"            # anything else; ChatResult.detail has the technical text


class InvalidOllamaResponse(ValueError):
    """The decoded response does not contain a usable chat message."""


def ollama_answer(data):
    """The reply text inside Ollama's JSON answer (stripped), or InvalidOllamaResponse."""
    if not isinstance(data, dict):
        raise InvalidOllamaResponse()
    message = data.get("message")
    if not isinstance(message, dict):
        raise InvalidOllamaResponse()
    content = message.get("content")
    if not isinstance(content, str):
        raise InvalidOllamaResponse()
    return content.strip()


@dataclass(frozen=True)
class ChatResult:
    """What chat() returns: either an answer, or an error kind (never both)."""
    answer: str = ""
    error: Optional[str] = None  # None, or one of the ERROR_* kinds above
    detail: str = ""             # technical text for ERROR_OTHER

    @property
    def ok(self):
        return self.error is None


def build_messages(system_prompt, history, limit=HISTORY_LIMIT):
    """The message list to send to Ollama: the system prompt, then the recent conversation.

    Only the last `limit` messages are kept (shorter prompts reply faster), and the
    notices JUGNU itself showed for failed requests (marked is_error) are left out so
    the model never sees them as part of the conversation.
    """
    recent = [
        m for m in history
        if not (m["role"] == "assistant" and m.get("is_error") is True)
    ][-limit:]
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in recent)
    return messages


def chat(messages, *, timeout=120, keep_alive=None, options=None):
    """Send `messages` to Ollama and wait for the whole reply.

    Never raises for a failed request: the problem is returned in the ChatResult.
    An empty reply is not an error here; each front end decides what to say about it.
    """
    payload = {"model": MODEL, "messages": messages, "stream": False}
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if options is not None:
        payload["options"] = options
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        return ChatResult(answer=ollama_answer(response.json()))
    except InvalidOllamaResponse:
        return ChatResult(error=ERROR_INVALID)
    except requests.Timeout:  # checked before ConnectionError: a connect timeout is both
        return ChatResult(error=ERROR_TIMEOUT)
    except requests.ConnectionError:
        return ChatResult(error=ERROR_CONNECTION)
    except Exception as error:  # bad status, unreadable JSON, anything unexpected
        return ChatResult(error=ERROR_OTHER, detail=str(error))


def stream_chat(messages, *, timeout=180, keep_alive=None, options=None):
    """Send `messages` to Ollama with stream=True and return the streaming response object."""
    payload = {"model": MODEL, "messages": messages, "stream": True}
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if options is not None:
        payload["options"] = options
    return requests.post(OLLAMA_URL, json=payload, stream=True, timeout=timeout)
