import os
import py_compile
import tempfile
import threading

_SKILLS_DIR = os.path.dirname(__file__)
_lock = threading.Lock()

def create_skill(name, code, overwrite=False):
    """
    Write a new skill Python file to the client skills/ directory and hot-reload.

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
        path = os.path.join(_SKILLS_DIR, f"{name}.py")
        if os.path.exists(path) and not overwrite:
            return f"Error: skill '{name}' already exists. Pass overwrite=true to replace it."
        try:
            with open(path, "w") as f:
                f.write(code)
            from core.tools import reload_skills
            reload_skills()
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
