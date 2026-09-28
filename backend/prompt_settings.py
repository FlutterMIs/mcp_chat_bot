"""Workspace-level AI instructions (Prompt Builder), stored in `Workspace.settings["ai_instructions"]`.

No new table: the JSON settings column already exists, so this works on SQLite and Postgres without a migration."""
from __future__ import annotations

import prompt_builder
from .db import Workspace, session_scope
from .workspaces import Forbidden, require

KEY = "ai_instructions"


def get(workspace_id) -> dict:
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        if ws is None:
            raise Forbidden("Workspace nahi mila.")
        return prompt_builder.clean((ws.settings or {}).get(KEY) or {})


def save(workspace_id, user_id, instructions: dict) -> dict:
    """Validates, then stores the cleaned instructions. Raises ValueError with the validation errors."""
    check = prompt_builder.validate(instructions)
    if not check["ok"]:
        raise ValueError(" ".join(check["errors"]))
    with session_scope() as s:
        ws = require(s, workspace_id, user_id)
        settings = dict(ws.settings or {})
        settings[KEY] = check["clean"]
        ws.settings = settings
    prompt_builder.invalidate(workspace_id)
    return check["clean"]


def reset(workspace_id, user_id) -> dict:
    with session_scope() as s:
        ws = require(s, workspace_id, user_id)
        settings = dict(ws.settings or {})
        settings.pop(KEY, None)
        ws.settings = settings
    prompt_builder.invalidate(workspace_id)
    return dict(prompt_builder.DEFAULTS)
