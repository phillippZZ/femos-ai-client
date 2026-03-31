"""
ui/base_app.py

Toolkit-agnostic base class for all FEMOS AI UIs.

Subclasses must implement the six abstract surface methods:
    _write(text, role)          — append a line to the chat display
    _set_busy(busy: bool)       — toggle input controls (True = at least one task active)
    _set_connected(connected)   — update the connection indicator
    _set_status(text)           — update the status / progress area
    _set_input(text)            — insert text into the input field
    _clear_chat()               — wipe the chat history display

Optional overrides:
    _dispatch(fn, *args)        — route callbacks to the right thread
    _on_mic_started()           — called when recording begins
    _on_mic_stopped()           — called when recording ends
    _on_task_started(task_id, title)        — task accepted by server
    _on_task_progress_log(task_id, level, text, ts) — real-time log entry
    _on_task_finished(task_id, success)     — task completed or failed

Public API (call from UI event handlers):
    send(text)                  — always sends as a new task (never steers)
    steer(text, task_id="")     — steer a specific (or latest) active task
    interrupt(task_id="")       — interrupt a specific (or all) tasks
    cancel_task(task_id)        — cancel a task by ID
    replace_task(task_id, text) — replace a task
    new_chat()                  — clears history and chat display
    toggle_mic()                — starts/stops microphone recording

Multi-task model:
    The UI always allows sending new messages (server enforces the cap).
    self._active_tasks tracks which task_ids are currently running.
    _set_busy(True) whenever any task is active; False when all finish.
"""


class BaseApp:
    def __init__(self, core):
        self._core = core
        self._busy = False
        self._recording = False
        self._was_connected = False
        self._active_tasks: dict = {}   # task_id → title
        self._task_stages: dict = {}    # task_id → current stage label ("Planning", "Executing", …)
        self._focused_task_id: str = "" # task currently receiving steer input ("" = new-task mode)

        core.on_connected    = lambda c: self._dispatch(self._on_connected, c)
        core.on_status       = lambda t: self._dispatch(self._on_status, t)
        core.on_thinking     = lambda t: self._dispatch(self._on_thinking, t)
        core.on_result       = lambda tid, t: self._dispatch(self._on_result, tid, t)
        core.on_error        = lambda t: self._dispatch(self._on_error, t)
        core.on_user_message = lambda t: self._dispatch(self._on_user_message, t)
        core.on_transcribed  = lambda t: self._dispatch(self._on_transcribed, t)
        core.on_task_started   = lambda tid, title: self._dispatch(self._on_task_started, tid, title)
        core.on_task_log       = lambda tid, lv, tx, ts: self._dispatch(self._on_task_log, tid, lv, tx, ts)
        core.on_task_completed = lambda tid: self._dispatch(self._on_task_completed, tid)
        core.on_task_failed    = lambda tid, err: self._dispatch(self._on_task_failed, tid, err)
        core.on_queue_full     = lambda msg: self._dispatch(self._on_queue_full, msg)

    # ── Thread dispatch (override for thread-safe UIs) ────────────────
    def _dispatch(self, fn, *args):
        fn(*args)

    # ── Abstract surface ──────────────────────────────────────────────
    def _write(self, text: str, role: str):
        raise NotImplementedError

    def _set_busy(self, busy: bool):
        raise NotImplementedError

    def _set_connected(self, connected: bool):
        raise NotImplementedError

    def _set_status(self, text: str):
        raise NotImplementedError

    def _set_input(self, text: str):
        raise NotImplementedError

    def _clear_chat(self):
        raise NotImplementedError

    # ── Core callback handlers ────────────────────────────────────────
    def _on_connected(self, connected: bool):
        self._set_connected(connected)
        if connected and self._was_connected is False:
            if self._busy:
                self._active_tasks.clear()
                self._set_busy(False)
                self._set_status("")
        elif not connected and self._was_connected:
            self._set_status("Disconnected\u2026 reconnecting")
        self._was_connected = connected

    def _on_status(self, text: str):
        if text.startswith("Session resumed"):
            self._write(f"({text})", "status")
            self._set_status("")
        else:
            self._set_status(text)

    def _on_thinking(self, text: str):
        self._write(text, "thinking")

    def _on_result(self, task_id: str, text: str):
        self._set_status("")
        label = f"[{task_id}] " if task_id else ""
        self._write(f"Assistant: {label}{text}", "assistant")

    def _on_error(self, text: str):
        self._write(f"[Error] {text}", "error")

    def _on_user_message(self, text: str):
        self._write(f"You: {text}", "you")
        self._set_status("Thinking\u2026")

    def _on_transcribed(self, text: str):
        self._set_status("")
        if text:
            self._set_input(text)
        else:
            self._write("(no speech detected)", "status")

    # ── Task lifecycle handlers ───────────────────────────────────────
    def _on_task_started(self, task_id: str, title: str):
        self._active_tasks[task_id] = title
        self._task_stages[task_id] = "Starting"
        short = title[:60] + ("…" if len(title) > 60 else "")
        self._write(f"▶ Task {task_id}: {short}", "status")
        self._set_busy(True)
        self._on_task_started_hook(task_id, title)

    def _on_task_log(self, task_id: str, level: str, text: str, ts: int):
        # Update stage label so the UI bar reflects live progress
        stage_map = {
            "plan":     "Planning",
            "execute":  "Executing",
            "validate": "Validating",
            "error":    "Error",
        }
        if level in stage_map and task_id in self._task_stages:
            self._task_stages[task_id] = stage_map[level]
        self._on_task_progress_log(task_id, level, text, ts)

    def _on_task_completed(self, task_id: str):
        self._active_tasks.pop(task_id, None)
        self._task_stages.pop(task_id, None)
        if self._focused_task_id == task_id:
            self._focused_task_id = ""
            self._on_focus_cleared()
        self._write(f"\u2713 Task {task_id} completed.", "status")
        if not self._active_tasks:
            self._set_busy(False)
            self._set_status("")
        self._on_task_finished(task_id, success=True)

    def _on_task_failed(self, task_id: str, error: str):
        self._active_tasks.pop(task_id, None)
        self._task_stages.pop(task_id, None)
        if self._focused_task_id == task_id:
            self._focused_task_id = ""
            self._on_focus_cleared()
        label = f"Task {task_id} cancelled/failed" + (f": {error}" if error else ".")
        self._write(label, "error")
        if not self._active_tasks:
            self._set_busy(False)
            self._set_status("")
        self._on_task_finished(task_id, success=False)

    def _on_queue_full(self, msg: dict):
        reason = msg.get("reason", "")
        total  = msg.get("active", "?")
        limit  = msg.get("limit", "?")
        if reason == "has_tasks":
            ids = msg.get("active_ids", [])
            ids_str = ", ".join(ids) if ids else "running"
            self._write(
                f"[Queue] Server at capacity ({total}/{limit}). "
                f"Your tasks are still running ({ids_str}). "
                "Wait for one to finish or use cancel_task / replace_task.",
                "error"
            )
        else:
            self._write(
                f"[Queue] Server at capacity ({total}/{limit}). "
                "You have no active tasks \u2014 you have priority. Please retry in a moment.",
                "status"
            )

    # ── Optional subclass hooks (override without calling super) ─────
    def _on_task_started_hook(self, task_id: str, title: str):
        pass

    def _on_task_progress_log(self, task_id: str, level: str, text: str, ts: int):
        """Default: show plan/execute/validate logs as thinking text."""
        icon = {"plan": "📋", "execute": "⚙", "validate": "✔", "info": "ℹ", "error": "✖"}.get(level, "·")
        self._write(f"  {icon} [{task_id}] {text}", "thinking")

    def _on_task_finished(self, task_id: str, success: bool):
        pass

    def _on_focus_cleared(self):
        """Called when the focused task ends or focus is removed. Override in UI subclasses."""
        pass

    # ── Mic state hooks (optional UI override) ────────────────────────
    def _on_mic_started(self):
        pass

    def _on_mic_stopped(self):
        pass

    # ── Actions ───────────────────────────────────────────────────────
    def send(self, text: str):
        """Always sends a new task request (server may return queue_full)."""
        text = text.strip()
        if not text:
            return
        if not self._core.is_connected():
            self._write("Not connected to server. Retrying\u2026", "status")
            return
        self._core.send_text(text)

    def steer(self, text: str, task_id: str = ""):
        """Steer a specific task (or the latest active one)."""
        text = text.strip()
        if not text:
            return
        self._write(f"[Steer → {task_id or 'latest'}]: {text}", "you")
        self._core.steer(text, task_id)

    def interrupt(self, task_id: str = ""):
        """Interrupt a specific task or all tasks."""
        self._core.interrupt(task_id)
        label = f"task {task_id}" if task_id else "all tasks"
        self._write(f"Interrupted {label}.", "status")
        if not task_id:
            self._active_tasks.clear()
            self._task_stages.clear()
            self._focused_task_id = ""
            self._set_busy(False)
        elif task_id == self._focused_task_id:
            self._focused_task_id = ""
            self._on_focus_cleared()

    def cancel_task(self, task_id: str):
        self._core.cancel_task(task_id)

    def replace_task(self, task_id: str, text: str):
        self._core.replace_task(task_id, text)

    def new_chat(self):
        self._core.new_chat()
        self._clear_chat()
        self._active_tasks.clear()
        self._task_stages.clear()
        self._focused_task_id = ""
        self._set_status("")
        self._set_busy(False)
        self._write("(New conversation started)", "assistant")

    def toggle_mic(self):
        if not self._recording:
            self._recording = True
            self._write("\U0001f3a4 Recording\u2026 (click \u23f9 to stop)", "status")
            self._on_mic_started()
            self._core.start_recording()
        else:
            self._recording = False
            self._set_status("Transcribing\u2026")
            self._on_mic_stopped()
            self._core.stop_recording()
