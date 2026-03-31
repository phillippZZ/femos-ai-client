"""
core/builtins/background_skill.py

Builtins that let the AI start, stop, and list persistent background skill loops.

A background skill is a user skill that runs on a schedule in a daemon thread and
reports results/errors via skill_runtime.emit() — the same protocol one-shot skills use.
"""

import importlib
import json
import sys

from core import skill_runtime
from core.config import SKILLS_DIR
import os


def start_background_skill(name: str, args: dict = None,
                            interval: int = 60, task_id: str = "") -> str:
    """
    Start running a user skill in the background on a repeating schedule.

    The skill's SKILL_FN is called every `interval` seconds.
    Results and errors are emitted automatically via the skill_runtime protocol.

    Args:
        name:     The snake_case module name of the skill (must already be registered).
        args:     Keyword arguments to pass to the skill on each call.
        interval: How many seconds between each call (default 60).
        task_id:  Optional task context to associate events with.
    """
    args = args or {}

    # Load the skill module fresh
    skill_path = os.path.join(SKILLS_DIR, f"{name}.py")
    if not os.path.exists(skill_path):
        return f"Error: skill file '{name}.py' not found. Create the skill first."

    full_module = f"skills.{name}"
    try:
        if full_module in sys.modules:
            module = importlib.reload(sys.modules[full_module])
        else:
            module = importlib.import_module(full_module)
    except Exception as e:
        return f"Error: could not import skill '{name}': {e}"

    if not hasattr(module, "SKILL_FN"):
        return f"Error: skill '{name}' does not define SKILL_FN."

    fn = module.SKILL_FN
    skill_runtime.start_background(name, fn, args, interval=interval, task_id=task_id)
    return (
        f"Background skill '{name}' started. "
        f"It will run every {interval}s and emit results via the skill_event protocol. "
        f"Use stop_background_skill('{name}') to stop it."
    )


def stop_background_skill(name: str) -> str:
    """Stop a running background skill loop."""
    if skill_runtime.stop_background(name):
        return f"Background skill '{name}' stopped."
    return f"No background skill named '{name}' was running."


def list_background_skills() -> str:
    """
    List all currently registered background skill loops with their status.
    Returns a JSON summary.
    """
    entries = skill_runtime.list_background()
    if not entries:
        return "No background skills are currently running."
    return json.dumps(entries, indent=2)


# ── SKILL_FNS / SKILL_DEFS export ────────────────────────────────────────────

SKILL_FNS = [start_background_skill, stop_background_skill, list_background_skills]

SKILL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "start_background_skill",
            "description": (
                "Start a registered user skill running in a persistent background loop. "
                "The skill is called every `interval` seconds. Results, errors, and status "
                "are emitted automatically using the skill_runtime protocol — visible in the "
                "UI task log and forwarded to the server without any polling. "
                "Use for persistent tasks like email monitoring, price watchers, system health checks. "
                "The skill must already be created and validated before starting it in background mode."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Snake-case module name of the skill to run (e.g. 'email_manager').",
                    },
                    "args": {
                        "type": "object",
                        "description": "Keyword arguments forwarded to the skill on every call.",
                    },
                    "interval": {
                        "type": "integer",
                        "description": "Seconds between each call. Default 60.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional task context ID to associate emitted events with.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_background_skill",
            "description": "Stop a running background skill loop by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill name to stop.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_background_skills",
            "description": "List all currently running background skill loops with their interval and alive status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
