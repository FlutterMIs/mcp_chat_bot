"""OpenWA webhook parsing and verification.

Payload (OpenWA v0.14.2 src/modules/webhook/webhook.service.ts WebhookPayload):
  {"event", "timestamp", "sessionId", "idempotencyKey", "deliveryId", "data": IncomingMessage}
Headers: X-OpenWA-Event, X-OpenWA-Idempotency-Key, X-OpenWA-Delivery-Id, X-OpenWA-Retry-Count,
and X-OpenWA-Signature: sha256=<hex HMAC-SHA256 of the raw body with the webhook secret> when a secret is set.
IncomingMessage (src/engine/interfaces/whatsapp-engine.interface.ts): id, from, to, chatId, body, type,
timestamp, fromMe, isGroup, author, isStatusBroadcast, senderPhone, contact{pushName,name,number},
media{mimetype, filename, data(base64)?, omitted?, sizeBytes?}, quotedMessage{id, body}.
"""
from dataclasses import dataclass
import hashlib
import hmac


def verify_signature(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    if not header_value or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.strip())


@dataclass
class InboundMessage:
    channel: str
    provider: str
    event: str
    message_id: str
    idempotency_key: str
    session_id: str
    chat_id: str
    sender_id: str
    sender_name: str | None
    sender_phone: str | None
    message_type: str
    text: str
    media: dict | None
    timestamp: int | None
    from_me: bool
    is_group: bool
    is_status: bool
    quoted_text: str = ""          # body of the message this one replies to (WhatsApp "reply"/quote)


def normalize(payload: dict) -> InboundMessage | None:
    """OpenWA webhook JSON -> InboundMessage, or None for events this bot doesn't handle."""
    if not isinstance(payload, dict) or payload.get("event") != "message.received":
        return None
    d = payload.get("data") or {}
    if not isinstance(d, dict) or not d.get("id"):
        return None
    contact = d.get("contact") or {}
    session = payload.get("sessionId") or d.get("sessionId") or ""
    sender = d.get("author") or d.get("from") or ""
    return InboundMessage(
        channel="whatsapp", provider="openwa", event="message.received",
        message_id=str(d["id"]),
        # Keyed on the WhatsApp message id, not OpenWA's per-webhook idempotencyKey: two webhooks must not mean two replies.
        idempotency_key=f"msg_{session}_{d['id']}",
        session_id=session,
        chat_id=d.get("chatId") or d.get("from") or "",
        sender_id=sender,
        sender_name=contact.get("pushName") or contact.get("name"),
        sender_phone=d.get("senderPhone") or contact.get("number"),
        message_type=d.get("type") or "unknown",
        text=(d.get("body") or "").strip(),
        media=d.get("media") if isinstance(d.get("media"), dict) else None,
        timestamp=d.get("timestamp"),
        from_me=bool(d.get("fromMe")),
        is_group=bool(d.get("isGroup")) or str(d.get("chatId", "")).endswith("@g.us"),
        is_status=bool(d.get("isStatusBroadcast")) or str(d.get("chatId", "")).startswith("status@"),
        quoted_text=((d.get("quotedMessage") or {}).get("body") or "").strip() if isinstance(d.get("quotedMessage"), dict) else "",
    )
