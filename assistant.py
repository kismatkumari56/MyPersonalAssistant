"""Command-line version of JUGNU.

Only the terminal screen lives here: the greeting, the commands, and the wording
of the messages. Everything shared with the desktop app (the model, the clock, the
calculator and the request to Ollama) comes from core.py.
"""
import argparse
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
from context_ingest import (
    AttachedDocument,
    AttachmentError,
    assemble_prompt_with_attachments,
    ingest_file,
)

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

def ask_ai(user_message, attachments=None):
    prompt_text = (
        assemble_prompt_with_attachments(user_message, attachments)
        if attachments
        else user_message
    )
    result = chat(build_messages(SYSTEM_PROMPT, [{"role": "user", "content": prompt_text}]))

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


# ---------------- HELP ----------------

def show_help():
    print("\nAvailable commands:")
    print("1. time           - Show current time")
    print("2. date           - Show today's date")
    print("3. calculate      - Calculate a mathematical expression")
    print("4. /attach <path> - Attach a local code or text file")
    print("5. /files         - List attached files")
    print("6. /detach        - Clear all attachments")
    print("7. help           - Show available commands")
    print("8. bye            - Exit the assistant")
    print("\nYou can also ask any general question.")


# ---------------- MAIN ASSISTANT ----------------

def run_assistant(initial_files=None):
    active_attachments: list[AttachedDocument] = []
    if initial_files:
        for file_path in initial_files:
            try:
                doc = ingest_file(file_path)
                active_attachments.append(doc)
                print(f"[Attached: {doc.filename} ({doc.formatted_size})]")
            except AttachmentError as err:
                print(f"[Attachment error: {err}]")

    greet()

    while True:
        prompt_prefix = f"\nYou ({len(active_attachments)} attached): " if active_attachments else "\nYou: "
        user_input = input(prompt_prefix).strip()

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

        # Attach local file
        elif command.startswith("/attach ") or command.startswith("/file "):
            parts = user_input.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target:
                print("\nAssistant: Please provide a file path, e.g. /attach path/to/file.py")
            else:
                try:
                    doc = ingest_file(target)
                    active_attachments.append(doc)
                    print(f"\nAssistant: Attached '{doc.filename}' ({doc.formatted_size}, {doc.language}).")
                except AttachmentError as err:
                    print(f"\nAssistant: Could not attach file: {err}")

        # List attached files
        elif command in ["/files", "/attachments"]:
            if not active_attachments:
                print("\nAssistant: No files attached.")
            else:
                print("\nAttached files:")
                for idx, doc in enumerate(active_attachments, 1):
                    trunc = " (truncated)" if doc.is_truncated else ""
                    print(f"{idx}. {doc.filename} ({doc.formatted_size}, {doc.language}){trunc}")

        # Detach all attachments
        elif command in ["/detach", "/clear-attachments"]:
            count = len(active_attachments)
            active_attachments.clear()
            print(f"\nAssistant: Cleared {count} attached file(s).")

        # General AI question
        else:
            print("\nAssistant: Thinking...")
            answer = ask_ai(user_input, attachments=active_attachments)
            print("\nAssistant:", answer)
            active_attachments.clear()


# ---------------- START ----------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JUGNU CLI Assistant")
    parser.add_argument("--file", "-f", action="append", help="Path to local file to attach")
    args, _ = parser.parse_known_args()
    run_assistant(initial_files=args.file)
