"""Unit and integration test suite for Agentic Tools, AST Sandbox, and Multi-Turn ReAct Loop."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_loop import AgentLoop, AgentStep
from agent_tools import ToolRegistry
from sandbox import (
    ASTSecurityVisitor,
    SecurityViolationError,
    execute_sandboxed_python,
    validate_python_code,
)


@pytest.fixture
def temp_workspace(tmp_path):
    return str(tmp_path)


# ---------------- AST SECURITY TESTS ----------------

def test_ast_blocks_malicious_imports():
    malicious = [
        "import os\nos.system('echo pwned')",
        "import sys\nsys.exit(0)",
        "from subprocess import Popen",
        "import socket",
        "import shutil",
    ]
    for code in malicious:
        with pytest.raises(SecurityViolationError):
            validate_python_code(code)


def test_ast_blocks_dangerous_calls():
    dangerous = [
        "eval('1 + 1')",
        "exec('a = 5')",
        "__import__('os').system('dir')",
        "open('/etc/passwd', 'r')",
    ]
    for code in dangerous:
        with pytest.raises(SecurityViolationError):
            validate_python_code(code)


def test_ast_blocks_introspection_escapes():
    escapes = [
        "().__class__.__bases__[0].__subclasses__()",
        "def f(): pass\nf.__globals__",
        "x = (1).__class__",
    ]
    for code in escapes:
        with pytest.raises(SecurityViolationError):
            validate_python_code(code)


def test_ast_allows_safe_code():
    safe_samples = [
        "import math\nprint(math.sqrt(144))",
        "import datetime\nprint(datetime.date.today())",
        "res = [x * 2 for x in range(10) if x % 2 == 0]\nprint(res)",
        "def fib(n):\n    return n if n <= 1 else fib(n-1) + fib(n-2)\nprint(fib(6))",
    ]
    for code in safe_samples:
        validate_python_code(code)  # Should not raise


# ---------------- SANDBOX EXECUTION TESTS ----------------

def test_sandbox_executes_math():
    code = "import math\nprint(f'PI: {math.pi:.4f}')"
    res = execute_sandboxed_python(code)
    assert res["success"] is True
    assert "PI: 3.1416" in res["output"]
    assert res["error"] is None


def test_sandbox_catches_security_violations():
    code = "import os\nprint('hacked')"
    res = execute_sandboxed_python(code)
    assert res["success"] is False
    assert "strictly forbidden" in res["error"]


def test_sandbox_terminates_infinite_loop():
    code = "while True:\n    pass"
    res = execute_sandboxed_python(code, timeout=0.8)
    assert res["success"] is False
    assert "ExecutionTimeoutError" in res["error"]


# ---------------- TOOL REGISTRY TESTS ----------------

def test_tool_registry_file_inspector(temp_workspace):
    # Setup test files
    sample_file = os.path.join(temp_workspace, "hello.txt")
    with open(sample_file, "w", encoding="utf-8") as f:
        f.write("Line 1\nLine 2\nLine 3")

    reg = ToolRegistry(workspace_root=temp_workspace)

    # list_dir
    list_res = reg.execute_tool("file_inspector", {"action": "list_dir", "path": "."})
    assert list_res["success"] is True
    assert "hello.txt" in list_res["output"]

    # read_file
    read_res = reg.execute_tool("file_inspector", {"action": "read_file", "path": "hello.txt"})
    assert read_res["success"] is True
    assert "Line 1" in read_res["output"]

    # search_files
    search_res = reg.execute_tool("file_inspector", {"action": "search_files", "pattern": "*.txt"})
    assert search_res["success"] is True
    assert "hello.txt" in search_res["output"]


def test_tool_registry_traversal_blocked(temp_workspace):
    reg = ToolRegistry(workspace_root=temp_workspace)
    # Attempt to traverse outside workspace root
    res = reg.execute_tool("file_inspector", {"action": "read_file", "path": "../../secret.txt"})
    assert res["success"] is False
    assert "Access denied" in res["error"]


def test_tool_registry_system_diagnostics():
    reg = ToolRegistry()
    res = reg.execute_tool("system_diagnostics", {})
    assert res["success"] is True
    assert "OS Platform:" in res["output"]
    assert "CPU Cores:" in res["output"]


# ---------------- AGENT MULTI-TURN LOOP TESTS ----------------

def test_agent_loop_multi_turn():
    reg = ToolRegistry()
    loop = AgentLoop(registry=reg, max_turns=3)

    # Simulated LLM:
    # Turn 1: requests python_runner tool
    # Turn 2: produces final synthesis answer
    call_count = 0

    def mock_llm(messages, tools):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": "Let me calculate the square root of 256.",
                    "tool_calls": [
                        {
                            "name": "python_runner",
                            "args": {"code": "import math\nprint(math.sqrt(256))"},
                        }
                    ],
                }
            }
        else:
            return {
                "message": {
                    "role": "assistant",
                    "content": "The square root of 256 is 16.0.",
                }
            }

    result = loop.run(
        user_prompt="What is sqrt(256)?",
        chat_history=[],
        llm_fn=mock_llm,
    )

    assert result["final_answer"] == "The square root of 256 is 16.0."
    assert result["turns"] == 2
    step_types = [s.step_type for s in result["steps"]]
    assert "tool_call" in step_types
    assert "observation" in step_types
    assert "final_answer" in step_types


def test_agent_loop_inline_json_fallback():
    reg = ToolRegistry()
    loop = AgentLoop(registry=reg, max_turns=2)

    def mock_llm(messages, tools):
        return {
            "message": {
                "role": "assistant",
                "content": '```json\n{"tool": "system_diagnostics", "args": {}}\n```',
            }
        }

    res = loop.run(
        user_prompt="Show system stats",
        chat_history=[],
        llm_fn=mock_llm,
    )
    assert res["turns"] == 2
    obs = [s for s in res["steps"] if s.step_type == "observation"]
    assert len(obs) > 0
    assert "OS Platform" in obs[0].content


# ---------------- UI WIDGET TESTS ----------------

def test_tool_card_widget(qapp):
    from tool_card_widget import ToolExecutionWidget

    widget = ToolExecutionWidget(
        tool_name="python_runner",
        tool_args={"code": "print(42)"},
        tool_result={"success": True, "output": "42", "duration_ms": 12.5},
    )
    widget.show()
    assert "python_runner" in widget.tool_name
    assert widget.expanded is False

    # Toggle details
    widget.toggle_details()
    assert widget.expanded is True
    assert not widget.details_widget.isHidden()

    widget.toggle_details()
    assert widget.expanded is False
    assert widget.details_widget.isHidden()
    widget.close()
