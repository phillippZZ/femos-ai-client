"""
core/skill_runtime.py

Shared communication protocol for ALL FEMOS skills — one-shot and persistent alike.

Every skill (user-created or builtin) can import this module to:
  - emit structured events to the server and the UI at any time
  - start/stop long-running background loops (persistent skills)

Wire-up (done by WsClient at startup):
    from core import skill_runtime
    skill_runtime._set_sender(send_fn, log_fn)

The sender is set once and never changes during a session.  Skills don't need to
know anything about WebSockets — they just call emit() and the plumbing handles
routing to both the local UI log and the server.

Event flow:
    skill calls emit()
        → local: log_fn(task_id, level, text, ts)    — shows up in UI task log
        → remote: send_fn({type:"skill_event", ...}) — forwarded to server

Background skill flow:
    start_background(name, fn, args, interval)
        → daemon thread runs fn(**args) every interval seconds
        → results/errors emitted automatically via emit()
        → server receives skill_event with event="result"|"error"|"log"
"""

import logging
import threading
import time
import traceback as _tb

logger = logging.getLogger("femos.skill_runtime")

# ── Internal state ────────────────────────────────────────────────────────────

_send_fn = None       # callable({type: "skill_event", ...})  — set by WsClient
_log_fn  = None       # callable(task_id, level, text, ts)    — set by WsClient
_lock = threading.Lock()
_background: dict = {}   # skill_name → {"thread": Thread, "stop": Event, "interval": int, "task_id": str}


def _set_sender(send_fn, log_fn=None):
    """Called once by WsClient during startup to inject the communication channel."""
    global _send_fn, _log_fn
    _send_fn = send_fn
    _log_fn  = log_fn


# ── Public API ────────────────────────────────────────────────────────────────

def emit(skill_name: str, event_type: str, data, *, task_id: str = ""):
    """
    Send a structured event from a skill to both the local UI log and the server.

    Args:
        skill_name:  The skill's registered name (e.g. "email_manager").
        event_type:  "result" | "error" | "log" | "notification" | "warning"
        data:        Any str()-able payload.
        task_id:     Optional — associates the event with a task context.

    This is fire-and-forget.  Skills should call it for every meaningful outcome,
    whether they are one-shot (call it once with the final result) or persistent
    (call it every time something interesting happens).
    """
    ts = int(time.time())
    text = f"[{skill_name}] {data}"

    # 1. Local UI log (immediate — no network round-trip)
    if _log_fn is not None:
        try:
            level = "error" if event_type == "error" else "info"
            _log_fn(task_id or "", level, text, ts)
        except Exception:
            pass

    # 2. Remote server notification
    if _send_fn is not None:
        try:
            _send_fn({
                "type":    "skill_event",
                "skill":   skill_name,
                "event":   event_type,
                "data":    str(data),
                "task_id": task_id or "",
                "ts":      ts,
            })
        except Exception as e:
            logger.warning("[skill_runtime] emit failed for %s: %s", skill_name, e)
    else:
        logger.debug("[skill_runtime] no sender — emit dropped: %s %s", skill_name, event_type)


def start_background(skill_name: str, fn, args: dict = None,
                     interval: int = 60, *, task_id: str = ""):
    """
    Run fn(**args) repeatedly every `interval` seconds in a daemon thread.

    Results are automatically emitted:
      - successful call  → emit(..., "result", result)
      - exception        → emit(..., "error", traceback)
      - on stop          → emit(..., "log", "stopped")

    If a background task with the same name is already running, it is stopped first.

    Args:
        skill_name: used as the thread name and in all emitted events.
        fn:         the callable to run on each tick.
        args:       keyword arguments forwarded to fn on every call.
        interval:   seconds between calls (measured after each call returns).
        task_id:    optional task context to associate events with.
    """
    args = args or {}
    stop_ev = threading.Event()

    def _loop():
        emit(skill_name, "log", f"background started (interval={interval}s)", task_id=task_id)
        while not stop_ev.wait(timeout=interval):
            try:
                result = fn(**args)
                emit(skill_name, "result", result, task_id=task_id)
            except Exception as e:
                tb = _tb.format_exc(limit=4)
                emit(skill_name, "error",
                     f"{type(e).__name__}: {e}\n{tb.strip()}", task_id=task_id)
        emit(skill_name, "log", "background stopped", task_id=task_id)

    with _lock:
        # Stop any existing runner for this name
        existing = _background.get(skill_name)
        if existing:
            existing["stop"].set()

        t = threading.Thread(target=_loop, daemon=True, name=f"bg-{skill_name}")
        _background[skill_name] = {
            "thread":   t,
            "stop":     stop_ev,
            "interval": interval,
            "task_id":  task_id,
        }
        t.start()


def stop_background(skill_name: str) -> bool:
    """
    Stop a running background skill.  Returns True if it was running, False otherwise.
    """
    with _lock:
        entry = _background.pop(skill_name, None)
    if entry:
        entry["stop"].set()
        return True
    return False


def list_background() -> list:
    """
    Return a list of currently registered background skills with their status.
    """
    with _lock:
        return [
            {
                "name":     k,
                "interval": v["interval"],
                "task_id":  v["task_id"],
                "alive":    v["thread"].is_alive(),
            }
            for k, v in _background.items()
        ]
