import tkinter as tk
from tkinter import messagebox, filedialog
from tkinter import font as tkfont
import math
import json
import threading
import uuid
import os
import sys
import tempfile
import ast
import re

from core import (
    ERROR_CONNECTION, ERROR_INVALID, ERROR_TIMEOUT, build_messages, calculate,
    chat as ollama_chat, is_time_question as detect_time_question, local_datetime_text, local_now,
    stream_chat,
)
from context_ingest import (
    AttachedDocument,
    AttachmentError,
    assemble_prompt_with_attachments,
    ingest_file,
)
from strings import build_system_prompt, get_string, next_language, normalize_language
from tts import TTSManager, DEFAULT_RATE, DEFAULT_VOLUME, RATE_MIN, RATE_MAX, clamp
from history_lock import HistoryInUseError, HistoryLock
from markdown_render import render_markdown, plain_text

APP_TITLE = "JUGNU | Just an Unusually Genius Neural Utility"
HISTORY_FILE = "jugnu_history.json"
SETTINGS_FILE = "jugnu_settings.json"
MAX_LIST_TITLE = 34    # longer chat titles are shortened with "…" in the sidebar list
MAX_TITLE_LENGTH = 60  # longest title a user can give a chat
# Markdown styles that indent their lines; bubbles get a few extra columns so those lines do not wrap early.
INDENT_TAGS = ("code", "codehead", "quote") + tuple("li%d" % n for n in range(5))


class InvalidOllamaResponse(ValueError):
    """The decoded response does not contain a usable chat message."""


def ollama_answer(data):
    if not isinstance(data, dict):
        raise InvalidOllamaResponse()
    message = data.get("message")
    if not isinstance(message, dict):
        raise InvalidOllamaResponse()
    content = message.get("content")
    if not isinstance(content, str):
        raise InvalidOllamaResponse()
    return content.strip()


def ollama_stream_chunk(data):
    """Extract (token, is_done) from an Ollama stream chunk dictionary.

    Raises InvalidOllamaResponse if data is not a dictionary or has an invalid structure.
    """
    if not isinstance(data, dict):
        raise InvalidOllamaResponse()
    done = bool(data.get("done", False))
    if done and "message" not in data:
        return "", True

    message = data.get("message")
    if not isinstance(message, dict):
        raise InvalidOllamaResponse()
    content = message.get("content")
    if not isinstance(content, str):
        raise InvalidOllamaResponse()
    return content, done


def iter_response_lines(response):
    """Yield lines from a requests response, supporting both live streaming and mock _content."""
    if getattr(response, "raw", None) is not None:
        yield from response.iter_lines()
    elif getattr(response, "_content", None):
        for line in response._content.splitlines():
            yield line

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
        self.is_streaming = False
        self.current_stream_chat_id = None
        self.current_stream_response = None
        self.stop_event = threading.Event()
        self.streaming_bubble_info = None
        self.attachments = []

        self.build_ui()
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<Control-o>", lambda event: self.pick_attachments())
        self.root.bind("<Control-O>", lambda event: self.pick_attachments())
        if self.chats:
            self.current_id = self.chats[-1]["id"]  # Reopen most recent saved conversation.
        else:
            self.make_draft()
        self.apply_language()
        self.apply_theme()
        self.root.after(250, self.warm_up)
        self.poll_speech()

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

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({"dark_mode": self.dark_mode, "voice_enabled": self.voice_enabled,
                           "language": self.language, "voice_rate": self.voice_rate,
                           "voice_volume": self.voice_volume, "voice_id": self.voice_id}, f)
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
        if not self.tts.available:
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
        self.attachments_frame = tk.Frame(self.bottom)
        self.attach_btn = Button(self.bottom, command=self.pick_attachments,
                                 relief="flat", font=("Segoe UI", 10, "bold"), padx=12, pady=12)
        self.attach_btn.pack(side="left", padx=(0, 8))
        self.input_box = tk.Text(self.bottom, height=2, wrap="word", font=("Segoe UI", 11),
                                 relief="flat", padx=10, pady=10, undo=True)
        self.input_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.input_box.bind("<Control-Return>", self.send_message)
        self.input_box.bind("<Return>", self.send_message)
        self.input_box.bind("<Shift-Return>", lambda event: None)
        self.send_btn = Button(self.bottom, command=self.send_message,
                               relief="flat", font=("Segoe UI", 10, "bold"), padx=18, pady=12)
        self.send_btn.pack(side="right")
        self.stop_gen_btn = Button(self.bottom, command=self.stop_generating,
                                   relief="flat", font=("Segoe UI", 10, "bold"), padx=16, pady=12)

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
            (self.canvas, c["bg"]), (self.messages_frame, c["bg"]), (self.bottom, c["bg"])
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
        self.send_btn.configure(bg=c["accent"], fg="white", activebackground=c["accent2"],
                                activeforeground="white", cursor="hand2")
        if hasattr(self, "attach_btn"):
            self.attach_btn.configure(bg=c["accent"], fg="white", activebackground=c["accent2"],
                                      activeforeground="white", cursor="hand2")
        if hasattr(self, "attachments_frame"):
            self.attachments_frame.configure(bg=c["bg"])
        if hasattr(self, "stop_gen_btn"):
            self.stop_gen_btn.configure(bg=c["danger"], fg=c["accent2"], activebackground=c["danger"],
                                        activeforeground=c["text"], cursor="hand2")
        if hasattr(self, "render_attachment_chips"):
            self.render_attachment_chips()
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
        state = "normal" if self.tts.available else "disabled"
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
        if not self.tts.available:
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
        w["close"] = Button(win, command=self.close_voice_settings, relief="flat", pady=8)
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
        w["close"].pack(fill="x", padx=20, pady=(0, 18))
        self.update_voice_window()

    def update_voice_window(self):
        # Re-apply text (language) and colours (theme) to the window if it is open.
        if self.voice_win is None:
            return
        w, c = self.vw, self.colors
        w["win"].title(self.t("voice_settings_title"))
        w["win"].configure(bg=c["bg"])
        self.update_voice_value_labels()
        w["voice_label"].configure(text=self.t("voice_choice_label"))
        w["test"].configure(text=self.t("voice_test_btn"))
        w["close"].configure(text=self.t("voice_close_btn"))
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
        w["close"].configure(bg=c["danger"], fg=c["accent2"], activebackground=c["danger"],
                             activeforeground=c["text"], cursor="hand2")
        # The default entry's label depends on the language, so rebuild the dropdown list.
        labels, ids = self.voice_labels()
        menu = w["voice"]["menu"]
        menu.delete(0, "end")
        for label in labels:
            menu.add_command(label=label, command=lambda lb=label: self.on_voice_pick(lb))
        index = ids.index(self.voice_id) if self.voice_id in ids else 0
        self.voice_choice.set(labels[index])

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
        if getattr(self, "is_streaming", False):
            self.stop_generating()
        if self._resize_timer:
            try:
                self.root.after_cancel(self._resize_timer)
            except Exception:
                pass
        self.tts.shutdown()
        self.root.destroy()

    def _on_escape(self, _event=None):
        if getattr(self, "is_streaming", False):
            self.stop_generating()

    def stop_generating(self):
        """Abort an in-progress Ollama stream generation."""
        if not getattr(self, "is_streaming", False):
            return
        if hasattr(self, "stop_event") and self.stop_event is not None:
            self.stop_event.set()
        if getattr(self, "current_stream_response", None) is not None:
            try:
                self.current_stream_response.close()
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
        self.send_btn.configure(text=self.t("send_btn"))
        if hasattr(self, "attach_btn"):
            self.attach_btn.configure(text=self.t("attach_file_btn"))
        if hasattr(self, "stop_gen_btn"):
            self.stop_gen_btn.configure(text=self.t("stop_btn"))
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
        self.attachments = []
        if hasattr(self, "render_attachment_chips"):
            self.render_attachment_chips()
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
            self.attachments = []
            if hasattr(self, "render_attachment_chips"):
                self.render_attachment_chips()
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
        self.save_history()
        self.refresh_history_list()

    def delete_chat(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat is None:
            return
        if not messagebox.askyesno(self.t("delete_chat_title"),
                                   self.t("delete_chat_message", title=chat.get("title", ""))):
            return
        if getattr(self, "is_streaming", False) and getattr(self, "current_stream_chat_id", None) == chat_id:
            self.stop_generating()
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

    def clear_chat(self):
        chat = self.current_chat()
        if not chat or not chat.get("messages"):
            # Nothing saved to clear; leave the empty draft alone.
            return
        if not messagebox.askyesno(self.t("clear_dialog_title"), self.t("clear_dialog_message")):
            return
        if getattr(self, "is_streaming", False) and getattr(self, "current_stream_chat_id", None) == chat.get("id"):
            self.stop_generating()
        self.tts.stop()
        self.attachments = []
        if hasattr(self, "render_attachment_chips"):
            self.render_attachment_chips()
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

    # ---------- Document Attachments & Context Ingestion ----------
    def pick_attachments(self):
        """Open a file dialog to select local document(s) or code file(s)."""
        file_paths = filedialog.askopenfilenames(
            title=self.t("attach_dialog_title"),
            filetypes=[
                ("All Supported Files", "*.*"),
                ("Code & Scripts", "*.py;*.js;*.ts;*.html;*.css;*.cpp;*.c;*.h;*.hpp;*.java;*.rs;*.go;*.sh;*.bat;*.ps1"),
                ("Documents & Data", "*.txt;*.md;*.json;*.csv;*.log;*.yaml;*.yml;*.xml;*.ini;*.toml;*.sql"),
            ],
        )
        if not file_paths:
            return

        for path in file_paths:
            resolved = str(os.path.abspath(path))
            if any(att.path == resolved for att in self.attachments):
                continue
            try:
                doc = ingest_file(path)
                self.attachments.append(doc)
            except AttachmentError as exc:
                messagebox.showerror(self.t("attach_error_title"), str(exc))
            except Exception as exc:
                messagebox.showerror(
                    self.t("attach_error_title"),
                    self.t("attach_err_read", error=str(exc)),
                )

        self.render_attachment_chips()

    def remove_attachment(self, index):
        """Remove an attached document by index and refresh chip display."""
        if 0 <= index < len(self.attachments):
            self.attachments.pop(index)
            self.render_attachment_chips()

    def render_attachment_chips(self):
        """Render pill-style chip badges for currently attached documents."""
        if not hasattr(self, "attachments_frame"):
            return

        for child in self.attachments_frame.winfo_children():
            child.destroy()

        if not self.attachments:
            self.attachments_frame.pack_forget()
            return

        c = self.colors
        if hasattr(self, "attach_btn"):
            self.attachments_frame.pack(side="top", fill="x", pady=(0, 6), before=self.attach_btn)
        else:
            self.attachments_frame.pack(side="top", fill="x", pady=(0, 6))
        self.attachments_frame.configure(bg=c["bg"])

        for idx, doc in enumerate(self.attachments):
            chip = tk.Frame(
                self.attachments_frame,
                bg=c["sidebar"],
                padx=6,
                pady=2,
                highlightbackground=c["accent"],
                highlightthickness=1,
            )
            chip.pack(side="left", padx=(0, 6), pady=2)

            trunc_indicator = " ⚠️" if doc.is_truncated else ""
            label_text = f"📄 {doc.filename} ({doc.formatted_size}){trunc_indicator}"
            lbl = tk.Label(
                chip,
                text=label_text,
                bg=c["sidebar"],
                fg=c["text"],
                font=("Segoe UI", 8, "bold"),
            )
            lbl.pack(side="left", padx=(2, 4))

            remove_btn = Button(
                chip,
                text="✕",
                command=lambda i=idx: self.remove_attachment(i),
                bg=c["sidebar"],
                fg=c["muted"],
                activebackground=c["danger"],
                activeforeground="white",
                relief="flat",
                font=("Segoe UI", 8, "bold"),
                padx=3,
                pady=1,
            )
            remove_btn.pack(side="left")

    def send_message(self, _event=None):
        if self.busy:
            return "break"
        user_text = self.input_box.get("1.0", "end").strip()
        has_attachments = bool(getattr(self, "attachments", None))
        if not user_text and not has_attachments:
            return "break"
        self.tts.stop()  # A new message interrupts any reply still being read aloud.
        chat = self.current_chat()
        if not chat:
            self.make_draft()
            chat = self.current_chat()
        if self.draft_chat and chat.get("id") == self.draft_chat.get("id"):
            self.chats.append(self.draft_chat)
            self.draft_chat = None

        if has_attachments:
            assembled_message = assemble_prompt_with_attachments(user_text, self.attachments)
            first_att_name = self.attachments[0].filename
            self.attachments = []
            self.render_attachment_chips()
        else:
            assembled_message = user_text
            first_att_name = ""

        if chat.get("title") == "New Chat":
            if user_text:
                chat["title"] = user_text[:34]
            elif first_att_name:
                chat["title"] = f"File: {first_att_name}"[:34]

        time_now = message_time_text()
        chat["messages"].append({"role": "user", "content": assembled_message, "time": time_now})
        self.input_box.delete("1.0", "end")
        self.add_bubble("user", assembled_message, timestamp=time_now)
        self.refresh_history_list()
        self.save_history()
        self.busy = True
        self.send_btn.configure(state="disabled")
        self.set_status("status_thinking")

        # Answer clock/date questions locally for accuracy and speed—no model call required.
        if not has_attachments and self.is_time_question(user_text):
            answer = self.t("time_answer", when=local_datetime_text())
            self.finish_answer(answer, chat["id"])
            return "break"

        # Answer arithmetic questions locally using safe AST calculator—no model call required.
        if not has_attachments:
            calc_expr = self.extract_calc_expression(user_text)
            if calc_expr is not None:
                answer = calculate(calc_expr)
                self.finish_answer(answer, chat["id"])
                return "break"

        threading.Thread(target=self.ask_ollama, args=(chat["id"],), daemon=True).start()
        return "break"

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
        self.busy = False
        self.is_streaming = False
        self.current_stream_chat_id = None
        self.current_stream_response = None
        if hasattr(self, "stop_gen_btn"):
            try:
                self.stop_gen_btn.pack_forget()
            except Exception:
                pass
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
            if hasattr(self, "canvas"):
                self.canvas.yview_moveto(1.0)
            if getattr(self, "voice_enabled", False) and hasattr(self, "tts"):
                self.tts.speak(plain_text(answer))
        if hasattr(self, "busy"):
            self.busy = False
        if hasattr(self, "send_btn"):
            self.send_btn.configure(state="normal")
        if hasattr(self, "set_status"):
            self.set_status("status_ready")
        if hasattr(self, "refresh_history_list"):
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