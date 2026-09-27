"""Short-lived download links for charts/CSVs, served by whatsapp_server.py at /r/<token>.<ext>.

Used when OpenWA cannot send media (its engine returns 500 for every image/document): a text message with
the link still arrives, and WhatsApp shows the chart as a link preview. Tokens are 128-bit random, files
expire after `ttl` seconds, and only .png/.csv from this folder are ever served.
"""
import re
import secrets
import time
from pathlib import Path

TOKEN = re.compile(r"^[0-9a-f]{32}\.(png|csv|xlsx)$")
MIME = {"png": "image/png", "csv": "text/csv; charset=utf-8", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class SharedFiles:
    def __init__(self, folder, ttl=24 * 3600):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl

    def save(self, data: bytes, ext: str) -> str:
        self.cleanup()
        name = f"{secrets.token_hex(16)}.{ext}"
        (self.dir / name).write_bytes(data)
        return name

    def get(self, name):
        """(bytes, mimetype) for a valid unexpired name, else None."""
        if not TOKEN.match(name or ""):
            return None
        p = self.dir / name
        if not p.exists() or time.time() - p.stat().st_mtime > self.ttl:
            return None
        return p.read_bytes(), MIME[name.rsplit(".", 1)[1]]

    def cleanup(self):
        for p in self.dir.glob("*.*"):
            if TOKEN.match(p.name) and time.time() - p.stat().st_mtime > self.ttl:
                p.unlink(missing_ok=True)
