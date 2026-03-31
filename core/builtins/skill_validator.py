"""
core/builtins/skill_validator.py

Runs a skill in-process with caller-supplied test arguments and reports whether
it succeeded, what it returned, and whether the output meets any expected criteria.

This should be called by the AI after every `create_skill` as the VALIDATE step
of the plan→execute→validate loop.
"""

import importlib
import sys
import os
import traceback

from core.config import SKILLS_DIR


def validate_skill(name: str, test_args: dict = None,
                   expected_contains: str = "") -> str:
    """
    Import and call a user skill with test_args to verify it works correctly.

    Args:
        name:             Snake-case module name of the skill (e.g. 'get_weather').
        test_args:        Dict of keyword arguments to pass to SKILL_FN. Use {}
                          when the skill takes no arguments.
        expected_contains: Optional string. If provided, the result must contain
                          this substring (case-insensitive) for the test to pass.

    Returns a human-readable validation report: PASS or FAIL with details.
    """
    if test_args is None:
        test_args = {}

    skill_path = os.path.join(SKILLS_DIR, f"{name}.py")
    if not os.path.exists(skill_path):
        return f"VALIDATION FAIL: skill file '{name}.py' not found in skills/."

    # Force re-import so we always test the freshly written version
    full_module = f"skills.{name}"
    if full_module in sys.modules:
        del sys.modules[full_module]

    try:
        module = importlib.import_module(full_module)
    except Exception as e:
        return (
            f"VALIDATION FAIL: could not import '{name}'.\n"
            f"Import error: {e}\n"
            f"{traceback.format_exc()}"
        )

    if not hasattr(module, "SKILL_FN"):
        return f"VALIDATION FAIL: '{name}' does not define SKILL_FN."
    if not hasattr(module, "SKILL_DEF"):
        return f"VALIDATION FAIL: '{name}' does not define SKILL_DEF."

    fn = module.SKILL_FN
    try:
        result = fn(**test_args)
    except TypeError as e:
        return (
            f"VALIDATION FAIL: calling {name}(**{test_args!r}) raised TypeError.\n"
            f"Likely wrong argument names or missing required args.\nError: {e}"
        )
    except Exception as e:
        return (
            f"VALIDATION FAIL: {name}(**{test_args!r}) raised an exception.\n"
            f"Error: {e}\n"
            f"{traceback.format_exc()}"
        )

    result_str = str(result)

    # Check for skill-level errors returned as strings
    if result_str.startswith(("SKILL_ERROR", "Error:", "error:")):
        return (
            f"VALIDATION FAIL: skill returned an error.\n"
            f"Result: {result_str[:500]}"
        )

    # Optional content check
    if expected_contains:
        if expected_contains.lower() not in result_str.lower():
            return (
                f"VALIDATION FAIL: result did not contain expected string "
                f"{expected_contains!r}.\n"
                f"Result (first 300 chars): {result_str[:300]}"
            )

    preview = result_str[:300] + ("…" if len(result_str) > 300 else "")
    return f"VALIDATION PASS: {name} returned successfully.\nResult preview: {preview}"


SKILL_FN = validate_skill
SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "validate_skill",
        "description": (
            "Run a skill with test arguments to verify it works correctly. "
            "ALWAYS call this after create_skill as the VALIDATE step. "
            "If validation fails, diagnose and fix using create_skill(overwrite=true), "
            "then validate again. Mark the task complete only after VALIDATION PASS."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Snake-case module name of the skill, e.g. 'get_weather'."
                },
                "test_args": {
                    "type": "object",
                    "description": (
                        "Dict of keyword arguments to pass to the skill. "
                        "Use representative but safe test values. "
                        "Use {} if the skill takes no arguments."
                    )
                },
                "expected_contains": {
                    "type": "string",
                    "description": (
                        "Optional: a string the result must contain (case-insensitive) "
                        "for the test to pass. Leave empty to just check for no errors."
                    )
                }
            },
            "required": ["name"]
        }
    }
}
