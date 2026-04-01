"""
core/builtins/config_store.py

User-local configuration store for persistent settings such as API keys.

Config is stored at ~/.femos/user_config.json — outside the project repo
so it is never committed to version control or included when skills are shared.

Skills should follow this pattern for secrets:
    key = config_get("openweathermap_api_key")
    if not key:
        key = ask_user(question="Enter your OpenWeatherMap API key:", title="API Key Required")
        if key and key.strip():
            config_set("openweathermap_api_key", key.strip())
    # use key ...
"""

import json
import os
import threading

_CONFIG_DIR  = os.path.expanduser("~/.femos")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "user_config.json")
_lock        = threading.Lock()


def _load() -> dict:
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    os.makedirs(_CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _CONFIG_FILE)
    # Restrict to owner-only so API keys aren't world-readable.
    os.chmod(_CONFIG_FILE, 0o600)


def _config_get(key: str) -> str:
    """Return the stored value for *key*, or an empty string if not set."""
    if not key or not key.strip():
        return "error: key must not be empty"
    with _lock:
        return str(_load().get(key.strip(), ""))


def _config_set(key: str, value: str) -> str:
    """Persist *value* under *key*. Returns 'ok' on success."""
    if not key or not key.strip():
        return "error: key must not be empty"
    with _lock:
        data = _load()
        data[key.strip()] = value
        _save(data)
    return "ok"


def _config_delete(key: str) -> str:
    """Remove *key* from the store. Returns 'deleted' or 'not found'."""
    if not key or not key.strip():
        return "error: key must not be empty"
    with _lock:
        data = _load()
        if key.strip() in data:
            del data[key.strip()]
            _save(data)
            return "deleted"
        return "not found"


def _config_list() -> str:
    """Return a newline-separated list of stored keys (values are not shown)."""
    with _lock:
        keys = list(_load().keys())
    if not keys:
        return "no keys stored"
    return "\n".join(sorted(keys))


# ── SKILL_FNS / SKILL_DEFS export (multiple tools from one file) ──────────────

SKILL_FNS = [_config_get, _config_set, _config_delete, _config_list]

SKILL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "config_get",
            "description": (
                "Read a user config value (e.g. an API key) from the local config store "
                "(~/.femos/user_config.json). Returns the stored string, or empty string if not set. "
                "Use config_set to persist a value after asking the user once. "
                "Never hard-code secrets in skill source code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Config key to read, e.g. 'openweathermap_api_key'"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "config_set",
            "description": (
                "Persist a user config value (e.g. an API key) to the local config store "
                "(~/.femos/user_config.json). The store is outside the repo and is never shared. "
                "Call this immediately after ask_user so the user is not prompted again next time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key":   {"type": "string", "description": "Config key, e.g. 'openweathermap_api_key'"},
                    "value": {"type": "string", "description": "Value to store (plain string, not encrypted)"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "config_delete",
            "description": "Remove a key from the local config store. Returns 'deleted' or 'not found'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Config key to remove"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "config_list",
            "description": (
                "List all keys currently stored in the user config store. "
                "Values are not shown — use config_get to read a specific value."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]
