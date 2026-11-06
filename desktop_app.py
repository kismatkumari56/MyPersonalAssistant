import tkinter as tk
from tkinter import messagebox
from datetime import datetime
import json
import threading
import requests
import uuid
import os

from tts import TTSManager

APP_TITLE = "JUGNU | Just an Unusually Genius Neural Utility"
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "llama3.2:3b"
HISTORY_FILE = "jugnu_history.json"
SETTINGS_FILE = "jugnu_settings.json"

LIGHT = {
    "bg": "#F7F4FF", "sidebar": "#EEE8FF", "panel": "#FFFFFF",
    "text": "#27213B", "muted": "#756B91", "accent": "#8B5CF6",
    "accent2": "#EC4899", "user": "#E3DCFF", "assistant": "#F0EAFB",
    "entry": "#FFFFFF", "border": "#D9CFF5", "danger": "#FCE7F3"
}
DARK = {
    "bg": "#191728", "sidebar": "#211D35", "panel": "#28223D",
    "text": "#F4EEFF", "muted": "#B7A9D8", "accent": "#8B6CF0",
    "accent2": "#D946A6", "user": "#34365A", "assistant": "#2A2440",
    "entry": "#2A2440", "border": "#463A66", "danger": "#4B2A40"
}

def local_datetime_text():
    # Uses the Windows system's configured local timezone.
    now = datetime.now().astimezone()
    return now.strftime("%A, %d %B %Y, %I:%M:%S %p %Z")

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
        self.tts = TTSManager()  # Offline voice output; runs on its own thread.
        self.voice_enabled = self.load_voice_setting()

        self.build_ui()
        if self.chats:
            self.current_id = self.chats[-1]["id"]  # Reopen most recent saved conversation.
        else:
            self.make_draft()
        self.apply_theme()
        self.root.after(250, self.warm_up)

    def load_history(self):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def save_history(self):
        # Persist only conversations containing messages; never save empty "New Chat" rows.
        saved_chats = [chat for chat in self.chats if chat.get("messages")]
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(saved_chats, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.set_status(f"Could not save history: {e}")

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return bool(json.load(f).get("dark_mode", True))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return True

    def load_voice_setting(self):
        # Voice output is off by default; the choice is remembered between launches.
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return bool(json.load(f).get("voice_enabled", False))
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            return False

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({"dark_mode": self.dark_mode, "voice_enabled": self.voice_enabled}, f)
        except OSError:
            pass

    def build_ui(self):
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

        self.new_btn = tk.Button(self.sidebar, text="＋  New Chat", command=self.new_chat,
                                 font=("Segoe UI", 10, "bold"), relief="flat", pady=12)
        self.new_btn.pack(fill="x", padx=12, pady=(0, 18))

        self.history_label = tk.Label(self.sidebar, text="CHAT HISTORY", font=("Segoe UI", 9, "bold"),
                                      anchor="w", padx=18)
        self.history_label.pack(fill="x")

        self.history_list = tk.Listbox(self.sidebar, relief="flat", borderwidth=0,
                                       font=("Segoe UI", 9), activestyle="none")
        self.history_list.pack(fill="both", expand=True, padx=8, pady=8)
        self.history_list.bind("<<ListboxSelect>>", self.select_chat)

        self.clear_btn = tk.Button(self.sidebar, text="▤  Clear Current Chat", command=self.clear_chat,
                                   relief="flat", pady=10)
        self.clear_btn.pack(fill="x", padx=12, pady=(4, 8))
        self.voice_btn = tk.Button(self.sidebar, text="🔇  Voice: Off", command=self.toggle_voice,
                                   relief="flat", pady=10)
        self.voice_btn.pack(fill="x", padx=12, pady=(0, 8))
        if not self.tts.available:
            self.voice_btn.configure(state="disabled")  # pyttsx3 not installed
        self.theme_btn = tk.Button(self.sidebar, text="☼  Light Mode", command=self.toggle_theme,
                                   relief="flat", pady=10)
        self.theme_btn.pack(fill="x", padx=12, pady=(0, 12))

        self.content = tk.Frame(self.main)
        self.content.pack(side="left", fill="both", expand=True)

        self.header = tk.Frame(self.content, height=64)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.header_title = tk.Label(self.header, text="✦  Your little genius is here!",
                                     font=("Segoe UI", 15, "bold"), anchor="w", padx=22)
        self.header_title.pack(side="left", fill="y")
        self.status = tk.Label(self.header, text="• Ready", font=("Segoe UI", 9, "bold"),
                               anchor="e", padx=18)
        self.status.pack(side="right", fill="y")

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

        self.bottom = tk.Frame(self.content)
        self.bottom.pack(fill="x", padx=12, pady=(0, 12))
        self.input_box = tk.Text(self.bottom, height=2, wrap="word", font=("Segoe UI", 11),
                                 relief="flat", padx=10, pady=10, undo=True)
        self.input_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.input_box.bind("<Control-Return>", self.send_message)
        self.send_btn = tk.Button(self.bottom, text="➤  Send", command=self.send_message,
                                  relief="flat", font=("Segoe UI", 10, "bold"), padx=18, pady=12)
        self.send_btn.pack(side="right")

    def on_frame_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def on_mousewheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
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
        self.history_list.configure(bg=c["sidebar"], fg=c["text"],
                                    selectbackground=c["accent"], selectforeground="white")
        for btn in [self.new_btn, self.theme_btn, self.voice_btn]:
            btn.configure(bg=c["accent"], fg="white", activebackground=c["accent2"],
                          activeforeground="white", cursor="hand2")
        self.voice_btn.configure(text="🔊  Voice: On" if self.voice_enabled else "🔇  Voice: Off")
        self.clear_btn.configure(bg=c["danger"], fg=c["accent2"], activebackground=c["danger"],
                                 activeforeground=c["text"], cursor="hand2")
        self.input_box.configure(bg=c["entry"], fg=c["text"], insertbackground=c["text"])
        self.send_btn.configure(bg=c["accent"], fg="white", activebackground=c["accent2"],
                                activeforeground="white", cursor="hand2")
        self.theme_btn.configure(text="☼  Light Mode" if self.dark_mode else "☾  Dark Mode")
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
        self.voice_btn.configure(text="🔊  Voice: On" if self.voice_enabled else "🔇  Voice: Off")
        self.save_settings()

    def on_close(self):
        self.tts.shutdown()
        self.root.destroy()

    def refresh_history_list(self):
        selected = self.current_id
        self.history_list.delete(0, "end")
        for chat in reversed(self.chats):
            title = chat.get("title", "New Chat")
            self.history_list.insert("end", "  ◌  " + title[:34])
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

    def clear_chat(self):
        chat = self.current_chat()
        if not chat or not chat.get("messages"):
            # Nothing saved to clear; leave the empty draft alone.
            return
        if not messagebox.askyesno("Clear Current Chat", "Is current chat ko history se permanently clear kar dein?"):
            return
        self.tts.stop()
        self.chats = [saved for saved in self.chats if saved.get("id") != chat.get("id")]
        self.draft_chat = None
        self.make_draft()
        self.save_history()
        self.refresh_history_list()
        self.display_chat()

    def display_chat(self):
        for child in self.messages_frame.winfo_children():
            child.destroy()
        chat = self.current_chat()
        messages = chat.get("messages", []) if chat else []
        if not messages:
            self.show_welcome()
        else:
            for msg in messages:
                self.add_bubble(msg.get("role"), msg.get("content", ""))
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

        tk.Label(wrapper, text="Hey! I'm JUGNU",
                 font=("Segoe UI", 23, "bold"), bg=c["bg"], fg=c["text"]).pack(pady=(4, 5))
        tk.Label(wrapper, text="Just an Unusually Genius Neural Utility",
                 font=("Segoe UI", 12), bg=c["bg"], fg=c["muted"]).pack(pady=(0, 10))
        tk.Label(wrapper, text="What would you like to explore today?",
                 font=("Segoe UI", 12), bg=c["bg"], fg=c["accent"]).pack()

    def add_bubble(self, role, text):
        c = self.colors
        is_user = role == "user"
        row = tk.Frame(self.messages_frame, bg=c["bg"])
        row.pack(fill="x", pady=7, padx=8)
        bubble = tk.Frame(row, bg=c["user"] if is_user else c["assistant"])
        bubble.pack(side="right" if is_user else "left", padx=(40, 0) if is_user else (0, 40))
        name = "You" if is_user else "🤖 JUGNU"
        tk.Label(bubble, text=name, bg=bubble["bg"], fg=c["accent"],
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", padx=12, pady=(8, 1))
        tk.Label(bubble, text=text, bg=bubble["bg"], fg=c["text"],
                 font=("Segoe UI", 10), justify="left", anchor="w",
                 wraplength=680).pack(fill="x", padx=12, pady=(1, 10))
        self.root.update_idletasks()

    def set_status(self, text):
        self.status.configure(text="• " + text)

    def warm_up(self):
        def task():
            try:
                requests.post(OLLAMA_URL, json={
                    "model": MODEL, "messages": [{"role": "user", "content": "Reply with OK."}],
                    "stream": False, "keep_alive": "10m",
                    "options": {"num_predict": 3}
                }, timeout=90)
                self.root.after(0, lambda: self.set_status("Ready"))
            except requests.RequestException:
                self.root.after(0, lambda: self.set_status("Ollama offline"))
        threading.Thread(target=task, daemon=True).start()

    def is_time_question(self, text):
        q = text.lower().strip()
        keys = ["time", "date", "today", "day", "abhi time", "samay", "tareekh", "aaj"]
        return any(k in q for k in keys) and any(
            phrase in q for phrase in ["what time", "time kya", "date kya", "today", "aaj", "samay",
                                      "tareekh", "current date", "current time", "which day", "what is the date"]
        )

    def send_message(self, _event=None):
        if self.busy:
            return "break"
        user_text = self.input_box.get("1.0", "end").strip()
        if not user_text:
            return "break"
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
        chat["messages"].append({"role": "user", "content": user_text})
        self.input_box.delete("1.0", "end")
        self.add_bubble("user", user_text)
        self.refresh_history_list()
        self.save_history()
        self.busy = True
        self.send_btn.configure(state="disabled")
        self.set_status("Thinking…")

        # Answer clock/date questions locally for accuracy and speed—no model call required.
        if self.is_time_question(user_text):
            answer = "Abhi local date aur time hai: " + local_datetime_text()
            self.finish_answer(answer, chat["id"])
            return "break"

        threading.Thread(target=self.ask_ollama, args=(chat["id"],), daemon=True).start()
        return "break"

    def ask_ollama(self, chat_id):
        try:
            chat = next((c for c in self.chats if c.get("id") == chat_id), None)
            if not chat:
                return
            # Keep recent context only, to reduce prompt size and improve latency.
            recent = chat.get("messages", [])[-10:]
            messages = [{
                "role": "system",
                "content": (
                    "You are JUGNU, a helpful, friendly personal AI assistant. "
                    "Give clear, concise answers by default (usually 2-5 sentences). "
                    "Use easy language. If more detail is needed, provide it. "
                    f"Current local date and time: {local_datetime_text()}. "
                    "For date/time questions, use this exact current context; never guess."
                )
            }]
            messages.extend({"role": m["role"], "content": m["content"]} for m in recent)
            response = requests.post(OLLAMA_URL, json={
                "model": MODEL,
                "messages": messages,
                "stream": False,
                "keep_alive": "10m",
                "options": {"num_predict": 256, "temperature": 0.6}
            }, timeout=180)
            response.raise_for_status()
            answer = response.json().get("message", {}).get("content", "").strip()
            if not answer:
                answer = "Sorry, mujhe empty response mila. Ek baar phir try karo."
        except requests.Timeout:
            answer = "Response mein zyada time lag raha hai. Ollama/model load check karke dobara try karo."
        except requests.ConnectionError:
            answer = "Ollama se connect nahi ho pa raha. Check karo ki Ollama running hai aur model installed hai."
        except (requests.RequestException, ValueError) as e:
            answer = f"Error: {e}"
        self.root.after(0, lambda: self.finish_answer(answer, chat_id))

    def finish_answer(self, answer, chat_id=None):
        chat = self.current_chat()
        if chat_id:
            chat = next((c for c in self.chats if c.get("id") == chat_id), None)
        if chat:
            chat["messages"].append({"role": "assistant", "content": answer})
            self.save_history()
        # Only display if the user is still viewing the chat that received the answer.
        if chat and chat.get("id") == self.current_id:
            self.add_bubble("assistant", answer)
            self.canvas.yview_moveto(1.0)
            if self.voice_enabled:
                self.tts.speak(answer)
        self.busy = False
        self.send_btn.configure(state="normal")
        self.set_status("Ready")
        self.refresh_history_list()

if __name__ == "__main__":
    root = tk.Tk()
    app = JugnuApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()