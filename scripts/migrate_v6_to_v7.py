"""One-off migration from the V6 single-user install to the V7 workspace database.

    .venv/bin/python scripts/migrate_v6_to_v7.py --email you@example.com [--password ...] [--dry-run]

What it does (idempotent — safe to run twice):
  1. creates the bootstrap user (password mode) or reuses it, plus its personal workspace
  2. seeds sources from .env: WHATSAPP_GOOGLE_SHEET_URL (Google Sheet), WHATSAPP_DATA_FILES (files), WHATSAPP_DATABASE_URL (database)
  3. imports workspace_memory.json: mappings + confirmed rules keep their per-source scope by matching the old source id
     to the new one (old "google_sheet" → the seeded sheet; old sheet_<hash> ids → the sheet with the same URL hash)
  4. links the WHATSAPP_ALLOWED_NUMBERS to the workspace so their chats use the same sources
Nothing is deleted. The old files stay where they are.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default=None, help="for APP_AUTH_MODE=password; omit with oidc/none")
    ap.add_argument("--name", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from backend import auth, db, learning, whatsapp_link
    from backend.sources import SourceRegistry, sheet_id_of
    db.engine()
    try:
        user = auth.login_password(args.email, args.password) if args.password else None
    except auth.AuthError:
        user = None
    if user is None:
        if args.password:
            user = auth.register(args.email, args.password, args.name)
        else:
            user = auth.login_oidc(args.email, args.name)          # identity without a password (oidc / none mode)
    ws, uid = user["workspace_id"], user["user_id"]
    print(f"user {user['email']} → workspace {ws}")
    if args.dry_run:
        print("dry run: no sources / learning written")
        return 0

    reg = SourceRegistry()
    id_map = {}
    sheet_url = os.getenv("WHATSAPP_GOOGLE_SHEET_URL", "").strip() or os.getenv("GOOGLE_SHEET_URL", "").strip()
    if sheet_url:
        row = reg.add_google_sheet(ws, uid, sheet_url)
        print(f"sheet: {row['name']} → {row['id']} [{row['status']}]")
        id_map["google_sheet"] = row["id"]
        id_map["sheet_" + hashlib.sha1(sheet_url.encode()).hexdigest()[:8]] = row["id"]
    for i, path in enumerate([p for p in os.getenv("WHATSAPP_DATA_FILES", "").split(os.pathsep) if p.strip()]):
        p = Path(path.strip())
        if p.exists():
            row = reg.add_file(ws, uid, p.name, p.read_bytes())
            print(f"file: {p.name} → {row['id']} [{row['status']}]")
            id_map[f"file_{i + 1}_{p.stem}"[:60]] = row["id"]
    if os.getenv("WHATSAPP_DATABASE_URL", "").strip():
        row = reg.add_database(ws, uid, os.getenv("WHATSAPP_DATABASE_URL").strip())
        print(f"database: {row['name']} → {row['id']} [{row['status']}]")
        id_map["database"] = row["id"]

    mem = ROOT / "workspace_memory.json"
    if mem.exists():
        data = json.loads(mem.read_text(encoding="utf-8"))
        n = 0
        for m in data.get("mappings", []):
            new = id_map.get(m.get("source"))
            if new and m.get("term") and m.get("column"):
                learning.add_mapping(ws, new, m.get("sheet"), m["term"], m["column"], user_id=uid)
                n += 1
        r = 0
        for rule in data.get("rules", []):
            if isinstance(rule, dict) and rule.get("source_id") in id_map and rule.get("text"):
                learning.add_rule(ws, id_map[rule["source_id"]], rule["text"], uid)
                r += 1
        print(f"learning: {n} mappings, {r} rules imported (plans are re-learned from validated answers)")

    for num in [n.strip() for n in os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").split(",") if n.strip() and n.strip() != "*"]:
        code = whatsapp_link.create_code(ws, uid)
        whatsapp_link.link(code, num)
        print(f"whatsapp: number ending {num[-4:]} linked")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
