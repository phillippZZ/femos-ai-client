import os
import uuid
from dotenv import load_dotenv

# Search cwd and parents for .env (finds femos-client/.env when run from there)
load_dotenv()

SERVER_URL = os.getenv("SERVER_URL", "ws://localhost:8765")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Ollama base URL used by client-side builtins (RAG embeddings, etc.)
OLLAMA_BASE_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").replace("/api/chat", "").rstrip("/")

# Stable client identity — persisted across runs so history can be restored on reconnect.
# In the future this can be replaced with an authenticated user token.
_CLIENT_ID_PATH = os.path.expanduser("~/.femos_client_id")

def _load_or_create_client_id() -> str:
    if os.path.exists(_CLIENT_ID_PATH):
        try:
            cid = open(_CLIENT_ID_PATH).read().strip()
            if cid:
                return cid
        except OSError:
            pass
    cid = str(uuid.uuid4())
    try:
        with open(_CLIENT_ID_PATH, "w") as f:
            f.write(cid)
    except OSError:
        pass
    return cid

CLIENT_ID: str = _load_or_create_client_id()

# Per-client history file — stores the last conversation so it can be
# sent back to a stateless server on reconnect.
HISTORY_PATH: str = os.path.expanduser(f"~/.femos_history_{CLIENT_ID}.json")

# Directory where user-created skills live (hot-reloaded at runtime).
# core/config.py lives at  femos-ai-client/core/config.py
# so dirname(dirname(__file__)) == femos-ai-client/
_CLIENT_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR: str = os.path.join(_CLIENT_DIR, "skills")

# Workspace directory — all agent-generated artefacts live here so they are
# visible in the project file tree alongside the skills they refer to.
#   workspace/tasks/<task_id>/context.json + log.jsonl
#   workspace/indexes/<name>/chunks.json + embeddings.npy + meta.json
WORKSPACE_DIR: str = os.path.join(_CLIENT_DIR, "workspace")

# Root directory for task context files and real-time logs.
TASKS_DIR: str = os.path.join(WORKSPACE_DIR, "tasks")

# Root directory for RAG vector indexes.
INDEXES_DIR: str = os.path.join(WORKSPACE_DIR, "indexes")
