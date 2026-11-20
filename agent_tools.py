"""Core Agentic Tool Registry and System Capabilities for JUGNU.

Provides:
- python_runner: Sandboxed Python code execution.
- file_inspector: Safe workspace-restricted file listing and reading.
- system_diagnostics: Real-time CPU, RAM, disk, and platform status.
- ToolRegistry generating JSON Schema specs for Ollama tool calling.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sandbox import execute_sandboxed_python

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]

    def to_ollama_spec(self) -> Dict[str, Any]:
        """Format as Ollama / OpenAI compliant function tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Registry managing available agent tools and safe dispatch."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = os.path.abspath(workspace_root or os.getcwd())
        self.tools: Dict[str, Tool] = {}
        self._register_builtins()

    def register_tool(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def list_tools(self) -> List[Tool]:
        return list(self.tools.values())

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        return [tool.to_ollama_spec() for tool in self.tools.values()]

    def execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.get_tool(name)
        if not tool:
            return {
                "success": False,
                "output": "",
                "error": f"ToolNotFound: No tool named '{name}' is registered.",
            }
        try:
            return tool.handler(args)
        except Exception as e:
            logger.exception("Error executing tool '%s': %s", name, e)
            return {
                "success": False,
                "output": "",
                "error": f"ExecutionError: {e}",
            }

    # ---------------- BUILT-IN TOOL IMPLEMENTATIONS ----------------

    def _register_builtins(self) -> None:
        # 1. Python Runner
        self.register_tool(
            Tool(
                name="python_runner",
                description="Executes arbitrary Python code safely in an AST-validated sandbox with math, statistics, datetime, and random libraries.",
                parameters={
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Valid Python code to execute. Use print() to emit output.",
                        }
                    },
                    "required": ["code"],
                },
                handler=self._handle_python_runner,
            )
        )

        # 2. File Inspector
        self.register_tool(
            Tool(
                name="file_inspector",
                description="Inspects workspace files and directories. Restricted strictly to the local workspace root for security.",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list_dir", "read_file", "search_files"],
                            "description": "The file operation to perform.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Relative file or directory path within workspace.",
                            "default": ".",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern for search_files (e.g. '*.py').",
                            "default": "*",
                        },
                        "max_lines": {
                            "type": "integer",
                            "description": "Maximum lines to read for read_file.",
                            "default": 100,
                        },
                    },
                    "required": ["action"],
                },
                handler=self._handle_file_inspector,
            )
        )

        # 3. System Diagnostics
        self.register_tool(
            Tool(
                name="system_diagnostics",
                description="Returns real-time system metrics including platform OS, Python version, CPU count, and disk space.",
                parameters={
                    "type": "object",
                    "properties": {},
                },
                handler=self._handle_system_diagnostics,
            )
        )

    def _safe_resolve_path(self, rel_path: str) -> str:
        """Resolve path and ensure it does not escape workspace_root."""
        cleaned = rel_path.strip().lstrip("/\\")
        resolved = os.path.abspath(os.path.join(self.workspace_root, cleaned))
        # Ensure resolved starts with workspace_root
        common = os.path.commonpath([self.workspace_root, resolved])
        if common != self.workspace_root:
            raise PermissionError(f"Access denied: Path '{rel_path}' attempts to traverse outside the workspace.")
        return resolved

    def _handle_python_runner(self, args: Dict[str, Any]) -> Dict[str, Any]:
        code = args.get("code", "")
        if not code.strip():
            return {"success": False, "output": "", "error": "No Python code provided."}
        return execute_sandboxed_python(code, timeout=5.0)

    def _handle_file_inspector(self, args: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action", "list_dir")
        raw_path = args.get("path", ".")

        try:
            target_path = self._safe_resolve_path(raw_path)
        except PermissionError as pe:
            return {"success": False, "output": "", "error": str(pe)}

        if action == "list_dir":
            if not os.path.exists(target_path):
                return {"success": False, "output": "", "error": f"Directory not found: '{raw_path}'"}
            if not os.path.isdir(target_path):
                return {"success": False, "output": "", "error": f"Path is not a directory: '{raw_path}'"}
            try:
                entries = sorted(os.listdir(target_path))
                res = []
                for e in entries[:200]:
                    full = os.path.join(target_path, e)
                    kind = "DIR" if os.path.isdir(full) else f"{os.path.getsize(full)}B"
                    res.append(f"[{kind}] {e}")
                return {"success": True, "output": "\n".join(res) if res else "(empty directory)"}
            except Exception as e:
                return {"success": False, "output": "", "error": str(e)}

        elif action == "read_file":
            if not os.path.exists(target_path):
                return {"success": False, "output": "", "error": f"File not found: '{raw_path}'"}
            if not os.path.isfile(target_path):
                return {"success": False, "output": "", "error": f"Path is not a file: '{raw_path}'"}
            max_lines = int(args.get("max_lines", 100))
            try:
                with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = [f.readline() for _ in range(max_lines)]
                out = "".join(lines).strip()
                return {"success": True, "output": out if out else "(empty file)"}
            except Exception as e:
                return {"success": False, "output": "", "error": str(e)}

        elif action == "search_files":
            pattern = args.get("pattern", "*")
            base = Path(target_path if os.path.isdir(target_path) else self.workspace_root)
            try:
                matches = [
                    str(p.relative_to(self.workspace_root))
                    for p in base.rglob(pattern)
                    if not any(part.startswith(".") for part in p.parts)
                ][:100]
                return {"success": True, "output": "\n".join(matches) if matches else f"No files matching '{pattern}'"}
            except Exception as e:
                return {"success": False, "output": "", "error": str(e)}

        return {"success": False, "output": "", "error": f"Unknown file action '{action}'."}

    def _handle_system_diagnostics(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            total, used, free = shutil.disk_usage(self.workspace_root)
            gb = 1024 ** 3
            metrics = {
                "os_platform": platform.platform(),
                "python_version": platform.python_version(),
                "processor": platform.processor() or "Unknown CPU",
                "cpu_cores": os.cpu_count() or 1,
                "disk_total_gb": round(total / gb, 2),
                "disk_free_gb": round(free / gb, 2),
                "workspace": os.path.basename(self.workspace_root),
            }
            output_lines = [
                f"• OS Platform:    {metrics['os_platform']}",
                f"• Python Version: {metrics['python_version']}",
                f"• CPU Cores:      {metrics['cpu_cores']} cores ({metrics['processor']})",
                f"• Disk Space:     {metrics['disk_free_gb']} GB free of {metrics['disk_total_gb']} GB",
                f"• Workspace:      {metrics['workspace']}",
            ]
            return {"success": True, "output": "\n".join(output_lines)}
        except Exception as e:
            return {"success": False, "output": "", "error": str(e)}
