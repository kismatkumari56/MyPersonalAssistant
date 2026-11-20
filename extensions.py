"""Safe local extensions engine, custom slash commands API, and hook lifecycle system for JUGNU.

Provides:
- Extension base class with lifecycle hooks and slash command registration.
- ExtensionManager for lifecycle management, discovery, and fault-tolerant execution.
- Built-in offline extensions: TodoExtension, ScratchpadExtension, DigestExtension.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

EXTENSIONS_CONFIG_FILE = "jugnu_extensions.json"
TODOS_FILE = "jugnu_todos.json"
SCRATCHPAD_FILE = "jugnu_scratchpad.json"


class Extension:
    """Base class for all JUGNU extensions."""

    name: str = "base_extension"
    title: str = "Base Extension"
    description: str = "Base extension description"
    version: str = "1.0.0"

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.getcwd()
        self.commands: Dict[str, Callable[[str, Dict[str, Any]], str]] = {}
        self.register_commands()

    def register_commands(self) -> None:
        """Override to register slash commands using register_command."""
        pass

    def register_command(
        self,
        command_trigger: str,
        handler: Callable[[str, Dict[str, Any]], str],
        description: str = "",
        usage: str = "",
    ) -> None:
        """Register a slash command trigger (e.g. '/todo')."""
        trigger = command_trigger.strip()
        if not trigger.startswith("/"):
            trigger = "/" + trigger
        self.commands[trigger] = {
            "handler": handler,
            "description": description or self.description,
            "usage": usage or trigger,
        }

    # Lifecycle hooks (safe no-op defaults)
    def on_chat_created(self, chat_id: str) -> None:
        pass

    def before_message_sent(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return message

    def after_response_received(self, response: Dict[str, Any]) -> None:
        pass


@dataclass
class TodoItem:
    id: int
    text: str
    completed: bool = False
    priority: str = "normal"  # low, normal, high
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class TodoExtension(Extension):
    """Offline task and todo list manager directly accessible via /todo."""

    name = "todo"
    title = "Todo & Task Tracker"
    description = "Manage quick task lists and checklist items directly within chat."
    version = "1.1.0"

    def __init__(self, data_dir: Optional[str] = None):
        super().__init__(data_dir=data_dir)
        self.filepath = os.path.join(self.data_dir, TODOS_FILE)
        self.todos: List[TodoItem] = self._load()

    def register_commands(self) -> None:
        self.register_command(
            "/todo",
            self.handle_todo,
            description="Manage todos: /todo [add|list|done|del|clear] [task]",
            usage="/todo [add <text> | list | done <id> | del <id> | clear]",
        )
        self.register_command(
            "/todos",
            self.handle_todo,
            description="Alias for /todo list",
            usage="/todos",
        )

    def _load(self) -> List[TodoItem]:
        if not os.path.exists(self.filepath):
            return []
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                raw = json.load(f)
                return [TodoItem(**item) for item in raw]
        except Exception as e:
            logger.warning("Failed to load todos from %s: %s", self.filepath, e)
            return []

    def _save(self) -> None:
        try:
            temp_path = self.filepath + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump([asdict(item) for item in self.todos], f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.filepath)
        except Exception as e:
            logger.error("Failed to save todos to %s: %s", self.filepath, e)

    def handle_todo(self, args: str, context: Optional[Dict[str, Any]] = None) -> str:
        args_str = args.strip()
        if not args_str:
            return self._list_todos()

        parts = args_str.split(maxsplit=1)
        subcmd = parts[0].lower()
        subarg = parts[1].strip() if len(parts) > 1 else ""

        if subcmd in ["list", "all", "ls"]:
            return self._list_todos()

        elif subcmd in ["add", "new", "+"]:
            if not subarg:
                return "⚠️ Usage: `/todo add <task text> [--priority high|normal|low]`"
            priority = "normal"
            if "--priority" in subarg:
                p_parts = subarg.split("--priority")
                subarg = p_parts[0].strip()
                p_val = p_parts[1].strip().split()[0].lower() if p_parts[1].strip() else "normal"
                if p_val in ["high", "urgent"]:
                    priority = "high"
                elif p_val in ["low"]:
                    priority = "low"
            next_id = max([t.id for t in self.todos], default=0) + 1
            item = TodoItem(id=next_id, text=subarg, priority=priority)
            self.todos.append(item)
            self._save()
            p_badge = f" `[{priority.upper()}]`" if priority != "normal" else ""
            return f"✅ Added todo #{item.id}: **{item.text}**{p_badge}"

        elif subcmd in ["done", "complete", "check", "finish"]:
            if not subarg:
                return "⚠️ Usage: `/todo done <task number>`"
            try:
                target_id = int(subarg)
            except ValueError:
                return f"⚠️ Invalid todo number: '{subarg}'"
            for t in self.todos:
                if t.id == target_id:
                    t.completed = True
                    self._save()
                    return f"🎉 Marked todo #{t.id} as completed: ~~{t.text}~~"
            return f"⚠️ Todo #{target_id} not found."

        elif subcmd in ["del", "delete", "rm", "remove"]:
            if not subarg:
                return "⚠️ Usage: `/todo del <task number>`"
            try:
                target_id = int(subarg)
            except ValueError:
                return f"⚠️ Invalid todo number: '{subarg}'"
            initial_len = len(self.todos)
            self.todos = [t for t in self.todos if t.id != target_id]
            if len(self.todos) < initial_len:
                self._save()
                return f"🗑️ Deleted todo #{target_id}."
            return f"⚠️ Todo #{target_id} not found."

        elif subcmd in ["clear", "prune"]:
            prev_len = len(self.todos)
            self.todos = [t for t in self.todos if not t.completed]
            removed = prev_len - len(self.todos)
            self._save()
            return f"🧹 Cleared {removed} completed todo(s)."

        else:
            # Treat entire text as adding a new todo item
            next_id = max([t.id for t in self.todos], default=0) + 1
            item = TodoItem(id=next_id, text=args_str)
            self.todos.append(item)
            self._save()
            return f"✅ Added todo #{item.id}: **{item.text}**"

    def _list_todos(self) -> str:
        if not self.todos:
            return "📝 **Todo List**: No tasks registered. Use `/todo add <task>` to add one."
        lines = ["📝 **Your Tasks & Todos**:"]
        pending = [t for t in self.todos if not t.completed]
        done = [t for t in self.todos if t.completed]

        if pending:
            lines.append("\n**Pending:**")
            for t in pending:
                p_badge = " 🔴" if t.priority == "high" else (" 🟢" if t.priority == "low" else "")
                lines.append(f"- [ ] **#{t.id}** {t.text}{p_badge}")

        if done:
            lines.append(f"\n**Completed ({len(done)}):**")
            for t in done:
                lines.append(f"- [x] ~~#{t.id} {t.text}~~")

        summary = f"\n*Total: {len(self.todos)} | Pending: {len(pending)} | Completed: {len(done)}*"
        lines.append(summary)
        return "\n".join(lines)


class ScratchpadExtension(Extension):
    """Offline persistent scratchpad for code snippets, reference notes, and working memory."""

    name = "scratchpad"
    title = "Scratchpad & Snippet Buffer"
    description = "Persistent clipboard for draft prompts, code snippets, and quick scratch notes."
    version = "1.0.0"

    def __init__(self, data_dir: Optional[str] = None):
        super().__init__(data_dir=data_dir)
        self.filepath = os.path.join(self.data_dir, SCRATCHPAD_FILE)
        self.content = self._load()

    def register_commands(self) -> None:
        self.register_command(
            "/scratch",
            self.handle_scratch,
            description="Manage scratchpad: /scratch [get|set|append|clear] [content]",
            usage="/scratch [get | set <text> | append <text> | clear]",
        )
        self.register_command(
            "/scratchpad",
            self.handle_scratch,
            description="Alias for /scratch",
            usage="/scratchpad",
        )

    def _load(self) -> str:
        if not os.path.exists(self.filepath):
            return ""
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("content", "")
        except Exception as e:
            logger.warning("Failed to load scratchpad from %s: %s", self.filepath, e)
            return ""

    def _save(self) -> None:
        try:
            temp_path = self.filepath + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({"content": self.content, "updated_at": datetime.now().isoformat()}, f, indent=2)
            os.replace(temp_path, self.filepath)
        except Exception as e:
            logger.error("Failed to save scratchpad to %s: %s", self.filepath, e)

    def handle_scratch(self, args: str, context: Optional[Dict[str, Any]] = None) -> str:
        args_str = args.strip()
        if not args_str:
            return self._get_content()

        parts = args_str.split(maxsplit=1)
        subcmd = parts[0].lower()
        subarg = parts[1] if len(parts) > 1 else ""

        if subcmd in ["get", "show", "view", "cat"]:
            return self._get_content()

        elif subcmd in ["set", "write"]:
            self.content = subarg
            self._save()
            return f"📋 Scratchpad updated ({len(self.content)} chars)."

        elif subcmd in ["append", "add", "+"]:
            if self.content:
                self.content += "\n" + subarg
            else:
                self.content = subarg
            self._save()
            return f"📋 Appended to scratchpad ({len(self.content)} chars total)."

        elif subcmd in ["clear", "reset", "empty"]:
            self.content = ""
            self._save()
            return "📋 Scratchpad cleared."

        else:
            # Treat entire text as append
            if self.content:
                self.content += "\n" + args_str
            else:
                self.content = args_str
            self._save()
            return f"📋 Appended to scratchpad ({len(self.content)} chars total)."

    def _get_content(self) -> str:
        if not self.content:
            return "📋 **Scratchpad is empty.** Use `/scratch set <content>` or `/scratch append <content>`."
        return f"📋 **Scratchpad Buffer** ({len(self.content)} chars):\n\n```\n{self.content}\n```"


class DigestExtension(Extension):
    """Conversation digest and telemetry metrics generator."""

    name = "digest"
    title = "Conversation Digest & Telemetry"
    description = "Computes word count, turns, latency estimates, and readability stats for the active conversation."
    version = "1.0.0"

    def register_commands(self) -> None:
        self.register_command(
            "/digest",
            self.handle_digest,
            description="Generate a metrics and statistics digest of the conversation.",
            usage="/digest",
        )

    def handle_digest(self, args: str, context: Optional[Dict[str, Any]] = None) -> str:
        ctx = context or {}
        messages = ctx.get("messages", [])
        chat_title = ctx.get("title", "Active Conversation")

        if not messages:
            return "📊 **Conversation Digest**: No messages in active chat to analyze."

        user_messages = [m for m in messages if m.get("role") == "user"]
        assistant_messages = [m for m in messages if m.get("role") == "assistant"]

        user_words = sum(len(m.get("content", "").split()) for m in user_messages)
        assistant_words = sum(len(m.get("content", "").split()) for m in assistant_messages)
        total_words = user_words + assistant_words

        total_chars = sum(len(m.get("content", "")) for m in messages)
        est_tokens = int(total_chars / 4) if total_chars else 0

        # Calculate user vs assistant dialogue balance ratio
        ratio = (assistant_words / user_words) if user_words > 0 else 0.0

        return (
            f"📊 **Conversation Digest: {chat_title}**\n\n"
            f"• **Total Turns**: {len(messages)} ({len(user_messages)} User, {len(assistant_messages)} Assistant)\n"
            f"• **User Word Count**: {user_words:,} words\n"
            f"• **Assistant Word Count**: {assistant_words:,} words\n"
            f"• **Total Word Volume**: {total_words:,} words\n"
            f"• **Estimated Token Footprint**: ~{est_tokens:,} tokens\n"
            f"• **Expansion Ratio**: {ratio:.1f}x assistant response length per prompt\n"
            f"• **Average Reply Length**: {(assistant_words // len(assistant_messages)) if assistant_messages else 0} words"
        )


class ExtensionManager:
    """Central registry and lifecycle dispatcher for JUGNU extensions."""

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.getcwd()
        self.config_file = os.path.join(self.data_dir, EXTENSIONS_CONFIG_FILE)
        self.extensions: Dict[str, Extension] = {}
        self.enabled_map: Dict[str, bool] = {}
        self._load_config()
        self._register_builtins()

    def _load_config(self) -> None:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.enabled_map = data.get("enabled", {})
            except Exception as e:
                logger.warning("Failed to load extension config: %e", e)
                self.enabled_map = {}

    def _save_config(self) -> None:
        try:
            temp_path = self.config_file + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({"enabled": self.enabled_map}, f, indent=2)
            os.replace(temp_path, self.config_file)
        except Exception as e:
            logger.error("Failed to save extension config: %e", e)

    def _register_builtins(self) -> None:
        """Register default offline extensions."""
        self.register_extension(TodoExtension(data_dir=self.data_dir))
        self.register_extension(ScratchpadExtension(data_dir=self.data_dir))
        self.register_extension(DigestExtension(data_dir=self.data_dir))

    def register_extension(self, extension: Extension) -> None:
        """Register an extension instance."""
        self.extensions[extension.name] = extension
        if extension.name not in self.enabled_map:
            self.enabled_map[extension.name] = True
        self._save_config()

    def is_enabled(self, name: str) -> bool:
        return self.enabled_map.get(name, True)

    def enable_extension(self, name: str) -> bool:
        if name in self.extensions:
            self.enabled_map[name] = True
            self._save_config()
            return True
        return False

    def disable_extension(self, name: str) -> bool:
        if name in self.extensions:
            self.enabled_map[name] = False
            self._save_config()
            return True
        return False

    def list_extensions(self) -> List[Dict[str, Any]]:
        """Return list of all registered extensions with metadata and state."""
        return [
            {
                "name": ext.name,
                "title": ext.title,
                "description": ext.description,
                "version": ext.version,
                "enabled": self.is_enabled(ext.name),
                "commands": list(ext.commands.keys()),
            }
            for ext in self.extensions.values()
        ]

    def get_available_slash_commands(self) -> List[Dict[str, str]]:
        """Return all active slash command definitions for autocomplete."""
        results = []
        for ext_name, ext in self.extensions.items():
            if not self.is_enabled(ext_name):
                continue
            for trigger, meta in ext.commands.items():
                results.append({
                    "command": trigger,
                    "description": meta.get("description", ext.description),
                    "usage": meta.get("usage", trigger),
                    "extension": ext.title,
                })
        return sorted(results, key=lambda x: x["command"])

    def is_slash_command(self, text: str) -> bool:
        trimmed = text.strip()
        if not trimmed.startswith("/"):
            return False
        first_token = trimmed.split()[0].lower()
        for ext_name, ext in self.extensions.items():
            if not self.is_enabled(ext_name):
                continue
            if first_token in ext.commands:
                return True
        return False

    def handle_command(self, text: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Execute a matching slash command safely. Returns output string or None."""
        trimmed = text.strip()
        if not trimmed.startswith("/"):
            return None

        tokens = trimmed.split(maxsplit=1)
        trigger = tokens[0].lower()
        args = tokens[1] if len(tokens) > 1 else ""

        for ext_name, ext in self.extensions.items():
            if not self.is_enabled(ext_name):
                continue
            if trigger in ext.commands:
                cmd_meta = ext.commands[trigger]
                handler = cmd_meta["handler"]
                try:
                    return handler(args, context or {})
                except Exception as e:
                    logger.exception("Error executing extension command %s: %s", trigger, e)
                    return f"❌ Error executing `{trigger}`: {e}"

        return None

    # Lifecycle dispatches with full fault-tolerance
    def dispatch_chat_created(self, chat_id: str) -> None:
        for ext_name, ext in self.extensions.items():
            if self.is_enabled(ext_name):
                try:
                    ext.on_chat_created(chat_id)
                except Exception as e:
                    logger.warning("Extension %s raised in on_chat_created: %s", ext_name, e)

    def dispatch_before_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        msg = message
        for ext_name, ext in self.extensions.items():
            if self.is_enabled(ext_name):
                try:
                    res = ext.before_message_sent(msg)
                    if res is not None:
                        msg = res
                except Exception as e:
                    logger.warning("Extension %s raised in before_message_sent: %s", ext_name, e)
        return msg

    def dispatch_after_response(self, response: Dict[str, Any]) -> None:
        for ext_name, ext in self.extensions.items():
            if self.is_enabled(ext_name):
                try:
                    ext.after_response_received(response)
                except Exception as e:
                    logger.warning("Extension %s raised in after_response_received: %s", ext_name, e)


# Global singleton instance
_default_manager: Optional[ExtensionManager] = None


def get_extension_manager(data_dir: Optional[str] = None) -> ExtensionManager:
    global _default_manager
    if _default_manager is None or (data_dir and _default_manager.data_dir != data_dir):
        _default_manager = ExtensionManager(data_dir=data_dir)
    return _default_manager
