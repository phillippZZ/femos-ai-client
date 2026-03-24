"""
ui/base_app.py

Toolkit-agnostic base class for all FEMOS AI UIs.

Subclasses must implement the six abstract surface methods:
    _write(text, role)          — append a line to the chat display
    _set_busy(busy)             — toggle input controls / send↔steer label
    _set_connected(connected)   — update the connection indicator
    _set_status(text)           — update the status / progress area
    _set_input(text)            — insert text into the input field
    _clear_chat()               — wipe the chat history display

Subclasses may override:
    _dispatch(fn, *args)        — route callbacks to the right thread
                                  (default: call directly; tkinter needs root.after)
    _on_mic_started()           — called when recording begins (update mic button)
    _on_mic_stopped()           — called when recording ends (update mic button)

Public API (call from UI event handlers):
    send(text)      — dispatches as send or steer depending on busy state
    interrupt()     — stops the running agent
    new_chat()      — clears history and chat display
    toggle_mic()    — starts/stops microphone recording
"""


class BaseApp:
    def __init__(self, core):
        self._core = core
        self._busy = False
        self._recording = False
        self._was_connected = False

        core.on_connected    = lambda c: self._dispatch(self._on_connected, c)
        core.on_status       = lambda t: self._dispatch(self._on_status, t)
        core.on_thinking     = lambda t: self._dispatch(self._on_thinking, t)
        core.on_result       = lambda t: self._dispatch(self._on_result, t)
        core.on_error        = lambda t: self._dispatch(self._on_error, t)
        core.on_user_message = lambda t: self._dispatch(self._on_user_message, t)
        core.on_transcribed  = lambda t: self._dispatch(self._on_transcribed, t)

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
            # Reconnected — reset any stale busy state on the client.
            # The server will send a session_resumed event if history was restored.
            if self._busy:
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

    def _on_result(self, text: str):
        self._set_status("")
        self._write(f"Assistant: {text}", "assistant")
        self._set_busy(False)

    def _on_error(self, text: str):
        self._write(f"[Error] {text}", "error")
        self._set_busy(False)

    def _on_user_message(self, text: str):
        self._write(f"You: {text}", "you")
        self._set_status("Thinking\u2026")

    def _on_transcribed(self, text: str):
        self._set_status("")
        if text:
            self._set_input(text)
        else:
            self._write("(no speech detected)", "status")

    # ── Mic state hooks (optional UI override) ────────────────────────
    def _on_mic_started(self):
        pass

    def _on_mic_stopped(self):
        pass

    # ── Actions ───────────────────────────────────────────────────────
    def send(self, text: str):
        text = text.strip()
        if not text:
            return
        if not self._core.is_connected():
            self._write("Not connected to server. Retrying\u2026", "status")
            return
        if self._busy:
            self._write(f"[Steer]: {text}", "you")
            self._core.steer(text)
        else:
            self._set_busy(True)
            self._core.send_text(text)

    def interrupt(self):
        self._core.interrupt()
        self._write("Interrupted.", "status")
        self._set_busy(False)

    def new_chat(self):
        self._core.new_chat()
        self._clear_chat()
        self._set_status("")
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
