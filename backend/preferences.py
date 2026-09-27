"""User preferences per workspace (language, number style, TTS, debug) — the sidebar settings that follow the user."""
from __future__ import annotations

from .db import UserPreference, session_scope

DEFAULTS = {"number_style": "indian", "show_period": True, "debug_mode": False, "tts_enabled": True, "tts_provider": "browser", "tts_auto": False,
            "tts_lang": "auto", "tts_voice": "", "tts_speed": 1.0, "tts_volume": 1.0, "language": "auto"}


def get(user_id, workspace_id) -> dict:
    with session_scope() as s:
        row = s.get(UserPreference, (user_id, workspace_id))
        return {**DEFAULTS, **((row.prefs or {}) if row else {})}


def save(user_id, workspace_id, prefs: dict):
    clean = {k: v for k, v in (prefs or {}).items() if k in DEFAULTS}
    with session_scope() as s:
        row = s.get(UserPreference, (user_id, workspace_id))
        if row is None:
            s.add(UserPreference(user_id=user_id, workspace_id=workspace_id, prefs=clean))
        else:
            row.prefs = clean
    return {**DEFAULTS, **clean}
