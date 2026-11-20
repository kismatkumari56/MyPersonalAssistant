"""Command-line version of JUGNU.

Only the terminal screen lives here: the greeting, the commands, and the wording
of the messages. Everything shared with the desktop app (the model, the clock, the
calculator and the request to Ollama) comes from core.py.
"""
import argparse
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
from agent_tools import ToolRegistry
from analytics import SessionAnalytics
from context_ingest import (
    AttachedDocument,
    AttachmentError,
    assemble_prompt_with_attachments,
    ingest_file,
)
from export_manager import export_chat
from models import (
    DEFAULT_MODEL,
    fetch_local_models,
    format_bytes,
    resolve_active_model,
)
from personas import get_persona, list_personas
from scheduler import CronEvaluator, ScheduledTask, TaskStore
from search_engine import SearchEngine

# calculate() and is_time_question() now live in core.py; they stay importable from here.
__all__ = ["calculate", "is_time_question", "ask_ai", "run_assistant", "session_analytics"]

session_analytics = SessionAnalytics()

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

def ask_ai(user_message, persona=None, model=None, attachments=None):
    if isinstance(persona, str):
        persona = get_persona(persona)
    system_prompt = (persona.system_prompt + "\n\n") if persona and getattr(persona, "system_prompt", None) else SYSTEM_PROMPT
    options = {"temperature": persona.temperature} if persona and hasattr(persona, "temperature") else None
    prompt_text = (
        assemble_prompt_with_attachments(user_message, attachments)
        if attachments
        else user_message
    )
    result = chat(
        build_messages(system_prompt, [{"role": "user", "content": prompt_text}]),
        model=model,
        options=options,
    )

    if result.ok:
        if result.metrics:
            session_analytics.record(result.metrics)
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
    print("4. /models               - List installed Ollama models")
    print("5. /model <name>         - Switch active Ollama model")
    print("6. personas              - List all available assistant personas")
    print("7. persona <name>        - Switch active persona (e.g. persona code)")
    print("8. /attach <path>        - Attach a local code or text file")
    print("9. /files                - List attached files")
    print("10. /detach              - Clear all attachments")
    print("11. /search <query>      - Search across conversation history")
    print("12. /export [id] <format> - Export chat to md, html, json, or txt")
    print("13. /chats               - List all saved conversation history")
    print("14. /schedule            - Manage proactive background tasks and cron jobs")
    print("15. /shortcuts, /palette - View global keyboard shortcuts")
    print("16. /stats               - Show session inference performance analytics")
    print("17. /exec, /tool         - Run sandboxed agent tools (python, files, diagnostics)")
    print("18. help                 - Show available commands")
    print("19. bye                  - Exit the assistant")
    print("\nYou can also ask any general question.")


# ---------------- MAIN ASSISTANT ----------------

def run_assistant(model=None, persona_name="default", initial_files=None):
    active_persona = get_persona(persona_name)
    active_model = resolve_active_model(model or DEFAULT_MODEL)
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
    if active_persona and active_persona.id != "default":
        print(f"Active Persona: {active_persona.display_label} (temp: {active_persona.temperature})")

    while True:
        model_part = f" ({active_model})" if active_model != DEFAULT_MODEL else ""
        attach_part = f" [{len(active_attachments)} attached]" if active_attachments else ""
        prompt_prefix = f"\nYou{model_part}{attach_part}: "
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

        # Personas list
        elif command in ["personas", "/personas"]:
            print("\nAvailable Personas:")
            for p in list_personas():
                curr = " (Active)" if p.id == active_persona.id else ""
                print(f"- {p.display_label} [{p.id}] (temp: {p.temperature}){curr}")
                print(f"  {p.description}")

        # Persona switch
        elif command.startswith("persona ") or command.startswith("/persona "):
            parts = user_input.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target:
                print("\nAssistant: Please specify a persona name or ID, e.g. persona code")
            else:
                active_persona = get_persona(target)
                print(f"\nAssistant: Switched persona to {active_persona.display_label} (temp: {active_persona.temperature})")

        elif command in ["persona", "/persona"]:
            print(f"\nAssistant: Current persona is {active_persona.display_label} (temp: {active_persona.temperature})")
            print(f"Description: {active_persona.description}")

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

        # List models
        elif command in ["/models", "models"]:
            print("\nAssistant: Fetching installed Ollama models...")
            models_list = fetch_local_models()
            if not models_list:
                print("No installed models found or Ollama is offline.")
            else:
                print(f"\nInstalled Models (Active: {active_model}):")
                for m in models_list:
                    star = "* " if m.name == active_model else "  "
                    print(f" {star}{m.display_name}")

        # Switch model
        elif command.startswith("/model ") or command.startswith("model ") or command.startswith("/switch "):
            parts = user_input.split(maxsplit=1)
            new_model = parts[1].strip() if len(parts) > 1 else ""
            if new_model:
                active_model = new_model
                print(f"\nAssistant: Switched active model to '{active_model}'.")
            else:
                print(f"\nAssistant: Current active model is '{active_model}'.")

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

        # Schedule / Automation commands
        elif command.startswith("/schedule") or command.startswith("/cron") or command.startswith("schedule"):
            store = TaskStore()
            args_str = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
            subparts = args_str.split(maxsplit=1)
            subcmd = subparts[0].lower() if subparts else "list"

            if subcmd in ["list", ""]:
                tasks = store.all()
                if not tasks:
                    print("\nAssistant: No scheduled automation tasks found. Use '/schedule add' to create one.")
                else:
                    print(f"\nScheduled Tasks ({len(tasks)}):")
                    for idx, t in enumerate(tasks, 1):
                        status_mark = "● Active" if t.enabled else "⏸ Paused"
                        print(f"{idx}. [{t.id}] {t.title} ({status_mark})")
                        print(f"   Schedule: {t.cron_expression} (Next: {t.next_run or 'N/A'})")
                        print(f"   Prompt:   {t.prompt[:80]}{'…' if len(t.prompt) > 80 else ''}")
            elif subcmd == "add":
                rem = subparts[1] if len(subparts) > 1 else ""
                import shlex
                try:
                    tokens = shlex.split(rem)
                except Exception:
                    tokens = rem.split()
                if len(tokens) < 2:
                    print("\nAssistant: Usage: /schedule add \"<cron>\" \"<prompt>\" [--name \"<title>\"]")
                else:
                    cron_exp = tokens[0]
                    prompt_text = tokens[1]
                    title_text = "Scheduled Task"
                    if "--name" in tokens:
                        n_idx = tokens.index("--name")
                        if n_idx + 1 < len(tokens):
                            title_text = tokens[n_idx + 1]
                    try:
                        t = store.add(title=title_text, cron_expression=cron_exp, prompt=prompt_text, persona=active_persona.id)
                        print(f"\nAssistant: Successfully scheduled task [{t.id}] '{t.title}' for '{t.cron_expression}'.")
                    except Exception as e:
                        print(f"\nAssistant: Error scheduling task: {e}")
            elif subcmd == "run":
                target_id = subparts[1].strip() if len(subparts) > 1 else ""
                task = store.get(target_id)
                if not task:
                    all_tasks = store.all()
                    try:
                        idx = int(target_id) - 1
                        if 0 <= idx < len(all_tasks):
                            task = all_tasks[idx]
                    except ValueError:
                        pass
                if not task:
                    print(f"\nAssistant: Task '{target_id}' not found.")
                else:
                    print(f"\nAssistant: Executing scheduled task [{task.id}] '{task.title}'...")
                    answer = ask_ai(task.prompt, persona=task.persona)
                    print(f"\nResult:\n{answer}")
                    now_str = local_now().isoformat()
                    store.update(task.id, last_status="success", last_output=answer[:160], last_run=now_str)
            elif subcmd == "toggle":
                target_id = subparts[1].strip() if len(subparts) > 1 else ""
                task = store.get(target_id)
                if not task:
                    all_tasks = store.all()
                    try:
                        idx = int(target_id) - 1
                        if 0 <= idx < len(all_tasks):
                            task = all_tasks[idx]
                    except ValueError:
                        pass
                if not task:
                    print(f"\nAssistant: Task '{target_id}' not found.")
                else:
                    t = store.toggle(task.id)
                    state = "enabled" if t.enabled else "disabled"
                    print(f"\nAssistant: Task '{t.title}' is now {state}.")
            elif subcmd == "delete":
                target_id = subparts[1].strip() if len(subparts) > 1 else ""
                task = store.get(target_id)
                if not task:
                    all_tasks = store.all()
                    try:
                        idx = int(target_id) - 1
                        if 0 <= idx < len(all_tasks):
                            task = all_tasks[idx]
                    except ValueError:
                        pass
                if not task:
                    print(f"\nAssistant: Task '{target_id}' not found.")
                else:
                    store.delete(task.id)
                    print(f"\nAssistant: Deleted scheduled task [{task.id}] '{task.title}'.")
            else:
                print(f"\nAssistant: Unknown schedule action '{subcmd}'. Available: list, add, run, toggle, delete.")

# Agent tool commands
        elif command.startswith("/exec") or command.startswith("/python"):
            reg = ToolRegistry()
            code = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
            res = reg.execute_tool("python_runner", {"code": code})
            out = res.get("output") or res.get("error") or "(no output)"
            print(f"\n[Tool: python_runner ({res.get('duration_ms', 0):.1f}ms)]\n{out}")
        elif command.startswith("/tool"):
            reg = ToolRegistry()
            parts = user_input.split(maxsplit=2)
            t_name = parts[1].strip() if len(parts) > 1 else ""
            t_arg = parts[2].strip() if len(parts) > 2 else "{}"
            import json
            try:
                t_args = json.loads(t_arg)
            except Exception:
                t_args = {"raw": t_arg}
            res = reg.execute_tool(t_name, t_args)
            out = res.get("output") or res.get("error") or "(no output)"
            print(f"\n[Tool: {t_name}]\n{out}")

        # Performance analytics
        elif command in ["/stats", "/analytics", "/metrics"]:
            print(f"\nAssistant: {session_analytics.summary()}")

        # General AI question
        else:
            print(f"\nAssistant: Thinking ({active_model})...", flush=True)
            system_prompt = (active_persona.system_prompt + "\n\n") if active_persona and getattr(active_persona, "system_prompt", None) else SYSTEM_PROMPT
            options = {"temperature": active_persona.temperature} if active_persona and hasattr(active_persona, "temperature") else None
            user_content = assemble_prompt_with_attachments(user_input, active_attachments) if active_attachments else user_input
            result = chat(build_messages(system_prompt, [{"role": "user", "content": user_content}]), model=active_model, options=options)
            if result.ok:
                if result.metrics:
                    session_analytics.record(result.metrics)
                print("\nAssistant:", result.answer)
                if result.metrics and result.metrics.tps > 0:
                    print(f"[{result.metrics.short_badge}]")
            elif result.error == ERROR_CONNECTION:
                print("I cannot connect to Ollama. Please make sure Ollama is running.")
            elif result.error == ERROR_TIMEOUT:
                print("The AI is taking too long to respond. Please try again.")
            elif result.error == ERROR_INVALID:
                print("An error occurred: Ollama returned an invalid response.")
            else:
                print(f"An error occurred: {result.detail}")
            active_attachments.clear()


# ---------------- START ----------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="JUGNU Terminal Assistant")
    parser.add_argument("--model", type=str, default=None, help="Ollama model to use")
    parser.add_argument("--persona", default="default", help="Initial persona (default, code, creative, tutor, concise)")
    parser.add_argument("--file", "-f", action="append", help="Path to local file to attach")
    args, _ = parser.parse_known_args()
    run_assistant(model=args.model, persona_name=args.persona, initial_files=args.file)
