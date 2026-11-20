"""Safe AST-Validated Python Interpreter Sandbox with Resource & Timeout Guards.

Strictly blocks dangerous modules, arbitrary system execution, and introspection
escapes before executing code within a sandboxed runtime.
"""

from __future__ import annotations

import ast
import contextlib
import datetime
import io
import json
import logging
import math
import random
import statistics
import sys
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SecurityViolationError(Exception):
    """Raised when arbitrary or unsafe code violates the AST security policy."""
    pass


class ASTSecurityVisitor(ast.NodeVisitor):
    """Inspects an Abstract Syntax Tree to reject unauthorized operations and imports."""

    BANNED_MODULES = {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "ctypes",
        "pty",
        "posix",
        "builtins",
        "signal",
        "multiprocessing",
        "threading",
        "pickle",
        "urllib",
        "requests",
        "http",
        "webbrowser",
        "importlib",
        "inspect",
    }

    BANNED_FUNCTIONS = {
        "eval",
        "exec",
        "__import__",
        "compile",
        "globals",
        "locals",
        "open",
        "input",
        "breakpoint",
        "getattr",
        "setattr",
        "delattr",
    }

    BANNED_ATTRIBUTES = {
        "__subclasses__",
        "__globals__",
        "__code__",
        "__bases__",
        "__mro__",
        "__dict__",
        "__class__",
        "__builtins__",
    }

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            base_mod = alias.name.split(".")[0].lower()
            if base_mod in self.BANNED_MODULES:
                raise SecurityViolationError(f"Importing module '{alias.name}' is strictly forbidden.")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            base_mod = node.module.split(".")[0].lower()
            if base_mod in self.BANNED_MODULES:
                raise SecurityViolationError(f"Importing from module '{node.module}' is strictly forbidden.")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            func_name = node.func.id.lower()
            if func_name in self.BANNED_FUNCTIONS:
                raise SecurityViolationError(f"Calling function '{func_name}()' is forbidden in sandbox.")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.BANNED_ATTRIBUTES:
            raise SecurityViolationError(f"Accessing restricted attribute '{node.attr}' is forbidden.")
        self.generic_visit(node)


def validate_python_code(code: str) -> None:
    """Parse and validate python source code against the security visitor."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SyntaxError(f"Invalid Python syntax: {e}")
    visitor = ASTSecurityVisitor()
    visitor.visit(tree)


def get_safe_builtins() -> Dict[str, Any]:
    """Whitelisted dictionary of safe builtins."""
    safe_names = [
        "abs",
        "all",
        "any",
        "ascii",
        "bin",
        "bool",
        "bytearray",
        "bytes",
        "chr",
        "complex",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "hash",
        "hex",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "oct",
        "ord",
        "pow",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "zip",
    ]
    import builtins
    d = {name: getattr(builtins, name) for name in safe_names if hasattr(builtins, name)}
    whitelisted = {"math": math, "datetime": datetime, "random": random, "statistics": statistics, "json": json}

    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        base = name.split(".")[0]
        if base in whitelisted:
            return whitelisted[base]
        raise ImportError(f"Importing module '{name}' is not allowed in sandbox.")

    d["__import__"] = _safe_import
    return d


def execute_sandboxed_python(code: str, timeout: float = 4.0) -> Dict[str, Any]:
    """Executes Python code in a safe AST-validated sandbox with timeout.

    Returns dict:
        success: bool
        output: str
        error: Optional[str]
        duration_ms: float
    """
    start_time = time.perf_counter()

    # Step 1: AST validation
    try:
        validate_python_code(code)
    except (SecurityViolationError, SyntaxError) as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": False,
            "output": "",
            "error": str(e),
            "duration_ms": round(duration_ms, 2),
        }

    # Step 2: Environment setup
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    safe_globals = {
        "__builtins__": get_safe_builtins(),
        "math": math,
        "datetime": datetime,
        "random": random,
        "statistics": statistics,
        "json": json,
    }

    exec_result: Dict[str, Any] = {"error": None}

    def _worker():
        try:
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                exec(code, safe_globals)
        except Exception as e:
            exec_result["error"] = f"{type(e).__name__}: {e}"

    # Step 3: Run thread with timeout
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    duration_ms = (time.perf_counter() - start_time) * 1000

    if thread.is_alive():
        return {
            "success": False,
            "output": stdout_buf.getvalue(),
            "error": f"ExecutionTimeoutError: Execution exceeded {timeout:.1f}s limit.",
            "duration_ms": round(duration_ms, 2),
        }

    err = exec_result.get("error") or stderr_buf.getvalue().strip()
    out = stdout_buf.getvalue().strip()

    return {
        "success": not bool(err),
        "output": out,
        "error": err if err else None,
        "duration_ms": round(duration_ms, 2),
    }
