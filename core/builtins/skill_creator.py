import os
import importlib
import sys
import py_compile
import tempfile
import threading

from core.config import SKILLS_DIR

_lock = threading.Lock()


def create_skill(name, code, overwrite=False):
    """
    Write a new skill Python file to the user skills/ directory and hot-reload.

    The code must define:
      - A callable function (the skill implementation)
      - SKILL_FN  = <that function>
      - SKILL_DEF = { "type": "function", "function": { "name": ..., "description": ..., "parameters": ... } }

    overwrite=True allows replacing a broken existing skill.
    After reload the client automatically re-registers the updated skill list with the server.
    """
    if not name.isidentifier():
        return f"Error: '{name}' is not a valid Python module name."

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as e:
        return f"Syntax error in skill code: {e}"
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


SKILL_FN = create_skill
SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": (
            "Write a new reusable Python skill file and hot-reload it into the agent. "
            "Use when the same type of task will recur and deserves a dedicated skill. "
            "The code argument must be complete Python source that defines SKILL_FN and SKILL_DEF. "
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
                        "Must define SKILL_FN (the callable) and SKILL_DEF (tool config dict)."
                    )
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Set true to replace an existing broken skill with the same name."
                }
            },
            "required": ["name", "code"]
        }
    }
}
