"""WhatsApp channel: OpenWA message in -> shared analyst brain -> WhatsApp reply out.

No business logic lives here; it only adapts messages, files and voice notes to analyst.answer().
"""
from dataclasses import dataclass, field
import base64
import os
import re
import threading
import time
from pathlib import Path

import requests

import analyst
from analyst import Conversation
from openrouter import OpenRouterAI
from source_loader import read_file
from .format import HELP, LIST_LIMIT, compose, rest_as_text
from .links import SharedFiles
from .log import log
from .openwa import OpenWAError
from .store import chat_key, chat_slug
from .webhook import InboundMessage, normalize

DATA_EXTS = {"csv", "xlsx", "xls", "pdf", "docx", "pptx", "txt", "md", "json", "html", "htm"}
MIME_EXT = {"text/csv": "csv", "application/pdf": "pdf", "application/json": "json", "text/plain": "txt", "text/html": "html",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx", "application/vnd.ms-excel": "xls",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx"}
AUDIO_FMT = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm"}
IGNORED_TYPES = {"call", "revoked", "masked", "poll", "location", "contact", "unknown"}
URL = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]]", re.I)
# Domains typed without http(s):// ("Callsaathi.ai", "www.example.in/pricing"); not e-mail addresses or file names.
BARE_URL = re.compile(r"(?<![@\w./-])(?:www\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]+)*\."
                      r"(?:com|in|ai|io|org|net|co|app|dev|me|info|biz|xyz|tech|store|shop|online|site|live|cloud|us|uk)(?:/[^\s<>\"']*)?(?![\w@])", re.I)


def find_links(text):
    """Full URLs plus bare domains (normalised to https://)."""
    text = text or ""
    urls = URL.findall(text)
    rest = URL.sub(" ", text)
    urls += ["https://" + m.group(0).rstrip(".,;:!?)") for m in BARE_URL.finditer(rest)]
    return urls


def strip_links(text):
    return BARE_URL.sub("", URL.sub("", text or ""))
DB_URL = re.compile(r"\b(postgres(ql)?|mysql|mariadb|mssql|oracle|sqlite|snowflake|redshift)(\+\w+)?://", re.I)
CHART_WORDS = re.compile(r"\b(graph|graf|chart|plot|visual)\w*", re.I)
MAX_FILES_PER_CHAT = 5


def _digits(wid):
    return re.sub(r"\D", "", str(wid or "").split("@")[0])


def _allowed(candidates, allowlist):
    """919876543210 matches an entry written as 919876543210 or 9876543210 (no country code)."""
    return any(c and a and (c == a or (len(a) >= 10 and c.endswith(a))) for c in candidates for a in allowlist)


def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    openrouter_key: str = ""
    model: str = "openai/gpt-4.1-mini"
    stt_model: str = "google/gemini-2.5-flash"
    session: str = ""
    allowed_numbers: set = field(default_factory=set)   # {"*"} = everyone; empty = nobody
    allow_groups: bool = False
    sheet_url: str = ""
    data_files: list = field(default_factory=list)
    voice_reply: str = "off"                             # off | voice (ogg voice note) | audio (plain file)
    voice_reply_to_voice_only: bool = True
    tts_language: str = "auto"
    tts_voice: str | None = None
    tts_speed: float = 1.0
    files_dir: Path = Path("whatsapp_files")
    source_ttl: int = 300
    database_url: str = ""
    auto_chart: bool = True
    public_url: str = ""                                 # base URL of this server, for chart/CSV links
    shared_dir: Path = Path("whatsapp_shared")
    allow_chat_links: bool = True

    @classmethod
    def from_env(cls):
        nums = {n.strip() for n in os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").split(",") if n.strip()}
        return cls(
            openrouter_key=os.getenv("OPENROUTER_API_KEY", ""),
            model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini"),
            stt_model=os.getenv("OPENROUTER_STT_MODEL", "google/gemini-2.5-flash"),
            session=os.getenv("OPENWA_SESSION", ""),
            allowed_numbers={n if n == "*" else _digits(n) for n in nums},
            allow_groups=_env_bool("WHATSAPP_ALLOW_GROUPS"),
            sheet_url=os.getenv("WHATSAPP_GOOGLE_SHEET_URL") or os.getenv("GOOGLE_SHEET_URL", ""),
            data_files=[p.strip() for p in os.getenv("WHATSAPP_DATA_FILES", "").split(",") if p.strip()],
            voice_reply=os.getenv("WHATSAPP_VOICE_REPLY", "off").strip().lower(),
            voice_reply_to_voice_only=_env_bool("WHATSAPP_VOICE_REPLY_ONLY_TO_VOICE", True),
            tts_language=os.getenv("WHATSAPP_TTS_LANGUAGE", "auto"),
            tts_voice=os.getenv("WHATSAPP_TTS_VOICE") or None,
            tts_speed=float(os.getenv("WHATSAPP_TTS_SPEED", "1.0")),
            files_dir=Path(os.getenv("WHATSAPP_FILES_DIR", "whatsapp_files")),
            source_ttl=int(os.getenv("WHATSAPP_SOURCE_REFRESH_SECONDS", "300")),
            database_url=os.getenv("WHATSAPP_DATABASE_URL", ""),
            auto_chart=_env_bool("WHATSAPP_AUTO_CHART", True),
            public_url=os.getenv("WHATSAPP_PUBLIC_URL", "").rstrip("/"),
            shared_dir=Path(os.getenv("WHATSAPP_SHARED_DIR", "whatsapp_shared")),
            allow_chat_links=_env_bool("WHATSAPP_ALLOW_CHAT_LINKS", True),
        )


class SourceUnavailable(RuntimeError):
    pass


class Sources:
    """Registers data sources with the tools (MCP server) and re-registers after an MCP restart or TTL."""

    def __init__(self, tools, cfg):
        self.tools, self.cfg = tools, cfg
        self.schemas, self._loaded_at, self._gen = {}, {}, None
        self._lock = threading.Lock()
        self.defaults = {}
        if cfg.sheet_url:
            self.defaults["google_sheet"] = ("sheet", cfg.sheet_url)
        for i, path in enumerate(cfg.data_files):
            self.defaults[f"file_{i + 1}_{Path(path).stem}"[:60]] = ("file", path)
        if cfg.database_url:
            self.defaults["database"] = ("database", cfg.database_url)

    def _register(self, sid, spec):
        kind, target = spec
        try:
            if kind == "sheet":
                schema = self.tools.call("register_google_sheet", {"source_id": sid, "url": target})
            elif kind == "web":
                schema = self.tools.call("register_web", {"source_id": sid, "url": target})
            elif kind == "database":
                schema = self.tools.call("register_database", {"source_id": sid, "url": target})
            else:
                schema = self.tools.call("load_file", {"source_id": sid, "path": str(target)})
        except (requests.RequestException, OSError, ValueError) as e:
            err = str(e).replace(target, "<url>") if kind == "database" else str(e)
            log("source_failed", level="warning", source=sid, kind=kind, error=err[:200])
            raise SourceUnavailable(f"{sid}: {err}") from e
        self.schemas[sid], self._loaded_at[sid] = schema, time.time()
        log("source_loaded", source=sid, kind=kind, transport=getattr(self.tools, "transport", "?"))
        return schema

    def ensure(self, specs):
        """specs: {sid: (kind, target)} -> {sid: schema}. Stale Google Sheets are re-downloaded after the TTL."""
        with self._lock:
            gen = getattr(self.tools, "generation", 0)
            if gen != self._gen:
                self.schemas.clear(); self._loaded_at.clear(); self._gen = gen
            out = {}
            for sid, spec in specs.items():
                stale = spec[0] in ("sheet", "database") and time.time() - self._loaded_at.get(sid, 0) > self.cfg.source_ttl
                if sid not in self.schemas or stale:
                    try:
                        self._register(sid, spec)
                    except SourceUnavailable:
                        if sid not in self.schemas:
                            raise
                        log("source_stale_used", level="warning", source=sid)   # keep last good copy
                out[sid] = self.schemas[sid]
            return out


class WhatsAppBot:
    def __init__(self, cfg, openwa, store, tools):
        self.cfg, self.openwa, self.store, self.tools = cfg, openwa, store, tools
        self.sources = Sources(tools, cfg)
        self.own_numbers = set()
        self.session_ids = {cfg.session} if cfg.session else set()   # configured name + resolved UUID
        self._locks, self._locks_guard = {}, threading.Lock()
        self._lid_cache = {}
        self.shared = SharedFiles(cfg.shared_dir)
        self.public_base = cfg.public_url or None   # else learnt from incoming webhook requests
        self._media_broken_until = 0.0              # OpenWA media sends failing: go straight to links for a while

    # ---------- startup ----------
    def warm_up(self):
        try:
            s = self.openwa.resolve_session() if hasattr(self.openwa, "resolve_session") else self.openwa.get_session()
            self.session_ids.add(s.get("id"))
            if s.get("phone"):
                self.own_numbers.add(_digits(s["phone"]))
            log("openwa_session", status=s.get("status"), engine_loaded=s.get("engineLoaded"))
        except OpenWAError as e:
            log("openwa_session_failed", level="warning", error=str(e))
        try:
            self.sources.ensure(self.sources.defaults)
        except SourceUnavailable as e:
            log("default_source_failed", level="warning", error=str(e)[:200])

    # ---------- entry point (runs on a worker thread) ----------
    def handle_payload(self, payload):
        msg = normalize(payload)
        if msg is None:
            log("webhook_ignored", reason="not message.received", event=(payload or {}).get("event"))
            return
        reason = self.skip_reason(msg)
        log("webhook_received", message_id=msg.message_id, chat=msg.chat_id, sender=msg.sender_id, type=msg.message_type, skip=reason)
        if reason:
            return
        if not self.store.claim(msg.idempotency_key):
            log("duplicate_ignored", message_id=msg.message_id)
            return
        with self._chat_lock(msg.chat_id):
            self.process(msg)

    def skip_reason(self, msg: InboundMessage):
        """Loop and scope guards. Anything our own account sent must never reach the AI."""
        if msg.from_me:
            return "from_me"
        if msg.is_status:
            return "status_broadcast"
        if self.session_ids and msg.session_id and msg.session_id not in self.session_ids:
            return "other_session"
        sender = _digits(msg.sender_phone) or self._lid_phone(msg.sender_id) or _digits(msg.sender_id)
        if sender and sender in self.own_numbers:
            return "own_number"
        if msg.is_group and not self.cfg.allow_groups:
            return "group"
        if msg.message_type in IGNORED_TYPES:
            return f"type_{msg.message_type}"
        if "*" not in self.cfg.allowed_numbers and not _allowed({sender, _digits(msg.sender_id)}, self.cfg.allowed_numbers):
            return "not_allowlisted"
        return None

    def _lid_phone(self, wid):
        """WhatsApp may identify senders by an @lid privacy id instead of their number; ask OpenWA to map it."""
        if not str(wid).endswith("@lid"):
            return ""
        if wid not in self._lid_cache:
            try:
                self._lid_cache[wid] = _digits(self.openwa.resolve_phone(wid) or "")
            except (OpenWAError, AttributeError) as e:
                log("lid_resolve_failed", level="warning", sender=wid, error=str(e)[:120])
                return ""
            log("lid_resolved", sender=wid, found=bool(self._lid_cache[wid]))
        return self._lid_cache[wid]

    def _chat_lock(self, chat_id):
        with self._locks_guard:
            return self._locks.setdefault(chat_id, threading.Lock())

    # ---------- processing ----------
    def process(self, msg):
        key = chat_key(msg.session_id or self.cfg.session, msg.chat_id)
        state = self.store.load(key)
        t0 = time.time()
        self._typing(msg.chat_id, "typing")
        try:
            question, voice_in = msg.text, False
            if question.startswith("/"):
                return self.command(msg, key, state, question)
            if msg.message_type in ("voice", "audio"):
                question, voice_in = self.transcribe(msg), True
                if not question:
                    return self.send(msg.chat_id, "🎤 Voice message samajh nahi aaya. Thoda saaf bolke dobara bhejo, ya type kar do.")
                log("stt_done", message_id=msg.message_id, chars=len(question))
            elif msg.message_type in ("document", "image", "video", "sticker") or (msg.media and msg.message_type != "text"):
                question = self.receive_file(msg, key, state)
                if not question:
                    return
            if not question:
                return
            if msg.message_type == "text":
                question = self.receive_links(msg, key, state, question)
                if not question:
                    return
            reply = self.ask(msg, key, state, question)
            if reply is not None:
                self.deliver(msg, reply, question, voice_in)
        except OpenWAError as e:
            log("openwa_failed", level="error", message_id=msg.message_id, error=str(e))
        except Exception as e:  # never let one message kill the worker
            log("process_failed", level="error", message_id=msg.message_id, error=f"{type(e).__name__}: {str(e)[:300]}")
            self.send(msg.chat_id, "⚠️ Kuch gadbad ho gayi, jawab nahi bana paaya. Thodi der mein dobara try karo.")
        finally:
            self._typing(msg.chat_id, "paused")
            log("message_done", message_id=msg.message_id, ms=int((time.time() - t0) * 1000))

    def chat_specs(self, state):
        specs = dict(self.sources.defaults)
        for f in state.get("files", []):
            specs[f["sid"]] = (f.get("kind", "file"), f.get("target") or f["path"])
        return specs

    def ask(self, msg, key, state, question):
        try:
            schemas = self.sources.ensure(self.chat_specs(state))
        except SourceUnavailable:
            self.send(msg.chat_id, "⚠️ Data source (Google Sheet/file) abhi reach nahi ho paaya, isliye main koi number nahi bataunga. Thodi der baad try karo.")
            return None
        conv = Conversation(schemas=schemas, last_plan=state.get("last_plan"), history=state.get("history", []),
                            recent_plans=state.get("recent_plans", []), focus=state.get("focus"), pending_rule=state.get("pending_rule"), state=state.get("analysis"),
                            pending_choice=state.get("pending_choice"), totals=dict(state.get("totals") or {}))
        if state.get("last_result"):
            import pandas as pd
            conv.last_result = pd.DataFrame(state["last_result"]["rows"])
        for attempt in range(2):
            try:
                reply = analyst.answer(conv, question, self.cfg.openrouter_key, self.cfg.model, self.tools,
                                       log=lambda ev, **f: log(ev, message_id=msg.message_id, **f))
                break
            except ConnectionError:
                if attempt:
                    raise
                conv.schemas = self.sources.ensure(self.chat_specs(state))   # MCP restarted: re-register and retry once
            except requests.RequestException as e:
                log("llm_failed", level="error", message_id=msg.message_id, error=type(e).__name__)
                self.send(msg.chat_id, "⚠️ AI service abhi respond nahi kar rahi. Ek minute baad dobara pucho.")
                return None
            except ValueError as e:
                # Data tool refused every plan: say so, never guess a number.
                log("data_failed", level="warning", message_id=msg.message_id, error=str(e)[:300])
                self.send(msg.chat_id, f"⚠️ Data se ye jawab nahi nikal paaya: {str(e)[:300]}")
                return None
        state["last_plan"], state["history"], state["recent_plans"], state["pending_rule"] = conv.last_plan, conv.history, conv.recent_plans, conv.pending_rule
        state["analysis"] = conv.state
        state["pending_choice"] = conv.pending_choice
        state["totals"] = dict(list(conv.totals.items())[-50:])
        if reply.df is not None and not reply.df.empty:
            state["last_result"] = {"rows": reply.df.head(200).to_dict(orient="records"), "chart": reply.chart, "metric": reply.metric}
        self.store.save(key, state)
        return reply

    # ---------- output ----------
    def send(self, chat_id, text):
        try:
            return self.openwa.send_text(chat_id, text)
        except OpenWAError as e:
            log("send_failed", level="error", chat=chat_id, error=str(e))

    def deliver(self, msg, reply, question, voice_in):
        text, more = compose(reply, heard=question if voice_in else None)
        if reply.images:
            text += "\n" + "\n".join(f"{i}. {img['alt']}" for i, img in enumerate(reply.images, 1))
        self.openwa.send_text(msg.chat_id, text)
        if reply.images:
            self.send_page_images(msg.chat_id, reply.images)
        for f in reply.files:      # generated reports
            try:
                self.openwa.send_document(msg.chat_id, f["bytes"], f["name"], f["mime"], caption="📄 Report")
            except OpenWAError as e:
                self._media_failed("document", e)
                url = self._link(f["bytes"], "xlsx") if f["name"].endswith(".xlsx") else None
                self.send(msg.chat_id, f"📄 Report download: {url}" if url else "Report bhej nahi paaya.")
        from response import finalize
        final = finalize(reply, question)                          # the same response policy as the web UI
        has_rows = reply.df is not None and not reply.df.empty
        asked = reply.want_chart or bool(CHART_WORDS.search(question))
        if has_rows and final.chart is not None and (asked or self.cfg.auto_chart):
            reply.chart = final.chart
            self.send_chart(msg.chat_id, reply, quiet=not asked)
        if has_rows and more:
            self.send_full_result(msg.chat_id, reply)
        if self.cfg.voice_reply in ("voice", "audio") and (voice_in or not self.cfg.voice_reply_to_voice_only):
            self.send_voice(msg.chat_id, reply, question)

    def send_page_images(self, chat_id, images, limit=4):
        """Screenshots from the connected website: sent as images (OpenWA fetches the URL), else as links."""
        sent_links = []
        for img in images[:limit]:
            if self._media_ok():
                try:
                    self.openwa.send_image_url(chat_id, img["url"], caption=img["alt"])
                    continue
                except OpenWAError as e:
                    self._media_failed("image", e)
            sent_links.append(f"• {img['alt']}: {img['url']}")
        if sent_links:
            self.send(chat_id, "\n".join(sent_links))

    def _media_ok(self):
        return time.time() >= self._media_broken_until

    def _media_failed(self, what, e):
        log(f"send_{what}_failed", level="warning", error=str(e))
        if getattr(e, "status", None) and e.status >= 500:
            self._media_broken_until = time.time() + 600

    def _link(self, data, ext):
        if not self.public_base:
            return None
        return f"{self.public_base}/r/{self.shared.save(data, ext)}"

    def send_full_result(self, chat_id, reply):
        """Every row: CSV document, else a CSV download link, else the remaining rows as text."""
        csv = reply.df.to_csv(index=False).encode("utf-8-sig")
        if self._media_ok():
            try:
                return self.openwa.send_document(chat_id, csv, "report.csv", "text/csv", caption=f"📎 Poora result — {len(reply.df)} rows")
            except OpenWAError as e:
                self._media_failed("document", e)
        url = self._link(csv, "csv")
        if url:
            return self.send(chat_id, f"📎 Poora result ({len(reply.df)} rows) — CSV download karo (24 ghante tak):\n{url}")
        self.send(chat_id, rest_as_text(reply.df, reply.metric, LIST_LIMIT))

    def send_chart(self, chat_id, reply, quiet=False):
        from chart_ui import render_png
        title = (reply.plan or {}).get("title") or "Chart"
        try:
            png = render_png(reply.df, reply.chart, reply.metric, title)
        except Exception as e:
            log("chart_failed", level="warning", error=f"{type(e).__name__}: {e}"[:200])
            png = None
        if not png:
            if not quiet:
                self.send(chat_id, "📉 Is result ka graph nahi ban sakta (ek hi value hai). Upar text mein poora result hai.")
            return
        if self._media_ok():
            try:
                return self.openwa.send_image(chat_id, png, caption=title)
            except OpenWAError as e:
                self._media_failed("image", e)
        url = self._link(png, "png")
        if url:   # WhatsApp shows the image as the link preview
            return self.send(chat_id, f"📊 *{title}*\n{url}")
        self.send(chat_id, "📉 Graph bhej nahi paaya (WhatsApp server media nahi bhej raha). Upar text mein poora result hai.")

    def send_voice(self, chat_id, reply, question):
        from tts_service import build_speech_text, get_provider
        try:
            rows = reply.df.to_dict(orient="records") if reply.df is not None and not reply.df.empty else None
            lang = None if self.cfg.tts_language == "auto" else self.cfg.tts_language
            text, lang = build_speech_text(reply.text, question=question, rows=rows, chart=reply.chart, metric=reply.metric, language=lang)
            speech = get_provider("local").speak(text, language=lang, voice=self.cfg.tts_voice, speed=self.cfg.tts_speed)
            if speech.audio is None:
                raise RuntimeError("local TTS unavailable")
            audio, mime, ptt = speech.audio, speech.mime, False
            if self.cfg.voice_reply == "voice":
                try:
                    audio, mime = self.openwa.convert_voice(audio)
                    ptt = True
                except OpenWAError as e:
                    log("voice_convert_failed", level="warning", error=str(e))   # fall back to a plain audio file
            self.openwa.send_audio(chat_id, audio, mime, ptt=ptt)
        except Exception as e:
            log("voice_reply_failed", level="warning", error=f"{type(e).__name__}: {e}"[:200])

    def _typing(self, chat_id, state):
        try:
            self.openwa.send_typing(chat_id, state)
        except Exception:
            pass

    # ---------- media ----------
    def media_bytes(self, msg):
        m = msg.media or {}
        if m.get("data") and not m.get("omitted"):
            return base64.b64decode(m["data"]), (m.get("mimetype") or "").split(";")[0]
        data, mime = self.openwa.download_media(msg.chat_id, msg.message_id)
        return data, (m.get("mimetype") or mime or "").split(";")[0]

    def transcribe(self, msg):
        try:
            audio, mime = self.media_bytes(msg)
        except OpenWAError as e:
            log("media_download_failed", level="warning", message_id=msg.message_id, error=str(e))
            return ""
        fmt = AUDIO_FMT.get(mime, "ogg")
        for attempt in range(2):
            try:
                return OpenRouterAI(self.cfg.openrouter_key, self.cfg.model).transcribe(audio, fmt=fmt, model=self.cfg.stt_model).strip()
            except requests.RequestException as e:
                log("stt_failed", level="warning", attempt=attempt + 1, error=type(e).__name__)
        return ""

    def receive_links(self, msg, key, state, text):
        """Google Sheet / website links in a chat message become this chat's sources.
        Returns what is left of the message as a question ("" when the message was only a link)."""
        if DB_URL.search(text):
            self.send(msg.chat_id, "🔒 Database ka connection string (password ke saath) WhatsApp pe mat bhejo — main use save nahi kar raha. "
                                   "Admin server ke .env mein WHATSAPP_DATABASE_URL set kare. Is password ko badal dena behtar hai.")
            log("db_url_refused", message_id=msg.message_id)
            return ""
        urls = find_links(text)
        quoted_link = False
        if not urls and msg.quoted_text:
            urls, quoted_link = find_links(msg.quoted_text), True      # "isme batao kya hai" as a reply to a link
        if not urls:
            if msg.quoted_text:   # replying to an earlier answer: give the planner that context
                return f"{text}\n[Ye message is pichhle message ka reply hai: \"{msg.quoted_text[:300]}\"]"
            return text
        if not self.cfg.allow_chat_links:
            self.send(msg.chat_id, "🔗 Links se naya data jodna is bot pe band hai.")
            return ""
        added = []
        for url in urls[:3]:
            kind = "sheet" if "docs.google.com/spreadsheets/" in url else "web"
            existing = next((f for f in state.get("files", []) if f.get("target") == url), None)
            sid = existing["sid"] if existing else f"wa_{chat_slug(key)}_{kind}_{int(time.time() * 1000) % 10_000_000}"
            try:
                schema = self.sources.ensure({sid: (kind, url)})[sid]
            except SourceUnavailable as e:
                hint = " Sheet 'Anyone with the link can view' honi chahiye." if kind == "sheet" else ""
                self.send(msg.chat_id, f"🔗 Ye link khol nahi paaya.{hint}\n({str(e).split(': ', 1)[-1][:160]})")
                continue
            if not existing:
                files = [f for f in state.get("files", []) if f.get("kind", "file") != "file" or Path(f["path"]).exists()]
                files.append({"sid": sid, "kind": kind, "target": url, "path": "", "name": schema.get("name") or url})
                state["files"] = files[-MAX_FILES_PER_CHAT:]
            added.append((kind, schema, sid))
            state["focus"] = sid
        if added:
            self.store.save(key, state)
        if not added:
            return ""
        rest = (text if quoted_link else strip_links(text)).strip(" \n\t,.-:")
        kind, schema, sid = added[-1]
        about = f"\n[Sawal is {'sheet' if kind == 'sheet' else 'website'} ke baare mein hai: {schema.get('name') or sid} (source_id {sid})]"
        if rest and len(rest.split()) >= 2:
            return rest + about
        if kind == "web":   # a bare website link: people want to know what it is
            return "Is website ka short summary do: ye kya hai aur kya karti hai." + about
        for kind, schema, _ in added:
            self.send(msg.chat_id, _link_summary(kind, schema))
        return ""

    def receive_file(self, msg, key, state):
        """Save an incoming file into this chat's context. Returns the caption as a question, if any."""
        m = msg.media or {}
        name = Path(m.get("filename") or "").name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else MIME_EXT.get((m.get("mimetype") or "").split(";")[0], "")
        caption = msg.text if msg.text and msg.text != name else ""
        if msg.message_type in ("image", "sticker", "video") or ext not in DATA_EXTS:
            self.send(msg.chat_id, "🖼️ Ye file type main abhi analyse nahi kar sakta. Excel, CSV, PDF, Word, PowerPoint, TXT ya JSON bhejo.")
            return ""
        try:
            data, _ = self.media_bytes(msg)
        except OpenWAError as e:
            log("media_download_failed", level="warning", message_id=msg.message_id, error=str(e))
            self.send(msg.chat_id, "📎 File download nahi ho paayi. OpenWA mein media archiving on hai? Chhoti file (1 MB se kam) seedhe aati hai.")
            return ""
        name = name or f"file.{ext}"
        slug = chat_slug(key)
        folder = self.cfg.files_dir / slug
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{int(time.time())}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)}"
        path.write_bytes(data)
        try:
            read_file(name, data)            # validate with the same parser the web app uses
        except Exception as e:
            path.unlink(missing_ok=True)
            self.send(msg.chat_id, f"📎 {name} padh nahi paaya: {str(e)[:200]}")
            return ""
        files = [f for f in state.get("files", []) if f.get("kind", "file") != "file" or Path(f["path"]).exists()]
        sid = f"wa_{slug}_{len(files) + 1}_{int(time.time()) % 100000}"
        files.append({"sid": sid, "kind": "file", "name": name, "path": str(path)})
        state["focus"] = sid
        state["files"] = files[-MAX_FILES_PER_CHAT:]
        self.store.save(key, state)
        schema = self.sources.ensure({sid: ("file", str(path))})[sid]
        log("file_received", message_id=msg.message_id, ext=ext, bytes=len(data))
        if caption:
            return caption
        self.send(msg.chat_id, _file_summary(name, schema))
        return ""

    # ---------- commands ----------
    def command(self, msg, key, state, text):
        cmd = text.split()[0].lower()
        if cmd == "/help":
            return self.send(msg.chat_id, HELP)
        if cmd == "/reset":
            keep = [] if "all" in text.lower() else state.get("files", [])
            self.store.reset(key)
            if keep:
                self.store.save(key, {"files": keep})
            return self.send(msg.chat_id, "🧹 Is chat ki baat-cheet reset kar di." + (" Bheji hui files bhi hata di." if not keep and state.get("files") else " Business data aur files safe hain."))
        if cmd == "/status":
            try:
                schemas = self.sources.ensure(self.chat_specs(state))
                lines = [_source_line(sid, s) for sid, s in schemas.items()] or ["(koi data connected nahi)"]
            except SourceUnavailable:
                lines = ["⚠️ Default data source abhi reach nahi ho raha"]
            return self.send(msg.chat_id, "📡 *Status*\n\n" + "\n".join(lines) + f"\n\nModel: {self.cfg.model}\nData tools: {getattr(self.tools, 'transport', '?')}")
        return self.send(msg.chat_id, "Ye command nahi pata. /help bhejo.")


def _source_line(sid, schema):
    name = schema.get("name") or sid
    if schema.get("sheets"):
        return f"• {name}: " + ", ".join(f"{s['name']} ({s['row_count']} rows)" for s in schema["sheets"])
    if schema.get("row_count") is not None:
        return f"• {name}: {schema['row_count']} rows"
    return f"• {name}"


def _link_summary(kind, schema):
    name = schema.get("name") or "link"
    tabs = ", ".join(f"{s['name']} ({s['row_count']} rows)" for s in schema.get("sheets") or [])
    if kind == "sheet":
        return f"📊 *{name}* connect ho gayi. Tabs: {tabs}\n\nAb pucho, jaise \"sales amount batao\"."
    extra = f"\nTables: {tabs}" if tabs else ""
    return f"🌐 *{name}* padh liya.{extra}\n\nAb pucho, jaise \"is website ka summary do\"."


def _file_summary(name, schema):
    if schema.get("columns"):
        cols = ", ".join(c["name"] for c in schema["columns"][:12])
        return f"📎 *{name}* load ho gayi — {schema.get('row_count')} rows.\nColumns: {cols}\n\nAb is file ke baare mein pucho, jaise \"total batao\" ya \"top 5 dikhao\"."
    if schema.get("sheets"):
        tabs = ", ".join(f"{s['name']} ({s['row_count']} rows)" for s in schema["sheets"])
        return f"📎 *{name}* load ho gayi. Tabs: {tabs}\n\nAb pucho."
    return f"📎 *{name}* load ho gayi. Ab is document ke baare mein pucho."
