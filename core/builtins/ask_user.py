"""
ask_user.py — builtin skill that pauses the agent and prompts the user for input.

Default (CLI / test) implementation reads from stdin.
The macOS UI overrides the dispatch in macos_app.py to show a native dialog.
"""

import sys


def ask_user(question: str, title: str = "AI Assistant", default: str = "") -> str:
    """Ask the user a question and return their typed answer.

    Used by the AI agent whenever it needs a user-provided value it cannot
    infer on its own: API keys, tokens, credentials, file paths, usernames, etc.
    """
    print(f"\n╔══  {title}  ══╗", file=sys.stderr)
    print(f"  {question}", file=sys.stderr)
    if default:
        print(f"  (press Enter to accept default: {default!r})", file=sys.stderr)
    width = max(len(title) + 8, 20)1
    print(f"╚{'═' * width}╝", file=sys.stderr)
    try:
        response = input("→ ").strip()
        return response if response else default
    except EOFError:
        return default


SKILL_FN = ask_user

SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Pause execution and ask the user a question, blocking until they type an answer. "
            "Use this ONLY when you genuinely need information you cannot know: API keys, "
            "secret tokens, OAuth credentials, account IDs, file paths, domain names, "
            "or configuration values that cannot be inferred. "
            "NEVER use ask_user to ask for permission or confirmation before routine actions "
            "like creating skills, writing files, or running code — just do the task. "
            "NEVER hard-code or guess secrets — call ask_user for those. "
            "Returns the user's typed answer as a plain string."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Clear, specific question for the user. "
                        "State exactly what format is needed, e.g.: "
                        "'Enter your OpenWeatherMap API key (32-character hex string):'"
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Short dialog title shown above the question, "
                        "e.g. 'API Key Required', 'Confirm Action', 'Configuration'."
                    ),
                },
                "default": {
                    "type": "string",
                    "description": (
                        "Optional pre-filled default value shown in the input box. "
                        "Leave empty for secrets, passwords, and tokens."
                    ),
                },
            },
            "required": ["question"],
        },
    },
}
