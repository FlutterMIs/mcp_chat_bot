"""Embedding providers behind one `embed(texts) -> [[float]]` interface.

    hash                   built-in: hashed word / bigram / character-trigram features (no model, no network, deterministic).
                           Semantic enough for typo-tolerant matching; the keyword half of the hybrid search covers exact terms.
    openrouter             POST {OPENROUTER_URL}/embeddings with RAG_EMBEDDING_MODEL (spends credit; only when configured).
    sentence_transformers  local model (RAG_EMBEDDING_MODEL, default multilingual MiniLM) if the package is installed.

Every provider L2-normalises so cosine similarity is a dot product."""
from __future__ import annotations

import hashlib
import math
import os
import re

DIM = 1024


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _tokens(text):
    return re.findall(r"[a-z0-9ऀ-ॿ]+", str(text or "").lower())


class HashEmbedder:
    name = "hash"

    def __init__(self, dim=DIM):
        self.dim = dim

    def _features(self, text):
        toks = _tokens(text)
        feats = []
        for t in toks:
            feats.append(("w", t))
            if len(t) >= 5:                         # character trigrams make "pranjli" ≈ "pranjali", "recoin" ≈ "recording"
                feats += [("c", t[i:i + 3]) for i in range(len(t) - 2)]
        feats += [("b", a + "_" + b) for a, b in zip(toks, toks[1:])]
        return feats

    def embed_one(self, text):
        v = [0.0] * self.dim
        for kind, f in self._features(text):
            h = int(hashlib.blake2b(f"{kind}:{f}".encode(), digest_size=8).hexdigest(), 16)
            idx, sign = h % self.dim, 1.0 if (h >> 63) & 1 else -1.0
            weight = {"w": 1.0, "b": 0.7, "c": 0.35}[kind]
            v[idx] += sign * weight
        return _norm(v)

    def embed(self, texts):
        return [self.embed_one(t) for t in texts]


class OpenRouterEmbedder:
    name = "openrouter"

    def __init__(self, model=None, api_key=None):
        self.model = model or os.getenv("RAG_EMBEDDING_MODEL") or "openai/text-embedding-3-small"
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")

    def embed(self, texts):
        import requests
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for RAG embeddings")
        out = []
        for i in range(0, len(texts), 64):
            r = requests.post("https://openrouter.ai/api/v1/embeddings", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                              json={"model": self.model, "input": texts[i:i + 64]}, timeout=120)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            out += [_norm(d["embedding"]) for d in data]
        return out


class SentenceTransformersEmbedder:
    name = "sentence_transformers"

    def __init__(self, model=None):
        from sentence_transformers import SentenceTransformer   # optional dependency
        self.model = SentenceTransformer(model or os.getenv("RAG_EMBEDDING_MODEL") or "paraphrase-multilingual-MiniLM-L12-v2")

    def embed(self, texts):
        return [_norm([float(x) for x in v]) for v in self.model.encode(list(texts), show_progress_bar=False)]


def get_embedder(name=None):
    name = (name or os.getenv("RAG_EMBEDDING_PROVIDER") or "hash").strip().lower()
    if name == "openrouter":
        return OpenRouterEmbedder()
    if name in ("sentence_transformers", "sentence-transformers", "local"):
        try:
            return SentenceTransformersEmbedder()
        except Exception:
            return HashEmbedder()                       # package/model missing → still works offline
    return HashEmbedder()
