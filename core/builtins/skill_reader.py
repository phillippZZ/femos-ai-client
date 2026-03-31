import os

from core.config import SKILLS_DIR


def read_skill(name):
    """Return the source code of an existing user skill file."""
    path = os.path.join(SKILLS_DIR, f"{name}.py")
    if not os.path.exists(path):
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
