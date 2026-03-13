import os
from dotenv import load_dotenv

# Search cwd and parents for .env (finds femos-client/.env when run from there)
load_dotenv()

SERVER_URL = os.getenv("SERVER_URL", "ws://localhost:8765")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
