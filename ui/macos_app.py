"""
ui/macos_app.py

macOS tkinter UI. Extends BaseApp — only handles widget creation and rendering.
"""

import tkinter as tk
from tkinter import scrolledtext
from ui.base_app import BaseApp


class App(BaseApp):
    def __init__(self, root: tk.Tk, core):
        self.root = root
        self._anim_after_id = None
        self._anim_base = ""
        self._anim_dots = 0

        root.title("FEMOS AI")
        root.geometry("640x680")
        root.minsize(480, 520)
        root.configure(bg="#f2f2f7")

        # ── Toolbar ──────────────────────────────────────────────────
        toolbar = tk.Frame(root, bg="#f2f2f7")
        toolbar.pack(fill=tk.X, padx=12, pady=(8, 0))
        tk.Button(
            toolbar, text="New Chat", command=self.new_chat,
            bg="#e5e5ea", relief=tk.FLAT, font=("Helvetica", 11), padx=8, cursor="hand2"
        ).pack(side=tk.LEFT)
        self._stop_btn = tk.Button(
            toolbar, text="Stop", command=self.interrupt,
            bg="#ff3b30", fg="white", relief=tk.FLAT, font=("Helvetica", 11), padx=8,
            cursor="hand2", state=tk.DISABLED
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._conn_label = tk.Label(toolbar, text="\u26ab Connecting\u2026",
                                    bg="#f2f2f7", font=("Helvetica", 11), fg="#8e8e93")
        self._conn_label.pack(side=tk.RIGHT)

        # ── Chat area ─────────────────────────────────────────────────
        self._chat = scrolledtext.ScrolledText(
            root, state="disabled", wrap=tk.WORD,
            bg="white", relief=tk.FLAT, font=("Helvetica", 13),
            padx=10, pady=8
        )
        self._chat.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 0))
        self._chat.tag_config("you",       foreground="#007AFF", font=("Helvetica", 13, "bold"))
        self._chat.tag_config("assistant", foreground="#1c1c1e")
        self._chat.tag_config("status",    foreground="#8e8e93", font=("Helvetica", 12, "italic"))
        self._chat.tag_config("thinking",  foreground="#aaaaaa", font=("Helvetica", 11, "italic"))
        self._chat.tag_config("error",     foreground="#ff3b30", font=("Helvetica", 12, "italic"))

        # ── Status label ──────────────────────────────────────────────
        self._status_label = tk.Label(
            root, text="", anchor="w",
            bg="#f2f2f7", font=("Helvetica", 12, "italic"), fg="#8e8e93",
            padx=14
        )
        self._status_label.pack(fill=tk.X)

        # ── Input row ─────────────────────────────────────────────────
        bar = tk.Frame(root, bg="#f2f2f7")
        bar.pack(fill=tk.X, padx=12, pady=8)

        # Multi-line input: Return = newline, Shift/Cmd+Return = send
        self._entry = tk.Text(
            bar, font=("Helvetica", 13), relief=tk.SOLID, bd=1,
            height=3, wrap=tk.WORD
        )
        self._entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._entry.bind("<Shift-Return>", self._on_send_event)
        self._entry.bind("<Command-Return>", self._on_send_event)

        btn_col = tk.Frame(bar, bg="#f2f2f7")
        btn_col.pack(side=tk.RIGHT, fill=tk.Y)

        self._send_btn = tk.Button(
            btn_col, text="Send", command=self._on_send_event,
            bg="#007AFF", fg="white", relief=tk.FLAT,
            font=("Helvetica", 13), padx=12, cursor="hand2"
        )
        self._send_btn.pack(side=tk.TOP, pady=(0, 4))

        self._mic_btn = tk.Button(
            btn_col, text="\U0001f3a4", command=self.toggle_mic,
            bg="#f2f2f7", relief=tk.FLAT, font=("Helvetica", 16), padx=6, cursor="hand2"
        )
        self._mic_btn.pack(side=tk.TOP)

        self._entry.focus()

        # Wire callbacks via BaseApp (must be last)
        super().__init__(core)

    # ── Thread dispatch ───────────────────────────────────────────────
    def _dispatch(self, fn, *args):
        self.root.after(0, fn, *args)

    # ── Abstract surface implementations ─────────────────────────────
    def _write(self, text: str, role: str):
        self._chat.config(state="normal")
        self._chat.insert(tk.END, text + "\n", role)
        self._chat.config(state="disabled")
        self._chat.see(tk.END)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._stop_btn.config(state=tk.NORMAL if busy else tk.DISABLED)
        self._send_btn.config(text="Steer" if busy else "Send")
        self._mic_btn.config(state="disabled" if busy else "normal")
        if not busy:
            self._entry.focus()

    def _set_connected(self, connected: bool):
        if connected:
            self._conn_label.config(text="\U0001f7e2 Connected", fg="#34c759")
        else:
            self._conn_label.config(text="\U0001f534 Disconnected", fg="#ff3b30")

    def _set_status(self, text: str):
        if self._anim_after_id:
            self.root.after_cancel(self._anim_after_id)
            self._anim_after_id = None
        if text and text.rstrip(".\u2026") in ("Thinking", "Thinking\u2026"):
            self._anim_base = "Thinking"
            self._anim_dots = 0
            self._status_label.config(text="\u23f3 Thinking\u2026")
            self._anim_after_id = self.root.after(400, self._animate_dots)
        else:
            self._status_label.config(text=f"\u23f3 {text}" if text else "")

    def _set_input(self, text: str):
        self._entry.insert(tk.END, text)

    def _clear_chat(self):
        self._chat.config(state="normal")
        self._chat.delete("1.0", tk.END)
        self._chat.config(state="disabled")

    # ── Mic UI hooks ──────────────────────────────────────────────────
    def _on_mic_started(self):
        self._mic_btn.config(text="\u23f9", bg="#FF3B30")

    def _on_mic_stopped(self):
        self._mic_btn.config(text="\U0001f3a4", bg="#f2f2f7")

    # ── Send entry wrapper ────────────────────────────────────────────
    def _on_send_event(self, _event=None):
        text = self._entry.get("1.0", tk.END).rstrip("\n")
        self._entry.delete("1.0", tk.END)
        self.send(text)
        return "break"  # prevent default Return handling

    # ── Dot animation ─────────────────────────────────────────────────
    def _animate_dots(self):
        self._anim_dots = (self._anim_dots + 1) % 4
        dots = "." * self._anim_dots
        self._status_label.config(text=f"\u23f3 {self._anim_base}{dots}")
        self._anim_after_id = self.root.after(400, self._animate_dots)
