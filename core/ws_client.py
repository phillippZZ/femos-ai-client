"""
ui/ws_client.py

Platform-agnostic WebSocket client core.
Handles connection, reconnection, skill registration, event dispatch, and tool execution.

Usage by any UI:
    client = WsClient(server_url, skills, tools_config, reload_skills_fn)
    client.on_status    = fn(text: str)
    client.on_result    = fn(text: str)
    client.on_error     = fn(text: str)
    client.on_tool_call = fn(msg: dict)   # override only if you want custom dispatch
    client.on_connected = fn(connected: bool)
    client.start()          # begins asyncio loop in background thread
    client.send_message(text)
    client.clear_history()
"""

import asyncio
import json
import logging
import os
import threading

import websockets

from core.tools import reload_skills as _reload_skills, TOOLS_CONFIG as _TOOLS_CONFIG

logger = logging.getLogger("femos.client")


class WsClient:
    def __init__(self, server_url: str, skills: dict, tools_config: list,
                 reload_fn=None, client_id: str = "", history_path: str = ""):
        self.server_url = server_url
        self.skills = skills
        self.tools_config = tools_config
        self._reload_fn = reload_fn or _reload_skills
        self._client_id = client_id
        self._history_path = history_path
        self._history: list = self._load_history()

        self._ws = None
        self._loop = asyncio.new_event_loop()

        # ── Callbacks (set by the UI layer) ──────────────────────────
        self.on_status: callable = lambda text: None
        self.on_result: callable = lambda text: None
        self.on_error: callable = lambda text: None
        self.on_thinking: callable = lambda text: None
        self.on_connected: callable = lambda connected: None
        # on_tool_call: override if the UI wants to intercept before exec
        self.on_tool_call: callable = None  # defaults to self._exec_tool

    def start(self):
        """Start the WebSocket loop in a background daemon thread."""
        threading.Thread(target=self._run_loop, daemon=True).start()

    # ── History persistence ───────────────────────────────────────────
    def _load_history(self) -> list:
        if self._history_path and os.path.exists(self._history_path):
            try:
                with open(self._history_path) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    logger.info("[History] loaded %d msgs from %s", len(data), self._history_path)
                    return data
            except Exception as e:
                logger.warning("[History] failed to load: %s", e)
        return []

    def _save_history(self, history: list):
        if not self._history_path:
            return
        try:
            with open(self._history_path, "w") as f:
                json.dump(history, f)
            self._history = history
        except Exception as e:
            logger.warning("[History] failed to save: %s", e)

    def clear_local_history(self):
        self._history = []
        if self._history_path:
            try:
                os.remove(self._history_path)
            except OSError:
                pass

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        self.on_connected(False)
        retry_delay = 2
        while True:
            try:
                async with websockets.connect(self.server_url) as ws:
                    self._ws = ws
                    self.on_connected(True)
                    # Send identity + stored history so the stateless server can restore context
                    await ws.send(json.dumps({
                        "type": "hello",
                        "client_id": self._client_id,
                        "history": self._history,
                    }))
                    await ws.send(json.dumps({
                        "type": "register_skills",
                        "skills": self.tools_config,
                    }))
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            self._dispatch(msg)
                        except Exception:
                            pass
            except Exception:
                pass
            self._ws = None
            self.on_connected(False)
            await asyncio.sleep(retry_delay)

    def _dispatch(self, msg: dict):
        t = msg.get("type")
        if t == "status":
            self.on_status(msg.get("text", ""))
        elif t == "thinking":
            self.on_thinking(msg.get("text", ""))
        elif t == "result":
            self.on_result(msg.get("text", ""))
        elif t == "error":
            self.on_error(msg.get("text", ""))
        elif t == "session_resumed":
            n = msg.get("history_len", 0)
            self.on_status(f"Session resumed ({n} messages)")
        elif t == "history_snapshot":
            history = msg.get("history")
            if isinstance(history, list):
                self._save_history(history)
        elif t == "tool_call":
            handler = self.on_tool_call or self._exec_tool_threaded
            handler(msg)

    def _exec_tool_threaded(self, msg: dict):
        threading.Thread(target=self._exec_tool, args=(msg,), daemon=True).start()

    def _exec_tool(self, msg: dict):
        """Execute a client skill and send the result back to the server."""
        self._reload_fn()
        func_name = msg["name"]
        args = msg.get("args", {})
        arg_preview = {k: repr(v)[:60] for k, v in args.items()} if isinstance(args, dict) else args
        logger.info("[ACT] client skill: %s(%s)", func_name, arg_preview)
        if func_name in self.skills:
            try:
                result = self.skills[func_name](**args)
                result_str = str(result)
                preview = result_str[:150] + ("\u2026" if len(result_str) > 150 else "")
                logger.info("[OBSERVE] %s -> %s", func_name, preview)
            except Exception as e:
                result = f"SKILL_ERROR: {e}"
                logger.error("[OBSERVE] %s raised: %s", func_name, e)
        else:
            result = f"SKILL_NOT_FOUND: '{func_name}' is not a registered client skill."
            logger.warning("[OBSERVE] %s not found in registered skills", func_name)
        self.send({
            "type": "tool_result",
            "call_id": msg["call_id"],
            "name": func_name,
            "content": str(result),
        })

    # ── Public API ───────────────────────────────────────────────────
    def send(self, payload: dict):
        """Thread-safe send of a raw payload dict."""
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(payload)), self._loop
            )

    def send_message(self, text: str):
        self.send({"type": "message", "text": text})

    def steer(self, text: str):
        """Inject a steering message into the currently running agent."""
        self.send({"type": "steer", "text": text})

    def interrupt(self):
        """Tell the server to stop the current agent run immediately."""
        self.send({"type": "interrupt"})

    def clear_history(self):
        self.clear_local_history()
        self.send({"type": "clear_history"})

    def reregister_skills(self):
        self.send({"type": "register_skills", "skills": self.tools_config})
