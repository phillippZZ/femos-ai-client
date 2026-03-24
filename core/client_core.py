"""
ui/client_core.py

Platform-agnostic business logic controller.
Owns the WebSocket client, microphone, and transcription.
Any UI (macOS, Arduino, HTML) instantiates this, wires the callbacks, and calls the methods.

Callbacks (assign before calling start()):
    on_status(text: str)          — AI thinking / skill-in-progress status
    on_result(text: str)          — final assistant reply
    on_error(text: str)           — error message
    on_connected(connected: bool) — WebSocket connection state changed
    on_user_message(text: str)    — echoes back what the user just sent (for display)
    on_transcribed(text: str)     — speech-to-text result ready

Methods:
    start()                       — begin WS connection loop (background thread)
    send_text(text: str)          — send a text message to the server
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
        self.on_result: callable = lambda text: None
        self.on_error: callable = lambda text: None
        self.on_thinking: callable = lambda text: None
        self.on_connected: callable = lambda connected: None
        self.on_user_message: callable = lambda text: None
        self.on_transcribed: callable = lambda text: None

        # ── WebSocket client ─────────────────────────────────────────
        self._ws = WsClient(SERVER_URL, CLIENT_SKILLS, CLIENT_TOOLS_CONFIG,
                            reload_skills, client_id=CLIENT_ID, history_path=HISTORY_PATH)
        self._ws.on_status    = lambda t: self.on_status(t)
        self._ws.on_result    = lambda t: self.on_result(t)
        self._ws.on_error     = lambda t: self.on_error(t)
        self._ws.on_thinking  = lambda t: self.on_thinking(t)
        self._ws.on_connected = lambda c: self.on_connected(c)
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

    def interrupt(self):
        """Stop the current agent run immediately."""
        self._ws.interrupt()

    def steer(self, text: str):
        """Inject a steering message into the running agent."""
        self._ws.steer(text)

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
