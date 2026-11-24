"""Autonomous Multi-Turn ReAct Agent Loop for JUGNU.

Coordinates tool invocation, sandbox execution, observation feedback,
and final answer synthesis with strict recursion bounds.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent_tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class AgentStep:
    step_type: str  # "tool_call", "observation", "final_answer"
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[Dict[str, Any]] = None
    content: str = ""


class AgentLoop:
    """Multi-turn autonomous execution state machine."""

    def __init__(self, registry: Optional[ToolRegistry] = None, max_turns: int = 5):
        self.registry = registry or ToolRegistry()
        self.max_turns = max_turns

    def run(
        self,
        user_prompt: str,
        chat_history: List[Dict[str, Any]],
        llm_fn: Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], Dict[str, Any]],
        on_step: Optional[Callable[[AgentStep], None]] = None,
    ) -> Dict[str, Any]:
        """Runs the ReAct autonomous loop until completion or max_turns reached."""
        steps: List[AgentStep] = []
        messages: List[Dict[str, Any]] = list(chat_history)
        tools_schema = self.registry.get_tools_schema()

        # Add initial user message if not already present
        if not messages or messages[-1].get("content") != user_prompt:
            messages.append({"role": "user", "content": user_prompt})

        turn = 0
        final_answer = ""

        while turn < self.max_turns:
            turn += 1
            # Call model
            response = llm_fn(messages, tools_schema)
            msg = response.get("message", {})
            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls", [])

            # Fallback inline JSON parser for models that output tool calls in text
            if not tool_calls:
                inferred = self._parse_inline_tool_call(content)
                if inferred:
                    tool_calls = [inferred]

            if not tool_calls:
                # Model produced final text answer
                final_answer = content
                step = AgentStep(step_type="final_answer", content=final_answer)
                steps.append(step)
                if on_step:
                    on_step(step)
                break

            # Execute tool call(s)
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name") or tc.get("name")
                raw_args = fn.get("arguments") or tc.get("args") or {}

                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {"raw": raw_args}
                else:
                    args = dict(raw_args)

                # Record tool call step
                call_step = AgentStep(
                    step_type="tool_call",
                    tool_name=name,
                    tool_args=args,
                    content=f"Calling `{name}` with {json.dumps(args)}",
                )
                steps.append(call_step)
                if on_step:
                    on_step(call_step)

                # Execute in sandbox
                result = self.registry.execute_tool(name, args)

                # Record observation step
                obs_content = result.get("output") or result.get("error") or ""
                obs_step = AgentStep(
                    step_type="observation",
                    tool_name=name,
                    tool_result=result,
                    content=obs_content,
                )
                steps.append(obs_step)
                if on_step:
                    on_step(obs_step)

                # Append tool observation to message history
                messages.append({
                    "role": "tool",
                    "name": name,
                    "content": json.dumps(result),
                })

        if not final_answer and steps:
            # Fallback if max turns reached without synthesis
            last_obs = [s.content for s in steps if s.step_type == "observation"]
            final_answer = last_obs[-1] if last_obs else "Agent completed tool execution."

        return {
            "final_answer": final_answer,
            "steps": steps,
            "turns": turn,
        }

    def _parse_inline_tool_call(self, text: str) -> Optional[Dict[str, Any]]:
        """Detect and parse tool call formatted in markdown code fence or JSON."""
        # Match ```json {"tool": "python_runner", "args": {...}} ```
        pattern = r"```(?:json)?\s*(\{[\s\S]*?\"(?:tool|function|name)\"[\s\S]*?\})\s*```"
        match = re.search(pattern, text)
        if match:
            try:
                data = json.loads(match.group(1))
                name = data.get("tool") or data.get("function") or data.get("name")
                args = data.get("args") or data.get("arguments") or {}
                if name and self.registry.get_tool(name):
                    return {"name": name, "args": args}
            except Exception:
                pass

        # Check raw JSON string
        trimmed = text.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                data = json.loads(trimmed)
                name = data.get("tool") or data.get("function") or data.get("name")
                args = data.get("args") or data.get("arguments") or {}
                if name and self.registry.get_tool(name):
                    return {"name": name, "args": args}
            except Exception:
                pass

        return None
