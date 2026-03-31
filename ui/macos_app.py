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
        self._ctx_win = None

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
        tk.Button(
            toolbar, text="\U0001f4cb Context", command=self._open_context_viewer,
            bg="#e5e5ea", relief=tk.FLAT, font=("Helvetica", 11), padx=8, cursor="hand2"
        ).pack(side=tk.LEFT, padx=(8, 0))
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

    # ── Task context viewer ───────────────────────────────────────────
    def _open_context_viewer(self):
        """Open (or raise) a live task context + execution log viewer window."""
        import json as _json
        import os as _os
        import datetime as _dt
        from core.config import TASKS_DIR as _TASKS_DIR

        if self._ctx_win is not None:
            try:
                if self._ctx_win.winfo_exists():
                    self._ctx_win.lift()
                    self._ctx_win.focus_set()
                    return
            except Exception:
                pass
        self._ctx_win = None

        win = tk.Toplevel(self.root)
        win.title("Task Context Viewer")
        win.geometry("860x580")
        win.configure(bg="#f2f2f7")
        self._ctx_win = win

        # ── Header ───────────────────────────────────────────────────
        header = tk.Frame(win, bg="#f2f2f7")
        header.pack(fill=tk.X, padx=12, pady=(8, 4))

        tk.Label(header, text="Task:", bg="#f2f2f7",
                 font=("Helvetica", 12)).pack(side=tk.LEFT)
        task_var = tk.StringVar(value="")
        task_menu = tk.OptionMenu(header, task_var, "")
        task_menu.config(font=("Helvetica", 11), bg="white",
                         relief=tk.FLAT, width=44)
        task_menu.pack(side=tk.LEFT, padx=(4, 12))

        auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(header, text="Auto-refresh", variable=auto_var,
                       bg="#f2f2f7", font=("Helvetica", 11)).pack(side=tk.LEFT)
        refresh_btn = tk.Button(
            header, text="\u21ba", bg="#e5e5ea", relief=tk.FLAT,
            font=("Helvetica", 13), padx=6, cursor="hand2")
        refresh_btn.pack(side=tk.LEFT, padx=(6, 0))

        # ── Split pane: plan (left) | log (right) ────────────────────
        pane = tk.PanedWindow(win, orient=tk.HORIZONTAL, bg="#d1d1d6",
                              sashwidth=4, sashrelief=tk.FLAT)
        pane.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        # Left — plan / status
        left = tk.Frame(pane, bg="#f2f2f7")
        tk.Label(left, text="Plan & Status", bg="#f2f2f7",
                 font=("Helvetica", 11, "bold"), anchor="w").pack(
                     fill=tk.X, padx=6, pady=(4, 0))
        plan_txt = scrolledtext.ScrolledText(
            left, state="disabled", wrap=tk.WORD,
            bg="white", relief=tk.FLAT, font=("Helvetica", 11), padx=6, pady=6)
        plan_txt.pack(fill=tk.BOTH, expand=True)
        plan_txt.tag_config("header",  foreground="#1c1c1e", font=("Helvetica", 12, "bold"))
        plan_txt.tag_config("meta",    foreground="#3a3a3c")
        plan_txt.tag_config("sub",     foreground="#636366", font=("Helvetica", 10))
        plan_txt.tag_config("done",    foreground="#259c55")
        plan_txt.tag_config("active",  foreground="#007AFF", font=("Helvetica", 11, "bold"))
        plan_txt.tag_config("pending", foreground="#8e8e93")
        pane.add(left, minsize=220)

        # Right — execution log
        right = tk.Frame(pane, bg="#1c1c1e")
        tk.Label(right, text="Execution Log", bg="#1c1c1e", fg="#8e8e93",
                 font=("Helvetica", 11, "bold"), anchor="w").pack(
                     fill=tk.X, padx=6, pady=(4, 0))
        log_txt = scrolledtext.ScrolledText(
            right, state="disabled", wrap=tk.WORD,
            bg="#1c1c1e", fg="#d0d0d0", relief=tk.FLAT,
            font=("Menlo", 10), padx=6, pady=4)
        log_txt.pack(fill=tk.BOTH, expand=True)
        log_txt.tag_config("ts",           foreground="#636366")
        log_txt.tag_config("lv_plan",      foreground="#5ac8fa")
        log_txt.tag_config("lv_execute",   foreground="#ff9f0a")
        log_txt.tag_config("lv_validate",  foreground="#34c759")
        log_txt.tag_config("lv_error",     foreground="#ff453a")
        log_txt.tag_config("lv_info",      foreground="#98989e")
        log_txt.tag_config("body",         foreground="#d0d0d0")
        pane.add(right, minsize=360)

        _after_id = [None]

        def _populate_dropdown():
            menu = task_menu["menu"]
            menu.delete(0, "end")
            tasks = []
            if _os.path.isdir(_TASKS_DIR):
                _entries = sorted(e for e in _os.listdir(_TASKS_DIR)
                                  if _os.path.isdir(_os.path.join(_TASKS_DIR, e)))
                for tid in _entries:
                    ctx_p = _os.path.join(_TASKS_DIR, tid, "context.json")
                    label = tid
                    if _os.path.exists(ctx_p):
                        try:
                            with open(ctx_p) as _f:
                                c = _json.load(_f)
                            short = c.get("title", "")[:36]
                            label = f"{tid}  \u2014  {short}  ({c.get('status', '')})"
                        except Exception:
                            pass
                    tasks.append((tid, label))
            if not tasks:
                menu.add_command(label="(no task contexts found)",
                                 command=lambda: task_var.set(""))
                return
            for tid, lbl in tasks:
                menu.add_command(
                    label=lbl,
                    command=lambda t=tid: (task_var.set(t), _do_refresh()),
                )
            if not task_var.get() and tasks:
                task_var.set(tasks[0][0])

        def _render_plan(ctx: dict):
            steps = ctx.get("plan_steps", [])
            cs = ctx.get("current_step", 0)
            st = ctx.get("status", "")
            plan_txt.config(state="normal")
            plan_txt.delete("1.0", tk.END)
            plan_txt.insert(tk.END, ctx.get("title", "(untitled)") + "\n\n", "header")
            for key, lbl in (("status", "status"), ("parent_task_id", "parent"),
                              ("module_path", "module"), ("usage", "usage")):
                val = (ctx.get(key) or "").strip()
                if val:
                    plan_txt.insert(tk.END, f"{lbl+':':<10} {val}\n", "meta")
            plan_txt.insert(tk.END,
                f"{'step:':<10} {min(cs, len(steps))}/{len(steps)}\n", "meta")
            notes = (ctx.get("notes") or "").strip()
            if notes:
                plan_txt.insert(tk.END, f"{'notes:':<10} {notes}\n", "meta")
            subs = ctx.get("subtasks", [])
            if subs:
                plan_txt.insert(tk.END, "\nSub-tasks:\n", "meta")
                for s in subs:
                    icon = "\u2713" if s.get("status") == "completed" else "\u00b7"
                    plan_txt.insert(
                        tk.END, f"  {icon} {s.get('task_id', '')}  {s.get('title', '')}\n", "sub")
            plan_txt.insert(tk.END, "\nPlan:\n", "meta")
            all_done = (st == "completed")
            for i, step in enumerate(steps):
                if all_done or i < cs:
                    marker, tag = "\u2713", "done"
                elif i == cs:
                    marker, tag = "\u2192", "active"
                else:
                    marker, tag = "\u25cb", "pending"
                plan_txt.insert(tk.END, f"  {marker}  {i + 1}. {step}\n", tag)
            plan_txt.config(state="disabled")

        def _render_log(task_id: str):
            lpath = _os.path.join(_TASKS_DIR, task_id, "log.jsonl")
            log_txt.config(state="normal")
            log_txt.delete("1.0", tk.END)
            if not _os.path.exists(lpath):
                log_txt.insert(tk.END, "(no log entries yet)\n", "body")
                log_txt.config(state="disabled")
                return
            try:
                with open(lpath) as _f:
                    lines = [ln.strip() for ln in _f if ln.strip()]
            except Exception:
                lines = []
            for raw in lines[-120:]:
                try:
                    e = _json.loads(raw)
                    ts = e.get("ts", 0)
                    lv = e.get("level", "info")
                    body = e.get("text", "")
                    dt_s = (_dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                            if ts else "--:--:--")
                    lv_tag = (f"lv_{lv}"
                              if lv in ("plan", "execute", "validate", "error", "info")
                              else "lv_info")
                    log_txt.insert(tk.END, f"{dt_s} ", "ts")
                    log_txt.insert(tk.END, f"{lv:<9}", lv_tag)
                    log_txt.insert(tk.END, body + "\n", "body")
                except Exception:
                    log_txt.insert(tk.END, raw + "\n", "body")
            log_txt.config(state="disabled")
            log_txt.see(tk.END)

        def _do_refresh():
            try:
                _populate_dropdown()
                tid = task_var.get()
                if tid:
                    ctx_p = _os.path.join(_TASKS_DIR, tid, "context.json")
                    if _os.path.exists(ctx_p):
                        with open(ctx_p) as _f:
                            ctx = _json.load(_f)
                        _render_plan(ctx)
                        _render_log(tid)
                    else:
                        plan_txt.config(state="normal")
                        plan_txt.delete("1.0", tk.END)
                        plan_txt.insert(tk.END,
                            f"(task '{tid}' not found \u2014 may have been deleted)\n", "meta")
                        plan_txt.config(state="disabled")
            except Exception:
                pass
            if _after_id[0]:
                try:
                    win.after_cancel(_after_id[0])
                except Exception:
                    pass
                _after_id[0] = None
            if auto_var.get():
                try:
                    if win.winfo_exists():
                        _after_id[0] = win.after(2000, _do_refresh)
                except Exception:
                    pass

        def _on_close():
            if _after_id[0]:
                try:
                    win.after_cancel(_after_id[0])
                except Exception:
                    pass
            self._ctx_win = None
            win.destroy()

        refresh_btn.config(command=_do_refresh)
        auto_var.trace_add("write", lambda *_: _do_refresh() if auto_var.get() else None)
        win.protocol("WM_DELETE_WINDOW", _on_close)
        _do_refresh()
