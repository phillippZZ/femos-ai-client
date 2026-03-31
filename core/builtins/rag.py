"""
rag.py — Retrieval-Augmented Generation builtins.

Three skills:
  rag_index   — chunk + embed a directory (or a single file) and save the index
  rag_search  — embed a query and return the top-K most relevant chunks
  rag_drop    — delete a named index

Index layout on disk:
  ~/.femos/indexes/<name>/
      chunks.json        list of {file, start_line, end_line, text, tokens}
      embeddings.npy     float32 matrix (N × D)
      meta.json          {name, embed_model, created, total_chunks}

Design notes:
  • Pure numpy cosine similarity — no FAISS / ChromaDB required.
  • Python files are chunked at function/class boundaries via AST.
  • All other text files use a sliding-window line chunker.
  • Binary files are skipped automatically.
  • The embedding model defaults to "nomic-embed-text" (fast, 274 MB).
    Fall back to qwen2.5-coder:14b if nomic is not pulled.
"""

import ast
import fnmatch
import glob
import json
import os
import time
from pathlib import Path

import numpy as np
import requests


# ── Config ────────────────────────────────────────────────────────────────────

def _ollama_base() -> str:
    try:
        from core.config import OLLAMA_BASE_URL
        return OLLAMA_BASE_URL
    except ImportError:
        return os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")


INDEXES_DIR   = os.path.expanduser("~/.femos/indexes")
DEFAULT_MODEL = os.getenv("RAG_EMBED_MODEL", "nomic-embed-text")
CHUNK_LINES   = int(os.getenv("RAG_CHUNK_LINES", "60"))
OVERLAP_LINES = int(os.getenv("RAG_OVERLAP_LINES", "10"))
MAX_CHUNK_CHARS = 4000     # hard cap so one chunk never dominates the prompt


# ── Embedding ─────────────────────────────────────────────────────────────────

def _embed_batch(texts: list, model: str) -> np.ndarray:
    """Embed a list of texts in one API call each; returns (N, D) float32 array."""
    vecs = []
    base = _ollama_base()
    for txt in texts:
        resp = requests.post(
            f"{base}/api/embeddings",
            json={"model": model, "prompt": txt[:MAX_CHUNK_CHARS]},
            timeout=60,
        )
        resp.raise_for_status()
        vecs.append(resp.json()["embedding"])
    arr = np.array(vecs, dtype=np.float32)
    # L2-normalise so dot product == cosine similarity
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return arr / norms


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_python(source: str, filepath: str) -> list:
    """AST-aware chunker: emit one chunk per top-level function/class."""
    chunks = []
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _chunk_lines(source, filepath)   # fall back on broken syntax

    # Collect top-level + nested nodes that have their own lineno
    nodes = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and hasattr(n, "end_lineno")
    ]
    # Sort and de-overlap: only keep non-nested nodes
    nodes.sort(key=lambda n: n.lineno)
    used = set()
    for node in nodes:
        span = set(range(node.lineno, node.end_lineno + 1))
        if span & used:
            continue   # skip nested — parent already covers it
        used |= span
        start = node.lineno - 1
        end   = node.end_lineno
        text  = "\n".join(lines[start:end])
        if not text.strip():
            continue
        chunks.append({
            "file": filepath,
            "start_line": start + 1,
            "end_line": end,
            "text": text[:MAX_CHUNK_CHARS],
            "tokens": len(text) // 4,
        })

    if not chunks:
        # File has no functions/classes (e.g. pure script) — fall back
        return _chunk_lines(source, filepath)
    return chunks


def _chunk_lines(source: str, filepath: str) -> list:
    """Sliding-window line chunker for non-Python or structureless files."""
    lines = source.splitlines()
    chunks = []
    i = 0
    while i < len(lines):
        end = min(i + CHUNK_LINES, len(lines))
        text = "\n".join(lines[i:end])
        if text.strip():
            chunks.append({
                "file": filepath,
                "start_line": i + 1,
                "end_line": end,
                "text": text[:MAX_CHUNK_CHARS],
                "tokens": len(text) // 4,
            })
        i += CHUNK_LINES - OVERLAP_LINES
    return chunks


def _chunk_file(filepath: str) -> list:
    """Read a file and return chunks. Skips binary files silently."""
    try:
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            source = f.read()
    except OSError:
        return []
    if not source.strip():
        return []
    if filepath.endswith(".py"):
        return _chunk_python(source, filepath)
    return _chunk_lines(source, filepath)


# ── Index path helpers ────────────────────────────────────────────────────────

def _index_dir(name: str) -> str:
    return os.path.join(INDEXES_DIR, name)


def _load_index(name: str):
    """Returns (chunks: list, embeddings: np.ndarray) or raises FileNotFoundError."""
    d = _index_dir(name)
    chunks_path = os.path.join(d, "chunks.json")
    emb_path    = os.path.join(d, "embeddings.npy")
    if not os.path.exists(chunks_path):
        raise FileNotFoundError(f"Index '{name}' not found.")
    chunks = json.loads(open(chunks_path).read())
    emb    = np.load(emb_path)
    return chunks, emb


# ── Public skills ─────────────────────────────────────────────────────────────

def rag_index(
    path: str,
    name: str = "",
    glob_pattern: str = "**/*.py,**/*.md,**/*.txt,**/*.js,**/*.ts",
    embed_model: str = DEFAULT_MODEL,
    overwrite: bool = False,
) -> str:
    """
    Index a directory (or single file) for RAG search.

    Parameters
    ----------
    path          Absolute or relative path to a folder or file.
    name          Index name (slug).  Defaults to the folder/file basename.
    glob_pattern  Comma-separated glob patterns (relative to path).
    embed_model   Ollama embedding model.  Default: nomic-embed-text.
    overwrite     Rebuild index even if one already exists.

    Returns a summary: how many files/chunks were indexed.
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"SKILL_ERROR: path not found: {path}"

    name = name or Path(path).name.replace(" ", "_").lower()
    idx_dir = _index_dir(name)
    chunks_file = os.path.join(idx_dir, "chunks.json")

    if os.path.exists(chunks_file) and not overwrite:
        meta = json.loads(open(os.path.join(idx_dir, "meta.json")).read())
        return (f"Index '{name}' already exists ({meta.get('total_chunks', '?')} chunks, "
                f"model={meta.get('embed_model', '?')}). "
                "Pass overwrite=true to rebuild it.")

    # Collect files
    patterns = [p.strip() for p in glob_pattern.split(",") if p.strip()]
    files = []
    if os.path.isfile(path):
        files = [path]
    else:
        for pat in patterns:
            for f in glob.glob(os.path.join(path, pat), recursive=True):
                if os.path.isfile(f) and f not in files:
                    files.append(f)

    if not files:
        return f"No files matched patterns {patterns!r} under {path}"

    # Chunk all files
    all_chunks = []
    skipped = 0
    for f in files:
        c = _chunk_file(f)
        if c:
            all_chunks.extend(c)
        else:
            skipped += 1

    if not all_chunks:
        return "No text chunks produced — are the files readable?"

    # Embed all chunks (with progress reported via return string later)
    texts = [c["text"] for c in all_chunks]
    try:
        embeddings = _embed_batch(texts, embed_model)
    except Exception as e:
        return f"SKILL_ERROR: embedding failed: {e}"

    # Persist
    os.makedirs(idx_dir, exist_ok=True)
    with open(os.path.join(idx_dir, "chunks.json"), "w") as f:
        json.dump(all_chunks, f)
    np.save(os.path.join(idx_dir, "embeddings.npy"), embeddings)
    meta = {
        "name": name,
        "embed_model": embed_model,
        "source_path": path,
        "created": int(time.time()),
        "total_files": len(files),
        "skipped_files": skipped,
        "total_chunks": len(all_chunks),
        "embedding_dim": int(embeddings.shape[1]),
    }
    with open(os.path.join(idx_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    return (
        f"Index '{name}' built successfully.\n"
        f"  Files indexed : {len(files)} ({skipped} skipped)\n"
        f"  Chunks created: {len(all_chunks)}\n"
        f"  Embed model   : {embed_model}\n"
        f"  Stored at     : {idx_dir}"
    )


def rag_search(
    query: str,
    name: str,
    top_k: int = 5,
    embed_model: str = DEFAULT_MODEL,
    min_score: float = 0.0,
) -> str:
    """
    Search a RAG index and return the most relevant code/text chunks.

    Parameters
    ----------
    query       Natural-language question or code snippet to search for.
    name        Index name (as given to rag_index).
    top_k       Number of top results to return (default 5).
    embed_model Embedding model (must match what was used to build the index).
    min_score   Minimum cosine similarity threshold (0.0 = no filter).

    Returns a formatted string of matches ready to feed back into the agent.
    """
    try:
        chunks, emb_matrix = _load_index(name)
    except FileNotFoundError as e:
        return f"SKILL_ERROR: {e}. Run rag_index first."

    # Embed the query
    try:
        q_vec = _embed_batch([query], embed_model)[0]            # shape (D,)
    except Exception as e:
        return f"SKILL_ERROR: embedding failed: {e}"

    # Cosine similarity (matrix is already L2-normalised)
    scores = emb_matrix @ q_vec                                  # shape (N,)
    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for rank, idx in enumerate(top_indices, 1):
        score = float(scores[idx])
        if score < min_score:
            continue
        c = chunks[idx]
        header = (f"--- [{rank}] {c['file']}  "
                  f"lines {c['start_line']}–{c['end_line']}  "
                  f"(score {score:.3f}) ---")
        results.append(f"{header}\n{c['text']}")

    if not results:
        return f"No results above min_score={min_score} in index '{name}'."

    header_line = f"Top {len(results)} result(s) from index '{name}' for: {query!r}\n"
    return header_line + "\n\n".join(results)


def rag_list() -> str:
    """List all available RAG indexes with their metadata."""
    if not os.path.exists(INDEXES_DIR):
        return "No indexes found. Run rag_index to create one."
    names = [
        d for d in os.listdir(INDEXES_DIR)
        if os.path.isdir(os.path.join(INDEXES_DIR, d))
    ]
    if not names:
        return "No indexes found. Run rag_index to create one."
    lines = ["Available RAG indexes:"]
    for n in sorted(names):
        meta_path = os.path.join(INDEXES_DIR, n, "meta.json")
        if os.path.exists(meta_path):
            m = json.loads(open(meta_path).read())
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("created", 0)))
            lines.append(
                f"  {n:30s}  {m.get('total_chunks', '?'):>5} chunks  "
                f"model={m.get('embed_model', '?')}  created={ts}"
            )
        else:
            lines.append(f"  {n}  (no meta)")
    return "\n".join(lines)


def rag_drop(name: str) -> str:
    """Delete a RAG index by name."""
    import shutil
    idx_dir = _index_dir(name)
    if not os.path.exists(idx_dir):
        return f"Index '{name}' not found."
    shutil.rmtree(idx_dir)
    return f"Index '{name}' deleted."


# ── Skill exports (plural format) ─────────────────────────────────────────────

SKILL_FNS = [rag_index, rag_search, rag_list, rag_drop]

SKILL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "rag_index",
            "description": (
                "Index a local directory or file for RAG (Retrieval-Augmented Generation). "
                "Chunks the files, embeds each chunk with a local Ollama model, and saves "
                "the index to disk. Call this once before using rag_search. "
                "Python files are chunked at function/class boundaries; other files use "
                "a sliding window. Supports any text format: .py .md .txt .js .ts etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the folder or file to index.",
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Short slug name for the index, e.g. 'myproject'. "
                            "Defaults to the folder/file basename."
                        ),
                    },
                    "glob_pattern": {
                        "type": "string",
                        "description": (
                            "Comma-separated glob patterns, e.g. '**/*.py,**/*.md'. "
                            "Default covers .py .md .txt .js .ts"
                        ),
                    },
                    "embed_model": {
                        "type": "string",
                        "description": "Ollama embedding model. Default: nomic-embed-text.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Rebuild index even if it already exists.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": (
                "Search a RAG index and return the most relevant code/text chunks. "
                "Use this to find relevant context from a large codebase before answering "
                "questions or making changes. Always rag_index first. "
                "Returns formatted excerpts with file paths and line numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language question or code pattern to search for.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Index name (as given to rag_index).",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of top results to return. Default 5.",
                    },
                    "embed_model": {
                        "type": "string",
                        "description": "Must match the model used when indexing.",
                    },
                    "min_score": {
                        "type": "number",
                        "description": "Minimum cosine similarity (0.0–1.0). Default 0.0.",
                    },
                },
                "required": ["query", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_list",
            "description": "List all available RAG indexes with chunk counts and metadata.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_drop",
            "description": "Delete a RAG index by name to free disk space.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Index name to delete."},
                },
                "required": ["name"],
            },
        },
    },
]
