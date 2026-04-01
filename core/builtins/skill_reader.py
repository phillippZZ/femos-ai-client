import os

from core.config import SKILLS_DIR


def read_skill(name):
    """Return the source code of an existing user skill.

    Checks skills/<name>/__init__.py (folder structure) first,
    falling back to legacy skills/<name>.py.
    """
    folder_path = os.path.join(SKILLS_DIR, name, "__init__.py")
    flat_path   = os.path.join(SKILLS_DIR, f"{name}.py")
    if os.path.exists(folder_path):
        path = folder_path
    elif os.path.exists(flat_path):
        path = flat_path
    else:
        return f"Error: skill '{name}' not found."
    with open(path, "r") as f:
        return f.read()


SKILL_FN = read_skill
SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "read_skill",
        "description": (
            "Read and return the full source code of an existing client-side skill. "
            "Always call this before rewriting a skill so you can modify it rather than recreate it from scratch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The snake_case module name of the skill, e.g. 'get_weather'"
                }
            },
            "required": ["name"]
        }
    }
}
