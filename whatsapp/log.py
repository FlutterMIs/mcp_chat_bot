"""Structured JSON-line logs. Phone numbers are masked and message text is never logged."""
import json
import logging
import re
import sys
import time

_logger = logging.getLogger("whatsapp")
if not _logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(h)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

_SECRETISH = re.compile(r"(sk-or-[\w-]+|X-API-Key[^,}]*|Bearer\s+\S+)", re.I)


def mask_id(wid):
    """919990930426@c.us -> ••••0426@c.us (enough to tell chats apart, not to identify a person)."""
    if not wid:
        return wid
    user, _, domain = str(wid).partition("@")
    return f"••••{user[-4:]}" + (f"@{domain}" if domain else "")


def log(_name, level="info", **fields):
    for k in ("chat", "sender"):
        if k in fields:
            fields[k] = mask_id(fields[k])
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "log": _name, **fields}
    line = _SECRETISH.sub("[redacted]", json.dumps(rec, ensure_ascii=False, default=str))
    getattr(_logger, level)(line)
