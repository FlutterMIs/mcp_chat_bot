"""Point the OpenWA webhook at the current cloudflared quick-tunnel URL (it changes on every cloudflared restart).

Run after starting cloudflared:   .venv/bin/python update_webhook.py            (add --dry-run to only print)
Reads the tunnel hostname from cloudflared's local metrics endpoint (/quicktunnel) and updates the session's
message.received webhook with PUT /api/sessions/{id}/webhooks/{webhookId} — url only; secret and events stay.
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
from whatsapp.openwa import OpenWAService  # noqa: E402


def tunnel_host():
    for port in range(20241, 20250):   # cloudflared's default metrics ports
        try:
            host = requests.get(f"http://127.0.0.1:{port}/quicktunnel", timeout=2).json().get("hostname")
            if host:
                return host
        except (requests.RequestException, ValueError):
            continue
    return None


def main():
    dry = "--dry-run" in sys.argv
    host = tunnel_host()
    if not host:
        sys.exit("cloudflared tunnel nahi mila. Pehle chalao:  cloudflared tunnel --url http://localhost:8600")
    url = f"https://{host}/webhook/openwa"
    try:
        ok = requests.get(f"https://{host}/health", timeout=15).status_code == 200
    except requests.RequestException:
        ok = False
    print(f"Tunnel: {url}  ({'bot reachable' if ok else 'bot NOT reachable — is ./run_whatsapp_mac.sh running?'})")
    wa = OpenWAService(os.getenv("OPENWA_BASE_URL") or "https://w.ashveratech.com", os.getenv("OPENWA_API_KEY", ""), os.getenv("OPENWA_SESSION", ""))
    wa.resolve_session()
    hooks = [h for h in wa._request("GET", f"/sessions/{wa.session_id}/webhooks", idempotent=True, action="list_webhooks")
             if "message.received" in h.get("events", []) or "*" in h.get("events", [])]
    if not hooks:
        sys.exit("Is session pe message.received webhook nahi hai. README ke Setup step 5 se banao (secret ke saath).")
    for h in hooks:
        if h["url"] == url:
            print(f"Webhook {h['id'][:8]} already points here.")
            continue
        print(f"Webhook {h['id'][:8]}: {h['url']}  ->  {url}")
        if not dry:
            wa._request("PUT", f"/sessions/{wa.session_id}/webhooks/{h['id']}", json={"url": url}, action="update_webhook")
            test = wa._request("POST", f"/sessions/{wa.session_id}/webhooks/{h['id']}/test", action="test_webhook")
            print("Test delivery:", "OK" if test.get("success") else test)


if __name__ == "__main__":
    main()
