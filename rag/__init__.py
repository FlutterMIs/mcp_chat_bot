"""Optional hybrid RAG knowledge layer (OFF by default: RAG_ENABLED=false).

Scope: unstructured knowledge only — PDF / DOCX / TXT / manuals / SOPs / policies / FAQs / website text.
Never for sales totals, inventory, month-wise or category-wise figures: those always go through the MCP data tools.

    documents / websites → chunking (page / section metadata) → embeddings → hybrid retrieval (keyword + semantic)
                        → top-k chunks with source / page / score → LLM (citations added by code)

Configuration (env, or `configure()` for tests):
    RAG_ENABLED=false            RAG_TOP_K=5           RAG_MIN_SCORE=0.12
    RAG_EMBEDDING_PROVIDER=hash  (hash = built-in hashed lexical vectors, no network; openrouter = /embeddings API;
                                  sentence_transformers = local model if the package is installed)
    RAG_EMBEDDING_MODEL=...      RAG_CHUNK_CHARS=900   RAG_INDEX_PATH=<DATA_DIR>/rag_index.db (empty = memory only)
"""
from __future__ import annotations

import os
import threading

_override: dict = {}
_lock = threading.Lock()
_indexes: dict = {}          # source_id -> Index (process cache)


def _env(name, default):
    if name in _override:
        return _override[name]
    return os.getenv(name, default)


def configure(**kw):
    """configure(RAG_ENABLED=True, RAG_TOP_K=3, ...) for tests / programmatic setups; configure() clears overrides."""
    global _override
    with _lock:
        _override = {k: v for k, v in kw.items()} if kw else {}
        _indexes.clear()


def enabled() -> bool:
    return str(_env("RAG_ENABLED", "false")).strip().lower() in ("1", "true", "yes", "on")


def top_k() -> int:
    try:
        return max(1, min(20, int(_env("RAG_TOP_K", 5))))
    except (TypeError, ValueError):
        return 5


def min_score() -> float:
    try:
        return float(_env("RAG_MIN_SCORE", 0.12))
    except (TypeError, ValueError):
        return 0.12


def chunk_chars() -> int:
    try:
        return max(200, int(_env("RAG_CHUNK_CHARS", 900)))
    except (TypeError, ValueError):
        return 900


def provider_name() -> str:
    return str(_env("RAG_EMBEDDING_PROVIDER", "hash")).strip().lower() or "hash"


def index_path():
    p = _env("RAG_INDEX_PATH", None)
    if p is None:
        from pathlib import Path
        root = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent.parent / "app_data")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return str(root / "rag_index.db")
    return p or None


KNOWLEDGE_KINDS = ("document", "web", "text")


def is_knowledge_source(parsed_or_schema: dict) -> bool:
    return (parsed_or_schema or {}).get("kind") in KNOWLEDGE_KINDS and bool((parsed_or_schema or {}).get("text") or (parsed_or_schema or {}).get("text_length"))


# ---------------------------------------------------------------- indexing / search (used by mcp_server)
def index_source(source_id, parsed) -> bool:
    """Chunk + embed a document/web source (idempotent per content hash). Returns True when an index exists."""
    if not enabled() or not is_knowledge_source(parsed):
        return False
    from .index import Index
    with _lock:
        idx = _indexes.get(source_id)
        if idx is None:
            idx = Index(source_id, index_path())
            _indexes[source_id] = idx
    idx.ensure(parsed)
    return True


def has_index(source_id) -> bool:
    idx = _indexes.get(source_id)
    return bool(idx and idx.ready)


def search(source_id, query, k=None):
    """[{chunk_id, text, source, page, section, score, keyword_score, semantic_score}] — best first, empty when nothing is relevant."""
    idx = _indexes.get(source_id)
    if not idx or not idx.ready:
        return []
    return idx.search(query, k or top_k(), min_score())


HYBRID_MIN_SCORE = 0.3      # a data question only becomes MCP+RAG when a document passage matches this strongly


def hits_any(source_ids, query, strong=HYBRID_MIN_SCORE):
    """True when at least one knowledge source has a clearly relevant chunk for the question (cheap, local)."""
    for sid in source_ids:
        if has_index(sid):
            hits = search(sid, query, 1)
            if hits and hits[0]["score"] >= strong:
                return True
    return False


def forget(source_id=None):
    with _lock:
        if source_id:
            idx = _indexes.pop(source_id, None)
            if idx:
                idx.drop()
        else:
            _indexes.clear()
