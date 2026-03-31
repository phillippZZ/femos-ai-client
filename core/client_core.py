"""
ui/client_core.py

Platform-agnostic business logic controller.
Owns the WebSocket client, microphone, and transcription.
Any UI (macOS, Arduino, HTML) instantiates this, wires the callbacks, and calls the methods.

Callbacks (assign before calling start()):
    on_status(text: str)                    — AI thinking / skill-in-progress status
    on_result(task_id: str, text: str)      — final assistant reply for a task
    on_error(text: str)                     — error message
    on_connected(connected: bool)           — WebSocket connection state changed
    on_user_message(text: str)              — echoes back what the user just sent (for display)
    on_transcribed(text: str)               — speech-to-text result ready
    on_thinking(text: str)                  — verbose reasoning trace
    on_task_started(task_id, title)         — a new task was accepted by the server
    on_task_log(task_id, level, text, ts)   — real-time task log entry
    on_task_completed(task_id)              — task finished successfully
    on_task_failed(task_id, error)          — task cancelled or errored
    on_queue_full(msg: dict)                — server is at capacity

Methods:
    start()                       — begin WS connection loop (background thread)
    send_text(text: str)          — send a new task request to the server
    cancel_task(task_id: str)     — cancel a running task
    replace_task(task_id, text)   — cancel task_id and start a new one
    start_recording()             — begin mic capture
    stop_recording()              — stop capture, transcribe, fire on_transcribed
    new_chat()                    — clear server-side history
"""

import threading
from core.config import SERVER_URL, CLIENT_ID, HISTORY_PATH
from core.transcribe import Recorder
from core.tools import (SKILLS as CLIENT_SKILLS, TOOLS_CONFIG as CLIENT_TOOLS_CONFIG,
                        reload_skills, set_reregister_callback)
from core.ws_client import WsClient


class ClientCore:
    def __init__(self):
        self._recorder = Recorder()
        self._recording = False

        # ── Callbacks ────────────────────────────────────────────────
        self.on_status: callable = lambda text: None
        self.on_result: callable = lambda task_id, text: None
        self.on_error: callable = lambda text: None
        self.on_thinking: callable = lambda text: None
        self.on_connected: callable = lambda connected: None
        self.on_user_message: callable = lambda text: None
        self.on_transcribed: callable = lambda text: None
        # Task lifecycle
        self.on_task_started: callable = lambda task_id, title: None
        self.on_task_log: callable = lambda task_id, level, text, ts: None
        self.on_task_completed: callable = lambda task_id: None
        self.on_task_failed: callable = lambda task_id, error: None
        self.on_queue_full: callable = lambda msg: None

        # ── WebSocket client ─────────────────────────────────────────
        self._ws = WsClient(SERVER_URL, CLIENT_SKILLS, CLIENT_TOOLS_CONFIG,
                            reload_skills, client_id=CLIENT_ID, history_path=HISTORY_PATH)
        self._ws.on_status    = lambda t: self.on_status(t)
        self._ws.on_result    = lambda tid, t: self.on_result(tid, t)
        self._ws.on_error     = lambda t: self.on_error(t)
        self._ws.on_thinking  = lambda t: self.on_thinking(t)
        self._ws.on_connected = lambda c: self.on_connected(c)
        self._ws.on_task_started   = lambda tid, title: self.on_task_started(tid, title)
        self._ws.on_task_log       = lambda tid, lv, tx, ts: self.on_task_log(tid, lv, tx, ts)
        self._ws.on_task_completed = lambda tid: self.on_task_completed(tid)
        self._ws.on_task_failed    = lambda tid, err: self.on_task_failed(tid, err)
        self._ws.on_queue_full     = lambda msg: self.on_queue_full(msg)
        set_reregister_callback(self._ws.reregister_skills)

    def start(self):
        """Start the WebSocket connection loop in a background thread."""
        self._ws.start()

    # ── Messaging ─────────────────────────────────────────────────────
    def send_text(self, text: str):
        text = text.strip()
        if not text:
            return
        self.on_user_message(text)
        self._ws.send_message(text)

    def is_connected(self) -> bool:
        return self._ws._ws is not None

    def new_chat(self):
        self._ws.clear_history()

    def interrupt(self, task_id: str = ""):
        """Stop a specific task (or all tasks if no task_id given)."""
        self._ws.interrupt(task_id)

    def steer(self, text: str, task_id: str = ""):
        """Inject a steering message into an active task."""
        self._ws.steer(text, task_id)

    def cancel_task(self, task_id: str):
        """Cancel a running task by its task_id."""
        self._ws.cancel_task(task_id)

    def replace_task(self, task_id: str, new_text: str):
        """Cancel task_id (or oldest if empty) and start a new task."""
        self._ws.replace_task(task_id, new_text)

    # ── Microphone ────────────────────────────────────────────────────
    def start_recording(self):
        if self._recording:
            return
        self._recording = True
        self._recorder.start()

    def stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        threading.Thread(target=self._finish_recording, daemon=True).start()

    def _finish_recording(self):
        text = self._recorder.stop()
        self.on_transcribed(text or "")
