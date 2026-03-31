"""
core/builtins/workspace_files.py

Structured file management within the agent workspace directory.
All paths are relative to WORKSPACE_DIR and are sandboxed — ".." traversal
that would escape the workspace is blocked.

Use this for data files skills create (databases, CSVs, JSON configs, reports…).
Do NOT use it for workspace/tasks/ — use task_* tools for those.
Do NOT use it for workspace/indexes/ — use rag_* tools for those.
"""

import os
import shutil
import threading

from core.config import WORKSPACE_DIR

_lock = threading.Lock()
# Resolve once at import time so the sandbox boundary is stable.
_WS = os.path.abspath(WORKSPACE_DIR)


def _safe_abs(rel_path: str):
    """
    Resolve rel_path relative to WORKSPACE_DIR.
    Returns the absolute path, or None if it would escape the workspace.
    """
    if not rel_path:
        return _WS
    abs_path = os.path.normpath(os.path.join(_WS, rel_path))
    if abs_path != _WS and not abs_path.startswith(_WS + os.sep):
        return None
    return abs_path


def workspace_files(action: str, path: str = "", content: str = "", dest: str = "") -> str:
    """
    Manage files and directories within the agent workspace.

    action  — "list" | "read" | "write" | "delete" | "move"
    path    — Path relative to workspace root (e.g. "finance_tracker/data.db").
               Leave empty to target the workspace root for "list".
    content — Text content; used only with action="write".
    dest    — Destination path relative to workspace root; used only with action="move".

    Examples:
      workspace_files("list", "finance_tracker")
      workspace_files("read", "finance_tracker/transactions.db")
      workspace_files("write", "finance_tracker/config.json", content='{"currency":"USD"}')
      workspace_files("delete", "finance_tracker/old_reports")
      workspace_files("move", "finance_tracker/v1", dest="finance_tracker/archive/v1")
    """
    abs_path = _safe_abs(path)
    if abs_path is None:
        return "Error: path traversal outside workspace is not allowed."

    # ── list ──────────────────────────────────────────────────────────
    if action == "list":
        if not os.path.exists(abs_path):
            return f"Error: '{path or '.'}' does not exist in workspace."
        if os.path.isfile(abs_path):
            size = os.path.getsize(abs_path)
            return f"[file] {path}  ({size:,} B)"
        entries = []
        for entry in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, entry)
            if os.path.isdir(full):
                entries.append(f"[dir ] {entry}/")
            else:
                size = os.path.getsize(full)
                entries.append(f"[file] {entry}  ({size:,} B)")
        rel = path or "."
        header = f"workspace/{rel}/\n"
        return header + ("\n".join(entries) if entries else "  (empty)")

    # ── read ──────────────────────────────────────────────────────────
    elif action == "read":
        if not path:
            return "Error: path is required for read."
        if not os.path.isfile(abs_path):
            return f"Error: '{path}' is not a file (or does not exist)."
        try:
            with open(abs_path, "r", errors="replace") as f:
                data = f.read()
            if len(data) > 8000:
                data = data[:8000] + f"\n… (truncated — {len(data) - 8000:,} more chars)"
            return data
        except Exception as e:
            return f"Error reading '{path}': {e}"

    # ── write ─────────────────────────────────────────────────────────
    elif action == "write":
        if not path:
            return "Error: path is required for write."
        if os.path.isdir(abs_path):
            return f"Error: '{path}' is a directory — provide a file path."
        with _lock:
            parent = os.path.dirname(abs_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                with open(abs_path, "w") as f:
                    f.write(content)
                return f"Written {len(content):,} chars to workspace/{path}."
            except Exception as e:
                return f"Error writing '{path}': {e}"

    # ── delete ────────────────────────────────────────────────────────
    elif action == "delete":
        if not path:
            return "Error: path is required for delete."
        if not os.path.exists(abs_path):
            return f"Error: '{path}' does not exist in workspace."
        with _lock:
            try:
                if os.path.isdir(abs_path):
                    shutil.rmtree(abs_path)
                    return f"Deleted directory workspace/{path} (and all its contents)."
                else:
                    os.remove(abs_path)
                    return f"Deleted workspace/{path}."
            except Exception as e:
                return f"Error deleting '{path}': {e}"

    # ── move ──────────────────────────────────────────────────────────
    elif action == "move":
        if not path or not dest:
            return "Error: both path and dest are required for move."
        abs_dest = _safe_abs(dest)
        if abs_dest is None:
            return "Error: dest path traversal outside workspace is not allowed."
        if not os.path.exists(abs_path):
            return f"Error: '{path}' does not exist in workspace."
        with _lock:
            parent = os.path.dirname(abs_dest)
            if parent and parent != _WS:
                os.makedirs(parent, exist_ok=True)
            try:
                shutil.move(abs_path, abs_dest)
                return f"Moved workspace/{path} → workspace/{dest}."
            except Exception as e:
                return f"Error moving '{path}' to '{dest}': {e}"

    else:
        return f"Error: unknown action '{action}'. Valid actions: list, read, write, delete, move."


SKILL_FN = workspace_files
SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "workspace_files",
        "description": (
            "Manage files and directories within the agent workspace (workspace/). "
            "Use for data files that skills create — databases, CSVs, JSON configs, reports, etc. "
            "All paths are relative to the workspace root and cannot escape it. "
            "Do NOT use for workspace/tasks/ (use task_* tools) or workspace/indexes/ (use rag_*)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read", "write", "delete", "move"],
                    "description": (
                        "list — directory listing with sizes; "
                        "read — file contents (text, truncated at 8000 chars); "
                        "write — write/overwrite a text file; "
                        "delete — delete a file or entire directory tree; "
                        "move — rename or relocate within workspace."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Path relative to workspace root, e.g. 'finance_tracker/transactions.db'. "
                        "Leave empty with action='list' to list the workspace root."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write. Used only with action='write'.",
                },
                "dest": {
                    "type": "string",
                    "description": (
                        "Destination path relative to workspace root. "
                        "Used only with action='move'."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}
