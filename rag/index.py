"""Per-source index: chunks + vectors, hybrid retrieval, optional SQLite persistence (keyed by content hash).

Retrieval = BM25 over tokens (exact terms, codes, names)  +  cosine over embeddings (paraphrase / typos),
fused with reciprocal-rank fusion. Scores are returned per chunk so the caller can refuse when nothing is relevant."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter

from . import chunk_chars, provider_name
from .chunking import chunk_document
from .embeddings import get_embedder

STOP = {"kya", "hai", "hain", "iske", "iska", "iski", "isme", "isse", "ye", "yeh", "batao", "bta", "btao", "bata", "kaise", "kitna", "kitne", "kitni", "mein", "me",
        "ka", "ki", "ke", "ko", "aur", "bhi", "the", "and", "what", "is", "are", "this", "that", "about", "tell", "please", "bro", "bhai", "do", "de", "hota", "hoti",
        "wala", "wali", "of", "a", "an", "in", "on", "for", "to", "or", "with", "how", "which", "who", "kaun", "kon", "se", "par", "pe", "ho", "hi", "toh", "tha", "thi"}


def _tok(text):
    return [t for t in re.findall(r"[a-z0-9ऀ-ॿ]+", str(text or "").lower()) if t not in STOP and len(t) > 1]


class Index:
    def __init__(self, source_id, db_path=None):
        self.source_id, self.db_path = source_id, db_path
        self.chunks, self.vectors, self.content_hash = [], [], None
        self._df, self._len, self._avg = Counter(), [], 1.0
        self._lock = threading.Lock()
        self.embedder = get_embedder(provider_name())
        self.ready = False

    # ------------------------------------------------------------ build
    def ensure(self, parsed):
        text = parsed.get("text") or ""
        digest = hashlib.sha256((self.embedder.name + "|" + text).encode("utf-8", "replace")).hexdigest()
        with self._lock:
            if self.ready and digest == self.content_hash:
                return
            if self._load(digest):
                return
            chunks = chunk_document(parsed, max_chars=chunk_chars())
            vectors = self.embedder.embed([c["text"] for c in chunks]) if chunks else []
            self._set(chunks, vectors, digest)
            self._save()

    def _set(self, chunks, vectors, digest):
        self.chunks, self.vectors, self.content_hash = chunks, vectors, digest
        self._len = [len(_tok(c["text"])) for c in chunks]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        self._df = Counter()
        for c in chunks:
            self._df.update(set(_tok(c["text"])))
        self.ready = True

    # ------------------------------------------------------------ persistence (sqlite3, stdlib)
    def _conn(self):
        if not self.db_path:
            return None
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS rag_chunks (source_id TEXT, content_hash TEXT, chunk_id INTEGER, page INTEGER, section TEXT, source TEXT, text TEXT, vector TEXT, PRIMARY KEY (source_id, chunk_id))")
        return conn

    def _load(self, digest):
        conn = self._conn()
        if conn is None:
            return False
        try:
            rows = conn.execute("SELECT chunk_id, page, section, source, text, vector FROM rag_chunks WHERE source_id=? AND content_hash=? ORDER BY chunk_id", (self.source_id, digest)).fetchall()
        finally:
            conn.close()
        if not rows:
            return False
        chunks = [{"chunk_id": r[0], "page": r[1], "section": r[2], "source": r[3], "text": r[4], "start": None} for r in rows]
        self._set(chunks, [json.loads(r[5]) for r in rows], digest)
        return True

    def _save(self):
        conn = self._conn()
        if conn is None:
            return
        try:
            conn.execute("DELETE FROM rag_chunks WHERE source_id=?", (self.source_id,))
            conn.executemany("INSERT INTO rag_chunks VALUES (?,?,?,?,?,?,?,?)",
                             [(self.source_id, self.content_hash, c["chunk_id"], c.get("page"), c.get("section"), c.get("source"), c["text"], json.dumps([round(x, 6) for x in v]))
                              for c, v in zip(self.chunks, self.vectors)])
            conn.commit()
        finally:
            conn.close()

    def drop(self):
        conn = self._conn()
        if conn is not None:
            try:
                conn.execute("DELETE FROM rag_chunks WHERE source_id=?", (self.source_id,))
                conn.commit()
            finally:
                conn.close()
        self.chunks, self.vectors, self.ready = [], [], False

    # ------------------------------------------------------------ search
    def _bm25(self, qtoks, k1=1.5, b=0.75):
        n = len(self.chunks)
        scores = [0.0] * n
        for t in set(qtoks):
            df = self._df.get(t, 0)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, c in enumerate(self.chunks):
                tf = c.setdefault("_tf", Counter(_tok(c["text"]))).get(t, 0)
                if tf:
                    scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * self._len[i] / self._avg))
        return scores

    def _fuzzy_query(self, qtoks):
        """Typo-tolerant keyword side: a query word of 5+ letters that is not in the index vocabulary is replaced by its closest vocabulary word."""
        import difflib
        vocab = list(self._df)
        out = []
        for t in qtoks:
            if t in self._df or len(t) < 5:
                out.append(t)
                continue
            m = difflib.get_close_matches(t, [v for v in vocab if v[:1] == t[:1]], n=1, cutoff=0.8)
            out.append(m[0] if m else t)
        return out

    def search(self, query, k=5, min_score=0.05):
        if not self.ready or not self.chunks:
            return []
        qtoks = self._fuzzy_query(_tok(query))
        kw = self._bm25(qtoks)
        qv = self.embedder.embed([query])[0]
        sem = [sum(a * b for a, b in zip(qv, v)) for v in self.vectors]
        kw_rank = sorted(range(len(kw)), key=lambda i: -kw[i])
        sem_rank = sorted(range(len(sem)), key=lambda i: -sem[i])
        fused = {}
        for rank, i in enumerate(kw_rank):
            if kw[i] > 0:
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
        for rank, i in enumerate(sem_rank):
            if sem[i] > 0.05:
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
        qset = set(qtoks)
        out = []
        for i, f in sorted(fused.items(), key=lambda x: -x[1])[:k]:
            c = self.chunks[i]
            coverage = (len(qset & set(c.setdefault("_tf", Counter(_tok(c["text"]))))) / len(qset)) if qset else 0.0
            score = round(0.5 * coverage + 0.5 * max(sem[i], 0.0), 4)     # 0..1: share of query words present + cosine
            if score < min_score:
                continue
            out.append({"chunk_id": c["chunk_id"], "text": c["text"], "source": c.get("source"), "page": c.get("page"), "section": c.get("section"),
                        "score": score, "keyword_score": round(kw[i], 3), "semantic_score": round(sem[i], 3)})
        return out
