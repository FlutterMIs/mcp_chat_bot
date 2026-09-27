"""WhatsApp backend: OpenWA webhook -> shared Business Analyst -> OpenWA reply.

Run:  python whatsapp_server.py            (listens on WHATSAPP_HOST:WHATSAPP_PORT, default 0.0.0.0:8600)
Webhook URL for OpenWA:  https://<public host>/webhook/openwa   event: message.received
"""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import os

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

load_dotenv()

from whatsapp.bot import Config, WhatsAppBot          # noqa: E402  (env must be loaded first)
from whatsapp.log import log                          # noqa: E402
from whatsapp.openwa import OpenWAService             # noqa: E402
from whatsapp.store import Store                      # noqa: E402
from whatsapp.webhook import verify_signature         # noqa: E402

WEBHOOK_SECRET = os.getenv("OPENWA_WEBHOOK_SECRET", "")
ALLOW_UNSIGNED = os.getenv("WHATSAPP_ALLOW_UNSIGNED", "").lower() in {"1", "true", "yes"}
MAX_BODY = 20 * 1024 * 1024


def make_tools():
    if os.getenv("WHATSAPP_TOOL_TRANSPORT", "mcp").lower() == "local":
        from analyst import LocalTools
        from mcp_server import MCPServer
        return LocalTools(MCPServer(os.getenv("DATABASE_URL", "sqlite:///demo.db")))
    from mcp_tools import McpStdioTools
    return McpStdioTools()


def build_app(bot=None, executor=None, secret=WEBHOOK_SECRET, allow_unsigned=ALLOW_UNSIGNED):
    state = {"bot": bot, "executor": executor or ThreadPoolExecutor(max_workers=int(os.getenv("WHATSAPP_WORKERS", "4")), thread_name_prefix="wa")}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if state["bot"] is None:
            cfg = Config.from_env()
            try:
                openwa = OpenWAService(os.getenv("OPENWA_BASE_URL", "https://w.ashveratech.com"), os.getenv("OPENWA_API_KEY", ""), cfg.session)
            except ValueError as e:
                log("config_error", level="error", message=str(e))
                yield
                return
            state["bot"] = WhatsAppBot(cfg, openwa, Store(os.getenv("WHATSAPP_STATE_DB", "whatsapp_state.db")), make_tools())
            state["executor"].submit(state["bot"].warm_up)
            if not cfg.allowed_numbers:
                log("config_warning", level="warning", message="WHATSAPP_ALLOWED_NUMBERS is empty: the bot will not reply to anyone")
        if not secret and not allow_unsigned:
            log("config_warning", level="warning", message="OPENWA_WEBHOOK_SECRET not set: webhooks will be rejected")
        yield
        state["executor"].shutdown(wait=False)

    async def webhook(request):
        bot = state["bot"]
        if bot is not None and not bot.cfg.public_url:
            # Learn our public https base from the tunnel/proxy headers, for chart/CSV links.
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            if host:
                bot.public_base = f"{proto}://{host}"
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        if secret:
            # The Ashveratech build of OpenWA renames X-OpenWA-* headers to X-Ashveratech-* (seen on live deliveries).
            sig = request.headers.get("x-ashveratech-signature") or request.headers.get("x-openwa-signature")
            if not verify_signature(raw, sig, secret):
                log("webhook_rejected", level="warning", reason="bad_signature", has_signature=bool(sig),
                    delivery=request.headers.get("x-ashveratech-delivery-id") or request.headers.get("x-openwa-delivery-id"),
                    header_names=sorted(request.headers.keys()))
                _debug_rejection(raw, request.headers, secret)
                return JSONResponse({"error": "invalid signature"}, status_code=401)
        elif not allow_unsigned:
            return JSONResponse({"error": "webhook secret not configured"}, status_code=503)
        try:
            payload = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        event = payload.get("event") if isinstance(payload, dict) else None
        if state["bot"] is None:
            return JSONResponse({"error": "bot not configured"}, status_code=503)
        if event == "message.received":
            # OpenWA waits at most WEBHOOK_TIMEOUT (10s) for us: acknowledge now, think in the background.
            state["executor"].submit(_safe, state["bot"].handle_payload, payload)
        else:
            log("webhook_ignored", reason="event", event=event)
        return JSONResponse({"ok": True})

    async def shared(request):
        bot = state["bot"]
        found = bot.shared.get(request.path_params["name"]) if bot else None
        if not found:
            return JSONResponse({"error": "not found"}, status_code=404)
        data, mime = found
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600", "X-Robots-Tag": "noindex"})

    async def health(request):
        bot = state["bot"]
        return JSONResponse({"ok": bot is not None, "configured": bot is not None, "tools": getattr(getattr(bot, "tools", None), "transport", None)},
                            status_code=200 if bot else 503)

    return Starlette(routes=[Route("/webhook/openwa", webhook, methods=["POST"]), Route("/health", health), Route("/r/{name}", shared)], lifespan=lifespan)


def _debug_rejection(raw, headers, secret):
    """Write what arrived (header names, signature shape, our expected HMAC) to whatsapp_debug.json.
    No secret and no message text: the body itself is kept only for OpenWA's synthetic 'test' event."""
    import hashlib, hmac, time
    try:
        event = (json.loads(raw) or {}).get("event")
    except ValueError:
        event = None
    sig_like = {k: v for k, v in headers.items() if "sign" in k.lower() or "hmac" in k.lower() or k.lower().startswith("x-")}
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "body_len": len(raw),
           "body_sha256": hashlib.sha256(raw).hexdigest(), "header_names": sorted(headers.keys()), "x_headers": sig_like,
           "expected_sha256_hmac": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
           "body_if_test": raw.decode("utf-8", "replace") if event == "test" else None}
    try:
        with open("whatsapp_debug.json", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception as e:
        log("worker_crash", level="error", error=f"{type(e).__name__}: {str(e)[:300]}")


app = build_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("WHATSAPP_HOST", "0.0.0.0"), port=int(os.getenv("WHATSAPP_PORT", "8600")), log_level="warning")
