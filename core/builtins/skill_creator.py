import os
import importlib
import shutil
import sys
import py_compile
import tempfile
import threading

from core.config import SKILLS_DIR, WORKSPACE_DIR  # noqa: F401 (WORKSPACE_DIR used by name below)

_lock = threading.Lock()


def create_skill(name, code="", code_file="", overwrite=False, docs="", notes=""):
    """
    Write a new skill into its own folder under skills/ and hot-reload.

    Creates skills/<name>/__init__.py (the runnable code) and optionally:
      skills/<name>/manual.md   — user-facing docs (docs= param)
      skills/<name>/progress.md — development log  (notes= param)

    Sub-skills can be added later by creating a sub-folder inside
    skills/<name>/ with the same structure.

    The code must define:
      - SKILL_FN  = <callable>
      - SKILL_DEF = { "type": "function", "function": { "name": ..., ... } }

    code_file (optional): path relative to workspace/ from which to read the
                          code.  The staging file is deleted after success.
    overwrite=True allows replacing an existing skill with the same name.
    """
    # Normalise: strip any stray surrounding quotes the LLM may have added
    name = str(name).strip().strip("'\"")
    # Also accept overwrite as string "true"/"false" from JSON
    if isinstance(overwrite, str):
        overwrite = overwrite.lower() in ("true", "1", "yes")

    # Resolve code from code_file if given (sandboxed to workspace/ or skills/<name>/)
    _staging_path = None  # workspace staging file to delete on success (workspace/ only)
    if code_file:
        _ws = os.path.abspath(WORKSPACE_DIR)
        # Allow code_file relative to workspace/ or absolute path within skills/
        abs_path = os.path.abspath(os.path.join(_ws, code_file))
        if not abs_path.startswith(_ws + os.sep) and abs_path != _ws:
            return "Error: code_file path traversal is not allowed."
        if not os.path.isfile(abs_path):
            return f"Error: code_file '{code_file}' not found in workspace."
        with open(abs_path) as _f:
            code = _f.read()  # code_file always wins — overrides any inline code
        # Only track for cleanup if the file is inside workspace/ (not skills/)
        _sk = os.path.abspath(SKILLS_DIR)
        if not abs_path.startswith(_sk + os.sep):
            _staging_path = abs_path

    if not code:
        # No inline code and no code_file — check if __init__.py already exists
        # (model wrote directly to skills/<name>/__init__.py via workspace_files)
        _existing = os.path.join(os.path.abspath(SKILLS_DIR), name, "__init__.py")
        if os.path.isfile(_existing):
            with open(_existing) as _f:
                code = _f.read()
        else:
            return "Error: provide 'code' (Python source), 'code_file' (path in workspace/), or write the code to skills/<name>/__init__.py first."

    if not name.isidentifier():
        return f"Error: '{name}' is not a valid Python module name."

    # Check for common JSON-ism mistakes that pass py_compile but fail at import
    import re as _re
    _json_tokens = _re.findall(r'(?<![\w\'"#])\b(null|true|false)\b(?![\w\'"#])', code)
    if _json_tokens:
        bad = list(dict.fromkeys(_json_tokens))  # deduplicate, preserve order
        mapping = {'null': 'None', 'true': 'True', 'false': 'False'}
        fixes = ', '.join(f'{t!r} → {mapping[t]!r}' for t in bad)
        return (f"Syntax error: code contains JSON literals that are not valid Python: {fixes}. "
                f"Replace them with their Python equivalents before calling create_skill again.")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as e:
        # Replace the opaque temp path with the actual failing line from the source
        err_str = str(e)
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
        skill_dir  = os.path.join(SKILLS_DIR, name)
        skill_init = os.path.join(skill_dir, "__init__.py")
        flat_path  = os.path.join(SKILLS_DIR, f"{name}.py")  # legacy flat file

        exists_flat   = os.path.isfile(flat_path)
        exists_folder = os.path.isdir(skill_dir)

        if (exists_flat or exists_folder) and not overwrite:
            return f"Error: skill '{name}' already exists. Pass overwrite=true to replace it."

        # Remove legacy flat file when upgrading to folder structure
        if exists_flat:
            try:
                os.remove(flat_path)
            except Exception:
                pass

        try:
            os.makedirs(skill_dir, exist_ok=True)
            with open(skill_init, "w") as f:
                f.write(code)
            from core.tools import reload_skills
            reload_skills()
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

            # Write optional docs and progress log into the skill folder
            if docs:
                try:
                    with open(os.path.join(skill_dir, "manual.md"), "w") as f:
                        f.write(docs)
                except Exception:
                    pass
            if notes:
                try:
                    with open(os.path.join(skill_dir, "progress.md"), "w") as f:
                        f.write(notes)
                except Exception:
                    pass

            # Clean up any workspace staging file.
            # Only applies to files in workspace/ — skill files in skills/ are the
            # destination, not a staging copy, so they must NOT be deleted.
            if _staging_path and os.path.exists(_staging_path):
                try:
                    os.remove(_staging_path)
                except Exception:
                    pass
            # Also remove workspace/<name>.py if it exists and the model forgot
            # to pass code_file (wrote staging file then called with inline code=)
            _ws = os.path.abspath(WORKSPACE_DIR)
            _implicit = os.path.join(_ws, f"{name}.py")
            if _implicit != _staging_path and os.path.isfile(_implicit):
                try:
                    os.remove(_implicit)
                except Exception:
                    pass

            if registered:
                names_str = ", ".join(f"'{n}'" for n in registered)
                return (
                    f"Skill '{name}' created at skills/{name}/ and loaded successfully. "
                    f"Registered callable name(s): {names_str}. "
                    f"Use these exact names when calling the skill."
                )
            return f"Skill '{name}' created at skills/{name}/ and loaded successfully."
        except Exception as e:
            if os.path.isdir(skill_dir) and not exists_folder:
                shutil.rmtree(skill_dir, ignore_errors=True)
            return f"Error creating skill: {str(e)}"


def delete_skill(name: str) -> str:
    """
    Delete a user skill (folder or legacy flat file) and unregister it.

    Deletes the entire skills/<name>/ folder including manual.md, progress.md,
    and any sub-skill sub-folders.  Falls back to deleting skills/<name>.py
    for legacy flat skills.
    """
    name = str(name).strip().strip("'\"")
    if not name.isidentifier():
        return f"Error: '{name}' is not a valid Python module name."

    with _lock:
        skill_dir = os.path.join(SKILLS_DIR, name)
        flat_path = os.path.join(SKILLS_DIR, f"{name}.py")
        if not os.path.isdir(skill_dir) and not os.path.isfile(flat_path):
            return f"Error: skill '{name}' not found."
        try:
            if os.path.isdir(skill_dir):
                shutil.rmtree(skill_dir)
            elif os.path.isfile(flat_path):
                os.remove(flat_path)
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
                },
                "docs": {
                    "type": "string",
                    "description": (
                        "Markdown user manual written to skills/<name>/manual.md. "
                        "Include: what the skill does, parameters, return value, usage example."
                    )
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Development log written to skills/<name>/progress.md. "
                        "Record decisions, retries, and what changed between versions."
                    )
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
