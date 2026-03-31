import os
import importlib
import sys
import py_compile
import tempfile
import threading

from core.config import SKILLS_DIR, WORKSPACE_DIR

_lock = threading.Lock()


def create_skill(name, code="", code_file="", overwrite=False):
    """
    Write a new skill Python file to the user skills/ directory and hot-reload.

    The code must define:
      - A callable function (the skill implementation)
      - SKILL_FN  = <that function>
      - SKILL_DEF = { "type": "function", "function": { "name": ..., "description": ..., "parameters": ... } }

    code_file (optional): path relative to workspace/ from which to read the code.
    overwrite=True allows replacing a broken existing skill.
    After reload the client automatically re-registers the updated skill list with the server.
    """
    # Normalise: strip any stray surrounding quotes the LLM may have added
    name = str(name).strip().strip("'\"")
    # Also accept overwrite as string "true"/"false" from JSON
    if isinstance(overwrite, str):
        overwrite = overwrite.lower() in ("true", "1", "yes")

    # If code_file provided, read code from workspace (sandboxed)
    if code_file and not code:
        _ws = os.path.abspath(WORKSPACE_DIR)
        abs_path = os.path.abspath(os.path.join(_ws, code_file))
        if not abs_path.startswith(_ws + os.sep) and abs_path != _ws:
            return "Error: code_file path traversal is not allowed."
        if not os.path.isfile(abs_path):
            return f"Error: code_file '{code_file}' not found in workspace."
        with open(abs_path) as _f:
            code = _f.read()

    if not code:
        return "Error: provide either 'code' (Python source string) or 'code_file' (path relative to workspace/)."

    if not name.isidentifier():
        return f"Error: '{name}' is not a valid Python module name."

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as e:
        # Replace the opaque temp path with the actual failing line from the source
        err_str = str(e)
        # Extract line number from the error message
        import re as _re
        line_match = _re.search(r"line (\d+)", err_str)
        if line_match:
            lineno = int(line_match.group(1))
            lines = code.splitlines()
            if 1 <= lineno <= len(lines):
                bad_line = lines[lineno - 1].strip()
                return (f"Syntax error in skill code at line {lineno}: {bad_line!r}. "
                        f"Full error: {err_str}")
        return f"Syntax error in skill code: {err_str}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    with _lock:
        path = os.path.join(SKILLS_DIR, f"{name}.py")
        if os.path.exists(path) and not overwrite:
            return f"Error: skill '{name}' already exists. Pass overwrite=true to replace it."
        try:
            with open(path, "w") as f:
                f.write(code)
            from core.tools import reload_skills
            reload_skills()
            # Inspect the module to find the actual registered skill name(s).
            # The callable name comes from SKILL_DEF["function"]["name"], NOT the file name.
            registered = []
            try:
                full_name = f"skills.{name}"
                if full_name in sys.modules:
                    mod = importlib.reload(sys.modules[full_name])
                else:
                    mod = importlib.import_module(full_name)
                if hasattr(mod, "SKILL_FNS") and hasattr(mod, "SKILL_DEFS"):
                    for defn in mod.SKILL_DEFS:
                        registered.append(defn["function"]["name"])
                elif hasattr(mod, "SKILL_FN") and hasattr(mod, "SKILL_DEF"):
                    registered.append(mod.SKILL_DEF["function"]["name"])
            except Exception:
                pass
            if registered:
                names_str = ", ".join(f"'{n}'" for n in registered)
                return (
                    f"Skill '{name}' created and loaded successfully. "
                    f"Registered callable name(s): {names_str}. "
                    f"Use these exact names when calling the skill."
                )
            return f"Skill '{name}' created and loaded successfully."
        except Exception as e:
            if os.path.exists(path):
                os.remove(path)
            return f"Error creating skill: {str(e)}"


def delete_skill(name: str) -> str:
    """
    Delete a user skill file and remove it from the live skill registry.

    The skill is unloaded immediately — it will not appear in subsequent
    tool listings and cannot be called after this returns.
    """
    name = str(name).strip().strip("'\"")
    if not name.isidentifier():
        return f"Error: '{name}' is not a valid Python module name."

    with _lock:
        path = os.path.join(SKILLS_DIR, f"{name}.py")
        if not os.path.exists(path):
            return f"Error: skill '{name}' not found."
        try:
            os.remove(path)
            # Evict from sys.modules so reload() does not resurrect it
            sys.modules.pop(f"skills.{name}", None)
            from core.tools import reload_skills
            reload_skills()
            return f"Skill '{name}' deleted and unregistered."
        except Exception as e:
            return f"Error deleting skill '{name}': {e}"


_CREATE_SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": (
            "Write a new reusable Python skill file and hot-reload it into the agent. "
            "Use when the same type of task will recur and deserves a dedicated skill. "
            "Provide either 'code' (full Python source string) OR 'code_file' (path relative to workspace/ "
            "where the code was previously written with workspace_files). "
            "For long skills, prefer the two-step pattern: write code to workspace/ first, then pass code_file here. "
            "The code must define SKILL_FN and SKILL_DEF at module level. "
            "If a previous attempt failed due to a bug, set overwrite=true to replace it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short snake_case module name for the skill file, e.g. 'system_info'"
                },
                "code": {
                    "type": "string",
                    "description": (
                        "Full Python source of the skill module. "
                        "Must define SKILL_FN (the callable) and SKILL_DEF (tool config dict). "
                        "For long code use code_file instead."
                    )
                },
                "code_file": {
                    "type": "string",
                    "description": (
                        "Path relative to workspace/ where the skill code was already saved "
                        "(e.g. 'transaction_manager.py'). Use this instead of 'code' when the "
                        "source is long — write it with workspace_files first, then pass the path here."
                    )
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Set true to replace an existing broken skill with the same name."
                }
            },
            "required": ["name"]
        }
    }
}

_DELETE_SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "delete_skill",
        "description": (
            "Permanently delete a user skill file and unregister it from the agent. "
            "Use when a skill is no longer needed, was created by mistake, or has been "
            "superseded by a better implementation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The snake_case module name of the skill to delete, e.g. 'old_fetcher'"
                }
            },
            "required": ["name"]
        }
    }
}

# Plural exports — loaded by core/tools._load_builtins
SKILL_FNS = [create_skill, delete_skill]
SKILL_DEFS = [_CREATE_SKILL_DEF, _DELETE_SKILL_DEF]
