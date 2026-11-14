"""Command-line version of JUGNU.

Only the terminal screen lives here: the greeting, the commands, and the wording
of the messages. Everything shared with the desktop app (the model, the clock, the
calculator and the request to Ollama) comes from core.py.
"""
import json
from pathlib import Path

from core import (
    ERROR_CONNECTION,
    ERROR_INVALID,
    ERROR_TIMEOUT,
    build_messages,
    calculate,
    chat,
    is_time_question,
    local_now,
)
from export_manager import export_chat
from search_engine import SearchEngine

# calculate() and is_time_question() now live in core.py; they stay importable from here.
__all__ = ["calculate", "is_time_question", "ask_ai", "run_assistant"]

# ---------------- CONFIGURATION ----------------

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
    hour = local_now().hour

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
    current_time = local_now().strftime("%I:%M:%S %p")
    return f"The current time is {current_time}."


# ---------------- DATE ----------------

def get_date():
    current_date = local_now().strftime("%d %B %Y")
    return f"Today's date is {current_date}."


# ---------------- AI CHAT ----------------

def ask_ai(user_message):
    result = chat(build_messages(SYSTEM_PROMPT, [{"role": "user", "content": user_message}]))

    if result.ok:
        return result.answer

    if result.error == ERROR_CONNECTION:
        return (
            "I cannot connect to Ollama. "
            "Please make sure Ollama is running."
        )

    if result.error == ERROR_TIMEOUT:
        return (
            "The AI is taking too long to respond. "
            "Please try again."
        )

    if result.error == ERROR_INVALID:
        return "An error occurred: Ollama returned an invalid response."

    return f"An error occurred: {result.detail}"


def _load_history():
    try:
        with open("jugnu_history.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


# ---------------- SHORTCUTS & PALETTE ----------------

def show_shortcuts():
    print("\n" + "=" * 55)
    print("        JUGNU POWER-USER SHORTCUTS & PALETTE")
    print("=" * 55)
    print("• Navigation & Palette:")
    print("  Ctrl+K          - Open Command Palette HUD")
    print("  Ctrl+N          - New Conversation")
    print("  Ctrl+Up / Down  - Switch Between Conversations")
    print("  Esc             - Close Dialog / Cancel")
    print("\n• Appearance & Settings:")
    print("  Ctrl+D          - Toggle Dark / Light Theme")
    print("  Ctrl+L          - Switch Language (English / Hinglish)")
    print("  Ctrl+Shift+S    - Toggle Voice Speech Output")
    print("  Ctrl+,          - Open Voice Settings Dialog")
    print("\n• Chat & Help:")
    print("  Enter           - Send Message")
    print("  Shift+Enter     - Insert Newline")
    print("  F1 or Ctrl+/    - Shortcuts Cheat Sheet")
    print("  Ctrl+Q          - Quit Application")
    print("=" * 55)


# ---------------- HELP ----------------

def show_help():
    print("\nAvailable commands:")
    print("1. time                  - Show current time")
    print("2. date                  - Show today's date")
    print("3. calculate <expr>      - Calculate a mathematical expression")
    print("4. /search <query>       - Search across conversation history")
    print("5. /export [id] <format> - Export chat to md, html, json, or txt")
    print("6. /chats                - List all saved conversation history")
    print("7. /shortcuts, /palette  - View global keyboard shortcuts")
    print("8. help                  - Show available commands")
    print("9. bye                   - Exit the assistant")
    print("\nYou can also ask any general question.")


# ---------------- MAIN ASSISTANT ----------------

def run_assistant():
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

        # Shortcuts / Palette
        elif command in ["/shortcuts", "/palette", "shortcuts", "palette"]:
            show_shortcuts()

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

        # Search conversations
        elif command.startswith("/search"):
            query = user_input[7:].strip()
            if not query:
                print("\nAssistant: Please provide a search query, e.g. /search quicksort")
            else:
                history = _load_history()
                engine = SearchEngine(history)
                results = engine.search(query, limit=10)
                if not results:
                    print(f"\nAssistant: No matches found for '{query}'.")
                else:
                    print(f"\nAssistant: Found {len(results)} match(es) for '{query}':")
                    for idx, res in enumerate(results, 1):
                        role_tag = "User" if res.role == "user" else "JUGNU"
                        time_str = f" ({res.timestamp})" if res.timestamp else ""
                        print(f"{idx}. [{res.chat_title}]{time_str} {role_tag}: {res.snippet}")

        # Export chat
        elif command.startswith("/export"):
            parts = user_input.split()
            history = _load_history()
            if not history:
                print("\nAssistant: No conversation history found to export.")
            else:
                fmt = "md"
                target_chat = history[-1]
                if len(parts) == 2:
                    fmt = parts[1].lower().lstrip(".")
                elif len(parts) >= 3:
                    cid = parts[1]
                    fmt = parts[2].lower().lstrip(".")
                    matched = next((c for c in history if c.get("id") == cid), None)
                    if matched:
                        target_chat = matched
                    else:
                        print(f"\nAssistant: Chat ID '{cid}' not found. Using most recent chat.")

                raw_title = target_chat.get("title", "chat")
                safe_title = "".join(c if c.isalnum() else "_" for c in raw_title).strip("_") or "chat"
                filename = f"jugnu_export_{safe_title}.{fmt}"
                try:
                    export_chat(target_chat, fmt, output_path=filename)
                    print(f"\nAssistant: Successfully exported '{target_chat.get('title')}' to '{filename}'.")
                except Exception as err:
                    print(f"\nAssistant: Export error: {err}")

        # List saved chats
        elif command in ["/chats", "/history"]:
            history = _load_history()
            if not history:
                print("\nAssistant: No saved chats.")
            else:
                print(f"\nSaved Chats ({len(history)}):")
                for idx, c in enumerate(history, 1):
                    msg_count = len(c.get("messages", []))
                    print(f"{idx}. [{c.get('id')[:8]}] {c.get('title', 'New Chat')} ({msg_count} messages)")

        # General AI question
        else:
            print("\nAssistant: ", end="", flush=True)
            answer = ask_ai(user_input)
            print(answer)


# ---------------- START ----------------

if __name__ == "__main__":
    run_assistant()
