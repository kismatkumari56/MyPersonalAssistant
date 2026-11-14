"""Inference Performance Analytics Engine for JUGNU (#58).

Extracts and calculates generation performance metrics from local Ollama responses:
- Tokens per Second (TPS / generation throughput)
- Total latency and response duration
- Prompt evaluation speed and tokens evaluated
- Time to First Token (TTFT) / prompt latency
- Session-wide analytics aggregator

100% offline, pure Python standard library, zero external cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class InferenceMetrics:
    """Telemetry metrics for a single LLM inference response."""

    eval_count: int = 0                  # Tokens generated in response
    eval_duration_ns: int = 0            # Nanoseconds spent generating tokens
    prompt_eval_count: int = 0           # Tokens in prompt
    prompt_eval_duration_ns: int = 0     # Nanoseconds spent evaluating prompt
    total_duration_ns: int = 0           # Total request duration in nanoseconds
    load_duration_ns: int = 0            # Nanoseconds loading model into memory
    model: str = ""                      # Model name

    @property
    def eval_duration_s(self) -> float:
        """Generation duration in seconds."""
        return self.eval_duration_ns / 1e9

    @property
    def prompt_eval_duration_s(self) -> float:
        """Prompt evaluation duration in seconds."""
        return self.prompt_eval_duration_ns / 1e9

    @property
    def total_duration_s(self) -> float:
        """Total response time in seconds."""
        return self.total_duration_ns / 1e9

    @property
    def tps(self) -> float:
        """Generation speed in tokens per second."""
        dur = self.eval_duration_s
        return (self.eval_count / dur) if dur > 0 else 0.0

    @property
    def prompt_eval_tps(self) -> float:
        """Prompt evaluation throughput in tokens per second."""
        dur = self.prompt_eval_duration_s
        return (self.prompt_eval_count / dur) if dur > 0 else 0.0

    @property
    def ttft_ms(self) -> float:
        """Estimated Time to First Token (TTFT) in milliseconds."""
        dur_ns = self.prompt_eval_duration_ns + self.load_duration_ns
        return dur_ns / 1e6

    @property
    def short_badge(self) -> str:
        """Compact telemetry badge for message bubble headers."""
        if self.eval_count > 0 and self.eval_duration_ns > 0:
            return f"⚡ {self.tps:.1f} t/s • {self.total_duration_s:.2f}s"
        if self.total_duration_s > 0:
            return f"⚡ {self.total_duration_s:.2f}s"
        return "⚡ Local"

    @property
    def tooltip(self) -> str:
        """Detailed breakdown for hover tooltips or dialogs."""
        lines = [
            f"Generation Speed: {self.tps:.1f} tokens/sec",
            f"Output Tokens: {self.eval_count} tokens ({self.eval_duration_s:.2f}s)",
            f"Prompt Tokens: {self.prompt_eval_count} tokens ({self.prompt_eval_tps:.1f} t/s)",
            f"Total Latency: {self.total_duration_s:.2f}s",
        ]
        if self.ttft_ms > 0:
            lines.append(f"Prompt Latency / TTFT: {self.ttft_ms:.1f} ms")
        if self.model:
            lines.append(f"Model: {self.model}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to a JSON-serializable dictionary."""
        return {
            "eval_count": self.eval_count,
            "eval_duration_ns": self.eval_duration_ns,
            "prompt_eval_count": self.prompt_eval_count,
            "prompt_eval_duration_ns": self.prompt_eval_duration_ns,
            "total_duration_ns": self.total_duration_ns,
            "load_duration_ns": self.load_duration_ns,
            "model": self.model,
            "tps": round(self.tps, 2),
            "total_duration_s": round(self.total_duration_s, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> InferenceMetrics | None:
        """Reconstruct InferenceMetrics from dictionary, or None."""
        if not data or not isinstance(data, dict):
            return None
        return cls(
            eval_count=int(data.get("eval_count", 0) or 0),
            eval_duration_ns=int(data.get("eval_duration_ns", data.get("eval_duration", 0)) or 0),
            prompt_eval_count=int(data.get("prompt_eval_count", 0) or 0),
            prompt_eval_duration_ns=int(data.get("prompt_eval_duration_ns", data.get("prompt_eval_duration", 0)) or 0),
            total_duration_ns=int(data.get("total_duration_ns", data.get("total_duration", 0)) or 0),
            load_duration_ns=int(data.get("load_duration_ns", data.get("load_duration", 0)) or 0),
            model=str(data.get("model", "")),
        )


def parse_ollama_metrics(data: dict[str, Any]) -> InferenceMetrics | None:
    """Extract and calculate telemetry from raw Ollama API response dict."""
    if not isinstance(data, dict):
        return None
    # Only return an InferenceMetrics object if the response contains telemetry fields
    metric_keys = ("eval_count", "eval_duration", "total_duration", "prompt_eval_count", "prompt_eval_duration")
    if not any(k in data for k in metric_keys):
        return None
    return InferenceMetrics(
        eval_count=int(data.get("eval_count", 0) or 0),
        eval_duration_ns=int(data.get("eval_duration", 0) or 0),
        prompt_eval_count=int(data.get("prompt_eval_count", 0) or 0),
        prompt_eval_duration_ns=int(data.get("prompt_eval_duration", 0) or 0),
        total_duration_ns=int(data.get("total_duration", 0) or 0),
        load_duration_ns=int(data.get("load_duration", 0) or 0),
        model=str(data.get("model", "")),
    )


class SessionAnalytics:
    """Aggregates inference statistics across the current assistant session."""

    def __init__(self):
        self.metrics_history: list[InferenceMetrics] = []

    def record(self, metrics: InferenceMetrics | None) -> None:
        """Record an inference event."""
        if metrics and (metrics.eval_count > 0 or metrics.total_duration_ns > 0):
            self.metrics_history.append(metrics)

    @property
    def total_queries(self) -> int:
        return len(self.metrics_history)

    @property
    def total_tokens(self) -> int:
        return sum(m.eval_count for m in self.metrics_history)

    @property
    def total_time_s(self) -> float:
        return sum(m.total_duration_s for m in self.metrics_history)

    @property
    def avg_tps(self) -> float:
        valid = [m.tps for m in self.metrics_history if m.tps > 0]
        return (sum(valid) / len(valid)) if valid else 0.0

    def summary(self) -> str:
        """Format a human-readable session performance summary."""
        if not self.metrics_history:
            return "No inference activity recorded yet."
        return (
            f"Queries: {self.total_queries} | "
            f"Generated: {self.total_tokens} tokens | "
            f"Avg Speed: {self.avg_tps:.1f} tokens/sec | "
            f"Total Duration: {self.total_time_s:.2f}s"
        )
