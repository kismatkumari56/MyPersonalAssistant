import argparse
import ast
import datetime
import operator
import re

import requests

from models import (
    DEFAULT_MODEL,
    fetch_local_models,
    format_bytes,
    resolve_active_model,
)

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

# ---------------- CONFIGURATION ----------------

MODEL = DEFAULT_MODEL
OLLAMA_URL = "http://localhost:11434/api/chat"

SYSTEM_PROMPT = """
You are a helpful personal AI assistant.

Rules:
1. Always answer in clear and simple English only.
2. Never answer in Hindi or Hinglish.
3. Explain concepts in an easy-to-understand way.
4. Be polite, friendly, and helpful.
5. If you do not know something, say so honestly.
"""

# ---------------- GREETING ----------------

def greet():
    hour = datetime.datetime.now().hour

    if hour < 12:
        greeting = "Good morning!"
    elif hour < 17:
        greeting = "Good afternoon!"
    else:
        greeting = "Good evening!"

    print("\n" + "=" * 45)
    print("       MY PERSONAL AI ASSISTANT")
    print("=" * 45)
    print(greeting)
    print("I am ready to help you.")
    print("Type 'help' to see commands.")
    print("Type 'bye' to exit.")
    print("=" * 45)


# ---------------- TIME ----------------

def get_time():
    current_time = datetime.datetime.now().strftime("%I:%M:%S %p")
    return f"The current time is {current_time}."


# ---------------- DATE ----------------

def get_date():
    current_date = datetime.datetime.now().strftime("%d %B %Y")
    return f"Today's date is {current_date}."


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


# ---------------- AI CHAT ----------------

def ask_ai(user_message, model=None):
    target_model = model or MODEL
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": target_model,
                "messages": [
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": user_message
                    }
                ],
                "stream": False
            },
            timeout=120
        )

        response.raise_for_status()

        data = response.json()
        answer = data["message"]["content"]

        return answer.strip()

    except requests.exceptions.ConnectionError:
        return (
            "I cannot connect to Ollama. "
            "Please make sure Ollama is running."
        )

    except requests.exceptions.Timeout:
        return (
            "The AI is taking too long to respond. "
            "Please try again."
        )

    except Exception as error:
        return f"An error occurred: {error}"


# ---------------- HELP ----------------

def show_help():
    print("\nAvailable commands:")
    print("1. time           - Show current time")
    print("2. date           - Show today's date")
    print("3. calculate      - Calculate a mathematical expression")
    print("4. /models        - List locally installed Ollama models")
    print("5. /model <name>  - Switch the active model")
    print("6. help           - Show available commands")
    print("7. bye            - Exit the assistant")
    print("\nYou can also ask any general question.")


# ---------------- MAIN ASSISTANT ----------------

def run_assistant(model=None):
    global MODEL
    if model:
        MODEL = model
    else:
        # Auto-discover local models and fallback if needed
        try:
            available = fetch_local_models()
            if available:
                MODEL = resolve_active_model(MODEL, available)
        except Exception:
            pass

    greet()

    while True:
        user_input = input("\nYou: ").strip()

        if not user_input:
            continue

        command = user_input.lower()

        # Exit
        if command in ["bye", "exit", "quit", "goodbye"]:
            print("\nAssistant: Goodbye! Have a great day.")
            break

        # Help
        elif command == "help":
            show_help()

        # Time
        elif command in ["time", "what is the time", "what time is it"]:
            print("\nAssistant:", get_time())

        # Date
        elif command in ["date", "what is the date", "today's date"]:
            print("\nAssistant:", get_date())

        # Calculator
        elif command.startswith("calculate "):
            expression = user_input[10:].strip()
            print("\nAssistant:", calculate(expression))

        # List models
        elif command in ["/models", "models"]:
            print("\nAssistant: Fetching installed Ollama models...")
            models_list = fetch_local_models()
            if not models_list:
                print("No installed models found or Ollama is offline.")
            else:
                print(f"\nInstalled Models (Active: {MODEL}):")
                for m in models_list:
                    star = "* " if m.name == MODEL else "  "
                    print(f" {star}{m.display_name}")

        # Switch model
        elif command.startswith("/model ") or command.startswith("model ") or command.startswith("/switch "):
            parts = user_input.split(maxsplit=1)
            new_model = parts[1].strip() if len(parts) > 1 else ""
            if new_model:
                MODEL = new_model
                print(f"\nAssistant: Switched active model to '{MODEL}'.")
            else:
                print(f"\nAssistant: Current active model is '{MODEL}'.")

        # General AI question
        else:
            print(f"\nAssistant: Thinking ({MODEL})...")
            answer = ask_ai(user_input, model=MODEL)
            print("\nAssistant:", answer)


# ---------------- START ----------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JUGNU Terminal Assistant")
    parser.add_argument("--model", type=str, default=None, help="Ollama model to use")
    args = parser.parse_args()
    run_assistant(model=args.model)


