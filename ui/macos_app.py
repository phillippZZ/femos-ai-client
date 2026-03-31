"""
ui/macos_app.py

macOS tkinter UI. Extends BaseApp — only handles widget creation and rendering.

Multi-task support:
- Send always submits a new task (server returns queue_full if at capacity).
- Stop button interrupts all active tasks.
- Task log entries are shown inline in the chat as thinking-style lines.
- Active task IDs are shown in the status bar.
"""

import threading
import tkinter as tk
from tkinter import scrolledtext, simpledialog
from ui.base_app import BaseApp


class App(BaseApp):
    def __init__(self, root: tk.Tk, core):
        self.root = root
        self._anim_after_id = None
        self._anim_base = ""
        self._anim_dots = 0

        root.title("FEMOS AI")
        root.geometry("640x700")
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
            toolbar, text="Stop All", command=lambda: self.interrupt(),
            bg="#ff3b30", fg="white", relief=tk.FLAT, font=("Helvetica", 11), padx=8,
            cursor="hand2", state=tk.DISABLED
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._conn_label = tk.Label(toolbar, text="\u26ab Connecting\u2026",
                                    bg="#f2f2f7", font=("Helvetica", 11), fg="#8e8e93")
        self._conn_label.pack(side=tk.RIGHT)

        # ── Active tasks bar (click a task pill to steer it) ─────────
        self._tasks_frame = tk.Frame(root, bg="#e5e5ea", pady=2)
        # Pack+forget establishes position in the layout before the chat widget
        self._tasks_frame.pack(fill=tk.X, padx=12, pady=(4, 0))
        self._tasks_frame.pack_forget()

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

        # Override tool_call dispatch so ask_user shows a native dialog
        self._core._ws.on_tool_call = self._intercept_tool_call

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
        # Send button is always enabled — user can always queue a new task
        if not busy:
            self._tasks_frame.pack_forget()
            self._entry.focus()
        else:
            self._refresh_tasks_bar()

    def _refresh_tasks_bar(self):
        for w in self._tasks_frame.winfo_children():
            w.destroy()
        if not self._active_tasks:
            self._tasks_frame.pack_forget()
            return
        for tid, title in self._active_tasks.items():
            stage = self._task_stages.get(tid, "Running")
            stage_icon = {
                "Starting":   "○",
                "Planning":   "📋",
                "Executing":  "⚙",
                "Validating": "✔",
                "Error":      "✖",
            }.get(stage, "●")
            short = (title[:24] + "…") if len(title) > 24 else title
            is_focused = (tid == self._focused_task_id)
            bg = "#007AFF" if is_focused else "#c8c8d0"
            fg = "white"   if is_focused else "#3a3a3c"
            tk.Button(
                self._tasks_frame,
                text=f"{stage_icon} {short}  [{stage}]",
                bg=bg, fg=fg, activebackground="#0062cc", activeforeground="white",
                relief=tk.FLAT, font=("Helvetica", 10), padx=8, pady=2,
                cursor="hand2", command=lambda t=tid: self._toggle_focus(t),
            ).pack(side=tk.LEFT, padx=(4, 0), pady=2)
        self._tasks_frame.pack(fill=tk.X, padx=12, pady=(4, 0))

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

    # ── Task log display ──────────────────────────────────────────────
    def _on_task_started_hook(self, task_id: str, title: str):
        self._refresh_tasks_bar()

    def _on_task_progress_log(self, task_id: str, level: str, text: str, ts: int):
        """Show plan/execute/validate log entries as thinking lines and refresh the stage bar."""
        icon = {"plan": "📋", "execute": "⚙", "validate": "✔", "info": "ℹ", "error": "✖"}.get(level, "·")
        self._write(f"  {icon} {text}", "thinking")
        # Stage may have just been updated in base_app._on_task_log — refresh the bar
        self._refresh_tasks_bar()

    def _on_task_finished(self, task_id: str, success: bool):
        # Keep a brief "done" ghost pill so the user can see what just completed.
        icon = "✓" if success else "✖"
        title = self._active_tasks.get(task_id) or task_id
        short = (title[:24] + "…") if len(title) > 24 else title
        label = tk.Label(
            self._tasks_frame,
            text=f"{icon} {short}",
            bg="#d1fae5" if success else "#fee2e2",
            fg="#065f46" if success else "#991b1b",
            font=("Helvetica", 10), padx=8, pady=2,
        )
        label.pack(side=tk.LEFT, padx=(4, 0), pady=2)
        self._tasks_frame.pack(fill=tk.X, padx=12, pady=(4, 0))
        # Remove ghost after 4 seconds; if more tasks are still running, bar stays anyway
        self.root.after(4000, self._remove_ghost, label)

    def _remove_ghost(self, label: tk.Label):
        try:
            label.destroy()
        except tk.TclError:
            pass
        self._refresh_tasks_bar()

    # ── Focus / steer routing ─────────────────────────────────────────
    def _toggle_focus(self, task_id: str):
        """Click a task pill to enter steer mode for that task; click again to exit."""
        self._focused_task_id = "" if self._focused_task_id == task_id else task_id
        self._refresh_tasks_bar()
        self._update_send_mode()

    def _update_send_mode(self):
        """Reflect steer/send mode in the button label and input border."""
        if self._focused_task_id and self._focused_task_id in self._active_tasks:
            self._send_btn.config(text="Steer", bg="#ff9500")
            self._entry.config(bd=2, highlightthickness=2,
                               highlightbackground="#ff9500", highlightcolor="#ff9500")
        else:
            self._send_btn.config(text="Send", bg="#007AFF")
            self._entry.config(bd=1, highlightthickness=0)

    def _on_focus_cleared(self):
        self._update_send_mode()
        self._refresh_tasks_bar()

    # ── ask_user native dialog ──────────────────────────────────────────
    def _intercept_tool_call(self, msg: dict):
        """Route ask_user to a native dialog; all other tool calls run normally."""
        if msg.get("name") == "ask_user":
            threading.Thread(target=self._exec_ask_user, args=(msg,), daemon=True).start()
        else:
            self._core._ws._exec_tool_threaded(msg)

    def _exec_ask_user(self, msg: dict):
        """Show a native tkinter dialog, wait for the user's answer, send tool_result."""
        done = threading.Event()
        answer_box = [None]
        args = msg.get("args", {})
        question = args.get("question", "Please provide the required information:")
        title    = args.get("title",    "AI Assistant")
        default  = args.get("default",  "")

        def _show_dialog():
            answer = simpledialog.askstring(
                title, question, initialvalue=default, parent=self.root
            )
            answer_box[0] = answer
            done.set()

        self.root.after(0, _show_dialog)
        done.wait(timeout=300)  # up to 5 minutes for the user to respond

        answer = answer_box[0] if answer_box[0] is not None else ""
        if answer:
            is_sensitive = any(w in question.lower()
                               for w in ("key", "token", "secret", "password",
                                         "credential", "auth", "oauth"))
            display = ("*" * min(len(answer), 8) + "…") if is_sensitive else answer[:80]
            self.root.after(0, self._write, f"🔑 [You entered]: {display}", "you")
        else:
            self.root.after(0, self._write, "(Dialog cancelled — no input given)", "status")

        self._core._ws.send({
            "type": "tool_result",
            "call_id": msg["call_id"],
            "name": "ask_user",
            "content": answer if answer else "(no answer provided)",
        })

    # ── Mic UI hooks ──────────────────────────────────────────────────
    def _on_mic_started(self):
        self._mic_btn.config(text="\u23f9", bg="#FF3B30")

    def _on_mic_stopped(self):
        self._mic_btn.config(text="\U0001f3a4", bg="#f2f2f7")

    # ── Send entry wrapper ────────────────────────────────────────────
    def _on_send_event(self, _event=None):
        text = self._entry.get("1.0", tk.END).rstrip("\n")
        self._entry.delete("1.0", tk.END)
        if self._focused_task_id and self._focused_task_id in self._active_tasks:
            self.steer(text, self._focused_task_id)
        else:
            self._focused_task_id = ""  # stale focus — clear silently
            self.send(text)
        return "break"  # prevent default Return handling

    # ── Dot animation ─────────────────────────────────────────────────
    def _animate_dots(self):
        self._anim_dots = (self._anim_dots + 1) % 4
        dots = "." * self._anim_dots
        self._status_label.config(text=f"\u23f3 {self._anim_base}{dots}")
        self._anim_after_id = self.root.after(400, self._animate_dots)
