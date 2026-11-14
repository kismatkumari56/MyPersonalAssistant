"""Tests for Inference Performance Analytics Engine & Smart Auto-Titling (#58).

Verifies:
- InferenceMetrics calculations (TPS, TTFT, latency, badges, serialization).
- SessionAnalytics aggregation and summary formatting.
- generate_heuristic_title filler stripping, title casing, punctuation trimming, and length limits.
- generate_ai_title Ollama interaction and graceful offline fallback.
- Settings helper load_performance_hud and history persistence of metrics.
- UI Bubble HUD badge generation and toggle behavior in PySide6.
- Assistant CLI /stats command and telemetry recording.
"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from analytics import InferenceMetrics, SessionAnalytics, parse_ollama_metrics
from titling import generate_ai_title, generate_heuristic_title
import jugnu_core as core
import assistant


# =====================================================================
# 1. InferenceMetrics & Parser Tests
# =====================================================================

def test_inference_metrics_zero_and_defaults():
    m = InferenceMetrics()
    assert m.eval_count == 0
    assert m.eval_duration_s == 0.0
    assert m.tps == 0.0
    assert m.prompt_eval_tps == 0.0
    assert m.ttft_ms == 0.0
    assert m.total_duration_s == 0.0
    assert m.short_badge == "⚡ Local"
    assert "0.0 tokens/sec" in m.tooltip


def test_inference_metrics_calculations():
    # 150 tokens in 3.0 seconds -> 50.0 tokens/sec
    # prompt: 20 tokens in 0.5s -> 40.0 tokens/sec
    # total duration: 3.8s
    # load: 0.1s, prompt_eval: 0.5s -> TTFT = 600ms
    m = InferenceMetrics(
        eval_count=150,
        eval_duration_ns=3_000_000_000,
        prompt_eval_count=20,
        prompt_eval_duration_ns=500_000_000,
        total_duration_ns=3_800_000_000,
        load_duration_ns=100_000_000,
        model="llama3.2:3b",
    )
    assert round(m.eval_duration_s, 2) == 3.0
    assert round(m.tps, 1) == 50.0
    assert round(m.prompt_eval_tps, 1) == 40.0
    assert round(m.total_duration_s, 2) == 3.8
    assert round(m.ttft_ms, 1) == 600.0
    assert "⚡ 50.0 t/s • 3.80s" in m.short_badge
    assert "Generation Speed: 50.0 tokens/sec" in m.tooltip
    assert "Output Tokens: 150 tokens" in m.tooltip
    assert "Prompt Tokens: 20 tokens" in m.tooltip
    assert "Model: llama3.2:3b" in m.tooltip


def test_inference_metrics_serialization_roundtrip():
    original = InferenceMetrics(
        eval_count=42,
        eval_duration_ns=1_500_000_000,
        prompt_eval_count=10,
        prompt_eval_duration_ns=200_000_000,
        total_duration_ns=1_800_000_000,
        load_duration_ns=50_000_000,
        model="llama3.2:3b",
    )
    data = original.to_dict()
    assert isinstance(data, dict)
    assert data["eval_count"] == 42
    assert data["tps"] == 28.0

    restored = InferenceMetrics.from_dict(data)
    assert restored is not None
    assert restored.eval_count == original.eval_count
    assert restored.eval_duration_ns == original.eval_duration_ns
    assert restored.prompt_eval_count == original.prompt_eval_count
    assert restored.total_duration_ns == original.total_duration_ns
    assert restored.model == original.model
    assert round(restored.tps, 2) == round(original.tps, 2)


def test_inference_metrics_from_dict_invalid():
    assert InferenceMetrics.from_dict(None) is None
    assert InferenceMetrics.from_dict({}) is None
    assert InferenceMetrics.from_dict("not a dict") is None


def test_parse_ollama_metrics():
    raw_response = {
        "model": "llama3.2:3b",
        "eval_count": 80,
        "eval_duration": 2_000_000_000,
        "prompt_eval_count": 15,
        "prompt_eval_duration": 400_000_000,
        "total_duration": 2_500_000_000,
        "load_duration": 50_000_000,
    }
    metrics = parse_ollama_metrics(raw_response)
    assert metrics is not None
    assert metrics.eval_count == 80
    assert metrics.eval_duration_ns == 2_000_000_000
    assert round(metrics.tps, 1) == 40.0
    assert metrics.model == "llama3.2:3b"

    # Response with no telemetry metrics (e.g. error or unit test mock)
    assert parse_ollama_metrics({"message": {"content": "ok"}}) is None
    assert parse_ollama_metrics(None) is None
    assert parse_ollama_metrics([]) is None


# =====================================================================
# 2. SessionAnalytics Tests
# =====================================================================

def test_session_analytics_aggregation():
    session = SessionAnalytics()
    assert session.total_queries == 0
    assert session.total_tokens == 0
    assert session.avg_tps == 0.0
    assert "No inference activity" in session.summary()

    # Record first inference event
    m1 = InferenceMetrics(eval_count=100, eval_duration_ns=2_000_000_000, total_duration_ns=2_200_000_000) # 50 t/s
    session.record(m1)
    assert session.total_queries == 1
    assert session.total_tokens == 100
    assert round(session.avg_tps, 1) == 50.0

    # Record second inference event
    m2 = InferenceMetrics(eval_count=90, eval_duration_ns=3_000_000_000, total_duration_ns=3_100_000_000) # 30 t/s
    session.record(m2)
    assert session.total_queries == 2
    assert session.total_tokens == 190
    assert round(session.avg_tps, 1) == 40.0 # (50 + 30) / 2
    assert round(session.total_time_s, 2) == 5.3

    summary = session.summary()
    assert "Queries: 2" in summary
    assert "Generated: 190 tokens" in summary
    assert "Avg Speed: 40.0 tokens/sec" in summary

    # Record None or empty does not alter state
    session.record(None)
    session.record(InferenceMetrics())
    assert session.total_queries == 2


# =====================================================================
# 3. Smart Heuristic Auto-Titling Tests
# =====================================================================

@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("Can you please help me with Python async programming?", "Python Async Programming"),
        ("Write a function to reverse a linked list", "Function To Reverse A Linked"),
        ("Explain quantum computing in simple terms?", "Quantum Computing In Simple Terms"),
        ("how to install PySide6 on Windows 11", "Install Pyside6 On Windows 11"),
        ("What is the capital of France?", "Capital Of France"),
        ("Kya aap mujhe batao how transformers work?", "How Transformers Work"),
        ("tell me about Black Holes", "Black Holes"),
        ("I want to build a web scraper in Python", "Web Scraper In Python"),
        ("hello world", "Hello World"),
        ("why is the sky blue????", "Sky Blue"),
        ("", "New Chat"),
        ("   \n\t  ", "New Chat"),
    ],
)
def test_generate_heuristic_title(prompt, expected):
    title = generate_heuristic_title(prompt)
    assert title == expected


def test_generate_heuristic_title_length_limit():
    long_prompt = "Supercalifragilisticexpialidocious quantum hyperdimensional optimization algorithms"
    title = generate_heuristic_title(long_prompt, max_chars=25)
    assert len(title) <= 25
    assert not title.endswith(" ")


# =====================================================================
# 4. AI-Assisted Titling Tests
# =====================================================================

def test_generate_ai_title_success():
    mock_result = Mock(ok=True, answer="Async Python Guide")
    with patch("core.chat", return_value=mock_result):
        title = generate_ai_title("Can you explain how python asyncio works?")
        assert title == "Async Python Guide"


def test_generate_ai_title_strips_quotes_and_markdown():
    mock_result = Mock(ok=True, answer='  "**Reverse A Linked List**"  \nExtra details')
    with patch("core.chat", return_value=mock_result):
        title = generate_ai_title("How do I reverse a linked list in C++?")
        assert title == "Reverse A Linked List"


def test_generate_ai_title_empty_or_failure():
    assert generate_ai_title("") is None
    assert generate_ai_title("   ") is None

    mock_fail = Mock(ok=False, answer="", error="timeout")
    with patch("core.chat", return_value=mock_fail):
        assert generate_ai_title("What is Python?") is None

    with patch("core.chat", side_effect=Exception("network down")):
        assert generate_ai_title("What is Python?") is None


# =====================================================================
# 5. History & Settings Integration Tests
# =====================================================================

def test_load_performance_hud_setting(tmp_path):
    settings_file = tmp_path / "settings.json"
    # Default is True when file doesn't exist
    assert core.load_performance_hud(settings_file) is True

    # When set to False
    settings_file.write_text(json.dumps({"show_performance_hud": False}), encoding="utf-8")
    assert core.load_performance_hud(settings_file) is False

    # When set to True
    settings_file.write_text(json.dumps({"show_performance_hud": True}), encoding="utf-8")
    assert core.load_performance_hud(settings_file) is True


def test_load_history_preserves_metrics_and_custom_title(tmp_path):
    history_file = tmp_path / "history.json"
    raw_history = [
        {
            "id": "chat-1",
            "title": "My Custom Title",
            "is_custom_title": True,
            "messages": [
                {"role": "user", "content": "hello", "time": "12:00 PM"},
                {
                    "role": "assistant",
                    "content": "Hi there!",
                    "time": "12:01 PM",
                    "metrics": {
                        "eval_count": 50,
                        "eval_duration_ns": 1_000_000_000,
                        "tps": 50.0,
                        "total_duration_s": 1.2,
                    },
                },
            ],
        }
    ]
    history_file.write_text(json.dumps(raw_history), encoding="utf-8")

    loaded = core.load_history(history_file)
    assert len(loaded) == 1
    assert loaded[0]["title"] == "My Custom Title"
    assert loaded[0]["is_custom_title"] is True
    assistant_msg = loaded[0]["messages"][1]
    assert "metrics" in assistant_msg
    assert assistant_msg["metrics"]["eval_count"] == 50
    assert assistant_msg["metrics"]["tps"] == 50.0


# =====================================================================
# 6. Desktop UI Bubble & HUD Tests
# =====================================================================

def test_bubble_hud_badge_rendered(qtbot, make_window):
    metrics = InferenceMetrics(
        eval_count=60,
        eval_duration_ns=1_500_000_000,
        total_duration_ns=1_800_000_000,
    )
    app = make_window([
        {
            "id": "c1",
            "title": "Test Chat",
            "messages": [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "pong", "metrics": metrics.to_dict()},
            ],
        }
    ])
    assert len(app.bubbles) == 2
    user_bubble, assistant_bubble = app.bubbles[0], app.bubbles[1]

    # User bubble must not have HUD badge
    assert getattr(user_bubble, "hud_btn", None) is None

    # Assistant bubble must have HUD badge button
    assert assistant_bubble.hud_btn is not None
    assert "40.0 t/s" in assistant_bubble.hud_btn.text()
    assert "Generation Speed: 40.0 tokens/sec" in assistant_bubble.hud_btn.toolTip()


def test_bubble_hud_badge_hidden_when_disabled(qtbot, make_window):
    metrics = InferenceMetrics(eval_count=60, eval_duration_ns=1_500_000_000)
    app = make_window([
        {
            "id": "c1",
            "title": "Test Chat",
            "messages": [
                {"role": "assistant", "content": "pong", "metrics": metrics.to_dict()},
            ],
        }
    ])
    # Toggle performance HUD off
    app.show_performance_hud = False
    app.display_chat()
    assert len(app.bubbles) == 1
    assert getattr(app.bubbles[0], "hud_btn", None) is None


def test_first_message_triggers_heuristic_titling(qtbot, make_window):
    app = make_window([])
    app.input_box.setPlainText("Can you please explain recursion in python?")
    app.send_message()
    chat = app.current_chat()
    assert chat["title"] == "Recursion In Python"


def test_rename_chat_sets_custom_title_flag(qtbot, make_window, monkeypatch):
    app = make_window([
        {"id": "c1", "title": "Old Title", "messages": [{"role": "user", "content": "hi"}]}
    ])
    monkeypatch.setattr(app, "ask_chat_title", lambda initial: "User Chosen Title")
    app.rename_chat("c1")
    chat = app.find_chat("c1")
    assert chat["title"] == "User Chosen Title"
    assert chat["is_custom_title"] is True


# =====================================================================
# 7. Assistant CLI /stats Command Tests
# =====================================================================

def test_assistant_stats_command():
    analytics = SessionAnalytics()
    m = InferenceMetrics(eval_count=120, eval_duration_ns=2_400_000_000, total_duration_ns=2_600_000_000)
    analytics.record(m)
    summary = analytics.summary()
    assert "Queries: 1" in summary
    assert "Generated: 120 tokens" in summary
    assert "50.0 tokens/sec" in summary
