import subprocess

def exec_cmd(command):
    """Execute a shell command and return its combined output."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stdout and stderr:
            return f"{stdout}\n[stderr]: {stderr}"
        return stdout or stderr or "(no output)"
    except Exception as e:
        return f"Error: {str(e)}"

SKILL_FN = exec_cmd
SKILL_DEF = {
    "type": "function",
    "function": {
        "name": "exec_cmd",
        "description": (
            "Execute a shell command and return its output. "
            "Use for one-off system queries (e.g. date, uptime, df -h, ls). "
            "Prefer an existing named skill over this when one is available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run, e.g. 'date', 'uptime', 'df -h'"
                }
            },
            "required": ["command"]
        }
    }
}
