# JUGNU – Personal AI Assistant 🤖

[![Winter of Code Social 2026](https://img.shields.io/badge/WOCS-2026-blue?style=for-the-badge&logo=github)](https://woc.codesocial.tech)
[![Python](https://img.shields.io/badge/Python-3.9+-yellow?style=for-the-badge&logo=python)](https://python.org)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=for-the-badge)](CONTRIBUTING.md)

**JUGNU (Just an Unusually Genius Neural Utility)** is a personal desktop AI assistant built using Python, Qt (PySide6), Ollama, and Llama 3.2.

JUGNU is designed to provide a simple and friendly AI chat experience while exploring practical AI and Machine Learning applications on the desktop.

---

## ❄️ Winter of Code Social (WOCS) 2026

JUGNU is an official participating project in **Winter of Code Social (WOCS) 2026**! 🎉

- 🌐 **WOCS Website**: [woc.codesocial.tech](https://woc.codesocial.tech)
- 🏆 **Leaderboard**: [woc.codesocial.tech/leaderboard](https://woc.codesocial.tech/leaderboard)
- 📖 **Contributor Guide**: Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting pull requests!

### WOCS Difficulty Levels
Contributors can earn points on the leaderboard based on merged PRs tagged with:
- level 1 (2 points) - Beginner-friendly fixes, UI tweaks, doc updates.
- level 2 (5 points) - Feature enhancements, refactoring, new UI options.
- level 3 (11 points) - Advanced features, voice/model integration, major architecture upgrades.

---

## ✨ Features

- 💬 AI-powered chat using local Llama 3.2 via Ollama
- 🎙️ 100% Offline Local Voice Dictation (STT) with push-to-talk (`Ctrl+Space` or `Ctrl+Shift+V`), click-to-dictate, and real-time mic waveform HUD (`stt.py`)
- 🌊 Dynamic audio visualizer with silence detection, background noise suppression, and auto-punctuation
- 📤 Comprehensive Multi-Format Conversation Exporter: Export any chat to PDF (`.pdf`), Markdown (`.md`), offline HTML (`.html`) with syntax highlighting, and JSON (`.json`) datasets via modal dialog or `Ctrl+E` (`exporter.py`)
- 🎭 Multi-Persona System Prompt Studio & Custom AI Persona Switcher: Built-in specialized presets (Senior Code Reviewer, Python Architect, Academic Writing Tutor, Socratic Teacher, Debugging Specialist), per-conversation persona binding, temperature & context overrides, and JSON import/export (`personas.py`). Keyboard shortcut: `Ctrl+P`
- 🔒 Local Encrypted Chat Vault & Automated Backup Scheduler: Client-side database encryption (PBKDF2-HMAC-SHA256 authenticated envelope) with master passphrase, session auto-lock timer, automated background backup scheduler (daily/weekly/manual) with retention rotation, one-click integrity verification, vacuum compaction, and restore wizard (`vault.py`). Keyboard shortcut: `Ctrl+L`
- 📝 Persistent chat history
- ➕ New Chat & Clear Current Chat options
- 🌓 Dark and Light Mode support
- 🖋️ Markdown in replies: bold, italic, headings, lists, quotes, inline code and code blocks with a Copy button (`markdown_render.py`)
- 🌐 Interface and AI replies in English or Hinglish (switch with the Language button; add more in `strings.py`)
- 🕒 Current Date and Time support
- 🤖 Custom welcome screen with mascot

## 🛠️ Tech Stack

- **Language**: Python
- **GUI**: PySide6 (Qt for Python), themed with Qt Style Sheets (`light.qss` / `dark.qss`)
- **LLM Engine**: Ollama (Llama 3.2 3B)
- **Networking**: Requests, JSON

## 🗂️ Project Structure

JUGNU has two front ends, a desktop window and a terminal version, that share one core:

| File | What it does |
| --- | --- |
| `desktop_app.py` | The desktop window (Tkinter): screens, chat history, themes, voice, buttons |
| `assistant.py` | The terminal version: greeting, commands and its own wording |
| `core.py` | **Shared by both.** Model name and Ollama address, clock helpers, safe calculator, "what time is it?" detector, `build_messages()` and `chat()` (the one place that talks to Ollama) |
| `strings.py` | All text shown in the window, in English and Hinglish |
| `markdown_render.py` | Turns Markdown in replies into styled text for the chat bubbles |
| `tts.py` | Offline voice output |
| `stt.py` | 100% offline local speech-to-text voice dictation engine with real-time waveform HUD |
| `exporter.py` | Multi-format conversation exporter (PDF, Markdown with frontmatter, HTML with syntax highlighting, JSON) |
| `personas.py` | Multi-persona system prompt studio, specialized presets, custom persona manager, and prompt injector |
| `vault.py` | Local encrypted chat vault, authenticated encryption envelope, session auto-lock, and automated backup scheduler |
| `history_lock.py` | Stops two windows from using the same chat history |
| `tests/` | Automated tests (`test_core.py` covers core, `test_stt.py` covers voice dictation, `test_exporter.py` covers export, `test_personas.py` covers personas, `test_vault.py` covers encrypted vault) |
| `history_lock.py` | Stops two windows from using the same chat history |
| `tests/` | Automated tests (`test_core.py` covers core, `test_stt.py` covers voice dictation, `test_exporter.py` covers export, `test_personas.py` covers personas) |
| `history_lock.py` | Stops two windows from using the same chat history |
| `tests/` | Automated tests (`test_core.py` covers core, `test_stt.py` covers voice dictation, `test_exporter.py` covers conversation export) |

If something must behave the same in the window and in the terminal, it belongs in `core.py`. Wording and layout stay in the file that shows them.

## ⚙️ Setup Instructions

### 1. Install Python
Ensure Python 3.9+ is installed on your system.

### 2. Install Ollama
Download and install Ollama from [ollama.com](https://ollama.com/).

### 3. Download the AI Model
Open your terminal and run:
```bash
ollama pull llama3.2:3b
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

### 5. Run JUGNU
```bash
python desktop_app.py
```

Prefer the terminal? Run `python assistant.py` instead. It uses the same core.

---

## 🤝 Contributing

We welcome contributions of all levels!

Check out our [Open Issues](https://github.com/kismatkumari56/MyPersonalAssistant/issues) to find something you'd like to work on, and review [CONTRIBUTING.md](CONTRIBUTING.md) for full instructions and our AI policy.

---

## 🧪 Running Tests

JUGNU uses `pytest` for automated testing.

### 1. Install development dependencies

```bash
pip install -r requirements-dev.txt
```

### 2. Run all tests

```bash
python -m pytest -v
```

### 3. Run a specific test file

```bash
python -m pytest tests/test_calculator.py -v
```

```bash
python -m pytest tests/test_time_question.py -v
```

The test suite covers calculator operations, error handling, unsupported expressions, time/date question detection, and the shared core (`core.py`: building messages, talking to Ollama, and reporting errors).

**Note:** Running the tests does not require starting the Ollama server. The window tests run offscreen with `pytest-qt`, so no window opens; the app's logic (`jugnu_core.py`) is tested without any GUI.


## 👩‍💻 Project Admin

**Kismat Kumari** ([@kismatkumari56](https://github.com/kismatkumari56))

Built with curiosity 💜