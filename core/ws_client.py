"""
core/ws_client.py

Platform-agnostic WebSocket client core.
Handles connection, reconnection, skill registration, event dispatch, and tool execution.

New event types handled (server → client):
    task_started  — {task_id, title}
    task_log      — {task_id, level, text, ts}  (real-time progress)
    task_completed — {task_id}
    task_failed   — {task_id, error}
    queue_full    — {active, limit, active_ids, message}

New methods (client → server):
    cancel_task(task_id)
    replace_task(task_id_or_empty, new_text)

Callbacks added:
    on_task_started(task_id, title)
    on_task_log(task_id, level, text, ts)
    on_task_completed(task_id)
    on_task_failed(task_id, error)
    on_queue_full(msg: dict)

Usage by any UI:
    client = WsClient(server_url, skills, tools_config, reload_skills_fn)
    client.on_status        = fn(text: str)
    client.on_result        = fn(task_id: str, text: str)
    client.on_error         = fn(text: str)
    client.on_task_started  = fn(task_id: str, title: str)
    client.on_task_log      = fn(task_id: str, level: str, text: str, ts: int)
    client.on_task_completed = fn(task_id: str)
    client.on_task_failed   = fn(task_id: str, error: str)
    client.on_queue_full    = fn(msg: dict)
    client.on_connected     = fn(connected: bool)
    client.start()
    client.send_message(text)
    client.cancel_task(task_id)
    client.replace_task(task_id, new_text)
    client.clear_history()
"""

import asyncio
import json
import logging
import os
import threading

import websockets

from core.tools import reload_skills as _reload_skills, TOOLS_CONFIG as _TOOLS_CONFIG
from core import skill_runtime as _skill_runtime

logger = logging.getLogger("femos.client")


def _build_workspace_tasks_snapshot() -> list:
    """
    Read workspace/tasks/ and return compact summaries of in-progress task contexts.
    Sent to the server in every register_skills message so the orchestrator can
    inject knowledge of unfinished work into the prompt without a tool call.
    """
    import json as _json
    try:
        from core.config import TASKS_DIR
    except ImportError:
        return []
    if not os.path.isdir(TASKS_DIR):
        return []
    summaries = []
    for task_id in os.listdir(TASKS_DIR):
        if not os.path.isdir(os.path.join(TASKS_DIR, task_id)):
            continue
        ctx_path = os.path.join(TASKS_DIR, task_id, "context.json")
        if not os.path.exists(ctx_path):
            continue
        try:
            with open(ctx_path) as _f:
                ctx = _json.load(_f)
            status = ctx.get("status", "")
            artifacts = ctx.get("artifacts") or {}
            # Always include non-completed tasks.
            # Also include completed tasks that have artifacts, so the stale
            # detector can catch broken skills after version updates etc.
            if status == "completed" and not artifacts:
                continue
            # Flag tasks where current_step > 0 but artifacts don't fully cover
            # the completed steps. This means the task advanced without recording
            # what it built — verification is needed before continuing.
            needs_verification = (
                status != "completed"
                and ctx.get("current_step", 0) > 0
                and len(artifacts) < ctx.get("current_step", 0)
            )
            summaries.append({
                "task_id": task_id,
                "title": ctx.get("title", ""),
                "status": status,
                "current_step": ctx.get("current_step", 0),
                "total_steps": len(ctx.get("plan_steps", [])),
                "plan_steps": ctx.get("plan_steps", []),
                "notes": ctx.get("notes", ""),
                "module_path": ctx.get("module_path", ""),
                "artifacts": artifacts,
                "docs": ctx.get("docs", ""),
                "needs_verification": needs_verification,
            })
        except Exception:
            pass
    return summaries



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
        self.on_result: callable = lambda task_id, text: None
        self.on_error: callable = lambda text: None
        self.on_thinking: callable = lambda text: None
        self.on_connected: callable = lambda connected: None
        # Task lifecycle callbacks
        self.on_task_started: callable = lambda task_id, title: None
        self.on_task_log: callable = lambda task_id, level, text, ts: None
        self.on_task_completed: callable = lambda task_id: None
        self.on_task_failed: callable = lambda task_id, error: None
        self.on_queue_full: callable = lambda msg: None
        # on_tool_call: override if the UI wants to intercept before exec
        self.on_tool_call: callable = None  # defaults to self._exec_tool

        # Buffer of recent background skill events — injected into the next user
        # message so the AI has context when the user reacts to a notification.
        self._skill_event_buffer: list = []   # list of str summaries
        self._skill_event_lock = threading.Lock()

        # Wire skill_runtime so all skills (one-shot and persistent) can emit events.
        # Done once here; safe to call multiple times — _set_sender is idempotent.
        # Use lambdas that close over `self` so they always call the *current*
        # on_task_log/send callbacks even if the UI replaces them after startup.
        _skill_runtime._set_sender(
            send_fn=lambda payload: self._on_skill_emit(payload),
            log_fn=lambda tid, lvl, txt, ts: self.on_task_log(tid, lvl, txt, ts),
        )

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
                        "workspace_tasks": _build_workspace_tasks_snapshot(),
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
        task_id = msg.get("task_id", "")
        if t == "status":
            self.on_status(msg.get("text", ""))
        elif t == "thinking":
            self.on_thinking(msg.get("text", ""))
        elif t == "result":
            self.on_result(task_id, msg.get("text", ""))
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
        # ── Task lifecycle ───────────────────────────────────────────
        elif t == "task_started":
            self.on_task_started(task_id, msg.get("title", ""))
        elif t == "task_log":
            self.on_task_log(task_id, msg.get("level", "info"),
                             msg.get("text", ""), msg.get("ts", 0))
        elif t == "task_completed":
            self.on_task_completed(task_id)
        elif t == "task_failed":
            self.on_task_failed(task_id, msg.get("error", ""))
        elif t == "queue_full":
            self.on_queue_full(msg)
        # ── Skill events echoed back from server ───────────────────────────
        elif t == "skill_event":
            # The server echoes skill_event messages back as task_log so the UI
            # automatically captures them the same way it captures task progress.
            level = "error" if msg.get("event") == "error" else "info"
            skill = msg.get("skill", "?")
            event = msg.get("event", "event")
            data  = msg.get("data", "")
            ts    = msg.get("ts", 0)
            self.on_task_log(task_id, level, f"[{skill}:{event}] {data}", ts)
            # Also buffer server-echoed skill events so the next user message
            # carries them as context (in case the user is reacting to them).
            import time as _time
            summary = f"[{skill}:{event}] {data}"
            with self._skill_event_lock:
                self._skill_event_buffer.append(summary)

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

    def _on_skill_emit(self, payload: dict):
        """Called by skill_runtime.emit() for every local skill event.
        Buffers the event summary and forwards to the server."""
        skill = payload.get("skill", "?")
        event = payload.get("event", "event")
        data  = payload.get("data", "")
        summary = f"[{skill}:{event}] {data}"
        with self._skill_event_lock:
            self._skill_event_buffer.append(summary)
        self.send(payload)

    def _pop_skill_context(self) -> str:
        """Drain the skill event buffer and return a formatted context block,
        or empty string if nothing has accumulated since the last user message."""
        with self._skill_event_lock:
            if not self._skill_event_buffer:
                return ""
            lines = list(self._skill_event_buffer)
            self._skill_event_buffer.clear()
        header = (
            f"[Background skill events since your last message ({len(lines)} event(s))]\n"
            + "\n".join(f"  • {l}" for l in lines[-20:])   # cap at 20 most recent
            + "\n"
        )
        return header

    def send_message(self, text: str):
        prefix = self._pop_skill_context()
        full_text = prefix + text if prefix else text
        self.send({"type": "message", "text": full_text})

    def steer(self, text: str, task_id: str = ""):
        """Inject a steering message, optionally targeting a specific task."""
        payload = {"type": "steer", "text": text}
        if task_id:
            payload["task_id"] = task_id
        self.send(payload)

    def interrupt(self, task_id: str = ""):
        """Stop a specific task (or all tasks if no task_id given)."""
        payload = {"type": "interrupt"}
        if task_id:
            payload["task_id"] = task_id
        self.send(payload)

    def cancel_task(self, task_id: str):
        """Request the server to cancel a running task."""
        self.send({"type": "cancel_task", "task_id": task_id})

    def replace_task(self, task_id: str, new_text: str):
        """Cancel task_id (or oldest if empty) and immediately start new_text."""
        self.send({"type": "replace_task", "task_id": task_id, "text": new_text})

    def clear_history(self):
        self.clear_local_history()
        self.send({"type": "clear_history"})

    def reregister_skills(self):
        self.send({
            "type": "register_skills",
            "skills": self.tools_config,
            "workspace_tasks": _build_workspace_tasks_snapshot(),
        })
