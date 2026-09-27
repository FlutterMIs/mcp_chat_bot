"""OpenWA HTTP client. Every endpoint and body here comes from https://w.ashveratech.com/api/docs-json (v0.14.2).

Auth: X-API-Key header. Sends return 201 {messageId, timestamp}.
"""
import base64
import time
from urllib.parse import quote

import requests

from .log import log


class OpenWAError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class OpenWAService:
    def __init__(self, base_url, api_key, session_id, timeout=60):
        if not base_url or not api_key or not session_id:
            raise ValueError("OPENWA_BASE_URL, OPENWA_API_KEY and OPENWA_SESSION must be set")
        self.base = base_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update({"X-API-Key": api_key, "User-Agent": "BusinessAnalyst-WhatsApp/1.0"})

    # ---------- low level ----------
    def _url(self, path):
        return f"{self.base}/api{path}"

    def _request(self, method, path, *, json=None, idempotent=False, expect_json=True, action=None):
        """One OpenWA call. Sends are NOT idempotent (a resend is a second WhatsApp message), so they are
        retried only when the connection never opened. Reads (idempotent=True) also retry timeouts/5xx."""
        attempts = 3
        for i in range(attempts):
            t0 = time.time()
            try:
                r = self.http.request(method, self._url(path), json=json, timeout=(10, self.timeout))
            except requests.exceptions.ConnectionError as e:
                # ConnectTimeout / refused: request never reached OpenWA, safe to retry even for sends.
                # A ReadTimeout means it may have been delivered, so it is only retried for reads.
                never_sent = isinstance(e, requests.exceptions.ConnectTimeout) or "Connection refused" in str(e) or "NameResolution" in str(e)
                log("openwa_request", action=action or path, status="connect_error", attempt=i + 1, error=type(e).__name__)
                if i < attempts - 1 and (never_sent or idempotent):
                    time.sleep(1.5 * (i + 1))
                    continue
                raise OpenWAError(f"OpenWA unreachable: {type(e).__name__}") from e
            except requests.exceptions.Timeout as e:
                log("openwa_request", action=action or path, status="timeout", attempt=i + 1)
                if i < attempts - 1 and idempotent:
                    continue
                raise OpenWAError("OpenWA timed out") from e
            log("openwa_request", action=action or path, status=r.status_code, ms=int((time.time() - t0) * 1000), attempt=i + 1)
            if r.status_code == 429 or (idempotent and r.status_code >= 500):
                if i < attempts - 1:
                    time.sleep(float(r.headers.get("Retry-After", 2 * (i + 1))))
                    continue
            if r.status_code >= 400:
                detail = r.text[:300]
                try:
                    body = r.json()
                    detail = body.get("message") or body.get("error") or detail
                except ValueError:
                    pass
                raise OpenWAError(f"OpenWA {r.status_code}: {detail}", r.status_code)
            return r.json() if expect_json else r
        raise OpenWAError("OpenWA request failed")

    def _sess(self):
        return quote(self.session_id, safe="")

    # ---------- sessions ----------
    def get_session(self):
        """GET /api/sessions/{id} -> SessionResponseDto (status, phone, ...)."""
        return self._request("GET", f"/sessions/{self._sess()}", idempotent=True, action="get_session")

    def resolve_session(self):
        """OPENWA_SESSION may be the session name shown in the dashboard ("new-testing"); API paths and webhook
        payloads use its UUID id. GET /api/sessions -> [SessionResponseDto] maps one to the other."""
        sessions = self._request("GET", "/sessions?limit=1000", idempotent=True, action="list_sessions")
        for s in sessions if isinstance(sessions, list) else []:
            if self.session_id in (s.get("id"), s.get("name")):
                self.session_id = s["id"]
                return s
        raise OpenWAError(f"OpenWA session '{self.session_id}' not found for this API key", 404)

    def resolve_phone(self, contact_id):
        """GET /sessions/{sessionId}/contacts/{contactId}/phone -> {contactId, phone|null}: @lid privacy id -> MSISDN digits."""
        res = self._request("GET", f"/sessions/{self._sess()}/contacts/{quote(contact_id, safe='')}/phone", idempotent=True, action="resolve_phone")
        return (res or {}).get("phone")

    def health(self):
        return self._request("GET", "/health", idempotent=True, action="health")

    # ---------- sending ----------
    def send_text(self, chat_id, text):
        """POST /sessions/{sessionId}/messages/send-text {chatId, text<=4096}."""
        return self._request("POST", f"/sessions/{self._sess()}/messages/send-text", json={"chatId": chat_id, "text": text[:4096]}, action="send_text")

    def _send_media(self, kind, chat_id, data, mimetype, filename=None, caption=None, **extra):
        body = {"chatId": chat_id, "base64": base64.b64encode(data).decode(), "mimetype": mimetype}
        if filename:
            body["filename"] = filename
        if caption:
            body["caption"] = caption[:1024]
        body.update(extra)
        return self._request("POST", f"/sessions/{self._sess()}/messages/send-{kind}", json=body, action=f"send_{kind}")

    def send_image(self, chat_id, png, caption=None, filename="chart.png", mimetype="image/png"):
        """POST .../send-image SendMediaMessageDto {chatId, base64, mimetype, caption}."""
        return self._send_media("image", chat_id, png, mimetype, filename, caption)

    def send_image_url(self, chat_id, url, caption=None):
        """POST .../send-image SendMediaMessageDto {chatId, url, caption} — OpenWA fetches the image itself."""
        body = {"chatId": chat_id, "url": url}
        if caption:
            body["caption"] = caption[:1024]
        return self._request("POST", f"/sessions/{self._sess()}/messages/send-image", json=body, action="send_image_url")

    def send_document(self, chat_id, data, filename, mimetype, caption=None):
        """POST .../send-document SendMediaMessageDto; filename is rendered for documents."""
        return self._send_media("document", chat_id, data, mimetype, filename, caption)

    def send_audio(self, chat_id, data, mimetype, ptt=False):
        """POST .../send-audio SendAudioMessageDto; ptt=true needs Ogg/Opus for a playable voice note."""
        return self._send_media("audio", chat_id, data, mimetype, ptt=ptt)

    def send_typing(self, chat_id, state="typing"):
        """POST /sessions/{id}/chats/typing SendChatStateDto {chatId, state: typing|recording|paused}."""
        return self._request("POST", f"/sessions/{self._sess()}/chats/typing", json={"chatId": chat_id, "state": state}, expect_json=False, action="typing")

    # ---------- media ----------
    def download_media(self, chat_id, message_id):
        """GET .../messages/{chatId}/{messageId}/media -> archived bytes (404 when archiving is off or too large)."""
        r = self._request("GET", f"/sessions/{self._sess()}/messages/{quote(chat_id, safe='')}/{quote(message_id, safe='')}/media",
                          idempotent=True, expect_json=False, action="download_media")
        return r.content, r.headers.get("Content-Type", "application/octet-stream").split(";")[0]

    def convert_voice(self, data):
        """POST /sessions/{sessionId}/media/convert/voice {base64} -> {base64, mimetype: audio/ogg; codecs=opus, bytes}."""
        res = self._request("POST", f"/sessions/{self._sess()}/media/convert/voice", json={"base64": base64.b64encode(data).decode()},
                            idempotent=True, action="convert_voice")
        return base64.b64decode(res["base64"]), res.get("mimetype", "audio/ogg; codecs=opus")
