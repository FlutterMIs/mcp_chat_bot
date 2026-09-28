"""The permanent source registry: add / list / rename / refresh / remove sources of a workspace, load their data for the
tools, and keep a per-process cache so a Streamlit rerun or a WhatsApp message does not re-download anything.

    reg = SourceRegistry()
    reg.add_google_sheet(ws, user, url)          → row dict (status connected|error), data synced once
    reg.add_website(ws, user, url)               → website text/tables stored in web_documents (refresh re-fetches)
    reg.add_file(ws, user, filename, bytes)      → bytes stored under DATA_DIR/files/<workspace>/
    reg.add_database(ws, user, url)              → URL encrypted in source_credentials, only a safe name in config
    reg.ensure(server, ws)                       → registers every connected source of the workspace into an MCPServer
                                                   (from cache when fresh) and returns {source_id: schema}
Source ids (`src_…`) are the database ids: the same in the web app, WhatsApp, learning tables and results.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from . import credentials
from .db import Source, SourceFile, SourceSync, WebDocument, now, session_scope
from .events import log_event
from .workspaces import Forbidden, require_source

TTL = {"google_sheet": int(os.getenv("SOURCE_TTL_SHEET", "300")), "database": int(os.getenv("SOURCE_TTL_DB", "300")), "website": None, "file": None}
_cache: dict[str, dict] = {}          # source_id -> {"version", "parsed", "schema", "loaded_at"}
_cache_lock = threading.Lock()


class SourceError(ValueError):
    pass


def _files_root():
    root = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent.parent / "app_data") / "files"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "file"))[:120] or "file"


def _row(src: Source) -> dict:
    return {"id": src.id, "workspace_id": src.workspace_id, "name": src.name, "type": src.type, "provider": src.provider, "auth_mode": src.auth_mode,
            "status": src.status, "version": src.version, "last_synced_at": src.last_synced_at, "last_error": src.last_error,
            "connection_config": {k: v for k, v in (src.connection_config or {}).items()}, "credential_id": src.credential_id,
            "schema_snapshot": src.schema_snapshot, "metadata": src.meta or {}, "created_at": src.created_at, "updated_at": src.updated_at}


def sheet_id_of(url):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", str(url or ""))
    if not m:
        raise SourceError("Ye Google Sheet ka link nahi lag raha (…/spreadsheets/d/<id>/… chahiye).")
    return m.group(1)


class SourceRegistry:
    # ------------------------------------------------------------ create
    def add_google_sheet(self, workspace_id, user_id, url, name=None, credential_id=None):
        sid = sheet_id_of(url)
        with session_scope() as s:
            dup = s.scalar(select(Source).where(Source.workspace_id == workspace_id, Source.type == "google_sheet", Source.is_deleted == False))  # noqa: E712
            for cand in s.scalars(select(Source).where(Source.workspace_id == workspace_id, Source.type == "google_sheet", Source.is_deleted == False)):  # noqa: E712
                if (cand.connection_config or {}).get("sheet_id") == sid:
                    return _row(cand)                 # already saved: never a second copy of the same sheet
            src = Source(workspace_id=workspace_id, name=name or "Google Sheet", type="google_sheet", provider="google",
                         auth_mode="oauth" if credential_id else "public", credential_id=credential_id, created_by=user_id,
                         connection_config={"sheet_id": sid, "url": f"https://docs.google.com/spreadsheets/d/{sid}/edit"})
            s.add(src)
            s.flush()
            src_id = src.id
        return self.sync(workspace_id, src_id, first=True, autoname=not name)

    def add_website(self, workspace_id, user_id, url, name=None):
        url = str(url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            raise SourceError("Website ka poora URL do (https://…).")
        with session_scope() as s:
            for cand in s.scalars(select(Source).where(Source.workspace_id == workspace_id, Source.type == "website", Source.is_deleted == False)):  # noqa: E712
                if (cand.connection_config or {}).get("url", "").rstrip("/") == url.rstrip("/"):
                    return _row(cand)
            src = Source(workspace_id=workspace_id, name=name or url, type="website", provider="http", created_by=user_id,
                         connection_config={"url": url, "crawl_config": {"depth": 0}})
            s.add(src)
            s.flush()
            src_id = src.id
        return self.sync(workspace_id, src_id, first=True, autoname=not name)

    def add_file(self, workspace_id, user_id, filename, data: bytes, name=None, mime=None):
        digest = hashlib.sha256(data).hexdigest()
        with session_scope() as s:
            for f in s.scalars(select(SourceFile).where(SourceFile.workspace_id == workspace_id, SourceFile.sha256 == digest)):
                src = s.get(Source, f.source_id)
                if src and not src.is_deleted:
                    return _row(src)                   # same file uploaded again → the saved source
            src = Source(workspace_id=workspace_id, name=name or filename, type="file", provider="upload", created_by=user_id, connection_config={"filename": filename})
            s.add(src)
            s.flush()
            folder = _files_root() / workspace_id
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{src.id}__{_safe_name(filename)}"
            path.write_bytes(data)
            s.add(SourceFile(source_id=src.id, workspace_id=workspace_id, filename=filename, mime=mime, size=len(data), storage_path=str(path), sha256=digest))
            src.connection_config = {"filename": filename, "storage_path": str(path)}
            src_id = src.id
        return self.sync(workspace_id, src_id, first=True)

    def add_database(self, workspace_id, user_id, url, name=None):
        from source_loader import safe_db_name
        url = str(url or "").strip()
        if "://" not in url:
            raise SourceError("SQLAlchemy URL chahiye, jaise postgresql://user:pass@host/db")
        cred = credentials.store(workspace_id, "db_url", {"url": url})
        with session_scope() as s:
            src = Source(workspace_id=workspace_id, name=name or safe_db_name(url), type="database", provider=url.split(":", 1)[0].split("+")[0],
                         auth_mode="secret", credential_id=cred, created_by=user_id, connection_config={"label": safe_db_name(url)})
            s.add(src)
            s.flush()
            src_id = src.id
        return self.sync(workspace_id, src_id, first=True)

    # ------------------------------------------------------------ read / manage
    def list(self, workspace_id, include_disabled=True):
        with session_scope() as s:
            rows = s.scalars(select(Source).where(Source.workspace_id == workspace_id, Source.is_deleted == False).order_by(Source.created_at)).all()  # noqa: E712
            return [_row(r) for r in rows if include_disabled or r.status != "disabled"]

    def get(self, workspace_id, source_id):
        with session_scope() as s:
            return _row(require_source(s, workspace_id, source_id))

    def rename(self, workspace_id, source_id, name):
        with session_scope() as s:
            src = require_source(s, workspace_id, source_id)
            src.name = (name or "").strip() or src.name
            return _row(src)

    def set_enabled(self, workspace_id, source_id, enabled: bool):
        with session_scope() as s:
            src = require_source(s, workspace_id, source_id)
            src.status = "connected" if enabled else "disabled"
            src.version += 1
            return _row(src)

    def remove(self, workspace_id, source_id):
        """Soft delete; learned mappings/rules for the source go with it, files are removed from disk."""
        from . import learning
        with session_scope() as s:
            src = require_source(s, workspace_id, source_id)
            src.is_deleted, src.status, src.version = True, "disabled", src.version + 1
            for f in s.scalars(select(SourceFile).where(SourceFile.source_id == source_id)):
                try:
                    Path(f.storage_path).unlink(missing_ok=True)
                except OSError:
                    pass
        learning.forget_source(workspace_id, source_id)
        with _cache_lock:
            _cache.pop(source_id, None)
        try:
            import rag
            rag.forget(source_id)                      # its knowledge chunks go with it
        except Exception:
            pass
        log_event("source_removed", source_id=source_id)

    # ------------------------------------------------------------ data
    def load_parsed(self, src: dict):
        """The parsed source (same shape `MCPServer.register_file` takes) from its saved connection + credential."""
        cfg, t = src["connection_config"], src["type"]
        if t == "google_sheet":
            if src.get("auth_mode") == "oauth" and src.get("credential_id"):
                from . import google_oauth
                return google_oauth.load_workbook(src["workspace_id"], src["credential_id"], cfg["sheet_id"], name=src["name"])
            from mcp_server import MCPServer
            tabs = MCPServer().load_google_workbook(cfg["url"])
            return {"kind": "workbook", "name": src["name"], "url": cfg["url"], "tabs": tabs}
        if t == "website":
            return self._load_website(src)
        if t == "file":
            from source_loader import read_file
            path = Path(cfg.get("storage_path", ""))
            if not path.exists():
                raise SourceError(f"File server par nahi mili: {cfg.get('filename')}. Dobara upload karo.")
            parsed = read_file(cfg.get("filename") or path.name, path.read_bytes())
            parsed["name"] = src["name"]
            return parsed
        if t == "database":
            from source_loader import read_database
            url = credentials.reveal(src["workspace_id"], src["credential_id"])["url"]
            try:
                parsed = read_database(url)
            except Exception as e:
                raise SourceError(credentials.redact(str(e), [url])) from e
            parsed["name"] = src["name"]
            return parsed
        raise SourceError(f"Unknown source type {t}")

    def _load_website(self, src, refetch=False):
        import pandas as pd
        with session_scope() as s:
            doc = s.scalar(select(WebDocument).where(WebDocument.source_id == src["id"]).order_by(WebDocument.fetched_at.desc()))
        if doc is not None and not refetch:
            tables = {k: pd.DataFrame(v) for k, v in (doc.tables or {}).items()}
            return {"kind": "web", "name": src["name"], "url": doc.url, "text": doc.text or "", "tables": tables, "images": doc.images or [], "videos": doc.videos or [],
                    "fetched_at": doc.fetched_at}
        from source_loader import read_web
        parsed = read_web(src["connection_config"]["url"])
        digest = hashlib.sha256((parsed.get("text") or "").encode()).hexdigest()
        with session_scope() as s:
            s.add(WebDocument(source_id=src["id"], url=parsed.get("url") or src["connection_config"]["url"], title=parsed.get("name"), content_hash=digest,
                              text=parsed.get("text"), tables={k: v.astype(object).where(v.notna(), None).to_dict(orient="records") for k, v in (parsed.get("tables") or {}).items()},
                              images=parsed.get("images"), videos=parsed.get("videos")))
            for old in s.scalars(select(WebDocument).where(WebDocument.source_id == src["id"]).order_by(WebDocument.fetched_at.desc()).offset(3)):
                s.delete(old)                          # keep the last 3 fetches
        parsed["fetched_at"] = now()
        return parsed

    def sync(self, workspace_id, source_id, first=False, autoname=False):
        """(Re)load the source, store schema_snapshot + a source_syncs row, bump version so caches reload."""
        src = self.get(workspace_id, source_id)
        t0 = time.time()
        with session_scope() as s:
            rec = SourceSync(source_id=source_id, status="running")
            s.add(rec)
            s.flush()
            rec_id = rec.id
            row = s.get(Source, source_id)
            row.status = "syncing"
        try:
            parsed = self._load_website(src, refetch=True) if src["type"] == "website" else self.load_parsed(src)
            from mcp_server import MCPServer
            probe = MCPServer()
            schema = probe.register_file(source_id, parsed)
            tables = schema.get("sheets") or []
            rows = sum(int(t.get("row_count") or 0) for t in tables)
            with session_scope() as s:
                row = s.get(Source, source_id)
                row.status, row.last_error, row.last_synced_at, row.version = "connected", None, now(), row.version + 1
                row.schema_snapshot = {k: v for k, v in schema.items() if k != "source_id"}
                if autoname and parsed.get("name") and parsed["name"] != source_id:
                    row.name = str(parsed["name"])[:200]
                rec = s.get(SourceSync, rec_id)
                rec.status, rec.finished_at, rec.rows, rec.tables = "ok", now(), rows, len(tables)
                rec.content_hash = hashlib.sha256(str(sorted((t.get("name"), t.get("row_count")) for t in tables)).encode()).hexdigest()
            with _cache_lock:
                _cache[source_id] = {"version": src["version"] + 1, "parsed": parsed, "schema": schema, "loaded_at": time.time()}
            log_event("source_synced", ms=(time.time() - t0) * 1000, source_id=source_id, type=src["type"], rows=rows, tables=len(tables))
        except Exception as e:
            msg = credentials.redact(str(e), [])[:500]
            with session_scope() as s:
                row = s.get(Source, source_id)
                row.status, row.last_error = "error", msg
                rec = s.get(SourceSync, rec_id)
                rec.status, rec.finished_at, rec.error = "error", now(), msg
            log_event("source_sync_failed", ms=(time.time() - t0) * 1000, source_id=source_id, type=src["type"], error=type(e).__name__)
            if first:
                pass                                  # the row stays with status=error so the user sees why and can retry / remove
        return self.get(workspace_id, source_id)

    def refresh(self, workspace_id, source_id):
        return self.sync(workspace_id, source_id)

    def ensure(self, server, workspace_id, source_ids=None):
        """Register the workspace's connected sources into `server` (an MCPServer or anything with register_file) and
        return {source_id: schema}. Fresh cache entries are reused; stale sheets/databases are re-downloaded (TTL)."""
        out, errors = {}, {}
        for src in self.list(workspace_id, include_disabled=False):
            if src["status"] not in ("connected", "error") or (source_ids is not None and src["id"] not in source_ids):
                continue
            entry = _cache.get(src["id"])
            ttl = TTL.get(src["type"])
            stale = entry is None or entry["version"] != src["version"] or (ttl is not None and time.time() - entry["loaded_at"] > ttl)
            if stale:
                try:
                    parsed = self.load_parsed(src)
                    schema = server.register_file(src["id"], parsed)
                    with _cache_lock:
                        _cache[src["id"]] = {"version": src["version"], "parsed": parsed, "schema": schema, "loaded_at": time.time()}
                    if src["type"] in ("google_sheet", "database"):
                        with session_scope() as s:
                            row = s.get(Source, src["id"])
                            row.last_synced_at, row.status, row.last_error = now(), "connected", None
                except Exception as e:
                    errors[src["id"]] = str(e)[:300]
                    if entry is not None:             # serve the last good copy, but say so
                        server.register_file(src["id"], entry["parsed"])
                        out[src["id"]] = dict(entry["schema"], stale=True)
                    with session_scope() as s:
                        row = s.get(Source, src["id"])
                        row.status, row.last_error = "error", credentials.redact(str(e), [])[:500]
                    continue
            else:
                server.register_file(src["id"], entry["parsed"])
                schema = entry["schema"]
            out[src["id"]] = schema
        return out, errors

    def freshness(self, src: dict) -> str:
        """'Last synced 2 minutes ago' for the UI and for answers about a website."""
        ts = src.get("last_synced_at")
        if not ts:
            return "not synced yet"
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = max(0, (now() - ts).total_seconds())
        if secs < 90:
            return "just now"
        if secs < 3600:
            return f"{int(secs // 60)} min ago"
        if secs < 86400:
            return f"{int(secs // 3600)} h ago"
        return f"{int(secs // 86400)} d ago"


def invalidate(source_id=None):
    with _cache_lock:
        if source_id:
            _cache.pop(source_id, None)
        else:
            _cache.clear()
