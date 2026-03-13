"""
ui/macos_app.py

macOS tkinter UI. Pure rendering layer — wires ClientCore callbacks to the chat window.
"""

import tkinter as tk
from tkinter import scrolledtext

class App:
    def __init__(self, root: tk.Tk, core):
        self.root = root
        self._core = core
        root.title("FEMOS AI")
        root.geometry("640x540")
        root.configure(bg="#f2f2f7")

        # ── Toolbar ──────────────────────────────────────────────────
        toolbar = tk.Frame(root, bg="#f2f2f7")
        toolbar.pack(fill=tk.X, padx=12, pady=(8, 0))
        tk.Button(
            toolbar, text="New Chat", command=self._new_chat,
            bg="#e5e5ea", relief=tk.FLAT, font=("Helvetica", 11), padx=8, cursor="hand2"
        ).pack(side=tk.LEFT)
        self._conn_label = tk.Label(toolbar, text="⚫ Connecting…",
                                    bg="#f2f2f7", font=("Helvetica", 11), fg="#8e8e93")
        self._conn_label.pack(side=tk.RIGHT)

        # ── Chat area ─────────────────────────────────────────────────
        self.chat = scrolledtext.ScrolledText(
            root, state="disabled", wrap=tk.WORD,
            bg="white", relief=tk.FLAT, font=("Helvetica", 13),
            padx=10, pady=8
        )
        self.chat.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 0))
        self.chat.tag_config("you",       foreground="#007AFF", font=("Helvetica", 13, "bold"))
        self.chat.tag_config("assistant", foreground="#1c1c1e")
        self.chat.tag_config("status",    foreground="#8e8e93", font=("Helvetica", 12, "italic"))
        self.chat.tag_config("thinking",  foreground="#aaaaaa", font=("Helvetica", 11, "italic"))

        # ── Status label (between chat and input) ─────────────────────
        self._status_label = tk.Label(
            root, text="", anchor="w",
            bg="#f2f2f7", font=("Helvetica", 12, "italic"), fg="#8e8e93",
            padx=14
        )
        self._status_label.pack(fill=tk.X)

        # ── Input row ─────────────────────────────────────────────────
        bar = tk.Frame(root, bg="#f2f2f7")
        bar.pack(fill=tk.X, padx=12, pady=8)

        self.entry = tk.Entry(bar, font=("Helvetica", 13), relief=tk.SOLID, bd=1)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.entry.bind("<Return>", self.send)

        self.mic_btn = tk.Button(
            bar, text="🎙", command=self._toggle_mic,
            bg="#f2f2f7", relief=tk.FLAT, font=("Helvetica", 16), padx=6, cursor="hand2"
        )
        self.mic_btn.pack(side=tk.RIGHT, padx=(4, 0))

        self.btn = tk.Button(
            bar, text="Send", command=self.send,
            bg="#007AFF", fg="white", relief=tk.FLAT,
            font=("Helvetica", 13), padx=12, cursor="hand2"
        )
        self.btn.pack(side=tk.RIGHT, padx=(8, 0))

        self.entry.focus()
        self._recording = False
        self._anim_after_id = None
        self._anim_base = ""
        self._anim_dots = 0

        # ── Wire core callbacks ───────────────────────────────────────
        core.on_connected    = lambda c: self.root.after(0, self._set_connected, c)
        core.on_status       = lambda t: self.root.after(0, self._set_status_line, t)
        core.on_thinking     = lambda t: self.root.after(0, self._log_thinking, t)
        core.on_result       = lambda t: self.root.after(0, self._show, t)
        core.on_error        = lambda t: self.root.after(0, self._show, f"[Error] {t}")
        core.on_user_message = lambda t: self.root.after(0, self._on_user_message, t)
        core.on_transcribed  = lambda t: self.root.after(0, self._on_transcribed, t)

    # ── Thinking log ──────────────────────────────────────────────────
    def _log_thinking(self, text: str):
        self._write(text, "thinking")

    # ── Connection indicator ──────────────────────────────────────────
    def _set_connected(self, connected: bool):
        if connected:
            self._conn_label.config(text="🟢 Connected", fg="#34c759")
        else:
            self._conn_label.config(text="🔴 Disconnected", fg="#ff3b30")

    # ── UI helpers ────────────────────────────────────────────────────
    def _write(self, text: str, tag: str):
        self.chat.config(state="normal")
        self.chat.insert(tk.END, text + "\n", tag)
        self.chat.config(state="disabled")
        self.chat.see(tk.END)

    def _set_status_line(self, text: str):
        if self._anim_after_id:
            self.root.after_cancel(self._anim_after_id)
            self._anim_after_id = None
        if text and text.rstrip(".").rstrip("…") in ("Thinking", "Thinking…"):
            self._anim_base = "Thinking"
            self._anim_dots = 0
            self._status_label.config(text="⏳ Thinking…")
            self._anim_after_id = self.root.after(400, self._animate_dots)
        else:
            self._status_label.config(text=f"⏳ {text}" if text else "")

    def _animate_dots(self):
        self._anim_dots = (self._anim_dots + 1) % 4
        dots = "." * self._anim_dots
        self._status_label.config(text=f"⏳ {self._anim_base}{dots}")
        self._anim_after_id = self.root.after(400, self._animate_dots)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.btn.config(state=state)
        self.mic_btn.config(state=state)
        if not busy:
            self.entry.focus()

    # ── New Chat ──────────────────────────────────────────────────────
    def _new_chat(self):
        self._core.new_chat()
        self.chat.config(state="normal")
        self.chat.delete("1.0", tk.END)
        self.chat.config(state="disabled")
        self._set_status_line("")
        self._write("(New conversation started)", "assistant")

    # ── Mic ───────────────────────────────────────────────────────────
    def _toggle_mic(self):
        if not self._recording:
            self._recording = True
            self.mic_btn.config(text="⏹", bg="#FF3B30")
            self._write("🎙 Recording… (click ⏹ to stop)", "status")
            self._core.start_recording()
        else:
            self._recording = False
            self.mic_btn.config(text="🎙", bg="#f2f2f7")
            self._set_status_line("Transcribing…")
            self._core.stop_recording()

    def _on_transcribed(self, text: str):
        self._set_status_line("")
        if text:
            self.entry.insert(0, text)
        else:
            self._write("(no speech detected)", "status")

    # ── Send ──────────────────────────────────────────────────────────
    def send(self, _event=None):
        command = self.entry.get().strip()
        if not command:
            return
        if not self._core.is_connected():
            self._write("Not connected to server. Retrying…", "status")
            return
        self.entry.delete(0, tk.END)
        self._set_busy(True)
        self._core.send_text(command)

    def _on_user_message(self, text: str):
        self._write(f"You: {text}", "you")

        self._set_status_line("Thinking…")

    def _show(self, response: str):
        if self._anim_after_id:
            self.root.after_cancel(self._anim_after_id)
            self._anim_after_id = None
        self._status_label.config(text="")
        self._write(f"Assistant: {response}", "assistant")
        self._set_busy(False)
