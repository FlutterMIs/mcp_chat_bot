import io
from pathlib import Path
import json
import re
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from docx import Document
from pptx import Presentation


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Make duplicate/blank headers deterministic without inventing business fields.
    seen = {}
    cols = []
    for raw in df.columns:
        name = str(raw).strip()
        if not name or name.lower().startswith("unnamed:"):
            name = f"Column_{len(cols)+1}"
        base = name
        n = seen.get(base, 0)
        if n:
            name = f"{base}.{n+1}"
        seen[base] = n + 1
        cols.append(name)
    df.columns = cols
    return df.dropna(axis=0, how="all").dropna(axis=1, how="all").reset_index(drop=True)


def parse_dates(s: pd.Series) -> pd.Series:
    """ISO values (2026-01-05) are year-month-day; everything else is read day-first (05/01/2026)."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    text = s.astype(str).str.strip()
    iso = text.str.match(r"^\d{4}-\d{1,2}-\d{1,2}")
    out = pd.to_datetime(text.where(~iso), errors="coerce", dayfirst=True, format="mixed")
    out[iso] = pd.to_datetime(text[iso].str[:10], errors="coerce", format="%Y-%m-%d")
    return out


def column_info(df: pd.DataFrame):
    out = []
    for c in df.columns:
        s = df[c]
        role = "dimension"
        if pd.api.types.is_numeric_dtype(s):
            role = "metric"
        else:
            parsed = parse_dates(s)
            if len(s) and parsed.notna().mean() >= 0.70:
                role = "date"
        item = {"name": str(c), "dtype": str(s.dtype), "role": role}
        vals = s.dropna().astype(str).str.strip().drop_duplicates().head(12 if role != "dimension" else 40).tolist()
        if role == "dimension":
            item["distinct_count"] = int(s.dropna().astype(str).str.strip().nunique())
        item["sample_values"] = vals
        if role == "metric":
            nums = pd.to_numeric(s, errors="coerce").dropna()
            item["numeric_non_null"] = int(len(nums))
            item["min"] = None if nums.empty else float(nums.min())
            item["max"] = None if nums.empty else float(nums.max())
            item["distinct_ratio"] = round(float(nums.nunique() / len(nums)), 3) if len(nums) else 0.0
            item["integer_like"] = bool(len(nums)) and bool((nums.round() == nums).all())
        out.append(item)
    return out


def _read_excel_bytes(data: bytes):
    xls = pd.ExcelFile(io.BytesIO(data))
    tabs = {}
    for name in xls.sheet_names:
        tabs[name] = normalize_columns(pd.read_excel(xls, sheet_name=name))
    return tabs


def read_file(name: str, data: bytes):
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext in {"xlsx", "xls"}:
        tabs = _read_excel_bytes(data)
        return {"kind": "workbook", "name": name, "tabs": tabs}
    if ext == "csv":
        return {"kind": "table", "name": name, "df": normalize_columns(pd.read_csv(io.BytesIO(data)))}
    if ext in {"json"}:
        obj = json.loads(data.decode("utf-8", errors="replace"))
        if isinstance(obj, list):
            return {"kind": "table", "name": name, "df": normalize_columns(pd.json_normalize(obj))}
        return {"kind": "document", "name": name, "text": json.dumps(obj, ensure_ascii=False, indent=2)}
    if ext in {"txt", "md", "html", "htm"}:
        text = data.decode("utf-8", errors="replace")
        if ext in {"html", "htm"}:
            soup = BeautifulSoup(text, "html.parser")
            text = soup.get_text("\n", strip=True)
        return {"kind": "document", "name": name, "text": text}
    if ext == "pdf":
        reader = PdfReader(io.BytesIO(data))
        pages = [{"page": i, "text": (p.extract_text() or "")} for i, p in enumerate(reader.pages, 1)]
        text = "\n\n".join(p["text"] for p in pages)
        return {"kind": "document", "name": name, "text": text, "pages": pages}      # pages: page-level text for citations (RAG)
    if ext == "docx":
        doc = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        for table in doc.tables:
            text += "\n" + "\n".join(" | ".join(cell.text for cell in row.cells) for row in table.rows)
        return {"kind": "document", "name": name, "text": text}
    if ext == "pptx":
        prs = Presentation(io.BytesIO(data))
        parts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text:
                    parts.append(shape.text)
        return {"kind": "document", "name": name, "text": "\n".join(parts)}
    raise ValueError(f"Unsupported file type: .{ext}")


def check_public_url(url: str):
    """Refuse URLs that point inside this machine or network (localhost, 10.x, 192.168.x, cloud metadata...).
    Chat users can send links, so the server must never be tricked into fetching its own internals."""
    import ipaddress
    import os
    import socket
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Web URL must start with http:// or https://")
    if os.getenv("ALLOW_PRIVATE_URLS", "").lower() in {"1", "true", "yes"}:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"Website nahi mili: {parsed.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Ye URL private/internal network ka hai, isliye nahi khol sakta.")


def _get_public(url, max_redirects=5, **kw):
    """requests.get that re-checks every redirect hop against check_public_url."""
    for _ in range(max_redirects + 1):
        check_public_url(url)
        r = requests.get(url, allow_redirects=False, **kw)
        if r.is_redirect or r.is_permanent_redirect:
            from urllib.parse import urljoin
            url = urljoin(url, r.headers.get("Location", ""))
            continue
        return r
    raise ValueError("Too many redirects")


def read_web(url: str):
    r = _get_public(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (compatible; BusinessAnalyst/6.0)"})
    r.raise_for_status()
    # requests assumes ISO-8859-1 when the header has no charset, which garbles "–" into "â€“". Prefer UTF-8.
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "latin-1"):
        r.encoding = "utf-8" if b"charset=utf-8" in r.content[:4096].lower() or r.apparent_encoding in (None, "ascii", "utf-8") else r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    from urllib.parse import urljoin
    images, seen = [], set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = (img.get("alt") or img.get("title") or "").strip()
        if not src or src.startswith("data:") or not alt or src in seen:
            continue
        seen.add(src)
        images.append({"alt": alt, "url": urljoin(r.url, src)})
    videos, vseen = [], set()

    def add_video(url, title):
        if url and url not in vseen:
            vseen.add(url)
            videos.append({"title": (title or "Video").strip()[:120], "url": url})

    for f in soup.find_all("iframe"):   # YouTube embeds -> normal watch links people can open
        src = f.get("src") or f.get("data-src") or ""
        m = re.search(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|shorts/|watch\?v=)|youtu\.be/)([\w-]{6,})", src)
        if m:
            card = f.find_parent(class_=re.compile("card|video", re.I))
            label = card.find(["b", "strong", "h3", "h4"]) if card else None
            add_video(f"https://youtu.be/{m.group(1)}", (label.get_text(" ", strip=True) if label else "") or f.get("title"))
    for el in soup.find_all(attrs={"data-video": True}):   # click-to-play reels (data-video="videos/x.mp4")
        img = el.find("img")
        title = (el.find(class_=re.compile("title", re.I)) or img)
        add_video(urljoin(r.url, el["data-video"]), title.get_text(" ", strip=True) if hasattr(title, "get_text") and title.get_text(strip=True) else (img.get("alt") if img else el.get_text(" ", strip=True)))
    for v in soup.find_all("video"):
        for src in [v.get("src")] + [s.get("src") for s in v.find_all("source")]:
            if src:
                add_video(urljoin(r.url, src), v.get("title") or v.get("aria-label"))
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    text = soup.get_text("\n", strip=True)
    tables = {}
    try:
        dfs = pd.read_html(io.StringIO(r.text))  # same decoded text as above
        for i, df in enumerate(dfs):
            tables[f"table_{i+1}"] = normalize_columns(df)
    except Exception:
        pass
    if videos:
        text += "\n\nVideos on this page:\n" + "\n".join(f"- {v['title']}: {v['url']}" for v in videos[:40])
    if images:   # alt texts describe screenshots ("dashboard - Agent Performance"): useful for "kaun kaun se report"
        text += "\n\nImages on this page:\n" + "\n".join(f"- {i['alt']}" for i in images[:80])
    return {"kind": "web", "name": title, "url": url, "text": text[:120000], "tables": tables, "images": images[:200], "videos": videos[:60]}


def safe_db_name(url: str) -> str:
    """postgresql://user:pw@host/db -> postgresql://host/db (never show credentials)."""
    from sqlalchemy.engine import make_url
    try:
        u = make_url(url)
        return f"{u.get_backend_name()}://{u.host or ''}{'/' if u.host else ''}{u.database or ''}".rstrip("/")
    except Exception:
        return "database"


def allowed_file_roots():
    """Directories the app may read local files/SQLite databases from (project, upload dirs, temp)."""
    import os
    import tempfile
    roots = [Path(__file__).resolve().parent, Path(tempfile.gettempdir()).resolve()]
    for env in ("DATA_DIR", "WHATSAPP_FILES_DIR", "MCP_ALLOWED_FILE_ROOTS"):
        for p in (os.getenv(env) or "").split(os.pathsep):
            if p.strip():
                roots.append(Path(p.strip()).expanduser().resolve())
    for p in (os.getenv("WHATSAPP_DATA_FILES") or "").split(","):
        if p.strip():
            roots.append(Path(p.strip()).expanduser().resolve().parent)
    return roots


def check_allowed_path(path) -> Path:
    p = Path(path).expanduser().resolve()
    if not any(p == r or r in p.parents for r in allowed_file_roots()):
        raise ValueError("Ye file path allowed folders ke bahar hai (project folder, uploads ya DATA_DIR hi chalenge).")
    return p


def read_database(url: str, max_rows: int = 200_000):
    """Every table of a SQL database as tabs (read-only SELECTs, up to max_rows rows per table)."""
    from sqlalchemy import MetaData, Table, create_engine, inspect, select
    from sqlalchemy.engine import make_url
    u = make_url(url)
    if u.get_backend_name() == "sqlite" and u.database and u.database != ":memory:":
        check_allowed_path(u.database)
    engine = create_engine(url, future=True)
    try:
        tabs = {}
        insp = inspect(engine)
        names = insp.get_table_names() + [v for v in insp.get_view_names() if v not in insp.get_table_names()]
        meta = MetaData()
        with engine.connect() as conn:
            for name in names[:50]:
                t = Table(name, meta, autoload_with=engine)
                tabs[name] = normalize_columns(pd.read_sql(select(t).limit(max_rows), conn))
    finally:
        engine.dispose()
    if not tabs:
        raise ValueError("Database mein koi table nahi mili.")
    return {"kind": "workbook", "name": safe_db_name(url), "tabs": tabs, "origin": "database"}
