import tkinter as tk
from tkinter import messagebox, filedialog
from tkinter import font as tkfont
import math
import json
import threading
import time
import uuid
import os
import sys
import tempfile
import ast
import re

from core import (
    ERROR_CONNECTION, ERROR_INVALID, ERROR_TIMEOUT, build_messages, calculate,
    chat as ollama_chat, is_time_question as detect_time_question, local_datetime_text, local_now,
)
from strings import build_system_prompt, get_string, next_language, normalize_language
from tts import TTSManager, DEFAULT_RATE, DEFAULT_VOLUME, RATE_MIN, RATE_MAX, clamp
from stt import (
    STTManager, STTState, DEFAULT_HOTKEY, DEFAULT_SILENCE_TIMEOUT,
    compute_rms, compute_waveform_bars, format_transcription
)
from history_lock import HistoryInUseError, HistoryLock
from markdown_render import render_markdown, plain_text
from exporter import (
    ConversationExporter, extract_chat_metadata, sanitize_filename
)

APP_TITLE = "JUGNU | Just an Unusually Genius Neural Utility"
HISTORY_FILE = "jugnu_history.json"
SETTINGS_FILE = "jugnu_settings.json"
MAX_LIST_TITLE = 34    # longer chat titles are shortened with "…" in the sidebar list
MAX_TITLE_LENGTH = 60  # longest title a user can give a chat
# Markdown styles that indent their lines; bubbles get a few extra columns so those lines do not wrap early.
INDENT_TAGS = ("code", "codehead", "quote") + tuple("li%d" % n for n in range(5))


LIGHT = {
    "bg": "#F7F4FF", "sidebar": "#EEE8FF", "panel": "#FFFFFF",
    "text": "#27213B", "muted": "#756B91", "accent": "#8B5CF6",
    "accent2": "#EC4899", "user": "#E3DCFF", "assistant": "#F0EAFB",
    "entry": "#FFFFFF", "border": "#D9CFF5", "danger": "#FCE7F3",
    "code": "#FFFFFF", "inline_code": "#E2D9F7"
}
DARK = {
    "bg": "#191728", "sidebar": "#211D35", "panel": "#28223D",
    "text": "#F4EEFF", "muted": "#B7A9D8", "accent": "#8B6CF0",
    "accent2": "#D946A6", "user": "#34365A", "assistant": "#2A2440",
    "entry": "#2A2440", "border": "#463A66", "danger": "#4B2A40",
    "code": "#17142A", "inline_code": "#3A3157"
}

def message_time_text():
    return local_now().strftime("%I:%M %p")


class FlatButton(tk.Label):
    """A tk.Label that behaves like a tk.Button.

    On macOS, Tk draws tk.Button with the native Aqua style and ignores bg and
    activebackground, so themed buttons (white text) become unreadable. A Label
    respects every colour option, so it is used as the button there instead.
    It accepts the same options as tk.Button, including command and state.
    """

    def __init__(self, master=None, command=None, **kwargs):
        super().__init__(master, **kwargs)
        self._command = command
        self._pressed = False
        # Class-level bindings live in their own bindtag, so a widget's own
        # bind("<Enter>", ...) (like the Copy button's hover) does not replace them.
        self.bindtags((self.bindtags()[0], "FlatButton") + self.bindtags()[1:])
        if not self.bind_class("FlatButton"):
            self.bind_class("FlatButton", "<Enter>", FlatButton._on_enter)
            self.bind_class("FlatButton", "<Leave>", FlatButton._on_leave)
            self.bind_class("FlatButton", "<ButtonPress-1>", FlatButton._on_press)
            self.bind_class("FlatButton", "<ButtonRelease-1>", FlatButton._on_release)

    def configure(self, cnf=None, **kwargs):
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if cnf is None and not kwargs:
            return super().configure()
        return super().configure(cnf, **kwargs)

    config = configure

    def invoke(self):
        if str(self["state"]) != "disabled" and self._command:
            return self._command()

    @staticmethod
    def _on_enter(event):
        widget = event.widget
        if str(widget["state"]) != "disabled":
            widget.configure(state="active")  # shows activebackground / activeforeground

    @staticmethod
    def _on_leave(event):
        widget = event.widget
        widget._pressed = False
        if str(widget["state"]) != "disabled":
            widget.configure(state="normal")

    @staticmethod
    def _on_press(event):
        event.widget._pressed = str(event.widget["state"]) != "disabled"

    @staticmethod
    def _on_release(event):
        widget = event.widget
        inside = 0 <= event.x < widget.winfo_width() and 0 <= event.y < widget.winfo_height()
        if widget._pressed and inside:
            widget._pressed = False
            widget.invoke()
        widget._pressed = False


# tk.Button keeps the native look elsewhere; on macOS it ignores the theme colours.
Button = FlatButton if sys.platform == "darwin" else tk.Button


class WaveformHUD(tk.Frame):
    """Real-time Voice Waveform and Audio Activity HUD visualizer.

    Displays an animated waveform, live audio level bars, pulsing recording dot,
    elapsed timer, and offline transcription status in the chat input bar.
    """

    def __init__(self, master=None, on_cancel=None, **kwargs):
        super().__init__(master, **kwargs)
        self.on_cancel = on_cancel
        self.canvas = tk.Canvas(self, height=44, highlightthickness=0)
        self.canvas.pack(fill="x", expand=True)

        self.num_bars = 28
        self.levels = [0.0] * self.num_bars
        self.current_rms = 0.0
        self.is_listening = False
        self.is_transcribing = False
        self.start_time = 0.0
        self.pulse_phase = 0.0
        self.colors = LIGHT
        self.status_text = "Listening…"
        self.transcribing_text = "Transcribing speech offline…"
        self.cancel_text = "✕ Cancel"
        self._anim_timer = None
        self._cancel_rect = (0, 0, 0, 0)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", self._on_click)

    def set_theme(self, colors):
        self.colors = colors
        self.configure(bg=colors["bg"])
        self.canvas.configure(bg=colors["panel"])
        self.redraw()

    def set_texts(self, status_text=None, transcribing_text=None, cancel_text=None):
        if status_text:
            self.status_text = status_text
        if transcribing_text:
            self.transcribing_text = transcribing_text
        if cancel_text:
            self.cancel_text = cancel_text
        self.redraw()

    def start_listening(self):
        self.is_listening = True
        self.is_transcribing = False
        self.start_time = time.monotonic()
        self.levels = [0.0] * self.num_bars
        self.current_rms = 0.0
        self._start_animation()

    def start_transcribing(self):
        self.is_listening = False
        self.is_transcribing = True
        self._start_animation()

    def stop(self):
        self.is_listening = False
        self.is_transcribing = False
        if self._anim_timer:
            try:
                self.after_cancel(self._anim_timer)
            except Exception:
                pass
            self._anim_timer = None
        try:
            self.canvas.delete("all")
        except Exception:
            pass

    def update_levels(self, rms: float, bars: list[float]):
        self.current_rms = rms
        if len(bars) == self.num_bars:
            self.levels = bars
        elif bars:
            self.levels = bars[:self.num_bars] + [0.0] * max(0, self.num_bars - len(bars))
        self.redraw()

    def _start_animation(self):
        if not self._anim_timer:
            self._animate()

    def _animate(self):
        self.pulse_phase += 0.15
        self.redraw()
        if self.is_listening or self.is_transcribing:
            try:
                self._anim_timer = self.after(35, self._animate)
            except Exception:
                self._anim_timer = None
        else:
            self._anim_timer = None

    def _on_configure(self, _event):
        self.redraw()

    def _on_click(self, event):
        x1, y1, x2, y2 = self._cancel_rect
        if x1 <= event.x <= x2 and y1 <= event.y <= y2:
            if self.on_cancel:
                self.on_cancel()

    def redraw(self):
        try:
            self.canvas.delete("all")
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
        except Exception:
            return

        if w < 50 or h < 20:
            return

        c = self.colors
        # Draw background container border
        self.canvas.create_rectangle(1, 1, w - 2, h - 2, outline=c["border"], width=1, fill=c["panel"])

        if self.is_listening:
            # Left side: Live pulsing recording dot + Timer
            elapsed = int(time.monotonic() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            timer_str = f"{mins}:{secs:02d}"

            # Cancel button on right
            btn_w = 72
            btn_h = 24
            bx2 = w - 12
            bx1 = bx2 - btn_w
            by1 = (h - btn_h) // 2
            by2 = by1 + btn_h
            self._cancel_rect = (bx1, by1, bx2, by2)
            self.canvas.create_rectangle(bx1, by1, bx2, by2, fill=c["danger"], outline=c["accent2"], width=1)
            self.canvas.create_text((bx1 + bx2) // 2, (by1 + by2) // 2, text=self.cancel_text,
                                    font=("Segoe UI", 8, "bold"), fill=c["accent2"])

            # Pulsing recording dot
            pulse_rad = 5 + int(1.5 * math.sin(self.pulse_phase * 2))
            cx = 18
            cy = h // 2
            self.canvas.create_oval(cx - pulse_rad, cy - pulse_rad, cx + pulse_rad, cy + pulse_rad,
                                    fill=c["accent2"], outline="")
            self.canvas.create_text(30, cy, text=f"{self.status_text} {timer_str}",
                                    anchor="w", font=("Segoe UI", 9, "bold"), fill=c["accent2"])

            # Waveform Equalizer in Center
            left_bound = 150
            right_bound = max(left_bound + 60, bx1 - 20)
            available_w = right_bound - left_bound
            if available_w > 60:
                bar_spacing = available_w / self.num_bars
                bar_w = max(2, min(5, bar_spacing - 2))
                center_y = h // 2
                max_h = (h - 14) // 2

                for i in range(self.num_bars):
                    bx = left_bound + i * bar_spacing + bar_w / 2
                    ambient = 2.0 + 1.5 * math.sin(self.pulse_phase + i * 0.4)
                    voice = self.levels[i] * max_h
                    bh = max(ambient, voice)

                    # Dynamic color based on amplitude
                    ratio = min(1.0, bh / max(max_h, 1))
                    bar_color = c["accent2"] if ratio > 0.4 else c["accent"]

                    self.canvas.create_line(bx, center_y - bh, bx, center_y + bh,
                                            width=bar_w, fill=bar_color, capstyle="round")

        elif self.is_transcribing:
            # Transcribing animation
            self.canvas.create_text(16, h // 2, text=f"⏳ {self.transcribing_text}",
                                    anchor="w", font=("Segoe UI", 9, "bold"), fill=c["accent"])

            # Shimmer animation moving across
            shimmer_range = max(100, w - 240)
            shimmer_x = int((self.pulse_phase * 45) % shimmer_range) + 200
            self.canvas.create_line(shimmer_x, h // 2, min(w - 20, shimmer_x + 60), h // 2,
                                    width=4, fill=c["accent2"], capstyle="round")


class JugnuApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1200x760")
        self.root.minsize(850, 570)

        self.chats = self.load_history()
        self.current_id = None
        self.draft_chat = None  # Unsaved empty chat; saved only after first message.
        self.busy = False
        self.dark_mode = self.load_settings()
        self.colors = DARK if self.dark_mode else LIGHT
        self.voice_rate, self.voice_volume, self.voice_id = self.load_voice_prefs()
        self.tts = TTSManager(rate=self.voice_rate, volume=self.voice_volume,
                              voice_id=self.voice_id)  # Offline voice output; runs on its own thread.
        self.stt_auto_punctuate, self.stt_silence_detection, self.stt_silence_timeout, \
            self.stt_noise_suppression, self.stt_hotkey = self.load_stt_prefs()
        self.stt = STTManager(
            silence_timeout=self.stt_silence_timeout if self.stt_silence_detection else 999.0,
            noise_suppression=self.stt_noise_suppression,
            auto_punctuate=self.stt_auto_punctuate,
            auto_capitalize=True,
        )
        self.hud_shown = False
        self._hotkey_pressed_time = 0.0
        self._mic_btn_press_time = 0.0
        self.voice_win = None  # the Voice Settings window while it is open
        self.vw = {}           # its widgets, so theme and language changes can update them
        self.voice_enabled = self.load_voice_setting()
        self.language = self.load_language_setting()
        self._status = ("status_ready", {})  # Remembered so the status line can be re-translated.
        self.read_buttons = []  # "Read aloud" buttons currently on screen
        self.stop_shown = False  # is the header Stop button visible?
        self.bubble_font = tkfont.Font(family="Segoe UI", size=10)
        self.char_w = max(self.bubble_font.measure("0"), 1)
        self.bubble_items = []
        self._resize_timer = None
        self.canvas_width = None

        self.build_ui()
        self.bind_hotkeys()
        if self.chats:
            self.current_id = self.chats[-1]["id"]  # Reopen most recent saved conversation.
        else:
            self.make_draft()
        self.apply_language()
        self.apply_theme()
        self.root.after(250, self.warm_up)
        self.poll_speech()
        self.poll_stt()

    def load_history(self):
        """Load valid, non-empty conversations from the history file."""
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as history_file:
                data = json.load(history_file)

            if not isinstance(data, list):
                return []

            valid_chats = []
            seen_ids = set()

            for chat in data:
                if not isinstance(chat, dict):
                    continue

                chat_id = chat.get("id")
                messages = chat.get("messages")

                if not isinstance(chat_id, str) or not chat_id.strip():
                    continue
                if chat_id in seen_ids:
                    continue
                if not isinstance(messages, list):
                    continue

                valid_messages = []
                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    role = message.get("role")
                    content = message.get("content")

                    if role not in ("user", "assistant"):
                        continue
                    if not isinstance(content, str) or not content.strip():
                        continue

                    msg_dict = {
                        "role": role,
                        "content": content
                    }
                    time_val = message.get("time")
                    if isinstance(time_val, str) and time_val.strip():
                        msg_dict["time"] = time_val.strip()
                    if role == "assistant" and message.get("is_error") is True:
                        msg_dict["is_error"] = True

                    valid_messages.append(msg_dict)

                # Empty conversations should never appear in saved history.
                if not valid_messages:
                    continue

                chat["messages"] = valid_messages
                title = chat.get("title")
                chat["title"] = title.strip() if isinstance(title, str) and title.strip() else "New Chat"

                valid_chats.append(chat)
                seen_ids.add(chat_id)

            return valid_chats

        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def save_history(self):
        """Save non-empty conversations atomically to avoid partial JSON files."""
        saved_chats = [
            chat for chat in self.chats
            if isinstance(chat, dict)
            and isinstance(chat.get("messages"), list)
            and any(
                isinstance(message, dict)
                and message.get("role") in ("user", "assistant")
                and isinstance(message.get("content"), str)
                and message["content"].strip()
                for message in chat["messages"]
            )
        ]

        history_path = os.path.abspath(HISTORY_FILE)
        history_dir = os.path.dirname(history_path)
        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=history_dir,
                suffix=".tmp",
                delete=False
            ) as temp_file:
                temp_path = temp_file.name
                json.dump(saved_chats, temp_file, ensure_ascii=False, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())

            # The temporary file is in the same directory, so replacement is atomic.
            os.replace(temp_path, history_path)
            temp_path = None

        except (OSError, TypeError, ValueError) as error:
            self.set_status("status_save_error", error=error)

        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                return True

            return bool(data.get("dark_mode", True))

        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return True

    def load_voice_setting(self):
        # Voice output is off by default; the choice is remembered between launches.
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return bool(json.load(f).get("voice_enabled", False))
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            return False

    def load_voice_prefs(self):
        # Speed, volume and voice choice. Anything missing or damaged falls back to the defaults.
        rate, volume, voice_id = DEFAULT_RATE, DEFAULT_VOLUME, None
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_rate = data.get("voice_rate")
            saved_volume = data.get("voice_volume")
            saved_voice = data.get("voice_id")
            if isinstance(saved_rate, (int, float)) and not isinstance(saved_rate, bool):
                rate = int(clamp(saved_rate, RATE_MIN, RATE_MAX))
            if isinstance(saved_volume, (int, float)) and not isinstance(saved_volume, bool):
                volume = float(clamp(saved_volume, 0.0, 1.0))
            if isinstance(saved_voice, str) and saved_voice:
                voice_id = saved_voice
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            pass
        return rate, volume, voice_id

    def load_language_setting(self):
        # English by default; an unknown or damaged value also falls back to English.
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return normalize_language(json.load(f).get("language"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            return normalize_language(None)

    def load_stt_prefs(self):
        auto_punct = True
        silence_detect = True
        silence_timeout = DEFAULT_SILENCE_TIMEOUT
        noise_suppress = True
        hotkey = DEFAULT_HOTKEY
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "stt_auto_punctuate" in data:
                auto_punct = bool(data["stt_auto_punctuate"])
            if "stt_silence_detection" in data:
                silence_detect = bool(data["stt_silence_detection"])
            if "stt_silence_timeout" in data and isinstance(data["stt_silence_timeout"], (int, float)):
                silence_timeout = max(0.5, min(5.0, float(data["stt_silence_timeout"])))
            if "stt_noise_suppression" in data:
                noise_suppress = bool(data["stt_noise_suppression"])
            if "stt_hotkey" in data and isinstance(data["stt_hotkey"], str) and data["stt_hotkey"].strip():
                hotkey = data["stt_hotkey"].strip()
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            pass
        return auto_punct, silence_detect, silence_timeout, noise_suppress, hotkey

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "dark_mode": self.dark_mode,
                    "voice_enabled": self.voice_enabled,
                    "language": self.language,
                    "voice_rate": self.voice_rate,
                    "voice_volume": self.voice_volume,
                    "voice_id": self.voice_id,
                    "stt_auto_punctuate": getattr(self, "stt_auto_punctuate", True),
                    "stt_silence_detection": getattr(self, "stt_silence_detection", True),
                    "stt_silence_timeout": getattr(self, "stt_silence_timeout", DEFAULT_SILENCE_TIMEOUT),
                    "stt_noise_suppression": getattr(self, "stt_noise_suppression", True),
                    "stt_hotkey": getattr(self, "stt_hotkey", DEFAULT_HOTKEY),
                }, f)
        except OSError:
            pass

    def build_ui(self):
        # Visible text is filled in by apply_language(), so it can be switched at runtime.
        self.main = tk.Frame(self.root)
        self.main.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(self.main, width=230)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.brand = tk.Label(self.sidebar, text="🤖  JUGNU", font=("Segoe UI", 22, "bold"),
                              anchor="w", padx=18, pady=18)
        self.brand.pack(fill="x")
        self.tagline = tk.Label(self.sidebar, text="Just an Unusually Genius\nNeural Utility",
                                font=("Segoe UI", 9), justify="left", anchor="w", padx=20)
        self.tagline.pack(fill="x", pady=(0, 18))

        self.new_btn = Button(self.sidebar, command=self.new_chat,
                                 font=("Segoe UI", 10, "bold"), relief="flat", pady=12)
        self.new_btn.pack(fill="x", padx=12, pady=(0, 18))

        self.history_label = tk.Label(self.sidebar, font=("Segoe UI", 9, "bold"),
                                      anchor="w", padx=18)
        self.history_label.pack(fill="x")

        self.history_list = tk.Listbox(self.sidebar, relief="flat", borderwidth=0,
                                       font=("Segoe UI", 9), activestyle="none")
        self.history_list.pack(fill="both", expand=True, padx=8, pady=8)
        self.history_list.bind("<<ListboxSelect>>", self.select_chat)
        self.history_list.bind("<Button-3>", self.show_chat_menu)  # right-click (Windows / Linux)
        self.history_list.bind("<Button-2>", self.show_chat_menu)  # right-click (macOS)

        self.clear_btn = Button(self.sidebar, command=self.clear_chat,
                                   relief="flat", pady=10)
        self.clear_btn.pack(fill="x", padx=12, pady=(4, 8))
        self.voice_btn = Button(self.sidebar, command=self.toggle_voice,
                                   relief="flat", pady=10)
        self.voice_btn.pack(fill="x", padx=12, pady=(0, 8))
        if not self.tts.available:
            self.voice_btn.configure(state="disabled")  # pyttsx3 not installed
        self.voice_settings_btn = Button(self.sidebar, command=self.open_voice_settings,
                                            relief="flat", pady=10)
        self.voice_settings_btn.pack(fill="x", padx=12, pady=(0, 8))
        if not (self.tts.available or self.stt.has_mic or self.stt.has_recognizer):
            self.voice_settings_btn.configure(state="disabled")
        self.language_btn = Button(self.sidebar, command=self.toggle_language,
                                      relief="flat", pady=10)
        self.language_btn.pack(fill="x", padx=12, pady=(0, 8))
        self.theme_btn = Button(self.sidebar, command=self.toggle_theme,
                                   relief="flat", pady=10)
        self.theme_btn.pack(fill="x", padx=12, pady=(0, 12))

        self.content = tk.Frame(self.main)
        self.content.pack(side="left", fill="both", expand=True)

        self.header = tk.Frame(self.content, height=64)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.header_title = tk.Label(self.header, font=("Segoe UI", 15, "bold"),
                                     anchor="w", padx=22)
        self.header_title.pack(side="left", fill="y")
        self.status = tk.Label(self.header, font=("Segoe UI", 9, "bold"),
                               anchor="e", padx=18)
        self.status.pack(side="right", fill="y")
        self.export_btn = Button(self.header, command=self.open_export_dialog,
                                 relief="flat", font=("Segoe UI", 9, "bold"), padx=12, pady=4)
        self.export_btn.pack(side="right", padx=(0, 8), pady=14)
        # Shown only while speech is playing (see poll_speech); packed after status, so it sits left of it.
        self.stop_btn = Button(self.header, text="⏹  Stop", command=self.stop_speaking,
                                  relief="flat", font=("Segoe UI", 9, "bold"), padx=12, pady=4)

        self.chat_area = tk.Frame(self.content)
        self.chat_area.pack(fill="both", expand=True, padx=12, pady=12)
        self.canvas = tk.Canvas(self.chat_area, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self.chat_area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.messages_frame = tk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.messages_frame, anchor="nw")
        self.messages_frame.bind("<Configure>", self.on_frame_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind_all("<Button-4>", self.on_mousewheel)
        self.canvas.bind_all("<Button-5>", self.on_mousewheel)

        self.bottom = tk.Frame(self.content)
        self.bottom.pack(fill="x", padx=12, pady=(0, 12))

        self.hud_container = tk.Frame(self.bottom)
        self.waveform_hud = WaveformHUD(self.hud_container, on_cancel=self.cancel_dictation)
        self.waveform_hud.pack(fill="x", expand=True)

        self.input_row = tk.Frame(self.bottom)
        self.input_row.pack(fill="x", expand=True)

        self.input_box = tk.Text(self.input_row, height=2, wrap="word", font=("Segoe UI", 11),
                                 relief="flat", padx=10, pady=10, undo=True)
        self.input_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.input_box.bind("<Control-Return>", self.send_message)
        self.input_box.bind("<Return>", self.send_message)
        self.input_box.bind("<Shift-Return>", lambda event: None)

        self.mic_btn = Button(self.input_row, command=self.toggle_dictation,
                              relief="flat", font=("Segoe UI", 10, "bold"), padx=14, pady=12)
        self.mic_btn.pack(side="right", padx=(0, 8))
        self.mic_btn.bind("<ButtonPress-1>", self.on_mic_btn_press)
        self.mic_btn.bind("<ButtonRelease-1>", self.on_mic_btn_release)

        self.send_btn = Button(self.input_row, command=self.send_message,
                               relief="flat", font=("Segoe UI", 10, "bold"), padx=18, pady=12)
        self.send_btn.pack(side="right")

    def on_frame_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        if event.width == self.canvas_width:
            return
        self.canvas_width = event.width
        if self._resize_timer:
            try:
                self.root.after_cancel(self._resize_timer)
            except Exception:
                pass
        self._resize_timer = self.root.after(100, self.recalculate_bubbles)

    def current_canvas_width(self, fallback=580):
        """Return the active chat canvas width in pixels, or `fallback` before layout."""
        width = getattr(self, "canvas_width", None)
        if not width or width < 100:
            measured = self.canvas.winfo_width()
            width = measured if measured >= 100 else fallback
        return width

    def max_bubble_width_px(self, canvas_w):
        """Compute maximum bubble text width from `canvas_w`, keeping bubbles inside viewport."""
        # Reserve space for row padding (16), bubble side offset (40), text padding (24), and gutter.
        available_px = max(40, canvas_w - 100)
        return min(720, available_px, max(40, int(canvas_w * 0.72)))

    def compute_bubble_cols(self, text, canvas_w):
        """Compute tk.Text column width for `text` that wraps cleanly within `canvas_w`."""
        max_bubble_px = self.max_bubble_width_px(canvas_w)
        max_cols = max(1, max_bubble_px // self.char_w)
        min_cols = min(18, max_cols)
        lines = text.splitlines() or [""]
        max_line_px = max(self.bubble_font.measure(line.expandtabs(8)) for line in lines)
        needed_cols = max(1, math.ceil(max_line_px / self.char_w))
        return max(min_cols, min(max_cols, needed_cols))

    @staticmethod
    def fit_bubble_height(txt, text):
        """Return exact display-line height for a wrapped tk.Text bubble."""
        dlines = txt.count("1.0", "end", "displaylines")
        count = dlines[0] if isinstance(dlines, (tuple, list)) else (dlines or 0)
        if count and text.endswith("\n"):
            count -= 1
        count = max(1, count or 1)
        # Markdown headings, code blocks and Copy buttons are taller than a normal line,
        # so a count of lines is not enough: also ask Tk for the real height in pixels.
        try:
            pixels = txt.count("1.0", "end", "ypixels")
            pixels = pixels[0] if isinstance(pixels, (tuple, list)) else (pixels or 0)
            line = tkfont.nametofont(txt.cget("font"), root=txt).metrics("linespace")
            lines_by_pixels = math.ceil(pixels / line) if line else 0
            if text.endswith("\n"):
                lines_by_pixels -= 1
            count = max(count, lines_by_pixels)
        except (tk.TclError, TypeError, ValueError):
            pass
        return count

    def bubble_cols(self, txt, measure_text, canvas_w):
        """Column width for a bubble: compute_bubble_cols plus room for indented Markdown lines."""
        cols = self.compute_bubble_cols(measure_text, canvas_w)
        try:
            indented = any(txt.tag_ranges(name) for name in INDENT_TAGS)
        except tk.TclError:
            indented = False
        if indented:
            max_cols = max(1, self.max_bubble_width_px(canvas_w) // self.char_w)
            cols = min(max_cols, cols + 3)
        return cols

    def recalculate_bubbles(self):
        self._resize_timer = None
        canvas_w = self.current_canvas_width(fallback=0)
        if canvas_w < 100 or not self.bubble_items:
            return

        # Phase 1: Update widths so Tk wraps lines to the new dimension
        for _bubble, txt, orig_text in self.bubble_items:
            try:
                cols = self.bubble_cols(txt, orig_text, canvas_w)
                txt.configure(state="normal", width=cols)
            except tk.TclError:
                pass

        # Update geometry so displaylines accurately reflects the new width
        self.root.update_idletasks()

        # Phase 2: Set exact height matching display line count
        for _bubble, txt, orig_text in self.bubble_items:
            try:
                h = self.fit_bubble_height(txt, orig_text)
                txt.configure(height=h, state="disabled")
            except tk.TclError:
                pass

        self.root.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_mousewheel(self, event):
        try:
            target = self.canvas
            if event.widget == self.history_list:
                target = self.history_list
            elif event.widget == self.input_box:
                target = self.input_box

            if event.num == 4:
                target.yview_scroll(-1, "units")
            elif event.num == 5:
                target.yview_scroll(1, "units")
            elif event.delta:
                delta = event.delta
                if abs(delta) >= 120:
                    target.yview_scroll(int(-1 * (delta / 120)), "units")
                else:
                    target.yview_scroll(int(-1 * delta), "units")
            return "break"
        except tk.TclError:
            pass

    def apply_theme(self):
        c = self.colors
        widgets = [
            (self.root, c["bg"]), (self.main, c["bg"]), (self.sidebar, c["sidebar"]),
            (self.content, c["bg"]), (self.header, c["accent"]), (self.chat_area, c["bg"]),
            (self.canvas, c["bg"]), (self.messages_frame, c["bg"]), (self.bottom, c["bg"]),
            (self.input_row, c["bg"]), (self.hud_container, c["bg"])
        ]
        for widget, color in widgets:
            widget.configure(bg=color)
        for widget in [self.brand, self.tagline, self.history_label]:
            widget.configure(bg=c["sidebar"], fg=c["text"])
        self.brand.configure(fg=c["accent"])
        self.tagline.configure(fg=c["muted"])
        self.history_label.configure(fg=c["muted"])
        self.header_title.configure(bg=c["accent"], fg="white")
        self.status.configure(bg=c["accent"], fg="#D1FAE5")
        self.stop_btn.configure(bg=c["accent2"], fg="white", activebackground=c["danger"],
                                activeforeground=c["accent2"], cursor="hand2")
        self.export_btn.configure(bg=c["accent2"], fg="white", activebackground=c["accent"],
                                  activeforeground="white", cursor="hand2")
        self.history_list.configure(bg=c["sidebar"], fg=c["text"],
                                    selectbackground=c["accent"], selectforeground="white")
        for btn in [self.new_btn, self.theme_btn, self.voice_btn, self.language_btn,
                    self.voice_settings_btn]:
            btn.configure(bg=c["accent"], fg="white", activebackground=c["accent2"],
                          activeforeground="white", cursor="hand2")
        self.voice_btn.configure(text=self.voice_button_text())
        self.clear_btn.configure(bg=c["danger"], fg=c["accent2"], activebackground=c["danger"],
                                 activeforeground=c["text"], cursor="hand2")
        self.input_box.configure(bg=c["entry"], fg=c["text"], insertbackground=c["text"])
        mic_bg = c["accent2"] if self.stt.is_listening else c["accent"]
        self.mic_btn.configure(bg=mic_bg, fg="white", activebackground=c["accent2"],
                               activeforeground="white", cursor="hand2")
        self.waveform_hud.set_theme(c)
        self.send_btn.configure(bg=c["accent"], fg="white", activebackground=c["accent2"],
                                activeforeground="white", cursor="hand2")
        self.theme_btn.configure(text=self.theme_button_text())
        self.update_voice_window()
        self.refresh_history_list()
        self.display_chat()

    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.colors = DARK if self.dark_mode else LIGHT
        self.save_settings()
        self.apply_theme()

    def toggle_voice(self):
        self.voice_enabled = not self.voice_enabled
        if not self.voice_enabled:
            self.tts.stop()  # Muting silences the current reply immediately.
        self.voice_btn.configure(text=self.voice_button_text())
        self.save_settings()

    def stop_speaking(self):
        self.tts.stop()
        self.poll_speech(reschedule=False)  # hide the Stop button right away

    def read_aloud(self, text):
        # Reads exactly this message. Works even when the sidebar Voice toggle is off,
        # because the user asked for it explicitly. Replaces anything already being read.
        if not self.tts.available:
            return
        self.tts.stop()
        self.tts.speak(plain_text(text))  # without ** # ` and other Markdown symbols
        self.poll_speech(reschedule=False)

    def poll_speech(self, reschedule=True):
        # Keep the Stop button and the Read aloud buttons in step with the speech engine.
        speaking = self.tts.speaking
        if speaking and not self.stop_shown:
            self.stop_btn.pack(side="right", padx=(0, 4))
            self.stop_shown = True
        elif not speaking and self.stop_shown:
            self.stop_btn.pack_forget()
            self.stop_shown = False
        state = "normal" if (self.tts.available or self.stt.has_mic or self.stt.has_recognizer) else "disabled"
        self.voice_settings_btn.configure(state=state)
        for btn in self.read_buttons:
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass  # its bubble was already redrawn
        if reschedule:
            self.root.after(200, self.poll_speech)

    # ---------- Voice Settings window ----------
    def voice_labels(self):
        """Dropdown labels: the default entry first, then every installed voice (names made unique)."""
        labels, ids, seen = [self.t("voice_default_name")], [None], {}
        for vid, name in self.tts.voices:
            seen[name] = seen.get(name, 0) + 1
            labels.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
            ids.append(vid)
        return labels, ids

    def open_voice_settings(self):
        if not (self.tts.available or self.stt.has_mic or self.stt.has_recognizer):
            return
        if self.voice_win is not None:
            self.voice_win.lift()
            self.voice_win.focus_set()
            return
        win = tk.Toplevel(self.root)
        win.resizable(False, False)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self.close_voice_settings)
        self.voice_win = win
        labels, ids = self.voice_labels()
        # A saved voice that is no longer installed shows (and plays) as the default.
        index = ids.index(self.voice_id) if self.voice_id in ids else 0
        self.voice_choice = tk.StringVar(value=labels[index])
        w = self.vw = {"win": win}
        if self.tts.available:
            w["rate_label"] = tk.Label(win, font=("Segoe UI", 10, "bold"), anchor="w")
            w["rate"] = tk.Scale(win, from_=RATE_MIN, to=RATE_MAX, resolution=5, orient="horizontal",
                                 showvalue=False, length=300, highlightthickness=0, bd=0)
            w["volume_label"] = tk.Label(win, font=("Segoe UI", 10, "bold"), anchor="w")
            w["volume"] = tk.Scale(win, from_=0, to=100, resolution=5, orient="horizontal",
                                   showvalue=False, length=300, highlightthickness=0, bd=0)
            w["voice_label"] = tk.Label(win, font=("Segoe UI", 10, "bold"), anchor="w")
            w["voice"] = tk.OptionMenu(win, self.voice_choice, *labels, command=self.on_voice_pick)
            w["test"] = Button(win, command=self.test_voice, relief="flat",
                                  font=("Segoe UI", 10, "bold"), pady=8)
            # Set the starting positions first, then attach the callbacks, so opening the
            # window never rewrites the saved values.
            w["rate"].set(self.voice_rate)
            w["volume"].set(round(self.voice_volume * 100))
            w["rate"].configure(command=self.on_rate_change)
            w["volume"].configure(command=self.on_volume_change)
            for slider in (w["rate"], w["volume"]):
                slider.bind("<ButtonRelease-1>", lambda _e: self.save_settings())
                slider.bind("<KeyRelease>", lambda _e: self.save_settings())
            for key in ("rate_label", "rate", "volume_label", "volume", "voice_label", "voice"):
                w[key].pack(fill="x", padx=20, pady=(14, 0) if key.endswith("label") else (2, 0))
            w["test"].pack(fill="x", padx=20, pady=(18, 6))

        # STT Dictation Section
        w["stt_section_label"] = tk.Label(win, font=("Segoe UI", 10, "bold"), anchor="w")
        w["stt_punct_var"] = tk.BooleanVar(value=self.stt_auto_punctuate)
        w["stt_punct"] = tk.Checkbutton(win, variable=w["stt_punct_var"],
                                        command=self.on_stt_setting_change, anchor="w",
                                        relief="flat", highlightthickness=0)
        w["stt_silence_var"] = tk.BooleanVar(value=self.stt_silence_detection)
        w["stt_silence"] = tk.Checkbutton(win, variable=w["stt_silence_var"],
                                          command=self.on_stt_setting_change, anchor="w",
                                          relief="flat", highlightthickness=0)
        w["stt_noise_var"] = tk.BooleanVar(value=self.stt_noise_suppression)
        w["stt_noise"] = tk.Checkbutton(win, variable=w["stt_noise_var"],
                                        command=self.on_stt_setting_change, anchor="w",
                                        relief="flat", highlightthickness=0)
        w["stt_hotkey_label"] = tk.Label(win, font=("Segoe UI", 9), anchor="w")

        w["stt_section_label"].pack(fill="x", padx=20, pady=(14, 4))
        w["stt_punct"].pack(fill="x", padx=20, pady=2)
        w["stt_silence"].pack(fill="x", padx=20, pady=2)
        w["stt_noise"].pack(fill="x", padx=20, pady=2)
        w["stt_hotkey_label"].pack(fill="x", padx=20, pady=(4, 12))

        w["close"] = Button(win, command=self.close_voice_settings, relief="flat", pady=8)
        w["close"].pack(fill="x", padx=20, pady=(0, 18))
        self.update_voice_window()

    def update_voice_window(self):
        # Re-apply text (language) and colours (theme) to the window if it is open.
        if self.voice_win is None:
            return
        w, c = self.vw, self.colors
        w["win"].title(self.t("voice_settings_title"))
        w["win"].configure(bg=c["bg"])
        if self.tts.available and "rate_label" in w:
            self.update_voice_value_labels()
            w["voice_label"].configure(text=self.t("voice_choice_label"))
            w["test"].configure(text=self.t("voice_test_btn"))
            for key in ("rate_label", "volume_label", "voice_label"):
                w[key].configure(bg=c["bg"], fg=c["text"])
            for key in ("rate", "volume"):
                w[key].configure(bg=c["bg"], fg=c["text"], troughcolor=c["border"],
                                 activebackground=c["accent2"])
            w["voice"].configure(bg=c["panel"], fg=c["text"], activebackground=c["accent"],
                                 activeforeground="white", highlightthickness=0, relief="flat")
            w["voice"]["menu"].configure(bg=c["panel"], fg=c["text"], activebackground=c["accent"],
                                         activeforeground="white")
            w["test"].configure(bg=c["accent"], fg="white", activebackground=c["accent2"],
                                activeforeground="white", cursor="hand2")
            labels, ids = self.voice_labels()
            menu = w["voice"]["menu"]
            menu.delete(0, "end")
            for label in labels:
                menu.add_command(label=label, command=lambda lb=label: self.on_voice_pick(lb))
            index = ids.index(self.voice_id) if self.voice_id in ids else 0
            self.voice_choice.set(labels[index])

        if "stt_section_label" in w:
            w["stt_section_label"].configure(text=self.t("stt_settings_section"), bg=c["bg"], fg=c["text"])
            w["stt_punct"].configure(text=self.t("stt_auto_punctuate_label"), bg=c["bg"], fg=c["text"],
                                     activebackground=c["bg"], activeforeground=c["text"],
                                     selectcolor=c["panel"])
            w["stt_silence"].configure(text=self.t("stt_silence_detect_label"), bg=c["bg"], fg=c["text"],
                                       activebackground=c["bg"], activeforeground=c["text"],
                                       selectcolor=c["panel"])
            w["stt_noise"].configure(text=self.t("stt_noise_suppress_label"), bg=c["bg"], fg=c["text"],
                                     activebackground=c["bg"], activeforeground=c["text"],
                                     selectcolor=c["panel"])
            w["stt_hotkey_label"].configure(text=self.t("stt_hotkey_label", hotkey="Ctrl+Space"),
                                            bg=c["bg"], fg=c["muted"])

        w["close"].configure(text=self.t("voice_close_btn"))
        w["close"].configure(bg=c["danger"], fg=c["accent2"], activebackground=c["danger"],
                             activeforeground=c["text"], cursor="hand2")

    def on_stt_setting_change(self):
        w = self.vw
        if not w:
            return
        self.stt_auto_punctuate = bool(w["stt_punct_var"].get())
        self.stt_silence_detection = bool(w["stt_silence_var"].get())
        self.stt_noise_suppression = bool(w["stt_noise_var"].get())
        self.stt.apply_settings(
            silence_timeout=self.stt_silence_timeout if self.stt_silence_detection else 999.0,
            noise_suppression=self.stt_noise_suppression,
            auto_punctuate=self.stt_auto_punctuate,
        )
        self.save_settings()

    def update_voice_value_labels(self):
        w = self.vw
        w["rate_label"].configure(text=self.t("voice_rate_label", value=self.voice_rate))
        w["volume_label"].configure(
            text=self.t("voice_volume_label", value=round(self.voice_volume * 100)))

    def push_voice_settings(self):
        # Hand the current values to the speech thread. Cheap, so it is safe on every slider tick.
        self.tts.apply_settings(self.voice_rate, self.voice_volume, self.voice_id)

    def on_rate_change(self, value):
        self.voice_rate = int(float(value))
        self.push_voice_settings()
        self.update_voice_value_labels()

    def on_volume_change(self, value):
        self.voice_volume = int(float(value)) / 100
        self.push_voice_settings()
        self.update_voice_value_labels()

    def on_voice_pick(self, label):
        labels, ids = self.voice_labels()
        if label in labels:
            self.voice_id = ids[labels.index(label)]
            self.voice_choice.set(label)
            self.push_voice_settings()
            self.save_settings()

    def test_voice(self):
        # Plays a short sample with the current settings, even if the Voice toggle is off.
        if not self.tts.available:
            return
        self.tts.stop()
        self.tts.speak(self.t("voice_test_sample"))
        self.poll_speech(reschedule=False)

    def close_voice_settings(self):
        self.save_settings()
        if self.voice_win is not None:
            self.voice_win.destroy()
        self.voice_win = None
        self.vw = {}

    def on_close(self):
        if self._resize_timer:
            try:
                self.root.after_cancel(self._resize_timer)
            except Exception:
                pass
        self.stt.shutdown()
        self.tts.shutdown()
        self.root.destroy()

    # ---------- Voice Dictation (Offline STT) ----------
    def bind_hotkeys(self):
        self.root.bind("<Control-space>", self.on_hotkey_press)
        self.root.bind("<KeyRelease-space>", self.on_hotkey_release)
        self.root.bind("<Control-Shift-V>", lambda _e: self.toggle_dictation())
        self.root.bind("<Control-Shift-v>", lambda _e: self.toggle_dictation())
        self.root.bind("<Escape>", lambda _e: self.cancel_dictation())
        self.root.bind("<Control-e>", lambda _e: self.open_export_dialog())
        self.root.bind("<Control-E>", lambda _e: self.open_export_dialog())
        self.root.bind("<Control-Shift-e>", lambda _e: self.open_export_dialog())
        self.root.bind("<Control-Shift-E>", lambda _e: self.open_export_dialog())

    def mic_button_text(self):
        if self.stt.is_listening:
            return self.t("mic_btn_listening")
        if self.stt.is_transcribing:
            return self.t("mic_btn_transcribing")
        return self.t("mic_btn")

    def toggle_dictation(self):
        if not self.stt.has_mic:
            self.show_no_mic_dialog()
            return
        if not self.stt.has_recognizer:
            self.show_no_model_dialog()
            return
        if self.stt.is_listening:
            self.stop_dictation()
        else:
            self.start_dictation()

    def start_dictation(self):
        if not self.stt.has_mic:
            self.show_no_mic_dialog()
            return
        if not self.stt.has_recognizer:
            self.show_no_model_dialog()
            return
        if self.tts.speaking:
            self.tts.stop()
        self.stt.start_listening()

    def stop_dictation(self):
        self.stt.stop_listening()

    def cancel_dictation(self):
        self.stt.cancel_listening()

    def on_hotkey_press(self, _event=None):
        if not self.stt.has_mic:
            self.show_no_mic_dialog()
            return "break"
        if not self.stt.has_recognizer:
            self.show_no_model_dialog()
            return "break"
        if not self.stt.is_listening:
            self._hotkey_pressed_time = time.monotonic()
            self.start_dictation()
        return "break"

    def on_hotkey_release(self, _event=None):
        if self.stt.is_listening and self._hotkey_pressed_time > 0:
            held = time.monotonic() - self._hotkey_pressed_time
            self._hotkey_pressed_time = 0.0
            if held >= 0.4:  # Push-to-talk release
                self.stop_dictation()
        return "break"

    def on_mic_btn_press(self, _event=None):
        self._mic_btn_press_time = time.monotonic()

    def on_mic_btn_release(self, _event=None):
        if self._mic_btn_press_time > 0:
            held = time.monotonic() - self._mic_btn_press_time
            self._mic_btn_press_time = 0.0
            if held >= 0.4 and self.stt.is_listening:
                self.stop_dictation()

    def show_no_mic_dialog(self):
        messagebox.showinfo(
            self.t("mic_err_no_mic_title"),
            self.t("mic_err_no_mic_msg"),
            parent=self.root
        )

    def show_no_model_dialog(self):
        messagebox.showinfo(
            self.t("mic_err_no_model_title"),
            self.t("mic_err_no_model_msg"),
            parent=self.root
        )

    def poll_stt(self):
        while not self.stt.event_queue.empty():
            try:
                event_type, data = self.stt.event_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "state":
                if data == STTState.LISTENING:
                    if not self.hud_shown:
                        self.hud_container.pack(fill="x", pady=(0, 8), before=self.input_row)
                        self.hud_shown = True
                    self.waveform_hud.start_listening()
                    self.mic_btn.configure(text=self.t("mic_btn_listening"),
                                           bg=self.colors["accent2"], fg="white")
                elif data == STTState.TRANSCRIBING:
                    if not self.hud_shown:
                        self.hud_container.pack(fill="x", pady=(0, 8), before=self.input_row)
                        self.hud_shown = True
                    self.waveform_hud.start_transcribing()
                    self.mic_btn.configure(text=self.t("mic_btn_transcribing"),
                                           bg=self.colors["accent2"], fg="white")
                elif data in (STTState.IDLE, STTState.ERROR):
                    if self.hud_shown:
                        self.hud_container.pack_forget()
                        self.hud_shown = False
                    self.waveform_hud.stop()
                    self.mic_btn.configure(text=self.mic_button_text(),
                                           bg=self.colors["accent"], fg="white")
            elif event_type == "level":
                rms, bars = data
                self.waveform_hud.update_levels(rms, bars)
            elif event_type == "result":
                text = data
                if text:
                    current = self.input_box.get("1.0", "end-1c")
                    prefix = " " if current and not current.endswith((" ", "\n")) else ""
                    self.input_box.insert(tk.INSERT, prefix + text)
                    self.input_box.see(tk.INSERT)
                    self.input_box.focus_set()

        try:
            self.root.after(35, self.poll_stt)
        except Exception:
            pass

    def t(self, key, **kwargs):
        """Look up a user-facing string in the currently selected language."""
        return get_string(self.language, key, **kwargs)

    def voice_button_text(self):
        return self.t("voice_on" if self.voice_enabled else "voice_off")

    def theme_button_text(self):
        return self.t("theme_to_light" if self.dark_mode else "theme_to_dark")

    def toggle_language(self):
        self.language = next_language(self.language)
        self.save_settings()
        self.apply_language()
        self.display_chat()  # Redraw so the welcome screen and "You" labels switch too.

    def apply_language(self):
        # Set every piece of visible text from the strings dictionary.
        self.new_btn.configure(text=self.t("new_chat_btn"))
        self.history_label.configure(text=self.t("history_label"))
        self.clear_btn.configure(text=self.t("clear_chat_btn"))
        self.voice_btn.configure(text=self.voice_button_text())
        self.voice_settings_btn.configure(text=self.t("voice_settings_btn"))
        self.update_voice_window()
        self.language_btn.configure(text=self.t("language_btn", name=self.t("language_name")))
        self.theme_btn.configure(text=self.theme_button_text())
        self.header_title.configure(text=self.t("header_title"))
        self.export_btn.configure(text=self.t("export_btn"))
        self.send_btn.configure(text=self.t("send_btn"))
        self.mic_btn.configure(text=self.mic_button_text())
        self.waveform_hud.set_texts(
            status_text=self.t("mic_hud_listening"),
            transcribing_text=self.t("mic_hud_transcribing"),
            cancel_text=self.t("mic_cancel_btn")
        )
        key, kwargs = self._status
        self.set_status(key, **kwargs)

    def refresh_history_list(self):
        selected = self.current_id
        self.history_list.delete(0, "end")
        for chat in reversed(self.chats):
            title = chat.get("title", "New Chat")
            if len(title) > MAX_LIST_TITLE:
                title = title[:MAX_LIST_TITLE - 1] + "…"
            self.history_list.insert("end", "  ◌  " + title)
        if selected:
            for idx, chat in enumerate(reversed(self.chats)):
                if chat.get("id") == selected:
                    self.history_list.selection_set(idx)
                    break

    def make_draft(self):
        self.draft_chat = {"id": str(uuid.uuid4()), "title": "New Chat", "messages": []}
        self.current_id = self.draft_chat["id"]

    def new_chat(self):
        # A blank chat is temporary. It must not create a history entry until a message is sent.
        self.tts.stop()
        self.make_draft()
        self.refresh_history_list()
        self.display_chat()
        self.input_box.focus_set()

    def current_chat(self):
        if self.draft_chat and self.draft_chat.get("id") == self.current_id:
            return self.draft_chat
        for chat in self.chats:
            if chat.get("id") == self.current_id:
                return chat
        return None

    def select_chat(self, _event=None):
        selection = self.history_list.curselection()
        if not selection:
            return
        index = len(self.chats) - 1 - selection[0]
        if 0 <= index < len(self.chats):
            self.tts.stop()
            self.current_id = self.chats[index]["id"]
            self.draft_chat = None
            self.display_chat()

    # ---------- Rename / Delete a single chat (right-click on the history list) ----------
    def find_chat(self, chat_id):
        return next((c for c in self.chats if c.get("id") == chat_id), None)

    def chat_at_event(self, event):
        """The saved chat under the mouse, or None when the click missed every row."""
        if not self.chats:
            return None
        row = self.history_list.nearest(event.y)
        box = self.history_list.bbox(row)
        # nearest() also answers for empty space below the last row, so check the click really hit it.
        if not box or not (box[1] <= event.y <= box[1] + box[3]):
            return None
        position = len(self.chats) - 1 - row  # the list shows newest first
        return self.chats[position] if 0 <= position < len(self.chats) else None

    def show_chat_menu(self, event):
        chat = self.chat_at_event(event)
        if chat is None:
            return
        c = self.colors
        menu = tk.Menu(self.root, tearoff=0, bg=c["panel"], fg=c["text"],
                       activebackground=c["accent"], activeforeground="white")
        # The menu remembers the chat's id (not its row), so it acts on the chat that was clicked.
        menu.add_command(label=self.t("chat_menu_export"),
                         command=lambda cid=chat["id"]: self.open_export_dialog(cid))
        menu.add_separator()
        menu.add_command(label=self.t("chat_menu_rename"),
                         command=lambda cid=chat["id"]: self.rename_chat(cid))
        menu.add_command(label=self.t("chat_menu_delete"),
                         command=lambda cid=chat["id"]: self.delete_chat(cid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def rename_chat(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat is None:
            return
        new_title = self.ask_chat_title(chat.get("title", ""))
        if new_title is None:
            return  # cancelled
        new_title = " ".join(new_title.split())[:MAX_TITLE_LENGTH]  # tidy spaces, cap the length
        if not new_title or new_title == chat.get("title"):
            return  # an empty name is ignored: the old title stays
        chat["title"] = new_title
        chat["is_custom_title"] = True
        self.save_history()
        self.refresh_history_list()

    def delete_chat(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat is None:
            return
        if not messagebox.askyesno(self.t("delete_chat_title"),
                                   self.t("delete_chat_message", title=chat.get("title", ""))):
            return
        was_open = chat_id == self.current_id
        self.chats = [saved for saved in self.chats if saved.get("id") != chat_id]  # only this chat goes
        if was_open:
            self.tts.stop()
            self.draft_chat = None
            self.make_draft()  # deleting the open chat leaves a fresh empty one
        self.save_history()
        self.refresh_history_list()
        if was_open:
            self.display_chat()

    def ask_chat_title(self, initial):
        """Small themed dialog. Returns the text entered, or None if it was cancelled."""
        c = self.colors
        win = tk.Toplevel(self.root)
        win.title(self.t("rename_dialog_title"))
        win.configure(bg=c["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        result = {"value": None}

        tk.Label(win, text=self.t("rename_dialog_prompt"), bg=c["bg"], fg=c["text"],
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=20, pady=(16, 4))
        entry = tk.Entry(win, width=40, font=("Segoe UI", 11), relief="flat", bg=c["entry"],
                         fg=c["text"], insertbackground=c["text"])
        entry.insert(0, initial)
        entry.pack(fill="x", padx=20, pady=(0, 12), ipady=6)

        def accept(_event=None):
            result["value"] = entry.get()
            win.destroy()

        def cancel(_event=None):
            win.destroy()

        buttons = tk.Frame(win, bg=c["bg"])
        buttons.pack(fill="x", padx=20, pady=(0, 16))
        Button(buttons, text=self.t("rename_save_btn"), command=accept, relief="flat",
                  bg=c["accent"], fg="white", activebackground=c["accent2"],
                  activeforeground="white", cursor="hand2", padx=16, pady=6).pack(side="right")
        Button(buttons, text=self.t("rename_cancel_btn"), command=cancel, relief="flat",
                  bg=c["danger"], fg=c["accent2"], activebackground=c["danger"],
                  activeforeground=c["text"], cursor="hand2", padx=16, pady=6).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", accept)
        win.bind("<Escape>", cancel)
        win.protocol("WM_DELETE_WINDOW", cancel)

        # Open next to the main window and keep focus here until it is closed.
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            win.wait_visibility()
            win.grab_set()
        except tk.TclError:
            pass
        entry.focus_set()
        entry.select_range(0, "end")
        entry.icursor("end")
        self.root.wait_window(win)
        return result["value"]

    # ---------- Multi-Format Conversation Exporter ----------
    def open_export_dialog(self, chat_id=None):
        if chat_id is not None:
            chat = self.find_chat(chat_id)
        else:
            chat = self.current_chat()

        if not chat:
            chat = {"id": "draft", "title": "New Chat", "messages": []}

        meta = extract_chat_metadata(chat)
        clean_title = sanitize_filename(meta["title"])
        c = self.colors

        win = tk.Toplevel(self.root)
        win.title(self.t("export_dialog_title"))
        win.configure(bg=c["bg"])
        win.resizable(False, False)
        win.transient(self.root)

        # Header section
        header_frame = tk.Frame(win, bg=c["panel"], padx=20, pady=16)
        header_frame.pack(fill="x")

        title_lbl = tk.Label(header_frame, text=self.t("export_dialog_title"),
                             bg=c["panel"], fg=c["text"], font=("Segoe UI", 13, "bold"), anchor="w")
        title_lbl.pack(fill="x")

        sub_text = self.t("export_dialog_subtitle", title=meta["title"])
        sub_lbl = tk.Label(header_frame, text=sub_text,
                           bg=c["panel"], fg=c["muted"], font=("Segoe UI", 9), anchor="w")
        sub_lbl.pack(fill="x", pady=(2, 6))

        stats_text = self.t("export_summary_info", messages=meta["total_messages"],
                            words=meta["total_words"], tokens=meta["tokens_approx"])
        stats_lbl = tk.Label(header_frame, text=f"📊  {stats_text}",
                             bg=c["panel"], fg=c["accent"], font=("Segoe UI", 9, "bold"), anchor="w")
        stats_lbl.pack(fill="x")

        if not meta["total_messages"]:
            empty_lbl = tk.Label(header_frame, text=self.t("export_empty_notice"),
                                 bg=c["panel"], fg=c["muted"], font=("Segoe UI", 8, "italic"), anchor="w")
            empty_lbl.pack(fill="x", pady=(4, 0))

        # Main content area
        body_frame = tk.Frame(win, bg=c["bg"], padx=20, pady=14)
        body_frame.pack(fill="both", expand=True)

        # Formats section
        tk.Label(body_frame, text=self.t("export_format_label"), bg=c["bg"], fg=c["accent"],
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", pady=(0, 6))

        format_var = tk.StringVar(value="pdf")

        formats = [
            ("pdf", self.t("export_format_pdf")),
            ("md", self.t("export_format_md")),
            ("html", self.t("export_format_html")),
            ("json", self.t("export_format_json")),
        ]

        # Destination path setup
        default_dir = os.path.expanduser("~/Documents")
        if not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")
        init_file_path = os.path.join(default_dir, f"{clean_title}.pdf")

        path_var = tk.StringVar(value=init_file_path)

        def on_format_change():
            cur = path_var.get().strip()
            if not cur:
                return
            root_p, ext = os.path.splitext(cur)
            if ext.lower() in (".pdf", ".md", ".html", ".htm", ".json"):
                new_fmt = format_var.get()
                path_var.set(f"{root_p}.{new_fmt}")

        for val, label_text in formats:
            rb = tk.Radiobutton(body_frame, text=label_text, value=val, variable=format_var,
                                bg=c["bg"], fg=c["text"], selectcolor=c["entry"],
                                activebackground=c["bg"], activeforeground=c["text"],
                                font=("Segoe UI", 9), anchor="w", command=on_format_change)
            rb.pack(fill="x", pady=2)

        # Separator line
        sep1 = tk.Frame(body_frame, height=1, bg=c["border"])
        sep1.pack(fill="x", pady=12)

        # Options section
        tk.Label(body_frame, text=self.t("export_options_label"), bg=c["bg"], fg=c["accent"],
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", pady=(0, 6))

        opt_meta_var = tk.BooleanVar(value=True)
        opt_time_var = tk.BooleanVar(value=True)
        opt_syntax_var = tk.BooleanVar(value=True)

        cb_meta = tk.Checkbutton(body_frame, text=self.t("export_opt_metadata"), variable=opt_meta_var,
                                 bg=c["bg"], fg=c["text"], selectcolor=c["entry"],
                                 activebackground=c["bg"], activeforeground=c["text"],
                                 font=("Segoe UI", 9), anchor="w")
        cb_meta.pack(fill="x", pady=2)

        cb_time = tk.Checkbutton(body_frame, text=self.t("export_opt_timestamps"), variable=opt_time_var,
                                 bg=c["bg"], fg=c["text"], selectcolor=c["entry"],
                                 activebackground=c["bg"], activeforeground=c["text"],
                                 font=("Segoe UI", 9), anchor="w")
        cb_time.pack(fill="x", pady=2)

        cb_syntax = tk.Checkbutton(body_frame, text=self.t("export_opt_syntax"), variable=opt_syntax_var,
                                   bg=c["bg"], fg=c["text"], selectcolor=c["entry"],
                                   activebackground=c["bg"], activeforeground=c["text"],
                                   font=("Segoe UI", 9), anchor="w")
        cb_syntax.pack(fill="x", pady=2)

        # Separator line
        sep2 = tk.Frame(body_frame, height=1, bg=c["border"])
        sep2.pack(fill="x", pady=12)

        # Destination path section
        tk.Label(body_frame, text=self.t("export_dest_label"), bg=c["bg"], fg=c["text"],
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", pady=(0, 4))

        dest_row = tk.Frame(body_frame, bg=c["bg"])
        dest_row.pack(fill="x", pady=(0, 8))

        path_entry = tk.Entry(dest_row, textvariable=path_var, font=("Segoe UI", 9), relief="flat",
                              bg=c["entry"], fg=c["text"], insertbackground=c["text"])
        path_entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))

        def browse_destination():
            fmt = format_var.get()
            ext_map = {
                "pdf": (".pdf", [("PDF Document", "*.pdf"), ("All Files", "*.*")]),
                "md": (".md", [("Markdown File", "*.md"), ("All Files", "*.*")]),
                "html": (".html", [("HTML File", "*.html;*.htm"), ("All Files", "*.*")]),
                "json": (".json", [("JSON File", "*.json"), ("All Files", "*.*")]),
            }
            def_ext, types = ext_map.get(fmt, (".pdf", [("All Files", "*.*")]))
            current_path = path_var.get().strip()
            init_dir = os.path.dirname(current_path) if current_path else default_dir
            init_file = os.path.basename(current_path) if current_path else f"{clean_title}{def_ext}"
            picked = filedialog.asksaveasfilename(
                parent=win,
                title=self.t("export_dialog_title"),
                initialdir=init_dir,
                initialfile=init_file,
                defaultextension=def_ext,
                filetypes=types
            )
            if picked:
                path_var.set(picked)

        browse_btn = Button(dest_row, text=self.t("export_browse_btn"), command=browse_destination,
                            relief="flat", bg=c["panel"], fg=c["text"], activebackground=c["border"],
                            activeforeground=c["text"], cursor="hand2", padx=12, pady=4)
        browse_btn.pack(side="right")

        # Bottom Buttons
        btn_row = tk.Frame(win, bg=c["bg"], padx=20, pady=16)
        btn_row.pack(fill="x")

        def cancel(_e=None):
            win.destroy()

        def do_export(_e=None):
            target_path = (path_entry.get() or path_var.get()).strip()
            if not target_path:
                return
            selected_fmt = format_var.get()
            ext = os.path.splitext(target_path)[1].lower().lstrip(".")
            if ext in ("pdf", "md", "markdown", "html", "htm", "json"):
                selected_fmt = "html" if ext == "htm" else ("md" if ext == "markdown" else ext)
            options = {
                "include_metadata": opt_meta_var.get(),
                "include_timestamps": opt_time_var.get(),
                "syntax_highlight": opt_syntax_var.get(),
                "theme": "dark" if self.dark_mode else "light"
            }
            try:
                exporter = ConversationExporter(chat, options)
                out_path = exporter.export_to_file(selected_fmt, target_path)
                win.destroy()
                messagebox.showinfo(
                    self.t("export_success_title"),
                    self.t("export_success_msg", path=out_path),
                    parent=self.root
                )
            except Exception as ex:
                messagebox.showerror(
                    self.t("export_error_title"),
                    self.t("export_error_msg", error=str(ex)),
                    parent=win
                )

        export_action_btn = Button(btn_row, text=self.t("export_action_btn"), command=do_export,
                                   relief="flat", bg=c["accent"], fg="white", activebackground=c["accent2"],
                                   activeforeground="white", cursor="hand2", padx=18, pady=8,
                                   font=("Segoe UI", 9, "bold"))
        export_action_btn.pack(side="right")

        cancel_action_btn = Button(btn_row, text=self.t("export_cancel_btn"), command=cancel,
                                   relief="flat", bg=c["danger"], fg=c["accent2"], activebackground=c["danger"],
                                   activeforeground=c["text"], cursor="hand2", padx=16, pady=8,
                                   font=("Segoe UI", 9, "bold"))
        cancel_action_btn.pack(side="right", padx=(0, 8))

        win.format_var = format_var
        win.path_entry = path_entry
        win.path_var = path_var
        win.export_btn = export_action_btn
        win.cancel_btn = cancel_action_btn
        win.do_export = do_export

        win.bind("<Return>", do_export)
        win.bind("<Escape>", cancel)
        win.protocol("WM_DELETE_WINDOW", cancel)

        # Center on root
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            win.grab_set()
        except tk.TclError:
            pass
        return win

    def clear_chat(self):
        chat = self.current_chat()
        if not chat or not chat.get("messages"):
            # Nothing saved to clear; leave the empty draft alone.
            return
        if not messagebox.askyesno(self.t("clear_dialog_title"), self.t("clear_dialog_message")):
            return
        self.tts.stop()
        self.chats = [saved for saved in self.chats if saved.get("id") != chat.get("id")]
        self.draft_chat = None
        self.make_draft()
        self.save_history()
        self.refresh_history_list()
        self.display_chat()

    def display_chat(self):
        self.read_buttons = []
        self.bubble_items.clear()
        for child in self.messages_frame.winfo_children():
            child.destroy()
        chat = self.current_chat()
        messages = chat.get("messages", []) if chat else []
        if not messages:
            self.show_welcome()
        else:
            for msg in messages:
                self.add_bubble(msg.get("role"), msg.get("content", ""), timestamp=msg.get("time"))
        self.root.after(50, lambda: self.canvas.yview_moveto(1.0))

    def show_welcome(self):
        # Welcome splash shown on launch and for each empty New Chat.
        c = self.colors
        wrapper = tk.Frame(self.messages_frame, bg=c["bg"])
        wrapper.pack(fill="both", expand=True, pady=24)

        image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jugnu_bot.png")
        try:
            self.welcome_photo = tk.PhotoImage(file=image_path)
            # Keep it compact in the chat viewport.
            factor = max(1, int(max(self.welcome_photo.width() / 360,
                                    self.welcome_photo.height() / 260)))
            if factor > 1:
                self.welcome_photo = self.welcome_photo.subsample(factor, factor)
            tk.Label(wrapper, image=self.welcome_photo, bg=c["bg"]).pack(pady=(0, 10))
        except (tk.TclError, OSError):
            # Friendly fallback if the companion image hasn't been copied beside the script.
            tk.Label(wrapper, text="🤖", font=("Segoe UI Emoji", 76),
                     bg=c["bg"], fg=c["accent"]).pack(pady=(10, 18))

        tk.Label(wrapper, text=self.t("welcome_title"),
                 font=("Segoe UI", 23, "bold"), bg=c["bg"], fg=c["text"]).pack(pady=(4, 5))
        tk.Label(wrapper, text="Just an Unusually Genius Neural Utility",
                 font=("Segoe UI", 12), bg=c["bg"], fg=c["muted"]).pack(pady=(0, 10))
        tk.Label(wrapper, text=self.t("welcome_prompt"),
                 font=("Segoe UI", 12), bg=c["bg"], fg=c["accent"]).pack()

    def copy_to_clipboard(self, text, button):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            success_fg = "#10B981" if self.dark_mode else "#059669"
            button.configure(text="✓ Copied!", fg=success_fg)
            def revert():
                try:
                    button.configure(text="⧉ Copy", fg=self.colors["muted"], bg=button.master["bg"])
                except tk.TclError:
                    pass
            self.root.after(1500, revert)
        except Exception:
            pass

    # ---------- Markdown in assistant bubbles ----------
    def markdown_fonts(self):
        """Fonts for the Markdown styles. Built once and kept, because Tk drops unreferenced fonts."""
        fonts = getattr(self, "_markdown_fonts", None)
        if fonts is None:
            family, size = self.bubble_font.cget("family"), self.bubble_font.cget("size")
            installed = set(tkfont.families(self.root))
            mono = next((name for name in ("Consolas", "Cascadia Mono", "Menlo", "DejaVu Sans Mono",
                                           "Courier New") if name in installed),
                        tkfont.nametofont("TkFixedFont", root=self.root).actual("family"))

            def make(**options):
                return tkfont.Font(root=self.root, **options)

            fonts = self._markdown_fonts = {
                "bold": make(family=family, size=size, weight="bold"),
                "italic": make(family=family, size=size, slant="italic"),
                "bolditalic": make(family=family, size=size, weight="bold", slant="italic"),
                "h1": make(family=family, size=size + 4, weight="bold"),
                "h2": make(family=family, size=size + 2, weight="bold"),
                "h3": make(family=family, size=size + 1, weight="bold"),
                "mono": make(family=mono, size=size),
                "codehead": make(family=family, size=size - 1, weight="bold"),
            }
        return fonts

    def configure_markdown_tags(self, txt):
        """Give a bubble's tk.Text the look of each Markdown style, in the current theme."""
        c, f = self.colors, self.markdown_fonts()
        # Tags created later win over earlier ones, so inline code and code blocks come last.
        txt.tag_configure("bold", font=f["bold"])
        txt.tag_configure("italic", font=f["italic"])
        txt.tag_configure("bolditalic", font=f["bolditalic"])
        txt.tag_configure("strike", overstrike=True)
        txt.tag_configure("h1", font=f["h1"], foreground=c["accent"], spacing1=4, spacing3=2)
        txt.tag_configure("h2", font=f["h2"], foreground=c["accent"], spacing1=4, spacing3=2)
        txt.tag_configure("h3", font=f["h3"], foreground=c["accent"], spacing1=2, spacing3=1)
        txt.tag_configure("quote", foreground=c["muted"], lmargin1=4, lmargin2=16)
        for level in range(5):
            indent = 4 + level * 18
            txt.tag_configure("li%d" % level, lmargin1=indent, lmargin2=indent + 14)
        txt.tag_configure("rule", foreground=c["border"])
        txt.tag_configure("link", foreground=c["accent"], underline=True)
        txt.tag_configure("linkurl", foreground=c["muted"])
        txt.tag_configure("icode", font=f["mono"], background=c["inline_code"])
        txt.tag_configure("codehead", background=c["code"], foreground=c["muted"], font=f["codehead"],
                          lmargin1=10, lmargin2=10, spacing1=2)
        txt.tag_configure("code", background=c["code"], foreground=c["text"], font=f["mono"],
                          lmargin1=10, lmargin2=10, rmargin=10, wrap="char")

    def make_code_copy_button(self, txt, code):
        """The Copy button shown at the top of a code block; it copies exactly that code."""
        c = self.colors
        button = tk.Button(txt, text=self.t("code_copy_btn"), font=("Segoe UI", 8, "bold"),
                           relief="flat", bd=0, highlightthickness=0, bg=c["code"], fg=c["muted"],
                           activebackground=c["border"], activeforeground=c["accent"],
                           cursor="hand2", padx=6, pady=1)
        button.configure(command=lambda: self.copy_code(code, button))
        button.bind("<Enter>", lambda _e: button.configure(fg=c["accent"]))
        button.bind("<Leave>", lambda _e: button.configure(fg=c["muted"]))
        return button

    def copy_code(self, code, button):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.root.update()
            button.configure(text=self.t("code_copied_btn"),
                             fg="#10B981" if self.dark_mode else "#059669")

            def revert():
                try:
                    button.configure(text=self.t("code_copy_btn"), fg=self.colors["muted"])
                except tk.TclError:
                    pass  # the bubble was redrawn meanwhile
            self.root.after(1500, revert)
        except tk.TclError:
            pass

    @staticmethod
    def measure_text(segments):
        """Text used to size the bubble. Code is monospace, so each code character counts as one column."""
        parts = []
        for seg in segments:
            if seg.copy is not None:
                continue
            if "code" in seg.tags:
                parts.append("\n".join("0" * len(line.expandtabs(4)) for line in seg.text.split("\n")))
            else:
                parts.append(seg.text)
        return "".join(parts)

    def fill_bubble_text(self, txt, text, segments):
        """Put the message into `txt`, styled. Returns (text used for sizing, embedded buttons).

        If anything goes wrong while styling, the bubble shows the plain message instead.
        """
        buttons = []
        try:
            self.configure_markdown_tags(txt)
            for seg in segments:
                if seg.copy is not None:
                    button = self.make_code_copy_button(txt, seg.copy)
                    txt.window_create("end", window=button, padx=6)
                    buttons.append(button)
                else:
                    txt.insert("end", seg.text, seg.tags)
            return self.measure_text(segments), buttons
        except Exception:
            for button in buttons:
                button.destroy()
            txt.delete("1.0", "end")
            txt.insert("1.0", text)
            return text, []

    def add_bubble(self, role, text, timestamp=None):
        c = self.colors
        is_user = role == "user"
        row = tk.Frame(self.messages_frame, bg=c["bg"])
        row.pack(fill="x", pady=7, padx=8)
        bubble = tk.Frame(row, bg=c["user"] if is_user else c["assistant"])
        bubble.pack(side="right" if is_user else "left", padx=(40, 0) if is_user else (0, 40))
        header = tk.Frame(bubble, bg=bubble["bg"])
        header.pack(fill="x", padx=12, pady=(8, 2))

        name = self.t("you_label") if is_user else "🤖 JUGNU"
        name_lbl = tk.Label(header, text=name, bg=bubble["bg"], fg=c["accent"],
                            font=("Segoe UI", 9, "bold"), anchor="w")
        name_lbl.pack(side="left")

        time_lbl = None
        if timestamp:
            time_lbl = tk.Label(header, text=timestamp, bg=bubble["bg"], fg=c["muted"],
                                font=("Segoe UI", 8), anchor="w")
            time_lbl.pack(side="left", padx=(8, 0))

        # Per-message Read aloud button (next to name/copy for assistant messages)
        read_btn = None
        if not is_user:
            read_btn = Button(header, text="🔊", command=lambda t=text: self.read_aloud(t),
                                 relief="flat", bd=0, font=("Segoe UI Emoji", 10), cursor="hand2",
                                 bg=bubble["bg"], fg=c["accent"], activebackground=bubble["bg"],
                                 activeforeground=c["accent2"], disabledforeground=c["muted"],
                                 state="normal" if self.tts.available else "disabled")
            read_btn.pack(side="right", padx=(4, 0))
            self.read_buttons.append(read_btn)

        copy_btn = Button(header, text="⧉ Copy", font=("Segoe UI", 8, "bold"),
                             relief="flat", bd=0, highlightthickness=0,
                             bg=bubble["bg"], fg=c["muted"],
                             highlightbackground=bubble["bg"],
                             activebackground=c["border"], activeforeground=c["accent"],
                             cursor="hand2", padx=6, pady=1)
        copy_btn.pack(side="right", padx=(8, 0))
        copy_btn.configure(command=lambda t=text, b=copy_btn: self.copy_to_clipboard(t, b))

        hover_bg = c["border"]

        def on_btn_enter(_event):
            try:
                if "Copied" not in copy_btn.cget("text"):
                    copy_btn.configure(fg=c["accent"], bg=hover_bg)
            except tk.TclError:
                pass

        def on_btn_leave(_event):
            try:
                if "Copied" not in copy_btn.cget("text"):
                    copy_btn.configure(fg=c["muted"], bg=bubble["bg"])
                else:
                    copy_btn.configure(bg=bubble["bg"])
            except tk.TclError:
                pass

        copy_btn.bind("<Enter>", on_btn_enter)
        copy_btn.bind("<Leave>", on_btn_leave)

        canvas_w = self.current_canvas_width()
        # Assistant replies are Markdown; what you type yourself is always shown exactly as typed.
        segments = None if is_user else render_markdown(text)
        measure_text = text if segments is None else self.measure_text(segments)
        calc_cols = self.compute_bubble_cols(measure_text, canvas_w)

        txt = tk.Text(bubble, font=self.bubble_font, wrap="word", width=calc_cols,
                      relief="flat", bd=0, highlightthickness=0,
                      bg=bubble["bg"], fg=c["text"],
                      selectbackground=c["accent"], selectforeground="white",
                      inactiveselectbackground=c["accent"], cursor="xterm")
        embedded_buttons = []
        if segments is None:
            txt.insert("1.0", text)
        else:
            measure_text, embedded_buttons = self.fill_bubble_text(txt, text, segments)
            txt.configure(width=self.bubble_cols(txt, measure_text, canvas_w))
        txt.pack(fill="x", padx=12, pady=(2, 10))

        # Forward mouse wheel events so scrolling over bubbles scrolls the canvas
        widgets_to_bind = [row, bubble, header, name_lbl, copy_btn, txt]
        if read_btn:
            widgets_to_bind.append(read_btn)
        if time_lbl:
            widgets_to_bind.append(time_lbl)
        widgets_to_bind.extend(embedded_buttons)
        for w in widgets_to_bind:
            w.bind("<MouseWheel>", self.on_mousewheel)
            w.bind("<Button-4>", self.on_mousewheel)
            w.bind("<Button-5>", self.on_mousewheel)

        def copy_selection(_event=None):
            try:
                sel = txt.get("sel.first", "sel.last")
                if sel:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(sel)
                    self.root.update()
                    return "break"
            except tk.TclError:
                pass

        for key in ("<Control-c>", "<Control-C>", "<Command-c>", "<Command-C>"):
            txt.bind(key, copy_selection)

        def select_all(_event=None):
            txt.tag_add("sel", "1.0", "end - 1c")
            return "break"

        for key in ("<Control-a>", "<Control-A>", "<Command-a>", "<Command-A>"):
            txt.bind(key, select_all)

        menu = tk.Menu(txt, tearoff=0)
        menu.add_command(label="Copy Selection", command=copy_selection)
        menu.add_command(label="Select All", command=select_all)
        menu.add_command(label="Copy All", command=lambda: self.copy_to_clipboard(text, copy_btn))

        def show_context_menu(event):
            try:
                has_sel = bool(txt.tag_ranges("sel"))
                menu.entryconfigure("Copy Selection", state="normal" if has_sel else "disabled")
                menu.tk_popup(event.x_root, event.y_root)
            except tk.TclError:
                pass
            finally:
                try:
                    menu.grab_release()
                except tk.TclError:
                    pass

        txt.bind("<Button-3>", show_context_menu)
        txt.bind("<Button-2>", show_context_menu)

        self.root.update_idletasks()
        h = self.fit_bubble_height(txt, measure_text)
        txt.configure(height=h, state="disabled")

        self.bubble_items.append((bubble, txt, measure_text))

    def set_status(self, key, **kwargs):
        # Store the key (not the text) so a language switch can re-translate the status line.
        self._status = (key, kwargs)
        self.status.configure(text="• " + self.t(key, **kwargs))

    def warm_up(self):
        def task():
            result = ollama_chat([{"role": "user", "content": "Reply with OK."}],
                                 timeout=90, keep_alive="10m", options={"num_predict": 3})
            # Only "cannot reach Ollama" counts as offline; any reply at all means it is running.
            if result.error in (ERROR_CONNECTION, ERROR_TIMEOUT):
                self.root.after(0, lambda: self.set_status("status_offline"))
            else:
                self.root.after(0, lambda: self.set_status("status_ready"))
        threading.Thread(target=task, daemon=True).start()

    def is_time_question(self, text):
        return detect_time_question(text)

    def extract_calc_expression(self, text):
        """
        Extracts an arithmetic expression from user input if present.
        Supports explicit commands (e.g. 'calculate 12*7', 'solve 0/0'),
        conversational math requests (e.g. 'the expression is 5/3+3', 'what is 12*7?'),
        and plain arithmetic expressions (e.g. '12*7', '5/3+3').
        Returns the expression string if detected, or None for general chat.
        """
        q = text.strip()
        if not q:
            return None

        def is_math_ast(expr_str):
            if re.search(r"\d{1,4}[-/]\d{1,2}[-/]\d{2,4}", expr_str):
                return False
            if re.fullmatch(r"[\d\.\s\+\-\*\/\%\(\)]+", expr_str):
                if any(op in expr_str for op in ["+", "*", "/", "%"]) or re.search(r"\d\s*-\s*\d", expr_str):
                    try:
                        tree = ast.parse(expr_str, mode="eval")
                        valid_nodes = (
                            ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                            ast.Mod, ast.Pow, ast.USub, ast.UAdd
                        )
                        if all(isinstance(node, valid_nodes) for node in ast.walk(tree)):
                            if any(isinstance(node, ast.BinOp) for node in ast.walk(tree)):
                                return True
                    except Exception:
                        pass
            return False

        # 1. Direct command prefixes: 'calculate', 'calc', 'solve', 'evaluate'
        match = re.match(r"^(?:calculate|calc|solve|evaluate)(?:[:\s]+(.*)|$)", q, flags=re.IGNORECASE)
        if match:
            remainder = match.group(1).strip() if match.group(1) else ""
            # Strip optional 'the expression' / 'the value of' from remainder, e.g. 'calculate the expression 5/3+3'
            remainder = re.sub(r"^(?:the\s+)?(?:expression|value\s+of)[:\s]*", "", remainder, flags=re.IGNORECASE).strip()
            return remainder

        # 2. Conversational math prefixes: 'the expression is ...', 'expression: ...', 'what is ...', 'value of ...'
        m_conv = re.match(
            r"^(?:(?:what\s+is|what\'s)\s+(?:the\s+)?(?:value\s+of|expression\s+is|expression)?|(?:the\s+)?(?:expression|value)\s+(?:is|of)|expression)[:\s]*(.*)$",
            q, flags=re.IGNORECASE
        )
        if m_conv and m_conv.group(1).strip():
            candidate = m_conv.group(1).strip().rstrip("?. ")
            if is_math_ast(candidate):
                return candidate

        # 3. Plain arithmetic expression (e.g. '12*7', '5/3+3', '0/0')
        if is_math_ast(q):
            return q

        return None

    def send_message(self, _event=None):
        if self.busy:
            return "break"
        user_text = self.input_box.get("1.0", "end").strip()
        if not user_text:
            return "break"
        if self.stt.is_listening:
            self.stt.cancel_listening()
        self.tts.stop()  # A new message interrupts any reply still being read aloud.
        chat = self.current_chat()
        if not chat:
            self.make_draft()
            chat = self.current_chat()
        if self.draft_chat and chat.get("id") == self.draft_chat.get("id"):
            self.chats.append(self.draft_chat)
            self.draft_chat = None
        if chat.get("title") == "New Chat":
            chat["title"] = user_text[:34]
        time_now = message_time_text()
        chat["messages"].append({"role": "user", "content": user_text, "time": time_now})
        self.input_box.delete("1.0", "end")
        self.add_bubble("user", user_text, timestamp=time_now)
        self.refresh_history_list()
        self.save_history()
        self.busy = True
        self.send_btn.configure(state="disabled")
        self.set_status("status_thinking")

        # Answer clock/date questions locally for accuracy and speed—no model call required.
        if self.is_time_question(user_text):
            answer = self.t("time_answer", when=local_datetime_text())
            self.finish_answer(answer, chat["id"])
            return "break"

        # Answer arithmetic questions locally using safe AST calculator—no model call required.
        calc_expr = self.extract_calc_expression(user_text)
        if calc_expr is not None:
            answer = calculate(calc_expr)
            self.finish_answer(answer, chat["id"])
            return "break"

        threading.Thread(target=self.ask_ollama, args=(chat["id"],), daemon=True).start()
        return "break"

    def ask_ollama(self, chat_id):
        language = self.language  # The prompt and any error text use the language chosen at send time.
        saved_chat = next((c for c in self.chats if c.get("id") == chat_id), None)
        if not saved_chat:
            # The conversation may have been cleared while the request was running.
            self.root.after(0, lambda: self.finish_answer(None, chat_id))
            return
        messages = build_messages(build_system_prompt(language, local_datetime_text()),
                                  saved_chat.get("messages", []))
        # The request itself is shared with the command-line version (core.py); the wording
        # of each failure below is this window's own, so it can be translated.
        result = ollama_chat(messages, timeout=180, keep_alive="10m",
                             options={"num_predict": 256, "temperature": 0.6})
        is_error = True
        if result.ok and result.answer:
            answer, is_error = result.answer, False
        elif result.ok:
            answer = get_string(language, "error_empty_reply")
        elif result.error == ERROR_INVALID:
            answer = get_string(language, "error_invalid_response")
        elif result.error == ERROR_TIMEOUT:
            answer = get_string(language, "error_timeout")
        elif result.error == ERROR_CONNECTION:
            answer = get_string(language, "error_connection")
        else:
            answer = get_string(language, "error_generic", error=result.detail)
        self.root.after(0, lambda: self.finish_answer(answer, chat_id, is_error=is_error))

    def finish_answer(self, answer, chat_id=None, is_error=False):
        chat = self.current_chat()
        if chat_id:
            chat = next((c for c in self.chats if c.get("id") == chat_id), None)
        time_now = message_time_text()
        if chat and isinstance(answer, str) and answer.strip():
            message = {"role": "assistant", "content": answer, "time": time_now}
            if is_error:
                message["is_error"] = True
            chat["messages"].append(message)
            self.save_history()
        # Only display if the user is still viewing the chat that received the answer.
        if chat and chat.get("id") == self.current_id:
            self.add_bubble("assistant", answer, timestamp=time_now)
            self.canvas.yview_moveto(1.0)
            if self.voice_enabled:
                self.tts.speak(plain_text(answer))
        self.busy = False
        self.send_btn.configure(state="normal")
        self.set_status("status_ready")
        self.refresh_history_list()

if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    lock = HistoryLock(HISTORY_FILE)
    try:
        lock.acquire()
    except HistoryInUseError:
        messagebox.showerror(
            "Chat history in use",
            "Another JUGNU window is using this chat history. Close that window before opening another.",
            parent=root,
        )
        root.destroy()
    except OSError as error:
        messagebox.showerror(
            "Chat history unavailable",
            f"Could not lock chat history.\n\n{error}",
            parent=root,
        )
        root.destroy()
    else:
        try:
            app = JugnuApp(root)
            root.protocol("WM_DELETE_WINDOW", app.on_close)
            root.deiconify()
            root.mainloop()
        finally:
            lock.release()

# ===========================================================================
# PySide6 Application Window
# ===========================================================================

"""JUGNU's desktop window, built with PySide6 and themed with Qt Style Sheets.

All the logic (history, settings, local answers, the Ollama client) lives in jugnu_core.py;
this file only draws it. The theme is in light.qss and dark.qss.
Run it with:  python desktop_app.py
"""
import math
import os
import sys

from PySide6.QtCore import QEvent, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QKeySequence, QPixmap, QShortcut, QTextOption
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QSystemTrayIcon, QTextBrowser, QVBoxLayout,
                               QWidget)

import uuid

import jugnu_core as core
from analytics import InferenceMetrics
from titling import generate_ai_title, generate_heuristic_title
from automation_dialog import AutomationDialog
from command_palette import Command, CommandPaletteDialog, CommandRegistry, ShortcutsHelpDialog
from context_ingest import (
    AttachedDocument,
    AttachmentError,
    assemble_prompt_with_attachments,
    ingest_file,
)
from conversation_tree import ConversationTree
from export_manager import export_chat
from history_lock import HistoryInUseError, HistoryLock
from markdown_render import plain_text, render_html
from models import (
    DEFAULT_MODEL,
    RECOMMENDED_MODELS,
    ModelInfo,
    fetch_local_models,
    format_bytes,
    pull_model_stream,
    resolve_active_model,
)
from personas import get_default_manager, get_persona, list_personas
from scheduler import BackgroundScheduler, CronEvaluator, ScheduledTask, TaskStore
from search_engine import SearchEngine
from strings import get_string, next_language
from tray_daemon import JugnuTrayIcon
from tts import RATE_MAX, RATE_MIN, TTSManager

HERE = os.path.dirname(os.path.abspath(__file__))
BUBBLE_PT = 10        # chat text size, in points
COPY_TEXT = "⧉ Copy"
COPIED_TEXT = "✓ Copied!"


def load_stylesheet(dark_mode):
    """The whole theme: dark.qss or light.qss, from beside this file."""
    with open(os.path.join(HERE, "dark.qss" if dark_mode else "light.qss"), encoding="utf-8") as f:
        return f.read()


def max_bubble_width(viewport_width):
    """Widest a bubble may be, keeping it inside the chat area with room to show who sent it."""
    return max(40, min(720, viewport_width - 100, int(viewport_width * 0.72)))


def repolish(widget):
    """Re-apply the style sheet after a property used by a selector (variant, state) changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Task(QThread):
    """Runs one blocking call (an Ollama request) off the GUI thread.

    `done` is emitted from the worker thread; Qt queues it, so the connected slot runs on the
    GUI thread. The work itself never touches a widget.
    """
    done = Signal(object)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work

    def run(self):
        self.done.emit(self.work())


class InputBox(QPlainTextEdit):
    """The message box. Enter sends; Shift+Enter starts a new line."""
    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class BubbleText(QTextBrowser):
    """A message's text: read-only, selectable, as tall as its content and never scrolling itself."""
    copy_all = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("bubbleText")
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Long words and URLs wrap anywhere rather than running out of the bubble.
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def fit(self, max_width):
        """Size to the text: as narrow as it allows, at most `max_width`, and exactly as tall."""
        # Measure a copy: the widget lays out its own document at its current width, not ours.
        doc = self.document().clone(self)
        doc.setTextWidth(max_width)
        width = min(max_width, math.ceil(doc.idealWidth()) + 2)
        doc.setTextWidth(width)
        self.setFixedSize(width, math.ceil(doc.size().height()) + 2)
        doc.deleteLater()

    def wheelEvent(self, event):
        event.ignore()  # let the chat area scroll instead

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu(event.pos())
        menu.addAction("Copy All", self.copy_all.emit)
        menu.exec(event.globalPos())


class Bubble(QFrame):
    """One chat message: name, time, Copy (and Read aloud for replies) above the text."""

    def __init__(self, app, role, text, timestamp=None, node_id=None, siblings_info=(0, 1, []), metrics=None):
        super().__init__()
        self.app, self.text = app, text
        self.node_id = node_id
        self.siblings_info = siblings_info
        self.metrics = metrics
        is_user = role == "user"
        self.setObjectName("bubble")
        self.setProperty("role", "user" if is_user else "assistant")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.codes = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.name_label = QLabel(app.t("you_label") if is_user else "🤖 JUGNU")
        self.name_label.setObjectName("bubbleName")
        header.addWidget(self.name_label)
        self.time_label = None
        if timestamp:
            self.time_label = QLabel(timestamp)
            self.time_label.setObjectName("bubbleTime")
            header.addWidget(self.time_label)

        # Alternative Response Pagination: ◀ 1 / 2 ▶
        curr_idx, total_siblings, _ = siblings_info
        self.page_controls = []
        if total_siblings > 1 and node_id:
            header.addSpacing(6)
            self.prev_btn = self.small_button("◀", lambda: app.switch_message_branch(app.current_id, node_id, curr_idx - 1))
            self.prev_btn.setEnabled(curr_idx > 0)
            self.prev_btn.setToolTip(app.t("branch_prev_tooltip"))
            header.addWidget(self.prev_btn)

            self.page_lbl = QLabel(f"{curr_idx + 1}/{total_siblings}")
            self.page_lbl.setObjectName("bubbleTime")
            header.addWidget(self.page_lbl)

            self.next_btn = self.small_button("▶", lambda: app.switch_message_branch(app.current_id, node_id, curr_idx + 1))
            self.next_btn.setEnabled(curr_idx < total_siblings - 1)
            self.next_btn.setToolTip(app.t("branch_next_tooltip"))
            header.addWidget(self.next_btn)
            self.page_controls.extend([self.prev_btn, self.page_lbl, self.next_btn])

        header.addStretch(1)
        self.hud_btn = None
        if not is_user and metrics and getattr(app, "show_performance_hud", True):
            metrics_obj = InferenceMetrics.from_dict(metrics) if isinstance(metrics, dict) else metrics
            if metrics_obj and (metrics_obj.tps > 0 or metrics_obj.total_duration_s > 0):
                self.hud_btn = self.small_button(metrics_obj.short_badge, lambda: app.show_metrics_dialog(metrics_obj))
                self.hud_btn.setObjectName("hudBadge")
                self.hud_btn.setToolTip(metrics_obj.tooltip)
                header.addWidget(self.hud_btn)


        # User bubble actions: Edit prompt
        if is_user and node_id:
            self.edit_btn = self.small_button("✏️", lambda: app.edit_user_message(node_id, text))
            self.edit_btn.setToolTip(app.t("edit_prompt_tooltip"))
            header.addWidget(self.edit_btn)

        # Assistant bubble actions: Regenerate alternative response
        if not is_user and node_id:
            self.regen_btn = self.small_button("🔄", lambda: app.regenerate_response(node_id))
            self.regen_btn.setToolTip(app.t("regen_tooltip"))
            header.addWidget(self.regen_btn)

        # Fork chat button for both roles
        if node_id:
            self.fork_btn = self.small_button("⑂", lambda: app.fork_chat_at_node(node_id))
            self.fork_btn.setToolTip(app.t("fork_tooltip"))
            header.addWidget(self.fork_btn)

        self.copy_btn = self.small_button(COPY_TEXT, self.copy_message)
        header.addWidget(self.copy_btn)
        self.read_btn = None
        if not is_user:
            self.read_btn = self.small_button("🔊", lambda: app.read_aloud(text))
            self.read_btn.setEnabled(app.tts.available)
            header.addWidget(self.read_btn)
        layout.addLayout(header)

        self.body = BubbleText(self)
        self.body.setFont(app.bubble_font())
        if is_user:
            self.body.setPlainText(text)  # what you type is always shown exactly as typed
        else:
            self.show_markdown(app.t("code_copy_btn"))
        self.body.anchorClicked.connect(self.on_link)
        self.body.copy_all.connect(self.copy_message)
        layout.addWidget(self.body)

    def small_button(self, text, slot):
        button = QPushButton(text)
        button.setProperty("variant", "ghost")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def show_markdown(self, copy_label):
        """Show the reply as styled Markdown. If anything goes wrong, show the plain message instead."""
        try:
            html, self.codes = render_html(self.text, self.app.colors, mono=self.app.mono_family(),
                                           base_pt=BUBBLE_PT, copy_label=copy_label)
            self.body.setHtml(html)
        except Exception:
            self.codes = []
            self.body.setPlainText(self.text)

    def fit(self, max_width):
        self.body.fit(max_width)

    def copy_message(self):
        QApplication.clipboard().setText(self.text)
        self.copy_btn.setText(COPIED_TEXT)
        self.copy_btn.setProperty("state", "copied")
        repolish(self.copy_btn)
        QTimer.singleShot(1500, self, self.reset_copy_button)

    def reset_copy_button(self):
        self.copy_btn.setText(COPY_TEXT)
        self.copy_btn.setProperty("state", "")
        repolish(self.copy_btn)

    def on_link(self, url):
        # Only the code blocks' own "Copy" links do anything; other links are just shown.
        target = url.toString()
        if target.startswith("copy:"):
            index = int(target[5:])
            if 0 <= index < len(self.codes):
                QApplication.clipboard().setText(self.codes[index])
                self.show_markdown(self.app.t("code_copied_btn"))
                QTimer.singleShot(1500, self, lambda: self.show_markdown(self.app.t("code_copy_btn")))


class VoiceSettingsDialog(QDialog):
    """Speed, volume and voice. Changes are heard from the next sentence and saved as you go."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setModal(False)
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 18)
        layout.setSpacing(6)

        self.rate_label = self.field_label()
        self.rate = QSlider(Qt.Horizontal)
        self.rate.setRange(RATE_MIN, RATE_MAX)
        self.rate.setSingleStep(5)
        self.rate.setPageStep(25)
        self.volume_label = self.field_label()
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setSingleStep(5)
        self.voice_label = self.field_label()
        self.voice = QComboBox()
        self.test_btn = QPushButton()
        self.close_btn = QPushButton()
        self.close_btn.setProperty("variant", "danger")
        for widget in (self.rate_label, self.rate, self.volume_label, self.volume,
                       self.voice_label, self.voice):
            layout.addWidget(widget)
        layout.addSpacing(12)
        layout.addWidget(self.test_btn)
        layout.addWidget(self.close_btn)

        # Set the starting positions first, then connect, so opening the window never rewrites the saved values.
        self.rate.setValue(app.voice_rate)
        self.volume.setValue(round(app.voice_volume * 100))
        self.rate.valueChanged.connect(self.on_rate)
        self.volume.valueChanged.connect(self.on_volume)
        self.rate.sliderReleased.connect(app.save_settings)
        self.volume.sliderReleased.connect(app.save_settings)
        self.voice.activated.connect(self.on_voice)
        self.test_btn.clicked.connect(app.test_voice)
        self.close_btn.clicked.connect(self.close)
        self.retranslate()

    @staticmethod
    def field_label():
        label = QLabel()
        label.setProperty("variant", "field")
        return label

    def voice_choices(self):
        """Dropdown entries: the default first, then every installed voice (names made unique)."""
        choices, seen = [(self.app.t("voice_default_name"), None)], {}
        for vid, name in self.app.tts.voices:
            seen[name] = seen.get(name, 0) + 1
            choices.append((name if seen[name] == 1 else f"{name} ({seen[name]})", vid))
        return choices

    def retranslate(self):
        app = self.app
        self.setWindowTitle(app.t("voice_settings_title"))
        self.update_value_labels()
        self.voice_label.setText(app.t("voice_choice_label"))
        self.test_btn.setText(app.t("voice_test_btn"))
        self.close_btn.setText(app.t("voice_close_btn"))
        # The default entry's name depends on the language, so rebuild the list.
        self.voice.clear()
        for label, vid in self.voice_choices():
            self.voice.addItem(label, vid)
        # A saved voice that is no longer installed shows (and plays) as the default.
        index = self.voice.findData(app.voice_id) if app.voice_id else 0
        self.voice.setCurrentIndex(max(index, 0))

    def update_value_labels(self):
        self.rate_label.setText(self.app.t("voice_rate_label", value=self.app.voice_rate))
        self.volume_label.setText(self.app.t("voice_volume_label", value=round(self.app.voice_volume * 100)))

    def on_rate(self, value):
        self.app.voice_rate = int(value)
        self.app.push_voice_settings()
        self.update_value_labels()

    def on_volume(self, value):
        self.app.voice_volume = int(value) / 100
        self.app.push_voice_settings()
        self.update_value_labels()

    def on_voice(self, index):
        self.app.voice_id = self.voice.itemData(index)
        self.app.push_voice_settings()
        self.app.save_settings()


    def toggle_performance_hud(self):
        self.show_performance_hud = not self.show_performance_hud
        core.save_performance_hud(self.settings_file, self.show_performance_hud)
        self.display_chat()

    def show_metrics_dialog(self, metrics):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(self.t("hud_dialog_title"))
        layout = QVBoxLayout(dlg)
        for label, val in [
            (self.t("hud_speed"), f"{metrics.tps:.1f} tokens/s"),
            (self.t("hud_tokens"), f"{metrics.eval_count}"),
            (self.t("hud_total_time"), f"{metrics.total_duration_s:.2f} s"),
            (self.t("hud_ttft"), f"{metrics.prompt_eval_duration_s:.2f} s"),
            (self.t("hud_model"), f"{metrics.model or self.current_model}"),
        ]:
            row = QLabel(f"<b>{label}:</b>  {val}")
            layout.addWidget(row)
        bbox = QDialogButtonBox(QDialogButtonBox.Ok)
        bbox.accepted.connect(dlg.accept)
        layout.addWidget(bbox)
        dlg.exec()

    def start_ai_titling(self, chat_id, first_user_prompt):
        def work():
            return generate_ai_title(first_user_prompt)
        def slot(ai_title):
            if ai_title:
                chat = self.find_chat(chat_id)
                if chat and not chat.get("is_custom_title"):
                    chat["title"] = ai_title
                    self.save_history()
                    self.refresh_history_list()
        self.start_task(work, slot)

    def closeEvent(self, event):
        self.app.save_settings()
        super().closeEvent(event)


class PersonaDialog(QDialog):
    """View, customize, create, and delete assistant personas."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.manager = app.persona_manager
        self.setWindowTitle(app.t("persona_manage_title"))
        self.setMinimumSize(600, 480)
        self.selected_id = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        # Left list
        left = QVBoxLayout()
        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self.on_select_persona)
        left.addWidget(self.list_widget, 1)

        self.new_btn = QPushButton(app.t("persona_new_btn"))
        self.new_btn.clicked.connect(self.on_new)
        left.addWidget(self.new_btn)
        layout.addLayout(left, 1)

        # Right form
        right = QVBoxLayout()
        right.setSpacing(8)

        self.name_label = QLabel(app.t("persona_name_label"))
        right.addWidget(self.name_label)
        self.name_edit = QLineEdit()
        right.addWidget(self.name_edit)

        self.desc_label = QLabel(app.t("persona_desc_label"))
        right.addWidget(self.desc_label)
        self.desc_edit = QLineEdit()
        right.addWidget(self.desc_edit)

        self.temp_label = QLabel()
        right.addWidget(self.temp_label)
        self.temp_slider = QSlider(Qt.Horizontal)
        self.temp_slider.setRange(0, 150)
        self.temp_slider.setValue(60)
        self.temp_slider.valueChanged.connect(self.on_temp_changed)
        right.addWidget(self.temp_slider)

        self.prompt_label = QLabel(app.t("persona_prompt_label"))
        right.addWidget(self.prompt_label)
        self.prompt_edit = QPlainTextEdit()
        right.addWidget(self.prompt_edit, 1)

        btn_row = QHBoxLayout()
        self.save_btn = QPushButton(app.t("persona_save_btn"))
        self.save_btn.clicked.connect(self.on_save)
        btn_row.addWidget(self.save_btn)

        self.delete_btn = QPushButton(app.t("persona_delete_btn"))
        self.delete_btn.setProperty("variant", "danger")
        self.delete_btn.clicked.connect(self.on_delete)
        btn_row.addWidget(self.delete_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.close_btn)

        right.addLayout(btn_row)
        layout.addLayout(right, 2)

        self.on_temp_changed(self.temp_slider.value())
        self.populate_list()

    def on_temp_changed(self, val):
        self.temp_label.setText(self.app.t("persona_temp_label", temp=f"{val / 100:.2f}"))

    def populate_list(self, select_id=None):
        self.list_widget.clear()
        personas = self.manager.all()
        target_row = 0
        for idx, p in enumerate(personas):
            item = QListWidgetItem(p.display_label)
            item.setData(Qt.UserRole, p.id)
            self.list_widget.addItem(item)
            if select_id and p.id == select_id:
                target_row = idx
        if personas:
            self.list_widget.setCurrentRow(target_row)

    def on_select_persona(self, row):
        if row < 0:
            return
        item = self.list_widget.item(row)
        if not item:
            return
        persona_id = item.data(Qt.UserRole)
        persona = self.manager.get(persona_id)
        self.selected_id = persona.id
        self.name_edit.setText(persona.name)
        self.desc_edit.setText(persona.description)
        self.temp_slider.setValue(round(persona.temperature * 100))
        self.prompt_edit.setPlainText(persona.system_prompt)

        is_builtin = persona.is_builtin
        self.name_edit.setEnabled(not is_builtin)
        self.desc_edit.setEnabled(not is_builtin)
        self.temp_slider.setEnabled(not is_builtin)
        self.prompt_edit.setEnabled(not is_builtin)
        self.save_btn.setEnabled(not is_builtin)
        self.delete_btn.setEnabled(not is_builtin)

    def on_new(self):
        self.selected_id = None
        self.list_widget.setCurrentRow(-1)
        self.name_edit.setEnabled(True)
        self.desc_edit.setEnabled(True)
        self.temp_slider.setEnabled(True)
        self.prompt_edit.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.delete_btn.setEnabled(False)

        self.name_edit.setText("")
        self.desc_edit.setText("")
        self.temp_slider.setValue(60)
        self.prompt_edit.setPlainText("You are a specialized AI assistant.\nProvide helpful and accurate answers.")
        self.name_edit.setFocus()

    def on_save(self):
        name = self.name_edit.text().strip()
        desc = self.desc_edit.text().strip()
        temp = self.temp_slider.value() / 100
        prompt = self.prompt_edit.toPlainText().strip()

        if not name:
            QMessageBox.warning(self, "Invalid Name", "Please enter a persona name.")
            return
        if not prompt:
            QMessageBox.warning(self, "Invalid Prompt", "System prompt cannot be empty.")
            return

        try:
            if self.selected_id:
                p = self.manager.update_custom(self.selected_id, name=name, description=desc, system_prompt=prompt, temperature=temp)
            else:
                p = self.manager.add_custom(name=name, description=desc, system_prompt=prompt, temperature=temp)
            self.app.refresh_personas_ui()
            self.populate_list(select_id=p.id)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def on_delete(self):
        if not self.selected_id:
            return
        persona = self.manager.get(self.selected_id)
        if persona.is_builtin:
            return
        if QMessageBox.question(
            self,
            "Confirm Delete",
            self.app.t("persona_delete_confirm", name=persona.name),
            QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes:
            self.manager.delete_custom(self.selected_id)
            self.app.refresh_personas_ui()
            self.populate_list()

    def retranslate(self):
        self.setWindowTitle(self.app.t("persona_manage_title"))
        self.new_btn.setText(self.app.t("persona_new_btn"))
        self.name_label.setText(self.app.t("persona_name_label"))
        self.desc_label.setText(self.app.t("persona_desc_label"))
        self.prompt_label.setText(self.app.t("persona_prompt_label"))
        self.save_btn.setText(self.app.t("persona_save_btn"))
        self.delete_btn.setText(self.app.t("persona_delete_btn"))
        self.on_temp_changed(self.temp_slider.value())


    def toggle_performance_hud(self):
        self.show_performance_hud = not self.show_performance_hud
        core.save_performance_hud(self.settings_file, self.show_performance_hud)
        self.display_chat()

    def show_metrics_dialog(self, metrics):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(self.t("hud_dialog_title"))
        layout = QVBoxLayout(dlg)
        for label, val in [
            (self.t("hud_speed"), f"{metrics.tps:.1f} tokens/s"),
            (self.t("hud_tokens"), f"{metrics.eval_count}"),
            (self.t("hud_total_time"), f"{metrics.total_duration_s:.2f} s"),
            (self.t("hud_ttft"), f"{metrics.prompt_eval_duration_s:.2f} s"),
            (self.t("hud_model"), f"{metrics.model or self.current_model}"),
        ]:
            row = QLabel(f"<b>{label}:</b>  {val}")
            layout.addWidget(row)
        bbox = QDialogButtonBox(QDialogButtonBox.Ok)
        bbox.accepted.connect(dlg.accept)
        layout.addWidget(bbox)
        dlg.exec()

    def start_ai_titling(self, chat_id, first_user_prompt):
        def work():
            return generate_ai_title(first_user_prompt)
        def slot(ai_title):
            if ai_title:
                chat = self.find_chat(chat_id)
                if chat and not chat.get("is_custom_title"):
                    chat["title"] = ai_title
                    self.save_history()
                    self.refresh_history_list()
        self.start_task(work, slot)

    def closeEvent(self, event):
        self.app.persona_dialog = None
        super().closeEvent(event)


class ModelPullDialog(QDialog):
    """Download new Ollama models with progress and status updates."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setWindowTitle(self.app.t("model_pull_title"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(10)

        prompt = QLabel(self.app.t("model_pull_prompt"))
        prompt.setWordWrap(True)
        layout.addWidget(prompt)

        rec_label = QLabel(self.app.t("model_pull_recommended"))
        rec_label.setProperty("variant", "field")
        layout.addWidget(rec_label)

        self.rec_combo = QComboBox()
        self.rec_combo.addItem("-- Select recommended model --", "")
        for m in RECOMMENDED_MODELS:
            self.rec_combo.addItem(f"{m['name']} ({m['desc']})", m["name"])
        self.rec_combo.currentIndexChanged.connect(self.on_rec_selected)
        layout.addWidget(self.rec_combo)

        custom_label = QLabel(self.app.t("model_pull_custom_label"))
        custom_label.setProperty("variant", "field")
        layout.addWidget(custom_label)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("e.g. llama3.2:1b, qwen2.5:3b")
        layout.addWidget(self.model_input)

        self.status_label = QLabel(self.app.t("model_pull_status_idle"))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        self.pull_btn = QPushButton(self.app.t("model_pull_start_btn"))
        self.pull_btn.setCursor(Qt.PointingHandCursor)
        self.pull_btn.clicked.connect(self.start_pull)
        btn_row.addWidget(self.pull_btn)

        self.close_btn = QPushButton(self.app.t("model_pull_close_btn"))
        self.close_btn.setProperty("variant", "danger")
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    def on_rec_selected(self, index):
        data = self.rec_combo.currentData()
        if data:
            self.model_input.setText(data)

    def start_pull(self):
        target = self.model_input.text().strip()
        if not target:
            self.status_label.setText(self.app.t("model_pull_empty_name"))
            return
        self.pull_btn.setEnabled(False)
        self.rec_combo.setEnabled(False)
        self.model_input.setEnabled(False)
        self.status_label.setText(self.app.t("model_pull_status_downloading", model=target, percent=0, mb="0 MB"))

        def run_stream():
            try:
                last_msg = ""
                for update in pull_model_stream(target):
                    last_msg = update.get("status", "")
                return True, target, last_msg
            except Exception as err:
                return False, target, str(err)

        def on_done(result):
            ok, model_name, msg = result
            self.pull_btn.setEnabled(True)
            self.rec_combo.setEnabled(True)
            self.model_input.setEnabled(True)
            if ok:
                self.status_label.setText(self.app.t("model_pull_status_success", model=model_name))
                self.app.switch_model(model_name)
            else:
                self.status_label.setText(self.app.t("model_pull_status_error", error=msg))

        self.app.start_task(run_stream, on_done)


class JugnuWindow(QMainWindow):
    def __init__(self, history_file=core.HISTORY_FILE, settings_file=core.SETTINGS_FILE):
        super().__init__()
        self.history_file, self.settings_file = history_file, settings_file
        self.setWindowTitle(core.APP_TITLE)
        self.resize(1200, 760)
        self.setMinimumSize(850, 570)

        self.chats = core.load_history(history_file)
        self.current_id = None
        self.draft_chat = None  # Unsaved empty chat; saved only after first message.
        self.busy = False
        self.dark_mode = core.load_dark_mode(settings_file)
        self.voice_enabled = core.load_voice_enabled(settings_file)
        self.language = core.load_language(settings_file)
        self.show_performance_hud = core.load_performance_hud(settings_file)
        self.voice_rate, self.voice_volume, self.voice_id = core.load_voice_prefs(settings_file)
        self.tts = TTSManager(rate=self.voice_rate, volume=self.voice_volume,
                              voice_id=self.voice_id)  # Offline voice output; runs on its own thread.
        self.voice_dialog = None
        self.persona_manager = get_default_manager()
        self.persona_dialog = None
        self.palette_dialog = None
        self.shortcuts_dialog = None
        self.commands = CommandRegistry()
        self._status = ("status_ready", {})  # Remembered so the status line can be re-translated.
        self._mono = None
        self.bubbles = []
        self._stick_to_bottom = False

        self.tasks = []  # background requests that are still running
        self.closed = False
        self.attachments: list[AttachedDocument] = []
        self.current_model = self.load_model_setting()

        task_file = os.path.join(os.path.dirname(self.settings_file), core.TASKS_FILE) if os.path.dirname(self.settings_file) else core.TASKS_FILE
        self.task_store = TaskStore(task_file)
        self.scheduler = BackgroundScheduler(self.task_store, check_interval=5.0, executor_fn=self._execute_scheduled_task)
        self.scheduler.on_task_finished = self.on_scheduled_task_finished
        self.scheduler.start()
        self.automation_dialog = None
        self.tray_icon = None
        try:
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.tray_icon = JugnuTrayIcon(self, self.scheduler)
                self.tray_icon.show()
        except Exception:
            self.tray_icon = None

        self.build_ui()
        if self.chats:
            self.current_id = self.chats[-1]["id"]  # Reopen most recent saved conversation.
        else:
            self.make_draft()
        self.apply_theme()
        self.apply_language()

        self.resize_timer = QTimer(self, singleShot=True, interval=60)
        self.resize_timer.timeout.connect(self.fit_bubbles)
        self.speech_timer = QTimer(self, interval=200)
        self.speech_timer.timeout.connect(self.poll_speech)
        self.speech_timer.start()
        QTimer.singleShot(250, self, self.warm_up)

    # ---------- Layout ----------
    def build_ui(self):
        # Visible text is filled in by apply_language(), so it can be switched at runtime.
        root = QWidget()
        self.setCentralWidget(root)
        main = QHBoxLayout(root)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(230)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 12)
        side.setSpacing(8)
        side.addWidget(QLabel("🤖  JUGNU", objectName="brand"))
        side.addWidget(QLabel("Just an Unusually Genius\nNeural Utility", objectName="tagline"))
        side.addSpacing(10)
        self.new_btn = self.sidebar_button(side, self.new_chat)
        self.palette_btn = self.sidebar_button(side, self.open_command_palette)
        side.addSpacing(10)
        self.history_label = QLabel(objectName="historyLabel")
        side.addWidget(self.history_label)
        self.search_entry = QLineEdit(objectName="searchEntry")
        self.search_entry.setClearButtonEnabled(True)
        self.search_entry.textChanged.connect(self.on_search_query_changed)
        side.addWidget(self.search_entry)
        self.history_list = QListWidget(objectName="historyList")
        self.history_list.itemClicked.connect(self.select_chat)
        self.history_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self.show_chat_menu)
        side.addWidget(self.history_list, 1)
        self.clear_btn = self.sidebar_button(side, self.clear_chat)
        self.clear_btn.setProperty("variant", "danger")
        self.persona_sidebar_btn = self.sidebar_button(side, self.open_persona_manager)
        self.voice_btn = self.sidebar_button(side, self.toggle_voice)
        self.voice_settings_btn = self.sidebar_button(side, self.open_voice_settings)
        self.language_btn = self.sidebar_button(side, self.toggle_language)
        self.theme_btn = self.sidebar_button(side, self.toggle_theme)
        if not self.tts.available:
            self.voice_btn.setEnabled(False)  # pyttsx3 not installed
            self.voice_settings_btn.setEnabled(False)
        main.addWidget(sidebar)

        content = QWidget(objectName="content")
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        header = QFrame(objectName="header")
        header.setFixedHeight(64)
        head = QHBoxLayout(header)
        head.setContentsMargins(22, 0, 18, 0)
        head.setSpacing(10)
        self.header_title = QLabel(objectName="headerTitle")
        head.addWidget(self.header_title)
        head.addStretch(1)
        self.model_btn = QPushButton()
        self.model_btn.setObjectName("modelBtn")
        self.model_btn.setCursor(Qt.PointingHandCursor)
        self.model_btn.clicked.connect(self.show_model_menu)
        self.model_sidebar_btn = self.model_btn
        head.addWidget(self.model_btn)

        self.persona_label = QLabel(objectName="personaLabel")
        head.addWidget(self.persona_label)

        self.persona_combo = QComboBox(objectName="personaCombo")
        self.persona_combo.currentIndexChanged.connect(self.on_persona_picked)
        head.addWidget(self.persona_combo)

        self.persona_btn = QPushButton("⚙", objectName="personaBtn")
        self.persona_btn.setCursor(Qt.PointingHandCursor)
        self.persona_btn.setFixedWidth(36)
        self.persona_btn.clicked.connect(self.open_persona_manager)
        head.addWidget(self.persona_btn)

        self.auto_btn = QPushButton("⏱  Tasks", objectName="autoBtn")
        self.auto_btn.setCursor(Qt.PointingHandCursor)
        self.auto_btn.clicked.connect(self.open_automation_dialog)
        head.addWidget(self.auto_btn)
        QShortcut(QKeySequence("Ctrl+Shift+A"), self, self.open_automation_dialog)
        # Shown only while speech is playing (see poll_speech).
        self.stop_btn = QPushButton("⏹  Stop")
        self.stop_btn.setProperty("variant", "stop")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self.stop_speaking)
        self.stop_btn.hide()
        head.addWidget(self.stop_btn)
        self.status = QLabel(objectName="status")
        head.addWidget(self.status)
        column.addWidget(header)

        self.scroll = QScrollArea(objectName="chatScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.messages = QWidget(objectName="messages")
        self.messages_layout = QVBoxLayout(self.messages)
        self.messages_layout.setContentsMargins(20, 16, 20, 16)
        self.messages_layout.setSpacing(14)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.verticalScrollBar().rangeChanged.connect(self.on_scroll_range)
        column.addWidget(self.scroll, 1)

        bottom_container = QVBoxLayout()
        bottom_container.setContentsMargins(0, 0, 0, 0)
        bottom_container.setSpacing(4)

        # Attachment chips container
        self.attachments_container = QWidget()
        self.attachments_layout = QHBoxLayout(self.attachments_container)
        self.attachments_layout.setContentsMargins(12, 0, 12, 0)
        self.attachments_layout.setSpacing(6)
        self.attachments_container.setVisible(False)
        bottom_container.addWidget(self.attachments_container)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(12, 0, 12, 12)
        bottom.setSpacing(10)
        self.attach_btn = QPushButton("📎")
        self.attach_btn.setObjectName("attachBtn")
        self.attach_btn.setCursor(Qt.PointingHandCursor)
        self.attach_btn.setFixedSize(48, 48)
        self.attach_btn.clicked.connect(self.pick_attachments)
        bottom.addWidget(self.attach_btn)
        self.input_box = InputBox()
        self.input_box.setFixedHeight(64)
        self.input_box.submitted.connect(self.send_message)
        bottom.addWidget(self.input_box, 1)
        self.send_btn = QPushButton()
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setMinimumHeight(48)
        self.send_btn.clicked.connect(self.send_message)
        bottom.addWidget(self.send_btn)
        bottom_container.addLayout(bottom)
        column.addLayout(bottom_container)
        main.addWidget(content, 1)

        QShortcut(QKeySequence("Ctrl+N"), self, self.new_chat)
        QShortcut(QKeySequence("Ctrl+F"), self, self.focus_search)
        QShortcut(QKeySequence("Ctrl+K"), self, self.open_command_palette)
        QShortcut(QKeySequence("Ctrl+D"), self, self.toggle_theme)
        QShortcut(QKeySequence("Ctrl+L"), self, self.toggle_language)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, self.toggle_voice)
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_voice_settings)
        QShortcut(QKeySequence("Ctrl+Up"), self, self.navigate_previous_chat)
        QShortcut(QKeySequence("Ctrl+Down"), self, self.navigate_next_chat)
        QShortcut(QKeySequence("F1"), self, self.open_shortcuts_help)
        QShortcut(QKeySequence("Ctrl+/"), self, self.open_shortcuts_help)
        QShortcut(QKeySequence("Escape"), self, self.handle_escape)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)

    def focus_search(self):
        self.search_entry.setFocus()
        self.search_entry.selectAll()

    def on_search_query_changed(self, _text):
        self.refresh_history_list()

    def open_command_palette(self):
        """Open the interactive command palette HUD modal."""
        self.register_commands()
        dialog = CommandPaletteDialog(self.commands, self)
        self.palette_dialog = dialog
        dialog.exec()

    def open_shortcuts_help(self):
        """Open the keyboard shortcuts cheat sheet dialog."""
        dialog = ShortcutsHelpDialog(self, is_dark=self.dark_mode)
        self.shortcuts_dialog = dialog
        dialog.exec()

    def register_commands(self):
        """Populate the command registry with localized application commands."""
        self.commands.clear()
        self.commands.register(Command(
            id="chat:new",
            title=self.t("cmd_new_chat"),
            category=self.t("cat_chat"),
            shortcut="Ctrl+N",
            callback=self.new_chat,
            keywords=["new", "create", "start", "blank", "chat", "nayi"],
            icon="➕",
        ))
        self.commands.register(Command(
            id="chat:clear",
            title=self.t("cmd_clear_chat"),
            category=self.t("cat_chat"),
            shortcut="Ctrl+Shift+Delete",
            callback=self.clear_chat,
            keywords=["clear", "delete", "empty", "remove", "saaf"],
            icon="🗑️",
        ))
        self.commands.register(Command(
            id="theme:toggle",
            title=self.t("cmd_toggle_theme"),
            category=self.t("cat_appearance"),
            shortcut="Ctrl+D",
            callback=self.toggle_theme,
            keywords=["theme", "dark", "light", "mode", "color", "appearance"],
            icon="🎨",
        ))
        self.commands.register(Command(
            id="lang:toggle",
            title=self.t("cmd_toggle_language"),
            category=self.t("cat_settings"),
            shortcut="Ctrl+L",
            callback=self.toggle_language,
            keywords=["language", "english", "hinglish", "switch", "translate", "bhasha"],
            icon="🌐",
        ))
        self.commands.register(Command(
            id="voice:toggle",
            title=self.t("cmd_toggle_voice"),
            category=self.t("cat_voice"),
            shortcut="Ctrl+Shift+S",
            callback=self.toggle_voice,
            keywords=["voice", "speech", "mute", "unmute", "sound", "awaaz"],
            icon="🔊",
        ))
        self.commands.register(Command(
            id="voice:settings",
            title=self.t("cmd_voice_settings"),
            category=self.t("cat_voice"),
            shortcut="Ctrl+,",
            callback=self.open_voice_settings,
            keywords=["voice", "settings", "rate", "speed", "volume"],
            icon="⚙️",
        ))
        self.commands.register(Command(
            id="nav:prev_chat",
            title=self.t("cmd_prev_chat"),
            category=self.t("cat_navigation"),
            shortcut="Ctrl+Up",
            callback=self.navigate_previous_chat,
            keywords=["previous", "up", "back", "earlier", "pichli"],
            icon="⬆️",
        ))
        self.commands.register(Command(
            id="nav:next_chat",
            title=self.t("cmd_next_chat"),
            category=self.t("cat_navigation"),
            shortcut="Ctrl+Down",
            callback=self.navigate_next_chat,
            keywords=["next", "down", "forward", "later", "agli"],
            icon="⬇️",
        ))
        self.commands.register(Command(
            id="help:shortcuts",
            title=self.t("cmd_shortcuts_help"),
            category=self.t("cat_help"),
            shortcut="F1",
            callback=self.open_shortcuts_help,
            keywords=["shortcuts", "hotkeys", "keys", "help", "cheat sheet"],
            icon="⌨️",
        ))
        self.commands.register(Command(
            id="app:quit",
            title=self.t("cmd_quit"),
            category=self.t("cat_app"),
            shortcut="Ctrl+Q",
            callback=self.close,
            keywords=["quit", "exit", "close", "bye", "band"],
            icon="🚪",
        ))

    def navigate_previous_chat(self):
        """Switch to previous conversation in the history list."""
        count = self.history_list.count()
        if count <= 1:
            return
        row = self.history_list.currentRow()
        new_row = row - 1 if row > 0 else count - 1
        item = self.history_list.item(new_row)
        if item:
            self.history_list.setCurrentItem(item)
            self.select_chat(item)

    def navigate_next_chat(self):
        """Switch to next conversation in the history list."""
        count = self.history_list.count()
        if count <= 1:
            return
        row = self.history_list.currentRow()
        new_row = row + 1 if row < count - 1 else 0
        item = self.history_list.item(new_row)
        if item:
            self.history_list.setCurrentItem(item)
            self.select_chat(item)

    def handle_escape(self):
        """Global escape key handler."""
        if getattr(self, "palette_dialog", None) and self.palette_dialog.isVisible():
            self.palette_dialog.reject()
            return
        if getattr(self, "shortcuts_dialog", None) and self.shortcuts_dialog.isVisible():
            self.shortcuts_dialog.reject()
            return
        if getattr(self, "voice_dialog", None) and self.voice_dialog.isVisible():
            self.voice_dialog.reject()
            return
        if self.tts.speaking:
            self.stop_speaking()
            return
        if getattr(self, "is_streaming", False):
            self.stop_generating()
            return

    def sidebar_button(self, layout, slot):
        button = QPushButton()
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        layout.addWidget(button)
        return button

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Resize:
            self.resize_timer.start()  # re-fit bubbles once resizing settles
        return super().eventFilter(watched, event)

    def on_scroll_range(self, _minimum, maximum):
        if self._stick_to_bottom:
            self.scroll.verticalScrollBar().setValue(maximum)

    def scroll_to_bottom(self):
        # The new size is only known after the layout runs, so keep following it for a moment.
        self._stick_to_bottom = True
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())
        QTimer.singleShot(300, self, self.stop_sticking)

    def stop_sticking(self):
        self._stick_to_bottom = False

    # ---------- Theme and language ----------
    def bubble_font(self):
        font = self.font()
        font.setPointSize(BUBBLE_PT)
        return font

    def mono_family(self):
        if self._mono is None:
            self._mono = QFontDatabase.systemFont(QFontDatabase.FixedFont).family()
        return self._mono

    @property
    def colors(self):
        return core.DARK if self.dark_mode else core.LIGHT

    def apply_theme(self):
        QApplication.instance().setStyleSheet(load_stylesheet(self.dark_mode))
        self.theme_btn.setText(self.theme_button_text())
        self.display_chat()  # reply bubbles carry theme colours in their rich text

    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.save_settings()
        self.apply_theme()

    def t(self, key, **kwargs):
        """Look up a user-facing string in the currently selected language."""
        return get_string(self.language, key, **kwargs)

    def voice_button_text(self):
        return self.t("voice_on" if self.voice_enabled else "voice_off")

    def theme_button_text(self):
        return self.t("theme_to_light" if self.dark_mode else "theme_to_dark")

    def toggle_language(self):
        self.language = next_language(self.language)
        self.save_settings()
        self.apply_language()

    def apply_language(self):
        # Set every piece of visible text from the strings dictionary.
        self.new_btn.setText(self.t("new_chat_btn"))
        if hasattr(self, "palette_btn"):
            self.palette_btn.setText(self.t("command_palette_btn"))
        self.history_label.setText(self.t("history_label"))
        self.search_entry.setPlaceholderText(self.t("search_placeholder"))
        self.clear_btn.setText(self.t("clear_chat_btn"))
        self.persona_sidebar_btn.setText(self.t("sidebar_personas"))
        self.persona_label.setText(self.t("persona_label"))
        self.persona_btn.setToolTip(self.t("persona_manage_title"))
        self.voice_btn.setText(self.voice_button_text())
        self.voice_settings_btn.setText(self.t("voice_settings_btn"))
        self.language_btn.setText(self.t("language_btn", name=self.t("language_name")))
        self.theme_btn.setText(self.theme_button_text())
        self.header_title.setText(self.t("header_title"))
        self.auto_btn.setText(self.t("auto_btn"))
        self.send_btn.setText(self.t("send_btn"))
        if hasattr(self, "attach_btn"):
            self.attach_btn.setToolTip(self.t("attach_file_btn"))
        if hasattr(self, "commands"):
            self.register_commands()
        self.update_model_button_text()
        if self.voice_dialog is not None:
            self.voice_dialog.retranslate()
        if self.persona_dialog is not None:
            self.persona_dialog.retranslate()
        self.refresh_personas_ui()
        key, kwargs = self._status
        self.set_status(key, **kwargs)
        self.display_chat()  # Redraw so the welcome screen and "You" labels switch too.

    def set_status(self, key, **kwargs):
        # Store the key (not the text) so a language switch can re-translate the status line.
        self._status = (key, kwargs)
        try:
            self.status.setText("• " + self.t(key, **kwargs))
        except Exception:
            pass

    def save_settings(self):
        core.save_settings(self.settings_file, dark_mode=self.dark_mode,
                           voice_enabled=self.voice_enabled, language=self.language,
                           voice_rate=self.voice_rate, voice_volume=self.voice_volume,
                           voice_id=self.voice_id, default_model=getattr(self, "current_model", DEFAULT_MODEL))

    def save_history(self):
        try:
            core.save_history(self.chats, self.history_file)
        except (OSError, TypeError, ValueError) as error:
            self.set_status("status_save_error", error=error)

    # ---------- Voice ----------
    def toggle_voice(self):
        self.voice_enabled = not self.voice_enabled
        if not self.voice_enabled:
            self.tts.stop()  # Muting silences the current reply immediately.
        self.voice_btn.setText(self.voice_button_text())
        self.save_settings()

    def stop_speaking(self):
        self.tts.stop()
        self.poll_speech()  # hide the Stop button right away

    def read_aloud(self, text):
        # Reads exactly this message. Works even when the sidebar Voice toggle is off,
        # because the user asked for it explicitly. Replaces anything already being read.
        if not self.tts.available:
            return
        self.tts.stop()
        self.tts.speak(plain_text(text))  # without ** # ` and other Markdown symbols
        self.poll_speech()

    def test_voice(self):
        # Plays a short sample with the current settings, even if the Voice toggle is off.
        if not self.tts.available:
            return
        self.tts.stop()
        self.tts.speak(self.t("voice_test_sample"))
        self.poll_speech()

    def poll_speech(self):
        # Keep the Stop button and the Read aloud buttons in step with the speech engine.
        self.stop_btn.setVisible(bool(self.tts.speaking))
        available = bool(self.tts.available)
        self.voice_settings_btn.setEnabled(available)
        for bubble in self.bubbles:
            if bubble.read_btn is not None:
                bubble.read_btn.setEnabled(available)

    def push_voice_settings(self):
        # Hand the current values to the speech thread. Cheap, so it is safe on every slider tick.
        self.tts.apply_settings(self.voice_rate, self.voice_volume, self.voice_id)

    def open_voice_settings(self):
        if not self.tts.available:
            return
        if self.voice_dialog is None:
            self.voice_dialog = VoiceSettingsDialog(self)
        self.voice_dialog.show()
        self.voice_dialog.raise_()
        self.voice_dialog.activateWindow()

    # ---------- Personas ----------
    def current_persona(self):
        chat = self.current_chat()
        persona_id = (chat.get("persona") if chat else None) or "default"
        return self.persona_manager.get(persona_id)

    def set_chat_persona(self, persona_id):
        chat = self.current_chat()
        if chat:
            chat["persona"] = persona_id
            self.save_history()
        self.refresh_personas_ui()

    def refresh_personas_ui(self):
        if not hasattr(self, "persona_combo"):
            return
        self.persona_combo.blockSignals(True)
        self.persona_combo.clear()
        personas = self.persona_manager.all()
        current_id = self.current_persona().id
        active_index = 0
        for idx, p in enumerate(personas):
            self.persona_combo.addItem(p.display_label, p.id)
            if p.id == current_id:
                active_index = idx
        self.persona_combo.setCurrentIndex(active_index)
        self.persona_combo.blockSignals(False)

    def on_persona_picked(self, index):
        if index < 0:
            return
        persona_id = self.persona_combo.itemData(index)
        if not persona_id:
            return
        chat = self.current_chat()
        if chat and chat.get("persona") != persona_id:
            chat["persona"] = persona_id
            self.save_history()

    def open_persona_manager(self):
        if self.persona_dialog is None:
            self.persona_dialog = PersonaDialog(self)
        self.persona_dialog.show()
        self.persona_dialog.raise_()
        self.persona_dialog.activateWindow()

    def open_automation_dialog(self):
        if getattr(self, "automation_dialog", None) is None:
            self.automation_dialog = AutomationDialog(self, self.task_store, self.scheduler)
        self.automation_dialog.show()
        self.automation_dialog.raise_()
        self.automation_dialog.activateWindow()

    def restore_from_tray(self):
        """Restore window when triggered from system tray."""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _execute_scheduled_task(self, task):
        chat = {
            "messages": [{"role": "user", "content": task.prompt}],
            "persona": task.persona or "default",
        }
        answer, is_error = core.ask_ollama(chat, self.language, persona=task.persona)
        return answer, is_error

    def on_scheduled_task_finished(self, task, answer, is_error):
        def handle():
            try:
                if self.closed:
                    return
                time_now = core.message_time_text()
                target = None
                if task.target_chat_id:
                    target = self.find_chat(task.target_chat_id)
                if not target:
                    target = next((c for c in self.chats if c.get("title") == "Automated Tasks"), None)
                    if not target:
                        target = core.new_chat()
                        target["title"] = "Automated Tasks"
                        self.chats.append(target)
                target["messages"].append({
                    "role": "user",
                    "content": f"⏰ [Scheduled: {task.title}]\n{task.prompt}",
                    "time": time_now,
                })
                bot_msg = {"role": "assistant", "content": answer, "time": time_now}
                if is_error:
                    bot_msg["is_error"] = True
                target["messages"].append(bot_msg)
                self.save_history()
                self.refresh_history_list()
                if self.current_id == target.get("id"):
                    self.display_chat()
                if hasattr(self, "automation_dialog") and self.automation_dialog:
                    self.automation_dialog.populate_tasks(select_id=task.id)
            except Exception:
                pass
        QTimer.singleShot(0, handle)

    # ---------- Chats ----------
    def make_draft(self):
        self.draft_chat = core.new_chat()
        self.draft_chat["model"] = getattr(self, "current_model", DEFAULT_MODEL)
        if hasattr(self, "persona_combo") and self.persona_combo.count() > 0:
            current_p = self.persona_combo.currentData()
            if current_p:
                self.draft_chat["persona"] = current_p
        self.current_id = self.draft_chat["id"]

    def current_chat(self):
        if self.draft_chat and self.draft_chat.get("id") == self.current_id:
            return self.draft_chat
        return self.find_chat(self.current_id)

    def find_chat(self, chat_id):
        return next((c for c in self.chats if c.get("id") == chat_id), None)

    def refresh_history_list(self):
        self.history_list.clear()
        query = self.search_entry.text().strip() if hasattr(self, "search_entry") else ""
        if not query:
            chats_to_show = list(reversed(self.chats))
            self.history_label.setText(self.t("history_label"))
        else:
            engine = SearchEngine(self.chats)
            results = engine.search(query)
            seen_ids = set()
            ordered_chats = []
            for r in results:
                if r.chat_id not in seen_ids:
                    seen_ids.add(r.chat_id)
                    chat = self.find_chat(r.chat_id)
                    if chat:
                        ordered_chats.append(chat)
            chats_to_show = ordered_chats
            if chats_to_show:
                self.history_label.setText(self.t("search_matches_count", count=len(chats_to_show)))
            else:
                self.history_label.setText(self.t("search_no_matches"))

        for chat in chats_to_show:  # newest first or ranked by search
            item = QListWidgetItem(core.list_title(chat.get("title", core.NEW_CHAT_TITLE)))
            item.setData(Qt.UserRole, chat["id"])
            item.setToolTip(chat.get("title", ""))
            self.history_list.addItem(item)
            if chat["id"] == self.current_id:
                item.setSelected(True)
                self.history_list.setCurrentItem(item)

    def new_chat(self):
        # A blank chat is temporary. It must not create a history entry until a message is sent.
        self.tts.stop()
        self.attachments = []
        self.render_attachment_chips()
        self.make_draft()
        self.refresh_history_list()
        self.display_chat()
        self.input_box.setFocus()

    def select_chat(self, item):
        chat_id = item.data(Qt.UserRole)
        if self.find_chat(chat_id) is not None and chat_id != self.current_id:
            self.tts.stop()
            self.current_id = chat_id
            self.draft_chat = None
            self.display_chat()

    def show_chat_menu(self, pos):
        item = self.history_list.itemAt(pos)
        if item is None:
            return None
        chat_id = item.data(Qt.UserRole)  # the menu acts on the chat that was clicked, not on its row
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.addAction(self.t("chat_menu_export"), lambda: self.export_chat_dialog(chat_id))
        menu.addSeparator()
        menu.addAction(self.t("chat_menu_rename"), lambda: self.rename_chat(chat_id))
        menu.addAction(self.t("chat_menu_delete"), lambda: self.delete_chat(chat_id))
        menu.popup(self.history_list.viewport().mapToGlobal(pos))
        return menu

    def export_chat_dialog(self, chat_id):
        chat = self.find_chat(chat_id)
        if not chat:
            return
        default_title = chat.get("title", "chat")
        clean_title = "".join(c for c in default_title if c.isalnum() or c in (" ", "_", "-")).strip() or "chat"
        default_file = os.path.join(os.path.expanduser("~"), f"{clean_title}.md")
        filter_str = "Markdown (*.md);;HTML (*.html);;JSON (*.json);;Plain Text (*.txt)"
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            self.t("export_dialog_title"),
            default_file,
            filter_str
        )
        if not file_path:
            return
        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        if not ext:
            if "html" in selected_filter.lower():
                ext = "html"
            elif "json" in selected_filter.lower():
                ext = "json"
            elif "txt" in selected_filter.lower():
                ext = "txt"
            else:
                ext = "md"
            file_path = f"{file_path}.{ext}"

        try:
            export_chat(chat, ext, file_path, dark_mode=self.dark_mode)
            QMessageBox.information(
                self,
                self.t("export_success_title"),
                self.t("export_success_msg", path=file_path)
            )
        except Exception as error:
            QMessageBox.warning(
                self,
                "Export Error",
                self.t("export_error_msg", error=str(error))
            )

    def ask_chat_title(self, initial):
        """Returns the text entered, or None if it was cancelled."""
        dialog = QInputDialog(self)
        dialog.setWindowTitle(self.t("rename_dialog_title"))
        dialog.setLabelText(self.t("rename_dialog_prompt"))
        dialog.setTextValue(initial)
        dialog.setOkButtonText(self.t("rename_save_btn"))
        dialog.setCancelButtonText(self.t("rename_cancel_btn"))
        return dialog.textValue() if dialog.exec() == QDialog.Accepted else None

    def confirm(self, title, message):
        return QMessageBox.question(self, title, message) == QMessageBox.Yes

    def rename_chat(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat is None:
            return
        new_title = self.ask_chat_title(chat.get("title", ""))
        if new_title is None:
            return  # cancelled
        new_title = core.tidy_title(new_title)
        if not new_title or new_title == chat.get("title"):
            return  # an empty name is ignored: the old title stays
        chat["title"] = new_title
        chat["is_custom_title"] = True
        self.save_history()
        self.refresh_history_list()

    def delete_chat(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat is None:
            return
        if not self.confirm(self.t("delete_chat_title"),
                            self.t("delete_chat_message", title=chat.get("title", ""))):
            return
        if getattr(self, "is_streaming", False) and getattr(self, "current_stream_chat_id", None) == chat_id:
            self.stop_generating()
        was_open = chat_id == self.current_id
        self.chats = [saved for saved in self.chats if saved.get("id") != chat_id]  # only this chat goes
        if was_open:
            self.tts.stop()
            self.make_draft()  # deleting the open chat leaves a fresh empty one
        self.save_history()
        self.refresh_history_list()
        if was_open:
            self.display_chat()

    def clear_chat(self):
        chat = self.current_chat()
        if not chat or not chat.get("messages"):
            return  # Nothing saved to clear; leave the empty draft alone.
        if not self.confirm(self.t("clear_dialog_title"), self.t("clear_dialog_message")):
            return
        if getattr(self, "is_streaming", False) and getattr(self, "current_stream_chat_id", None) == chat.get("id"):
            self.stop_generating()
        self.tts.stop()
        self.attachments = []
        self.render_attachment_chips()
        self.chats = [saved for saved in self.chats if saved.get("id") != chat.get("id")]
        self.make_draft()
        self.save_history()
        self.refresh_history_list()
        self.display_chat()

    # ---------- Messages ----------
    def get_chat_tree(self, chat):
        if not chat:
            return ConversationTree()
        messages = chat.get("messages", [])
        if "tree" not in chat or not isinstance(chat["tree"], dict):
            tree = ConversationTree.from_linear_messages(messages)
            chat["tree"] = tree.to_dict()
            return tree

        tree = ConversationTree.from_dict(chat["tree"])
        active_msgs = tree.get_active_messages()
        # If messages array was directly mutated (e.g. in test fixtures or scripts), resync tree
        if len(messages) != len(active_msgs) or any(
            m.get("content") != am.get("content") or m.get("role") != am.get("role")
            for m, am in zip(messages, active_msgs)
        ):
            tree = ConversationTree.from_linear_messages(messages)
            chat["tree"] = tree.to_dict()

        return tree

    def clear_messages(self):
        self.bubbles = []
        while self.messages_layout.count() > 1:  # keep the stretch at the end
            widget = self.messages_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()

    def display_chat(self):
        self.clear_messages()
        self.refresh_history_list()
        chat = self.current_chat()
        if hasattr(self, "persona_combo"):
            persona_id = (chat.get("persona") if chat else None) or "default"
            idx = self.persona_combo.findData(persona_id)
            if idx >= 0:
                self.persona_combo.blockSignals(True)
                self.persona_combo.setCurrentIndex(idx)
                self.persona_combo.blockSignals(False)
        tree = self.get_chat_tree(chat) if chat else None
        active_path = tree.get_active_path() if tree else []
        messages = chat.get("messages", []) if chat else []
        if not messages:
            self.show_welcome()
        for idx, msg in enumerate(messages):
            node_id = msg.get("node_id")
            if not node_id and idx < len(active_path):
                node_id = active_path[idx].id
            siblings_info = tree.get_siblings(node_id) if (tree and node_id) else (0, 1, [])
            self.add_bubble(
                msg.get("role"),
                msg.get("content", ""),
                timestamp=msg.get("time"),
                node_id=node_id,
                siblings_info=siblings_info,
                metrics=msg.get("metrics"),
            )
        self.scroll_to_bottom()

    def show_welcome(self):
        # Welcome splash shown on launch and for each empty New Chat.
        welcome = QWidget()
        layout = QVBoxLayout(welcome)
        layout.setContentsMargins(0, 24, 0, 0)
        layout.setSpacing(6)
        pixmap = QPixmap(os.path.join(HERE, "jugnu_bot.png"))
        if pixmap.isNull():
            # Friendly fallback if the companion image hasn't been copied beside the script.
            image = QLabel("🤖", objectName="welcomeEmoji")
        else:
            image = QLabel()
            image.setPixmap(pixmap.scaled(360, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        for widget in (image, QLabel(self.t("welcome_title"), objectName="welcomeTitle"),
                       QLabel("Just an Unusually Genius Neural Utility", objectName="welcomeTagline"),
                       QLabel(self.t("welcome_prompt"), objectName="welcomePrompt")):
            layout.addWidget(widget, 0, Qt.AlignHCenter)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, welcome)

    def add_bubble(self, role, text, timestamp=None, node_id=None, siblings_info=(0, 1, []), metrics=None):
        if not self.bubbles and self.messages_layout.count() > 1:
            self.clear_messages()  # the first message replaces the welcome splash
        bubble = Bubble(self, role, text, timestamp, node_id=node_id, siblings_info=siblings_info, metrics=metrics)
        bubble.fit(max_bubble_width(self.scroll.viewport().width()))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(row)
        if role == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, holder)
        self.bubbles.append(bubble)
        return bubble

    def fit_bubbles(self):
        width = max_bubble_width(self.scroll.viewport().width())
        for bubble in self.bubbles:
            bubble.fit(width)

    # ---------- Sending ----------
    def start_task(self, work, slot):
        """Run `work()` on a QThread; `slot(result)` then runs on the GUI thread."""
        self.tasks = [task for task in self.tasks if task.isRunning()]
        task = Task(work)
        task.done.connect(slot)
        self.tasks.append(task)
        task.start()

    def wait_for_tasks(self, msecs):
        """Wait up to `msecs` for running requests. True when none is left."""
        return all(task.wait(msecs) for task in self.tasks)

    def warm_up(self):
        if self.closed:
            return  # the window was closed before the delayed warm-up started
        self.start_task(core.warm_up, self.on_warmed)

    def on_warmed(self, ok):
        self.set_status("status_ready" if ok else "status_offline")

    # ---------- Attachments ----------
    def pick_attachments(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            self.t("attach_dialog_title"),
            "",
            "All Files (*);;Text/Code Files (*.py *.js *.ts *.txt *.md *.json *.html *.css)",
        )
        if not file_paths:
            return
        for file_path in file_paths:
            try:
                if any(os.path.abspath(a.path) == os.path.abspath(file_path) for a in self.attachments):
                    continue
                doc = ingest_file(file_path)
                self.attachments.append(doc)
            except AttachmentError as err:
                QMessageBox.warning(self, self.t("attach_error_title"), str(err))
        self.render_attachment_chips()

    def remove_attachment(self, index):
        if 0 <= index < len(self.attachments):
            del self.attachments[index]
        self.render_attachment_chips()

    def render_attachment_chips(self):
        while self.attachments_layout.count():
            item = self.attachments_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not self.attachments:
            self.attachments_container.setVisible(False)
            return

        self.attachments_container.setVisible(True)
        for idx, doc in enumerate(self.attachments):
            chip = QFrame()
            chip.setObjectName("attachmentChip")
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(8, 4, 8, 4)
            chip_layout.setSpacing(6)

            trunc_tag = " (truncated)" if doc.is_truncated else ""
            label = QLabel(f"📄 {doc.filename} ({doc.formatted_size}){trunc_tag}")
            chip_layout.addWidget(label)

            del_btn = QPushButton("✕")
            del_btn.setFixedSize(18, 18)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setToolTip("Remove attachment")
            del_btn.clicked.connect(lambda checked=False, i=idx: self.remove_attachment(i))
            chip_layout.addWidget(del_btn)

            self.attachments_layout.addWidget(chip)

        self.attachments_layout.addStretch(1)

    def send_message(self):
        if self.busy:
            return
        user_text = self.input_box.toPlainText().strip()
        has_attachments = bool(self.attachments)
        if not user_text and not has_attachments:
            return
        self.tts.stop()  # A new message interrupts any reply still being read aloud.
        chat = self.current_chat()
        if not chat:
            self.make_draft()
            chat = self.current_chat()
        if self.draft_chat and chat.get("id") == self.draft_chat.get("id"):
            self.chats.append(self.draft_chat)
            self.draft_chat = None
        if chat.get("title") == core.NEW_CHAT_TITLE and not chat.get("is_custom_title"):
            if core.local_answer(user_text, self.language) is not None:
                chat["title"] = user_text[:core.MAX_LIST_TITLE]
            elif user_text:
                chat["title"] = generate_heuristic_title(user_text)
                self.start_ai_titling(chat["id"], user_text)
            else:
                default_title = f"Attachment: {self.attachments[0].filename}" if self.attachments else "Chat"
                chat["title"] = default_title[:core.MAX_LIST_TITLE]
        time_now = core.message_time_text()
        prompt_with_attachments = (
            assemble_prompt_with_attachments(user_text, self.attachments)
            if has_attachments
            else user_text
        )
        self.attachments = []
        self.render_attachment_chips()

        tree = self.get_chat_tree(chat)
        active_path = tree.get_active_path()
        parent_id = active_path[-1].id if active_path else None
        user_node = tree.add_node("user", prompt_with_attachments, parent_id=parent_id, timestamp=time_now)
        chat["tree"] = tree.to_dict()
        chat["messages"] = tree.get_active_messages()
        self.input_box.clear()
        siblings_info = tree.get_siblings(user_node.id)
        self.add_bubble("user", prompt_with_attachments, timestamp=time_now, node_id=user_node.id, siblings_info=siblings_info)
        self.scroll_to_bottom()
        self.refresh_history_list()
        self.save_history()
        self.busy = True
        self.send_btn.setEnabled(False)
        self.set_status("status_thinking")

        # Clock/date and arithmetic questions are answered locally—no model call required.
        if not has_attachments:
            answer = core.local_answer(user_text, self.language)
            if answer is not None:
                self.finish_answer(chat["id"], answer, False, parent_node_id=user_node.id)
                return
        self.ask_ollama(chat["id"], parent_node_id=user_node.id)

    def create_streaming_bubble(self, chat_id, timestamp=None):
        if getattr(self, "current_id", None) != chat_id:
            self.streaming_bubble_info = None
            return None

        c = getattr(self, "colors", LIGHT)
        row = tk.Frame(self.messages_frame, bg=c["bg"])
        row.pack(fill="x", pady=7, padx=8)
        bubble = tk.Frame(row, bg=c["assistant"])
        bubble.pack(side="left", padx=(0, 40))
        header = tk.Frame(bubble, bg=bubble["bg"])
        header.pack(fill="x", padx=12, pady=(8, 2))

        name_lbl = tk.Label(header, text="🤖 JUGNU", bg=bubble["bg"], fg=c["accent"],
                            font=("Segoe UI", 9, "bold"), anchor="w")
        name_lbl.pack(side="left")

        time_lbl = None
        if timestamp:
            time_lbl = tk.Label(header, text=timestamp, bg=bubble["bg"], fg=c["muted"],
                                font=("Segoe UI", 8), anchor="w")
            time_lbl.pack(side="left", padx=(8, 0))

        read_btn = None
        if hasattr(self, "tts"):
            read_btn = tk.Button(header, text="🔊", command=lambda: None,
                                 relief="flat", bd=0, font=("Segoe UI Emoji", 10), cursor="hand2",
                                 bg=bubble["bg"], fg=c["accent"], activebackground=bubble["bg"],
                                 activeforeground=c["accent2"], disabledforeground=c["muted"],
                                 state="disabled")
            read_btn.pack(side="right", padx=(4, 0))
            if hasattr(self, "read_buttons"):
                self.read_buttons.append(read_btn)

        copy_btn = tk.Button(header, text="⧉ Copy", font=("Segoe UI", 8, "bold"),
                             relief="flat", bd=0, highlightthickness=0,
                             bg=bubble["bg"], fg=c["muted"],
                             highlightbackground=bubble["bg"],
                             activebackground=c["border"], activeforeground=c["accent"],
                             cursor="hand2", padx=6, pady=1, state="disabled")
        copy_btn.pack(side="right", padx=(8, 0))

        hover_bg = c["border"]

        def on_btn_enter(_event):
            try:
                if "Copied" not in copy_btn.cget("text"):
                    copy_btn.configure(fg=c["accent"], bg=hover_bg)
            except tk.TclError:
                pass

        def on_btn_leave(_event):
            try:
                if "Copied" not in copy_btn.cget("text"):
                    copy_btn.configure(fg=c["muted"], bg=bubble["bg"])
                else:
                    copy_btn.configure(bg=bubble["bg"])
            except tk.TclError:
                pass

        copy_btn.bind("<Enter>", on_btn_enter)
        copy_btn.bind("<Leave>", on_btn_leave)

        txt = tk.Text(bubble, font=self.bubble_font, wrap="word", width=18,
                      relief="flat", bd=0, highlightthickness=0,
                      bg=bubble["bg"], fg=c["text"],
                      selectbackground=c["accent"], selectforeground="white",
                      inactiveselectbackground=c["accent"], cursor="xterm", height=1)
        txt.pack(fill="x", padx=12, pady=(2, 10))

        widgets_to_bind = [row, bubble, header, name_lbl, copy_btn, txt]
        if read_btn:
            widgets_to_bind.append(read_btn)
        if time_lbl:
            widgets_to_bind.append(time_lbl)
        for w in widgets_to_bind:
            w.bind("<MouseWheel>", self.on_mousewheel)
            w.bind("<Button-4>", self.on_mousewheel)
            w.bind("<Button-5>", self.on_mousewheel)

        self.streaming_bubble_info = {
            "chat_id": chat_id,
            "row": row,
            "bubble": bubble,
            "header": header,
            "txt": txt,
            "copy_btn": copy_btn,
            "read_btn": read_btn,
            "timestamp": timestamp,
        }
        if hasattr(self, "canvas"):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self.canvas.yview_moveto(1.0)
        return self.streaming_bubble_info

    def append_stream_token(self, chat_id, token):
        if not token:
            return
        if getattr(self, "streaming_bubble_info", None) and self.streaming_bubble_info.get("chat_id") == chat_id:
            txt = self.streaming_bubble_info["txt"]
            try:
                txt.configure(state="normal")
                txt.insert("end", token)
                full_text = txt.get("1.0", "end - 1c")
                canvas_w = self.current_canvas_width(fallback=580)
                calc_cols = self.compute_bubble_cols(full_text, canvas_w)
                txt.configure(width=calc_cols)
                self.root.update_idletasks()
                h = self.fit_bubble_height(txt, full_text)
                txt.configure(height=h, state="disabled")
                if hasattr(self, "canvas"):
                    self.canvas.configure(scrollregion=self.canvas.bbox("all"))
                    self.canvas.yview_moveto(1.0)
            except tk.TclError:
                pass

    def finish_streaming(self, chat_id, accumulated_text, is_error=False, error_message=None):
        self.busy = False
        self.is_streaming = False
        self.current_stream_chat_id = None
        self.current_stream_response = None
        if hasattr(self, "stop_event") and self.stop_event is not None:
            self.stop_event.clear()

        # Hide Stop button and restore Send button
        if hasattr(self, "stop_gen_btn"):
            try:
                self.stop_gen_btn.pack_forget()
            except Exception:
                pass
        if hasattr(self, "send_btn"):
            try:
                self.send_btn.configure(state="normal")
            except Exception:
                pass
        if hasattr(self, "set_status"):
            self.set_status("status_ready")

        chat = next((ct for ct in self.chats if ct.get("id") == chat_id), None)
        timestamp = message_time_text()

        # Case 1: Error or no non-whitespace text generated
        if is_error or not accumulated_text.strip():
            if getattr(self, "streaming_bubble_info", None) and self.streaming_bubble_info.get("chat_id") == chat_id:
                try:
                    self.streaming_bubble_info["row"].destroy()
                except (tk.TclError, KeyError):
                    pass
                self.streaming_bubble_info = None

            notice = error_message or get_string(self.language, "error_empty_reply")
            if chat:
                chat["messages"].append({
                    "role": "assistant",
                    "content": notice,
                    "time": timestamp,
                    "is_error": True
                })
                self.save_history()

            if chat and chat.get("id") == self.current_id:
                self.add_bubble("assistant", notice, timestamp=timestamp)
                if hasattr(self, "canvas"):
                    self.canvas.yview_moveto(1.0)
            if hasattr(self, "refresh_history_list"):
                self.refresh_history_list()
            return

        # Case 2: Generated tokens present (completed normally, stopped, or partial before error)
        final_text = accumulated_text.strip()
        if final_text and chat:
            msg = {
                "role": "assistant",
                "content": final_text,
                "time": timestamp
            }
            if is_error:
                msg["is_error"] = True
            chat["messages"].append(msg)
            self.save_history()

        # Finalize the visual bubble on screen if still viewing this chat
        if getattr(self, "streaming_bubble_info", None) and self.streaming_bubble_info.get("chat_id") == chat_id:
            info = self.streaming_bubble_info
            self.streaming_bubble_info = None
            measure_text = final_text
            try:
                txt = info["txt"]
                txt.configure(state="normal")
                txt.delete("1.0", "end")
                segments = render_markdown(final_text)
                canvas_w = self.current_canvas_width(fallback=580)
                measure_text, _ = self.fill_bubble_text(txt, final_text, segments)
                calc_cols = self.compute_bubble_cols(measure_text, canvas_w)
                txt.configure(width=calc_cols)
                self.root.update_idletasks()
                h = self.fit_bubble_height(txt, measure_text)
                txt.configure(height=h, state="disabled")
                copy_btn = info["copy_btn"]
                copy_btn.configure(
                    state="normal",
                    command=lambda t=final_text, b=copy_btn: self.copy_to_clipboard(t, b)
                )
                read_btn = info["read_btn"]
                if read_btn and hasattr(self, "tts") and self.tts.available:
                    read_btn.configure(
                        state="normal",
                        command=lambda t=final_text: self.read_aloud(t)
                    )
                if hasattr(self, "bubble_items"):
                    self.bubble_items.append((info["bubble"], txt, measure_text))
            except (tk.TclError, KeyError):
                pass

            if getattr(self, "voice_enabled", False) and not is_error:
                if hasattr(self, "tts"):
                    self.tts.speak(plain_text(final_text))

        elif chat and chat.get("id") == self.current_id and final_text:
            self.add_bubble("assistant", final_text, timestamp=timestamp)
            if hasattr(self, "canvas"):
                self.canvas.yview_moveto(1.0)
            if getattr(self, "voice_enabled", False) and not is_error:
                if hasattr(self, "tts"):
                    self.tts.speak(plain_text(final_text))

        if hasattr(self, "refresh_history_list"):
            self.refresh_history_list()

    # ---------- Models ----------
    def load_model_setting(self):
        return core.load_model_setting(self.settings_file)

    def update_model_button_text(self):
        if hasattr(self, "model_btn"):
            current = getattr(self, "current_model", DEFAULT_MODEL)
            self.model_btn.setText(self.t("model_btn", name=current) + " ▾")

    def show_model_menu(self):
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        header_action = menu.addAction(f"  {self.t('model_menu_title')}")
        header_action.setEnabled(False)
        menu.addSeparator()

        models = fetch_local_models()
        if not models:
            empty_action = menu.addAction(self.t("model_no_models_found"))
            empty_action.setEnabled(False)
        else:
            for m in models:
                mark = "✓ " if m.name == self.current_model else "   "
                action = menu.addAction(f"{mark}{m.display_name}")
                action.triggered.connect(lambda checked=False, name=m.name: self.switch_model(name))

        menu.addSeparator()
        menu.addAction(self.t("model_menu_refresh"), self.refresh_models)
        menu.addAction(self.t("model_menu_pull"), self.open_pull_dialog)
        menu.popup(self.model_btn.mapToGlobal(self.model_btn.rect().bottomLeft()))

    def refresh_models(self):
        self.start_task(fetch_local_models, lambda _models: self.show_model_menu())

    def switch_model(self, model_name):
        self.current_model = model_name
        self.update_model_button_text()
        self.save_settings()
        self.set_status("model_status_switching", name=model_name)
        def reset():
            try:
                if not getattr(self, "closed", False) and hasattr(self, "status"):
                    self.set_status("status_ready")
            except Exception:
                pass
        QTimer.singleShot(1500, reset)

    def open_pull_dialog(self):
        dialog = ModelPullDialog(self)
        dialog.exec()

    def ask_ollama(self, chat_id, parent_node_id=None):
        # The worker gets a copy, so it never reads the list while the GUI changes it.
        chat_doc = self.find_chat(chat_id)
        if chat_doc is None and self.draft_chat and self.draft_chat.get("id") == chat_id:
            chat_doc = self.draft_chat
        chat = {
            "messages": [dict(m) for m in (chat_doc["messages"] if chat_doc else [])],
            "persona": chat_doc.get("persona", "default") if chat_doc else "default",
            "model": (chat_doc.get("model") if chat_doc else None) or getattr(self, "current_model", DEFAULT_MODEL),
        }
        language = self.language  # The prompt and any error text use the language chosen at send time.
        def work():
            reply = core.ask_ollama(chat, language)
            metrics = getattr(reply, "metrics", None)
            return (chat_id, reply[0], reply[1], parent_node_id, metrics)
        self.start_task(work, self.on_answer)

    def on_answer(self, result):
        self.finish_answer(*result)

    def finish_answer(self, chat_id, answer, is_error=False, parent_node_id=None, metrics=None):
        chat = self.find_chat(chat_id)  # may be gone if it was deleted while the request ran
        time_now = core.message_time_text()
        if chat and isinstance(answer, str) and answer.strip():
            tree = self.get_chat_tree(chat)
            if parent_node_id is None:
                active_path = tree.get_active_path()
                parent_node_id = active_path[-1].id if active_path else None
            new_node = tree.add_node("assistant", answer, parent_id=parent_node_id,
                                     timestamp=time_now, is_error=is_error)
            if metrics:
                new_node.metrics = metrics.to_dict() if hasattr(metrics, "to_dict") else metrics
            chat["tree"] = tree.to_dict()
            chat["messages"] = tree.get_active_messages()
            if metrics and chat["messages"]:
                chat["messages"][-1]["metrics"] = metrics.to_dict() if hasattr(metrics, "to_dict") else metrics
            self.save_history()
            # Only display if the user is still viewing the chat that received the answer.
            if chat_id == self.current_id:
                siblings_info = tree.get_siblings(new_node.id)
                if siblings_info[1] > 1:
                    self.display_chat()
                else:
                    self.add_bubble("assistant", answer, timestamp=time_now,
                                    node_id=new_node.id, siblings_info=siblings_info, metrics=metrics)
                    self.scroll_to_bottom()
                if self.voice_enabled:
                    self.tts.speak(plain_text(answer))
        self.busy = False
        self.send_btn.setEnabled(True)
        self.set_status("status_ready")
        self.refresh_history_list()

    def switch_message_branch(self, chat_id, node_id, new_index):
        chat = self.find_chat(chat_id)
        if not chat:
            return
        tree = self.get_chat_tree(chat)
        if tree.switch_branch(node_id, new_index):
            chat["tree"] = tree.to_dict()
            chat["messages"] = tree.get_active_messages()
            self.save_history()
            if chat_id == self.current_id:
                self.display_chat()

    def regenerate_response(self, node_id):
        if self.busy:
            return
        chat = self.current_chat()
        if not chat:
            return
        tree = self.get_chat_tree(chat)
        target_node = tree.get_node(node_id)
        if not target_node or not target_node.parent_id:
            return
        parent_id = target_node.parent_id
        sub_tree = tree.fork_tree(parent_id)
        sub_messages = sub_tree.get_active_messages()
        if not sub_messages:
            return

        self.busy = True
        self.send_btn.setEnabled(False)
        self.set_status("status_thinking")

        def work():
            fake_chat = {"messages": sub_messages}
            return (chat["id"], *core.ask_ollama(fake_chat, self.language), parent_id)

        def on_regen_done(res):
            self.finish_answer(*res)

        self.start_task(work, on_regen_done)

    def edit_user_message(self, node_id, original_text):
        if self.busy:
            return
        chat = self.current_chat()
        if not chat:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle(self.t("edit_dialog_title"))
        dialog.setLabelText(self.t("edit_dialog_prompt"))
        dialog.setTextValue(original_text)
        if dialog.exec() != QDialog.Accepted:
            return
        new_text = dialog.textValue().strip()
        if not new_text or new_text == original_text:
            return

        tree = self.get_chat_tree(chat)
        target_node = tree.get_node(node_id)
        parent_id = target_node.parent_id if target_node else None

        time_now = core.message_time_text()
        user_node = tree.add_node("user", new_text, parent_id=parent_id, timestamp=time_now)
        chat["tree"] = tree.to_dict()
        chat["messages"] = tree.get_active_messages()
        self.save_history()
        self.display_chat()

        local_ans = core.local_answer(new_text, self.language)
        if local_ans is not None:
            self.finish_answer(chat["id"], local_ans, False, user_node.id)
            return

        self.busy = True
        self.send_btn.setEnabled(False)
        self.set_status("status_thinking")
        sub_tree = tree.fork_tree(user_node.id)
        sub_messages = sub_tree.get_active_messages()

        def work():
            fake_chat = {"messages": sub_messages}
            return (chat["id"], *core.ask_ollama(fake_chat, self.language), user_node.id)

        def on_edit_done(res):
            self.finish_answer(*res)

        self.start_task(work, on_edit_done)

    def fork_chat_at_node(self, node_id):
        chat = self.current_chat()
        if not chat:
            return
        tree = self.get_chat_tree(chat)
        forked_tree = tree.fork_tree(node_id)
        forked_messages = forked_tree.get_active_messages()
        if not forked_messages:
            return
        base_title = chat.get("title", "Chat")
        new_title = f"[Fork] {base_title}"[:core.MAX_LIST_TITLE]
        new_chat = {
            "id": str(uuid.uuid4()),
            "title": new_title,
            "messages": forked_messages,
            "tree": forked_tree.to_dict(),
        }
        self.chats.append(new_chat)
        self.current_id = new_chat["id"]
        self.save_history()
        self.refresh_history_list()
        self.display_chat()


    def toggle_performance_hud(self):
        self.show_performance_hud = not self.show_performance_hud
        core.save_performance_hud(self.settings_file, self.show_performance_hud)
        self.display_chat()

    def show_metrics_dialog(self, metrics):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(self.t("hud_dialog_title"))
        layout = QVBoxLayout(dlg)
        for label, val in [
            (self.t("hud_speed"), f"{metrics.tps:.1f} tokens/s"),
            (self.t("hud_tokens"), f"{metrics.eval_count}"),
            (self.t("hud_total_time"), f"{metrics.total_duration_s:.2f} s"),
            (self.t("hud_ttft"), f"{metrics.prompt_eval_duration_s:.2f} s"),
            (self.t("hud_model"), f"{metrics.model or self.current_model}"),
        ]:
            row = QLabel(f"<b>{label}:</b>  {val}")
            layout.addWidget(row)
        bbox = QDialogButtonBox(QDialogButtonBox.Ok)
        bbox.accepted.connect(dlg.accept)
        layout.addWidget(bbox)
        dlg.exec()

    def start_ai_titling(self, chat_id, first_user_prompt):
        def work():
            return generate_ai_title(first_user_prompt)
        def slot(ai_title):
            if ai_title:
                chat = self.find_chat(chat_id)
                if chat and not chat.get("is_custom_title"):
                    chat["title"] = ai_title
                    self.save_history()
                    self.refresh_history_list()
        self.start_task(work, slot)

    def closeEvent(self, event):
        self.closed = True
        self.speech_timer.stop()
        self.tts.shutdown()
        if hasattr(self, "scheduler") and self.scheduler:
            self.scheduler.stop(timeout=1.0)
        if hasattr(self, "tray_icon") and self.tray_icon:
            self.tray_icon.hide()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("JUGNU")
    lock = HistoryLock(core.HISTORY_FILE)
    try:
        lock.acquire()
    except HistoryInUseError:
        QMessageBox.critical(None, "Chat history in use",
                             "Another JUGNU window is using this chat history. "
                             "Close that window before opening another.")
        return 1
    except OSError as error:
        QMessageBox.critical(None, "Chat history unavailable", f"Could not lock chat history.\n\n{error}")
        return 1
    try:
        window = JugnuWindow()
        window.show()
        code = app.exec()
        finished = window.wait_for_tasks(2000)
    finally:
        lock.release()
    if not finished:
        # A request is still waiting on Ollama. A running QThread aborts the process when it is
        # destroyed, so leave right away; the history was already saved when the message was sent.
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
