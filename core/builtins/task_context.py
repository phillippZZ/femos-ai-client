"""
core/builtins/task_context.py

Builtins that let the AI model manage task context files.
Every task (and sub-task) has a JSON context file plus an append-only log.

Directory layout (under TASKS_DIR = ~/.femos/tasks/):
    <task_id>/
        context.json    — plan, status, sub-task refs, module locations
        log.jsonl       — append-only structured log (one JSON object per line)

Three skills are exposed to the LLM:
    task_init    — create or overwrite a task context (call at the start of any complex task)
    task_update  — update fields: status, current_step, module_path, usage, notes, docs,
                   artifacts (per-step artifact registry), add/remove sub-task, replace plan
    task_read    — read a task context (or list all tasks if no task_id given)

These are registered via SKILL_FNS / SKILL_DEFS (plural) so the builtin loader
can register all three from a single file.
"""

import json
import os
import time
import threading

from core.config import TASKS_DIR

_lock = threading.Lock()


def _task_dir(task_id: str) -> str:
    return os.path.join(TASKS_DIR, task_id)


def _context_path(task_id: str) -> str:
    return os.path.join(_task_dir(task_id), "context.json")


def _log_path(task_id: str) -> str:
    return os.path.join(_task_dir(task_id), "log.jsonl")


def _read_context(task_id: str) -> dict | None:
    p = _context_path(task_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _write_context(task_id: str, ctx: dict):
    d = _task_dir(task_id)
    os.makedirs(d, exist_ok=True)
    with open(_context_path(task_id), "w") as f:
        json.dump(ctx, f, indent=2)


def _append_log(task_id: str, level: str, text: str):
    d = _task_dir(task_id)
    os.makedirs(d, exist_ok=True)
    entry = json.dumps({"ts": int(time.time()), "level": level, "text": text})
    with open(_log_path(task_id), "a") as f:
        f.write(entry + "\n")


# ── task_init ────────────────────────────────────────────────────────────────

def task_init(task_id: str, title: str, plan_steps: list,
              parent_task_id: str = "") -> str:
    """
    Initialise (or reset) a task context file.

    Call this at the very start of any task that has more than one step, and
    for every sub-task you decompose it into.

    Args:
        task_id:        Unique ID.  Use the task_id injected into your system
                        prompt for the root task; generate sub-task IDs as
                        <parent_task_id>_sub_1, _sub_2, etc.
        title:          Short human-readable description of this task.
        plan_steps:     Ordered list of steps as plain-text strings.
        parent_task_id: Set to the parent's task_id when creating a sub-task.
    """
    if not task_id or not task_id.replace("_", "").replace("-", "").isalnum():
        return "Error: task_id must be alphanumeric (underscores/hyphens allowed)."
    if not plan_steps or not isinstance(plan_steps, list):
        return "Error: plan_steps must be a non-empty list of strings."

    ctx = {
        "task_id": task_id,
        "title": title,
        "parent_task_id": parent_task_id or None,
        "status": "in_progress",
        "plan_steps": [str(s) for s in plan_steps],
        "current_step": 0,
        "subtasks": [],          # list of {task_id, title, status}
        "module_path": None,     # path to created skill file (if any)
        "usage": None,           # how to call the resulting skill
        "notes": "",
        "docs": "",              # user-facing documentation / user manual (markdown)
        "artifacts": {},         # {"step_index": ["skill_name_or_filepath", ...]} — what was created per step
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }

    with _lock:
        _write_context(task_id, ctx)
        _append_log(task_id, "plan", f"Task initialised: {title}")
        for i, step in enumerate(plan_steps):
            _append_log(task_id, "plan", f"  Step {i + 1}: {step}")

    return f"Task context created for '{task_id}': {len(plan_steps)} steps planned."


# ── task_update ──────────────────────────────────────────────────────────────

def task_update(task_id: str, status: str = "", current_step: int = -1,
                module_path: str = "", usage: str = "", notes: str = "",
                set_docs: str = "", set_artifacts: dict = None,
                add_subtask_id: str = "", add_subtask_title: str = "",
                remove_subtask_id: str = "",
                replace_plan_steps: list = None) -> str:
    """
    Update fields in an existing task context.

    Call this when:
    - A step is completed: set current_step to the next step index.
    - A skill was created and validated: set set_artifacts={step_index: ["skill_name"]}.
    - A skill file was created: set module_path and usage.
    - The task finishes: set status to 'completed' or 'failed'.
    - Writing user docs: set set_docs to a markdown string describing the created system.
    - A sub-task was started: provide add_subtask_id + add_subtask_title.
    - A sub-task is no longer needed: provide remove_subtask_id.
    - The plan needs revision: provide replace_plan_steps with the new list.

    Omit (leave blank / None) any field you do not want to change.
    """
    with _lock:
        ctx = _read_context(task_id)
        if ctx is None:
            return f"Error: no context found for task_id '{task_id}'. Call task_init first."

        changed = []
        if status:
            ctx["status"] = status
            changed.append(f"status={status}")
        if current_step >= 0:
            ctx["current_step"] = current_step
            changed.append(f"current_step={current_step}")
        if module_path:
            ctx["module_path"] = module_path
            changed.append(f"module_path={module_path}")
        if usage:
            ctx["usage"] = usage
            changed.append(f"usage={usage}")
        if notes:
            ctx["notes"] = notes
            changed.append(f"notes updated")
        if set_docs:
            ctx["docs"] = set_docs
            changed.append("docs updated")
        if set_artifacts is not None:
            if not isinstance(ctx.get("artifacts"), dict):
                ctx["artifacts"] = {}
            for k, v in set_artifacts.items():
                ctx["artifacts"][str(k)] = list(v) if isinstance(v, list) else [str(v)]
            changed.append("artifacts updated")
        if add_subtask_id and add_subtask_title:
            ctx["subtasks"].append({
                "task_id": add_subtask_id,
                "title": add_subtask_title,
                "status": "in_progress",
            })
            changed.append(f"subtask added: {add_subtask_id}")
        # When a subtask completes, mark it in the parent
        if add_subtask_id and not add_subtask_title:
            for st in ctx["subtasks"]:
                if st["task_id"] == add_subtask_id:
                    st["status"] = status or "completed"
                    changed.append(f"subtask {add_subtask_id} marked {st['status']}")
        if remove_subtask_id:
            before = len(ctx["subtasks"])
            ctx["subtasks"] = [st for st in ctx["subtasks"] if st["task_id"] != remove_subtask_id]
            if len(ctx["subtasks"]) < before:
                changed.append(f"subtask {remove_subtask_id} removed")
            else:
                changed.append(f"subtask {remove_subtask_id} not found (no change)")
        if replace_plan_steps is not None:
            if not isinstance(replace_plan_steps, list) or not replace_plan_steps:
                changed.append("replace_plan_steps ignored (must be non-empty list)")
            else:
                ctx["plan_steps"] = [str(s) for s in replace_plan_steps]
                changed.append(f"plan_steps replaced ({len(ctx['plan_steps'])} steps)")

        ctx["updated_at"] = int(time.time())
        _write_context(task_id, ctx)
        log_text = "; ".join(changed) if changed else "no changes"
        _append_log(task_id, "info", f"Context updated: {log_text}")

    return f"Task '{task_id}' updated: {log_text}."


# ── task_read ────────────────────────────────────────────────────────────────

def task_read(task_id: str = "") -> str:
    """
    Read a task context and return it as a formatted JSON string.

    If task_id is empty, list all task IDs found in TASKS_DIR.
    """
    if not task_id:
        if not os.path.isdir(TASKS_DIR):
            return "No tasks directory found — no tasks have been created yet."
        entries = sorted(e for e in os.listdir(TASKS_DIR)
                         if os.path.isdir(os.path.join(TASKS_DIR, e)))
        tasks = []
        for name in entries:
            ctx = _read_context(name)
            if ctx:
                tasks.append({
                    "task_id": ctx.get("task_id"),
                    "title": ctx.get("title"),
                    "status": ctx.get("status"),
                    "current_step": ctx.get("current_step"),
                    "total_steps": len(ctx.get("plan_steps", [])),
                })
        return json.dumps(tasks, indent=2) if tasks else "No task contexts found."

    ctx = _read_context(task_id)
    if ctx is None:
        return f"Error: no context found for task_id '{task_id}'."
    return json.dumps(ctx, indent=2)


# ── task_delete ──────────────────────────────────────────────────────────────

import shutil as _shutil

def task_delete(task_id: str) -> str:
    """
    Permanently delete a task context folder (context.json + log.jsonl).

    Use when a task or sub-task is obsolete, was created by mistake, or has
    been superseded. This cannot be undone.
    """
    if not task_id:
        return "Error: task_id is required."
    d = _task_dir(task_id)
    if not os.path.isdir(d):
        return f"Error: no task context found for '{task_id}'."
    try:
        _shutil.rmtree(d)
        return f"Task '{task_id}' context deleted."
    except Exception as e:
        return f"Error deleting task '{task_id}': {e}"


# ── Plural export (loaded by core/tools._load_builtins) ──────────────────────

SKILL_FNS = [task_init, task_update, task_read, task_delete]

SKILL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "task_init",
            "description": (
                "Initialise a task context file with a plan. "
                "Call this at the start of any multi-step work and for every sub-task. "
                "Use the task_id from your system prompt for the root task; "
                "generate sub-task IDs as <parent>_sub_1, _sub_2, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Unique task identifier."},
                    "title": {"type": "string", "description": "Short description of this task."},
                    "plan_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of steps to complete this task.",
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": "Parent task_id when this is a sub-task. Leave empty for root tasks.",
                    },
                },
                "required": ["task_id", "title", "plan_steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": (
                "Update a task context. Call after completing a step, creating a skill, "
                "adding a sub-task, or finishing the task. Only provide the fields to change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task to update."},
                    "status": {
                        "type": "string",
                        "description": "New status: 'in_progress', 'completed', or 'failed'.",
                    },
                    "current_step": {
                        "type": "integer",
                        "description": "0-based index of the step now in progress.",
                    },
                    "module_path": {
                        "type": "string",
                        "description": "Path to the skill file created for this task.",
                    },
                    "usage": {
                        "type": "string",
                        "description": "How to call the resulting skill, e.g. 'email_manager(action=\"list\")'.",
                    },
                    "notes": {"type": "string", "description": "Free-form notes or completion summary."},
                    "add_subtask_id": {
                        "type": "string",
                        "description": "task_id of a sub-task to register under this task.",
                    },
                    "add_subtask_title": {
                        "type": "string",
                        "description": "Title for the new sub-task (required when add_subtask_id is set).",
                    },
                    "remove_subtask_id": {
                        "type": "string",
                        "description": "task_id of a sub-task to remove from this task's subtask list.",
                    },
                    "replace_plan_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Overwrite the entire plan_steps list with this new ordered list. "
                            "Use when the approach has changed and the old plan no longer applies."
                        ),
                    },
                    "set_artifacts": {
                        "type": "object",
                        "description": (
                            "Record artifacts created in a specific step. "
                            "Keys are step indices (as strings, 1-based), values are lists of identifiers: "
                            "skill names for registered skills, or relative file paths. "
                            "Example: {\"1\": [\"transaction_manager\"]} means step 1 created the transaction_manager skill. "
                            "These are used to detect stale completed steps when resuming a task after a restart."
                        ),
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "set_docs": {
                        "type": "string",
                        "description": (
                            "Write or update user-facing documentation for this task: "
                            "what the created skill/system does, how to use it, parameters, examples. "
                            "This is the user manual. Supports markdown."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_delete",
            "description": (
                "Permanently delete a task context folder. "
                "Use when a task or sub-task is obsolete, was created by mistake, "
                "or has been superseded. Cannot be undone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task_id of the context to delete.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_read",
            "description": (
                "Read a task context as JSON. "
                "Omit task_id to list all known tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID to read. Leave empty to list all tasks.",
                    },
                },
                "required": [],
            },
        },
    },
]
